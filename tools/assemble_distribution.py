"""Assemble per-OS/arch Release zips and pip wheels that ship Bun and pi.

    python tools/assemble_distribution.py
    python tools/build_release.py --with-runtime

The source zip from ``build_release.py`` (no flag) stays platform-neutral.
This tool downloads the pinned Bun and pi archives, checks archive and binary
SHA256, bundles the worker once, and writes those bytes into each artifact.
Nothing here is a pip/npm postinstall hook. Users do not run this script.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pangpang import __version__
from pangpang.execution import PI_LICENSE, PI_LICENSE_SHA256, PI_RELEASES, PI_VERSION, _read_pi_member
from pangpang.runtime_pins import (
    BUN_LICENSE_SHA256, BUN_RELEASES, BUN_VERSION, OFFICIAL_BUN_SHA256, PLATFORM_SLUGS, WHEEL_TAGS)

BUN_LICENSE = ROOT / "tools" / "third_party" / "bun-LICENSE-1.4.2.md"
BUN_URL = "https://github.com/oven-sh/bun/releases/download/bun-v%s/%s"
PI_URL = "https://github.com/earendil-works/pi/releases/download/v%s/%s"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_https(url):
    if not url.startswith("https://"):
        raise SystemExit("refusing non-HTTPS runtime download")


def fetch_archive(cache, name, url, expected_sha):
    """Return archive bytes. A cached file is used only when its sha matches."""
    _require_https(url)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / name
    if dest.is_file() and sha256_file(dest) == expected_sha:
        return dest.read_bytes()
    partial = dest.with_name(dest.name + ".partial")
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=180) as response, open(partial, "wb") as out:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != expected_sha:
            raise SystemExit("archive sha256 mismatch for %s" % name)
        os.replace(partial, dest)
    finally:
        if partial.exists():
            partial.unlink()
    if sha256_file(dest) != expected_sha:
        raise SystemExit("archive sha256 mismatch after write for %s" % name)
    return dest.read_bytes()


def bun_bytes_for(cache, key):
    archive_name, archive_sha, member, binary_sha = BUN_RELEASES[key]
    raw = fetch_archive(cache, archive_name, BUN_URL % (BUN_VERSION, archive_name), archive_sha)
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        data = bundle.read(member)
    if sha256_bytes(data) != binary_sha or binary_sha != OFFICIAL_BUN_SHA256[key]:
        raise SystemExit("bun binary sha256 mismatch for %s/%s" % key)
    return data


def pi_bytes_for(cache, key):
    archive_name, archive_sha, binary_sha = PI_RELEASES[key]
    raw = fetch_archive(cache, archive_name, PI_URL % (PI_VERSION, archive_name), archive_sha)
    fd, tmp_name = tempfile.mkstemp(prefix=".pi-archive-")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(raw)
        data = _read_pi_member(tmp, archive_name)
    finally:
        tmp.unlink(missing_ok=True)
    if data is None or sha256_bytes(data) != binary_sha:
        raise SystemExit("pi binary sha256 mismatch for %s/%s" % key)
    return data


def license_bytes():
    bun = BUN_LICENSE.read_bytes()
    pi = PI_LICENSE.read_bytes()
    if sha256_bytes(bun) != BUN_LICENSE_SHA256:
        raise SystemExit("vendored Bun license sha256 mismatch")
    if sha256_bytes(pi) != PI_LICENSE_SHA256:
        raise SystemExit("vendored pi license sha256 mismatch")
    return bun, pi


def _worker_stamp():
    digest = hashlib.sha256()
    for rel in ("package-lock.json", "package.json", "pangpang/pi_worker.mjs",
                "pangpang/pi_models.mjs", "pangpang/model_catalog.json"):
        digest.update((ROOT / rel).read_bytes())
    return digest.hexdigest()


def build_worker_bundle(cache, host_key):
    """Bundle pi_worker.mjs with the pinned Bun for this machine. One bundle, every OS."""
    out = cache / "worker-bundle"
    script = out / "pi_worker.bundle.mjs"
    stamp = out / "stamp"
    wanted = _worker_stamp()
    if script.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == wanted:
        return script.read_bytes()
    if out.exists():
        shutil.rmtree(out)
    host = cache / "host-bun"
    if host.exists():
        shutil.rmtree(host)
    host.mkdir(parents=True)
    bun = host / ("bun.exe" if host_key[0] == "Windows" else "bun")
    bun.write_bytes(bun_bytes_for(cache, host_key))
    bun.chmod(0o755)
    import build_bun_worker
    build_bun_worker.build(str(bun), out, "bundle")
    stamp.write_text(wanted, encoding="utf-8")
    data = script.read_bytes()
    if not data:
        raise SystemExit("worker bundle was empty")
    return data


def runtime_file_map(key, bun_bin, worker, pi_bin, bun_license, pi_license):
    """Relative runtime paths → bytes. Refuses bytes that miss the pin."""
    if key not in BUN_RELEASES or key not in PI_RELEASES or key not in WHEEL_TAGS:
        raise SystemExit("no Bun+pi wheel pin for %s/%s" % key)
    bun_name = "bun.exe" if key[0] == "Windows" else "bun"
    pi_name = "pi.exe" if key[0] == "Windows" else "pi"
    if sha256_bytes(bun_bin) != BUN_RELEASES[key][3]:
        raise SystemExit("refusing to pack Bun bytes that miss the pin")
    if sha256_bytes(pi_bin) != PI_RELEASES[key][2]:
        raise SystemExit("refusing to pack pi bytes that miss the pin")
    if sha256_bytes(bun_license) != BUN_LICENSE_SHA256 or sha256_bytes(pi_license) != PI_LICENSE_SHA256:
        raise SystemExit("refusing to pack license bytes that miss the pin")
    files = {
        "runtime/pi-worker/" + bun_name: bun_bin,
        "runtime/pi-worker/pi_worker.bundle.mjs": worker,
        "runtime/pi-worker/LICENSE.bun.md": bun_license,
        "runtime/pi/" + pi_name: pi_bin,
        "runtime/pi/LICENSE.pi.md": pi_license,
    }
    system, machine = key
    manifest = {
        "shipped": True,
        "pangpang_version": __version__,
        "pi_version": PI_VERSION,
        "bun_version": BUN_VERSION,
        "platform": system,
        "machine": machine,
        "files": {name: sha256_bytes(data) for name, data in sorted(files.items())},
    }
    files["runtime/SHIPPED.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return files


def write_runtime(anchor, files):
    executable = {"bun", "bun.exe", "pi", "pi.exe"}
    for rel, data in files.items():
        path = anchor / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if path.name in executable:
            path.chmod(0o755)
        if sha256_file(path) != sha256_bytes(data):
            raise SystemExit("runtime write drifted: " + rel)
    return anchor


def _zip_info(name, data, executable):
    info = zipfile.ZipInfo(name, (2026, 9, 14, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o100755 if executable else 0o100644
    info.external_attr = mode << 16
    return info


def platform_zip(out_dir, slug, source_files, runtime_files):
    """Source release tree plus the hashed runtime. Binaries are not content-scanned;
    their bytes were already refused unless they matched the pin."""
    import build_release
    version = build_release.read_version()
    prefix = "PangPang-%s-%s" % (version, slug)
    text_entries = []
    for rel in source_files:
        data = (ROOT / rel).read_bytes()
        text_entries.append((rel, data))
    for rel, data in runtime_files.items():
        if Path(rel).name not in {"bun", "bun.exe", "pi", "pi.exe"}:
            text_entries.append((rel, data))
    problems = build_release.scan(text_entries)
    if problems:
        raise SystemExit("platform archive hygiene failed:\n " + "\n ".join(problems))
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / (prefix + ".zip")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel, data in text_entries:
            archive.writestr(_zip_info(prefix + "/" + rel, data, False), data)
        for rel, data in runtime_files.items():
            if Path(rel).name in {"bun", "bun.exe", "pi", "pi.exe"}:
                archive.writestr(_zip_info(prefix + "/" + rel, data, True), data)
    digest = sha256_file(archive_path)
    sums = out_dir / "SHA256SUMS"
    lines = sums.read_text(encoding="utf-8").splitlines() if sums.exists() else []
    lines = sorted([line for line in lines if not line.endswith(" " + archive_path.name)] + [digest + " " + archive_path.name])
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(archive_path.name, digest)
    return archive_path


def _record_digest(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def retag_wheel(path, platform_tag):
    """setuptools emits py3-none-any. The tag must name the OS whose binaries are inside."""
    path = Path(path)
    with zipfile.ZipFile(path) as wheel:
        infos = {info.filename: info for info in wheel.infolist()}
        names = list(infos)
        wheel_name = next(n for n in names if n.endswith(".dist-info/WHEEL"))
        record_name = next(n for n in names if n.endswith(".dist-info/RECORD"))
        payloads = {name: wheel.read(name) for name in names}
    text = payloads[wheel_name].decode("utf-8")
    if "Tag: py3-none-any" not in text:
        raise SystemExit("unexpected wheel tag in " + wheel_name)
    payloads[wheel_name] = text.replace("Tag: py3-none-any", "Tag: py3-none-" + platform_tag).encode("utf-8")
    lines = []
    for line in payloads[record_name].decode("utf-8").splitlines():
        if not line:
            continue
        rel = line.split(",", 1)[0]
        if rel == wheel_name:
            data = payloads[wheel_name]
            lines.append("%s,%s,%d" % (rel, _record_digest(data), len(data)))
        else:
            lines.append(line)
    payloads[record_name] = ("\n".join(lines) + "\n").encode("utf-8")
    dest = path.with_name(path.name.replace("py3-none-any", "py3-none-" + platform_tag))
    executable_names = {"bun", "bun.exe", "pi", "pi.exe"}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as wheel:
        for name, data in payloads.items():
            info = zipfile.ZipInfo(name, (2026, 9, 14, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            if Path(name).name in executable_names:
                info.external_attr = 0o100755 << 16
            else:
                info.external_attr = infos[name].external_attr
            wheel.writestr(info, data)
    if dest != path:
        path.unlink()
    return dest


def build_wheel(stage_root, files, platform_tag, out_dir):
    stage = stage_root
    package = stage / "pangpang"
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(ROOT / "pangpang", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_shipped"))
    anchor = package / "_shipped"
    write_runtime(anchor, files)
    for name in ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, stage / name)
    wheel_dir = stage / "wheels"
    if wheel_dir.exists():
        shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                    "-w", str(wheel_dir), str(stage)], check=True)
    built = list(wheel_dir.glob("pangpang-*-py3-none-any.whl"))
    if len(built) != 1:
        raise SystemExit("expected one pure wheel, got %s" % [p.name for p in built])
    tagged = retag_wheel(built[0], platform_tag)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / tagged.name
    shutil.move(str(tagged), dest)
    with zipfile.ZipFile(dest) as wheel:
        names = wheel.namelist()
        if not any(name.endswith("/runtime/pi/pi") or name.endswith("/runtime/pi/pi.exe") for name in names):
            raise SystemExit("wheel is missing the pi binary: " + dest.name)
        if not any(name.endswith("pi_worker.bundle.mjs") for name in names):
            raise SystemExit("wheel is missing the Bun worker bundle: " + dest.name)
        entry_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        if "pangpang = pangpang.cli:main" not in wheel.read(entry_name).decode("utf-8"):
            raise SystemExit("wheel is missing the pangpang console script")
    print(dest.name, sha256_file(dest))
    return dest


def assemble_all(cache=None, out=None, only=None, require_clean=False):
    import build_release
    if require_clean:
        build_release.clean_checkout(build_release.release_candidates())
    cache = Path(cache or (ROOT / "dist" / "runtime-cache"))
    out = Path(out or (ROOT / "dist"))
    host = (platform.system(), platform.machine())
    if host not in BUN_RELEASES:
        raise SystemExit("build the worker bundle on a machine with a pinned Bun binary")
    worker = build_worker_bundle(cache, host)
    bun_license, pi_license = license_bytes()
    keys = [key for key, slug in PLATFORM_SLUGS.items() if only is None or slug in only]
    if not keys:
        raise SystemExit("no platform selected")
    source_files = build_release.release_candidates()
    built = []
    for key in keys:
        files = runtime_file_map(key, bun_bytes_for(cache, key), worker, pi_bytes_for(cache, key),
                                 bun_license, pi_license)
        built.append(platform_zip(out, PLATFORM_SLUGS[key], source_files, files))
        built.append(build_wheel(cache / ("wheel-" + PLATFORM_SLUGS[key]), files, WHEEL_TAGS[key], out))
    return built


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", action="append", default=None,
                        help="platform slug (linux-x86_64, ...). Repeatable. Default: every pinned platform.")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args(argv)
    assemble_all(cache=args.cache, out=args.out, only=args.only, require_clean=args.require_clean)


if __name__ == "__main__":
    main(sys.argv[1:])
