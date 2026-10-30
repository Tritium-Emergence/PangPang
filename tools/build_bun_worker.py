"""Build an opt-in packaged Pi worker from the existing npm lock (issues /).

Two shapes, one builder (same clean-staging + npm ci + provenance):

  --shape compile (preview) bun --compile single-file executable.
  --shape bundle (formal) pinned bun(.exe) runtime + bundled worker JS in one
                                   directory, selected via PANGPANG_PI_BUN_BUNDLE.
                                   The launcher runs the packaged bun with cwd pinned to
                                   this directory (no bunfig.toml/.env to autoload) and
                                   --no-install/--no-env-file; never system Bun/Node.

Requires Node/npm for the BUILD only, and Bun 1.4.2. Does not modify the lock, the Python
environment, runtime defaults, or publish an artifact.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Supply-chain pin: a version string is NOT provenance. The digests live in
# pangpang.runtime_pins so the installer and this builder share one table.
from pangpang.runtime_pins import BUN_LICENSE_SHA256, BUN_VERSION, OFFICIAL_BUN_SHA256

# Bun runtime license/notice vendored into the repo (offline evidence, not a URL), pinned so
# it cannot be silently swapped. Sourced from oven-sh/bun LICENSE.md @ tag bun-v1.4.2.
BUN_LICENSE = ROOT / "tools" / "third_party" / "bun-LICENSE-1.4.2.md"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_official_bun(executable):
    """Fail-closed unless the bun binary matches the reviewed official digest for this
    OS/arch. Returns the expected digest. Raising here refuses to package an unverified
    runtime even if `bun --version` reports the pinned version."""
    key = (platform.system(), platform.machine())
    expected = OFFICIAL_BUN_SHA256.get(key)
    if expected is None:
        raise SystemExit("No reviewed official Bun %s digest for %s/%s; verify the official "
                         "download against SHASUMS256.txt and add its extracted-binary sha256 "
                         "before packaging a release." % (BUN_VERSION, key[0], key[1]))
    actual = sha256(Path(executable))
    if actual != expected:
        raise SystemExit("Bun runtime failed the official-digest pin for %s/%s: expected %s got "
                         "%s. A version string is not provenance; refusing to package an "
                         "unverified runtime." % (key[0], key[1], expected, actual))
    return expected


def _resolve_bun(bun):
    executable = shutil.which(bun)
    if not executable:
        raise SystemExit("Bun 1.4.2 is required on the build machine")
    executable = str(Path(executable).resolve())
    env = {k: v for k, v in os.environ.items()
           if k not in {"BUN_OPTIONS", "NODE_OPTIONS", "NODE_PATH"}}
    version = subprocess.check_output([executable, "--version"], env=env, text=True, timeout=15).strip()
    if version != BUN_VERSION:
        raise SystemExit("Expected Bun " + BUN_VERSION + "; got " + version)
    return executable, version, env


def _stage_inputs(stage, env):
    inputs = {"package.json": ROOT / "package.json",
              "package-lock.json": ROOT / "package-lock.json",
              "pi_worker.mjs": ROOT / "pangpang/pi_worker.mjs",
              "pi_models.mjs": ROOT / "pangpang/pi_models.mjs",
              "model_catalog.json": ROOT / "pangpang/model_catalog.json",
              "LICENSE.pi-model-catalog.txt": ROOT / "pangpang/LICENSE.pi-model-catalog.txt"}
    for name, path in inputs.items():
        shutil.copy2(path, stage / name)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise SystemExit("npm is required on the build machine")
    # Clean staging avoids including the operator's.env, bunfig, local deps or live
    # state. npm ci verifies the existing lock and ignores lifecycle scripts.
    subprocess.run([npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
                   cwd=stage, env=env, check=True, timeout=600)
    return inputs


def _copy_licenses(stage, artifact):
    shutil.copy2(stage / "LICENSE.pi-model-catalog.txt", artifact / "LICENSE.pi-model-catalog.txt")
    modules = stage / "node_modules"
    covered = set()
    for path in modules.rglob("*"):
        if path.is_file() and path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            target = artifact / "licenses" / path.relative_to(modules)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            covered.add(path.parent)
    # gap: packages that only declare a license in package.json ship no
    # license file; record the declaration so attribution survives redistribution.
    for pkg_json in list(modules.glob("*/package.json")) + list(modules.glob("@*/*/package.json")):
        pkg_dir = pkg_json.parent
        if pkg_dir in covered:
            continue
        try:
            meta = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        declared = meta.get("license") or ""
        if not declared and isinstance(meta.get("licenses"), list) and meta["licenses"]:
            declared = meta["licenses"][0].get("type", "")
        if not declared:
            continue
        target = artifact / "licenses" / pkg_dir.relative_to(modules) / "LICENSE.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("%s %s\nDeclared license: %s (upstream ships no license file)\n"
                          % (meta.get("name", pkg_dir.name), meta.get("version", ""), declared),
                          encoding="utf-8")


def assert_artifact_matches(path, expected, label):
    """Fail-closed if a copied artifact file's bytes drift from its pin. verify_official_bun
    checks the source before copy; this re-checks the FINAL artifact so a verify->copy swap
    cannot ship an unverified file with an honest-but-wrong manifest. Returns the verified hash."""
    actual = sha256(Path(path))
    if actual != expected:
        raise SystemExit("Packaged %s failed its pin after copy (verify->copy drift): expected %s got %s"
                         % (label, expected, actual))
    return actual


def build(bun, output, shape):
    executable, version, env = _resolve_bun(bun)
    output = output.resolve()
    if output.exists():
        raise SystemExit("Output already exists; choose a new directory: " + str(output))
    with tempfile.TemporaryDirectory(prefix="xpp-bun-build-") as tmp:
        stage = Path(tmp)
        inputs = _stage_inputs(stage, env)
        artifact = stage / "artifact"
        artifact.mkdir()
        if shape == "compile":
            binary = artifact / ("pi-worker.exe" if os.name == "nt" else "pi-worker")
            subprocess.run([executable, "build", "./pi_worker.mjs", "--compile", "--minify",
                            "--no-compile-autoload-dotenv", "--no-compile-autoload-bunfig",
                            "--no-compile-autoload-tsconfig", "--no-compile-autoload-package-json",
                            "--outfile", str(binary)], cwd=stage, env=env, check=True, timeout=180)
            _copy_licenses(stage, artifact)
            manifest = {"experimental": True, "shape": "compile", "bun_version": version,
                        "platform": platform.system(), "machine": platform.machine(),
                        "inputs_sha256": {name: sha256(stage / name) for name in inputs},
                        "binary": binary.name, "binary_bytes": binary.stat().st_size,
                        "binary_sha256": sha256(binary), "autoload_project_config": False}
            (artifact / "SHA256SUMS").write_text(manifest["binary_sha256"] + " " + binary.name + "\n", encoding="utf-8")
            (artifact / "README.txt").write_text(
                "Experimental PangPang Pi worker (bun --compile single-file).\n"
                "Known-failing under Windows Defender; prefer --shape bundle.\n"
                "Includes the Bun runtime (https://github.com/oven-sh/bun/blob/main/LICENSE.md).\n", encoding="utf-8")
        else: # bundle: pinned bun runtime + bundled worker JS (formal shape)
            expected_bun = verify_official_bun(executable) # fail-closed before shipping bun
            if not BUN_LICENSE.is_file() or sha256(BUN_LICENSE) != BUN_LICENSE_SHA256:
                raise SystemExit("Vendored Bun license missing or altered: " + str(BUN_LICENSE))
            script = artifact / "pi_worker.bundle.mjs"
            subprocess.run([executable, "build", "./pi_worker.mjs", "--bundle", "--target=bun",
                            "--minify", "--outfile", str(script)], cwd=stage, env=env, check=True, timeout=180)
            bun_name = "bun.exe" if os.name == "nt" else "bun"
            shutil.copy2(executable, artifact / bun_name)
            if os.name != "nt":
                os.chmod(artifact / bun_name, 0o755)
            shutil.copy2(BUN_LICENSE, artifact / "LICENSE.bun.md") # offline runtime license evidence
            _copy_licenses(stage, artifact)
            # Re-hash the FINAL artifact files and fail-closed if they drift from the pins:
            # verify_official_bun() checked the source before copy, so a swap between verify
            # and copy would otherwise ship an unverified runtime with an honest-but-wrong
            # manifest. The manifest/SHA256SUMS below use these verified artifact hashes.
            artifact_bun_sha = assert_artifact_matches(artifact / bun_name, expected_bun, "Bun runtime")
            artifact_license_sha = assert_artifact_matches(artifact / "LICENSE.bun.md", BUN_LICENSE_SHA256, "Bun license")
            manifest = {"experimental": True, "shape": "bundle", "bun_version": version,
                        "platform": platform.system(), "machine": platform.machine(),
                        "inputs_sha256": {name: sha256(stage / name) for name in inputs},
                        "bun_runtime": bun_name, "bun_runtime_bytes": (artifact / bun_name).stat().st_size,
                        "bun_runtime_sha256": artifact_bun_sha,
                        "expected_runtime_sha256": expected_bun,
                        "runtime_identity": "official-sha256-pinned",
                        "bun_license": "LICENSE.bun.md", "bun_license_sha256": artifact_license_sha,
                        "worker": script.name, "worker_bytes": script.stat().st_size,
                        "worker_sha256": sha256(script),
                        "run_argv_tail": ["run", "--no-install", "--no-env-file", "--no-orphans", script.name],
                        "cwd_pinned_to_artifact_dir": True}
            (artifact / "SHA256SUMS").write_text(
                manifest["bun_runtime_sha256"] + " " + bun_name + "\n" +
                manifest["worker_sha256"] + " " + script.name + "\n" +
                manifest["bun_license_sha256"] + " LICENSE.bun.md\n", encoding="utf-8")
            (artifact / "README.txt").write_text(
                "PangPang packaged Bun Pi worker: pinned bun + bundled worker JS.\n"
                "Select by setting PANGPANG_PI_BUN_BUNDLE to this directory's absolute path.\n"
                "Unset PANGPANG_PI_WORKER / PANGPANG_PI_NODE_BUNDLE; never select more than one.\n"
                "No system Bun/Node/node_modules needed. Python + matching source still required.\n", encoding="utf-8")
        (artifact / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(artifact, output)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bun", default="bun", help="Bun executable (version is pinned)")
    # The formal shape is the default; the known-failing single-file is opt-in only.
    parser.add_argument("--shape", choices=("compile", "bundle"), default="bundle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    out = args.output or (ROOT / "dist" / ("bun-worker" if args.shape == "compile" else "bun-bundle"))
    build(args.bun, out, args.shape)
