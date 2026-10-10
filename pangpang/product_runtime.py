"""Fail-closed checks for a shipped Bun worker and the required pi binary.

A wheel or a Release tree with runtime/SHIPPED.json already contains the
bytes. Startup hashes them and refuses to serve on a miss. It does not
download a replacement. A plain source checkout (no SHIPPED.json) is the
dev path: Node remains the worker fallback, and pi is still installed by
the existing one-shot fetch when no other CLI is present.
"""
import json
import os
import platform

from . import __version__
from .execution import (PI_LICENSE_SHA256, PI_RELEASES, PI_VERSION, _sha256_file)
from . import layout
from .runtime_pins import BUN_LICENSE_SHA256, BUN_VERSION, OFFICIAL_BUN_SHA256


def product_runtime_required(anchor=None):
    """True when missing Bun/pi must fail before serve and must not use Node."""
    if layout.is_frozen() or not layout.is_source_checkout():
        return True
    anchor = layout.install_anchor() if anchor is None else anchor
    return (anchor / "runtime" / "SHIPPED.json").is_file()


def _manifest(anchor):
    path = anchor / "runtime" / "SHIPPED.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("runtime_manifest_invalid") from exc
    if not isinstance(data, dict) or data.get("shipped") is not True or not isinstance(data.get("files"), dict):
        raise ValueError("runtime_manifest_invalid")
    return data


def _check_manifest_identity(manifest, system, machine):
    if manifest.get("pangpang_version") != __version__:
        raise ValueError("runtime_version_mismatch")
    if manifest.get("platform") != system or manifest.get("machine") != machine:
        raise ValueError("runtime_platform_mismatch")
    if manifest.get("pi_version") != PI_VERSION or manifest.get("bun_version") != BUN_VERSION:
        raise ValueError("runtime_pin_version_mismatch")


def verify_pi(anchor=None):
    """Hash the shipped pi binary. Missing or mismatched bytes raise; nothing is downloaded."""
    anchor = layout.install_anchor() if anchor is None else anchor
    system, machine = platform.system(), platform.machine()
    release = PI_RELEASES.get((system, machine))
    if not release:
        raise ValueError("pi_platform_unpinned")
    binary_sha = release[2]
    name = "pi.exe" if system == "Windows" else "pi"
    path = anchor / "runtime" / "pi" / name
    if not path.is_file():
        raise ValueError("pi_missing")
    actual = _sha256_file(path)
    if actual != binary_sha:
        raise ValueError("pi_hash_mismatch")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise ValueError("pi_not_executable")
    manifest = _manifest(anchor)
    if manifest is not None:
        _check_manifest_identity(manifest, system, machine)
        rel = "runtime/pi/" + name
        if manifest["files"].get(rel) != binary_sha:
            raise ValueError("pi_manifest_mismatch")
        license_rel = "runtime/pi/LICENSE.pi.md"
        license_path = anchor / "runtime" / "pi" / "LICENSE.pi.md"
        if (manifest["files"].get(license_rel) != PI_LICENSE_SHA256
                or not license_path.is_file() or _sha256_file(license_path) != PI_LICENSE_SHA256):
            raise ValueError("pi_license_mismatch")
    return path


def verify_bun(anchor=None):
    """Hash the shipped Bun runtime. A missing worker never selects system Node."""
    anchor = layout.install_anchor() if anchor is None else anchor
    system, machine = platform.system(), platform.machine()
    expected = OFFICIAL_BUN_SHA256.get((system, machine))
    if not expected:
        raise ValueError("bun_platform_unpinned")
    name = "bun.exe" if system == "Windows" else "bun"
    root = anchor / "runtime" / "pi-worker"
    bun = root / name
    script = root / "pi_worker.bundle.mjs"
    if not bun.is_file() or not script.is_file():
        raise ValueError("pi_worker_incomplete_release_bundle")
    if os.name != "nt" and not os.access(bun, os.X_OK):
        raise ValueError("pi_worker_binary_not_executable")
    if _sha256_file(bun) != expected:
        raise ValueError("bun_hash_mismatch")
    manifest = _manifest(anchor)
    if manifest is not None:
        _check_manifest_identity(manifest, system, machine)
        if manifest["files"].get("runtime/pi-worker/" + name) != expected:
            raise ValueError("bun_manifest_mismatch")
        worker_rel = "runtime/pi-worker/pi_worker.bundle.mjs"
        recorded = manifest["files"].get(worker_rel)
        if not recorded or _sha256_file(script) != recorded:
            raise ValueError("worker_hash_mismatch")
        license_rel = "runtime/pi-worker/LICENSE.bun.md"
        license_path = root / "LICENSE.bun.md"
        if (manifest["files"].get(license_rel) != BUN_LICENSE_SHA256
                or not license_path.is_file() or _sha256_file(license_path) != BUN_LICENSE_SHA256):
            raise ValueError("bun_license_mismatch")
    return root


def prepare_product(role, parser, anchor=None):
    """Refuse to serve when this install ships (or must ship) Bun and pi and either is bad.

    ``anchor`` is for tests. The console script and launch.py omit it.
    """
    if anchor is None:
        anchor = layout.install_anchor()
    if not product_runtime_required(anchor):
        return
    try:
        if role == "cloud":
            verify_bun(anchor)
        verify_pi(anchor)
    except ValueError as exc:
        parser.error(
            "运行时校验失败（%s）。pi %s 与 Bun %s 应已在包内；"
            "摘要不符或文件缺失时拒绝启动，不会下载替换，也不会改用系统 Node。"
            % (exc, PI_VERSION, BUN_VERSION))
