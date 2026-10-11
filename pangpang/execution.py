"""Trusted installation configuration and portable SSH transport."""
import base64
import binascii
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import runner
from .consent import host_consented
from .store import Fault, digest, encode, os_family


def artifact_bytes(artifact, data):
    """Verify device bytes against the recorded artifact, never a reported hash."""
    try:
        encoded = data["data"]
        if not isinstance(encoded, str) or len(encoded) > ((8 * 1024 * 1024 + 2) // 3) * 4:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise Fault("invalid_artifact_data", 400) from None
    if (len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != artifact["digest"]
            or (artifact.get("size") is not None and len(raw) != artifact["size"])):
        raise Fault("source_changed")
    return raw


BOOTSTRAP = """import json,pathlib,os,subprocess,sys
s=json.loads(sys.stdin.buffer.read().decode('utf-8')); d=pathlib.Path(s['directory'])
d.mkdir(parents=True,exist_ok=True,mode=0o700)
if d.is_symlink() or (hasattr(os,'getuid') and d.stat().st_uid != os.getuid()): raise RuntimeError('unsafe_runner_directory')
os.chmod(d,0o700)
(d/'runner.py').write_text(s['runner'],encoding='utf-8')
if s.get('windows_job'):(d/'windows_job.py').write_text(s['windows_job'],encoding='utf-8')
if s.get('linux_job'):(d/'linux_job.py').write_text(s['linux_job'],encoding='utf-8')
if s.get('spec') is not None:
 p=d/'spec.json'
 try:
  with p.open('x',encoding='utf-8') as f: json.dump(s['spec'],f)
 except FileExistsError:
  if json.loads(p.read_text(encoding='utf-8')) != s['spec']: raise RuntimeError('execution_spec_conflict')
os.chmod(d/'runner.py',0o600)
print('{}')
"""


# Official pi 1.0.3 standalone binaries. A version string is not provenance: the
# archive sha is the release SHA256SUMS entry, and the binary sha is the extracted
# `pi` / `pi.exe`. Same pin shape as the Bun runtime. No other version is accepted.
PI_VERSION = "1.0.3"
PI_RELEASES = {
    # (system, machine): (archive name, archive sha256, extracted-binary sha256)
    ("Linux", "x86_64"): (
        "pi-linux-x64.tar.gz",
        "9b8c7ff523bd90881d1c1505168f0b9deb2c0357396b92b62f799550a48ff1ed",
        "2cfc19179b73eeec905db38745ff51b8fdd47c85c6bd44f306e3f55479dc20e5"),
    ("Linux", "aarch64"): (
        "pi-linux-arm64.tar.gz",
        "d3093a79ac9e22ef430e2a73e002a3ddeb451de07b722b3aa53aa6a94eaa9edc",
        "abfab34b0e1605eaedb69d349797ec352650dd0cc2775597c785f615992ace68"),
    ("Darwin", "arm64"): (
        "pi-darwin-arm64.tar.gz",
        "7b1aa89ba13542f1aca61ebd63486d0417b5d2fefd5c41ededd8d083ab719b23",
        "8207ce945286b0f7243b7ac25ae39a1bef938c526427666089197ef29378d119"),
    ("Darwin", "x86_64"): (
        "pi-darwin-x64.tar.gz",
        "769e0c1d599dbc8e63ac355957bec8e9832bff91f190d67432392e62ac552194",
        "5acac3d307fb099d1455dea9d1cd690155992d80fe3f8ea3bd3bc291e2a35f2f"),
    ("Windows", "AMD64"): (
        "pi-windows-x64.zip",
        "6cddc3d11210539faa7ba0188504813f2f3ae6d8f93a161d4145cc61a8bdd5de",
        "37cb85729c8db4e9ee739fd9610a3f3a02ed6ec8ea446daf41d7e8fb58e67aa9"),
    ("Windows", "ARM64"): (
        "pi-windows-arm64.zip",
        "ac29f24c02342886fa2d3c1c4629ae4e40d530357535b6a60b5c8ecaf803a11f",
        "a7dc9ab78443102cc2eb824b23f733fa7acb5e2f35ba85b6a4352eb583d48794"),
}
# Upstream MIT text (pi 1.0.3 LICENSE). Vendored so a Release does not point at a URL.
PI_LICENSE = Path(__file__).resolve().parents[1] / "tools" / "third_party" / "pi-LICENSE-1.0.3.md"
PI_LICENSE_SHA256 = "0457f5bcec3b3b211605dfb5d1a49042fd638f3686a410fe099c24a25af13c48"
PI_GUARD_PATH = Path(__file__).resolve().with_name("pi_cwd_guard.mjs")
PI_GUARD_SHA256 = "a3e72cd7fccc93ff62d8677ac22b9b6403d667be24808390515627f410396586"
# Placeholders filled when an installation is built. "{coordinator}" copies the
# coordinator process value or "" so a workspace.env cannot inject one later.
_PI_ISOLATED = "{isolated}"
_PI_COORDINATOR = "{coordinator}"
_PI_EXTRA_ENV = {
    "HTTP_PROXY": _PI_COORDINATOR,
    "HTTPS_PROXY": _PI_COORDINATOR,
    "ALL_PROXY": _PI_COORDINATOR,
    "NO_PROXY": _PI_COORDINATOR,
    "http_proxy": _PI_COORDINATOR,
    "https_proxy": _PI_COORDINATOR,
    "all_proxy": _PI_COORDINATOR,
    "no_proxy": _PI_COORDINATOR,
    "NODE_TLS_REJECT_UNAUTHORIZED": _PI_COORDINATOR,
    "NODE_EXTRA_CA_CERTS": _PI_COORDINATOR,
    "SSL_CERT_FILE": _PI_COORDINATOR,
    "PI_CODING_AGENT_DIR": _PI_ISOLATED,
    "PI_PACKAGE_DIR": _PI_ISOLATED,
    "PI_TELEMETRY": "0",
    "PI_SKIP_VERSION_CHECK": "1",
}
# Kept on the installation so an endpoint edit still changes extra_env identity
# (the existing key-rotation rule). pi itself reads the base URL from models.json.
_PI_BASE_URL_ENV = "XPP_PI_BASE_URL"


def bundled_pi_path():
    """Official binary location. runtime/ is gitignored in a checkout; a wheel
    carries the same relative path under its install anchor."""
    from .layout import install_anchor
    name = "pi.exe" if platform.system() == "Windows" else "pi"
    return install_anchor() / "runtime" / "pi" / name


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_sha(url, dest):
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response, open(dest, "wb") as out:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            out.write(block)
    return digest.hexdigest()


def _read_pi_member(archive_path, archive_name):
    """Bytes of the standalone binary only. photon wasm is not read out."""
    member_name = "pi.exe" if str(archive_name).endswith(".zip") else "pi/pi"
    if str(archive_name).endswith(".zip"):
        with zipfile.ZipFile(archive_path) as bundle:
            info = bundle.getinfo(member_name)
            if info.is_dir():
                return None
            return bundle.read(member_name)
    with tarfile.open(archive_path) as bundle:
        member = bundle.getmember(member_name)
        if not member.isreg():
            return None
        extracted = bundle.extractfile(member)
        if extracted is None:
            return None
        return extracted.read()


def fetch_pinned_pi():
    """Install this platform's pinned pi binary, or return None.

    Downloads the official 1.0.3 archive over HTTPS and writes only `pi/pi` or
    `pi.exe` (photon wasm is not extracted). The archive sha and the binary sha
    must match PI_RELEASES. The file is written to a temp name, checked, then
    renamed into place mode 0755. An unpinned platform, a digest mismatch, or
    any error leaves no new file and does not raise.
    """
    try:
        return _fetch_pinned_pi()
    except Exception:
        return None


def _fetch_pinned_pi():
    release = PI_RELEASES.get((platform.system(), platform.machine()))
    if not release:
        return None
    archive_name, archive_sha, binary_sha = release
    dest = bundled_pi_path()
    if dest.is_file() and _sha256_file(dest) == binary_sha:
        os.chmod(dest, 0o755)
        return dest
    # A shipped artifact already failed closed (or is about to). Never replace it.
    if (dest.parent.parent / "SHIPPED.json").is_file():
        return None
    url = "https://github.com/earendil-works/pi/releases/download/v%s/%s" % (PI_VERSION, archive_name)
    if not url.startswith("https://"):
        return None
    archive_tmp = None
    partial = dest.with_name(dest.name + ".partial")
    try:
        fd, archive_tmp = tempfile.mkstemp(prefix=".pi-archive-")
        os.close(fd)
        if _download_sha(url, archive_tmp) != archive_sha:
            return None
        raw = _read_pi_member(archive_tmp, archive_name)
        if raw is None or hashlib.sha256(raw).hexdigest() != binary_sha:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(raw)
        if _sha256_file(partial) != binary_sha:
            return None
        os.chmod(partial, 0o755)
        os.replace(partial, dest)
        return dest
    finally:
        if archive_tmp:
            try:
                os.unlink(archive_tmp)
            except OSError:
                pass
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass


def _pi_binary_sha():
    release = PI_RELEASES.get((platform.system(), platform.machine()))
    return release[2] if release else None


def isolated_pi_dir(data):
    """Config directory outside the workspace. One directory, not a new store."""
    return Path(data) / "pi-agent"


def binary_matches_pin(cat, path):
    """True when this row has no pin. A pinned row must match its digest exactly."""
    if not cat.get("pinned"):
        return True
    expected = cat.get("sha256") or ""
    if not expected or not path:
        return False
    try:
        actual = _sha256_file(path)
    except OSError:
        return False
    return actual == expected


def materialize_executor_env(cat, data):
    """Installation extra_env from catalog data. None when the row has none."""
    raw = cat.get("extra_env")
    if not raw:
        return None
    isolated = str(isolated_pi_dir(data))
    out = {}
    for key, value in raw.items():
        if value == _PI_ISOLATED:
            out[key] = isolated
        elif value == _PI_COORDINATOR:
            out[key] = os.environ.get(key) or ""
        else:
            out[key] = value
    return out


# Bot profile `api` -> pi 1.0.3 models.json `api` (packages/ai/src/types.ts KnownApi).
# Re-check on any pi upgrade. A protocol 1.0.3 does not implement is absent.
_PI_API = {
    "openai-completions": "openai-completions",
    "openai-responses": "openai-responses",
    "anthropic-messages": "anthropic-messages",
    "google-generative-ai": "google-generative-ai",
}


def pi_models_api(profile):
    """pi `api` for this Bot profile, or None when 1.0.3 has no match.
    A missing `api` is the Bot default, openai-completions."""
    if not profile:
        return None
    return _PI_API.get(profile.get("api") or "openai-completions")


def write_pi_provider_config(directory, base_url, secret_env, model_id, api):
    """models.json for one endpoint pi 1.0.3 can call. The key is $NAME, not the secret.
    `model_id` is the Bot model name: pi sends `model: model.id`.
    settings.json names that provider: 1.0.3 does not auto-select the only custom model."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise Fault("unsafe_pi_dir")
    models = {"providers": {"pangpang": {
        "baseUrl": base_url,
        "api": api,
        "apiKey": "$" + secret_env,
        "models": [{"id": model_id, "name": model_id, "reasoning": False,
                    "input": ["text"], "contextWindow": 128000, "maxTokens": 16384}]}}}
    settings = {"defaultProvider": "pangpang", "defaultModel": model_id}
    (directory / "models.json").write_text(json.dumps(models, indent=2) + "\n", encoding="utf-8")
    (directory / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def pinned_digests(installation):
    """Launch pins for a pinned catalog row: the argv binary and the -e guard file.
    Paths come from the argv that will run; expected digests come from the catalog."""
    cat = ADAPTER_CATALOG.get(installation.get("adapter") or "", {})
    if not cat.get("pinned"):
        return []
    argv = installation.get("argv") or []
    pins = []
    if argv:
        pins.append({"path": argv[0], "sha256": cat.get("sha256") or ""})
    if cat.get("guard_sha256"):
        guard = ""
        for index, part in enumerate(argv[:-1]):
            if part in ("-e", "--extension"):
                guard = argv[index + 1]
                break
        pins.append({"path": guard, "sha256": cat["guard_sha256"]})
    return pins


# Protocols the runner can actually speak. Declaring a tier without one of
# these does not advertise a channel (undeclared executors stay on tier 3).
CLAUDE_STREAM_JSON = "claude-stream-json"
# Codex cannot speak claude-stream-json. app-server JSON-RPC is its own
# protocol; `codex exec` closes stdin and is not an online inject channel.
CODEX_APP_SERVER = "codex-app-server"
_STDIN_PROTOCOLS = {CLAUDE_STREAM_JSON, CODEX_APP_SERVER}
# Tier 2 is not an open stdin. The runner interrupts the turn, then starts
# the same session again with `--resume` of the id it passed as `--session-id`.
# Another executor declares the same protocol name; the adapter name is not
# a branch.
CLI_SESSION_RESUME = "cli-session-resume"
_RESUME_PROTOCOLS = {CLI_SESSION_RESUME}
# A session channel, not an interjection tier. The runner speaks this name
# on the stdin it already owns. A row that names anything else does not
# advertise a channel.
ACP = "acp"
_SESSION_PROTOCOLS = frozenset({ACP})


def declared_session_protocol(catalog_row):
    """The session protocol a catalog row may advertise, or None.

    Membership in `_SESSION_PROTOCOLS` is the capability. A tier number,
    an stdin protocol, or an unregistered name does not qualify.
    """
    if not isinstance(catalog_row, dict):
        return None
    protocol = catalog_row.get("session_protocol")
    if protocol in _SESSION_PROTOCOLS:
        return protocol
    return None


def interjection_tier_of(catalog_row):
    """Interjection tier for one catalog row.

    Absent, or a tier this process cannot carry, is 3: cancel and redispatch.
    Tier 1 is honored only together with a stdin protocol the runner implements.
    Tier 2 is honored only together with a resume protocol the runner implements.
    A tier number alone does not advertise a channel.
    """
    if not isinstance(catalog_row, dict):
        return 3
    if catalog_row.get("interjection_tier") == 1 and catalog_row.get("stdin_protocol") in _STDIN_PROTOCOLS:
        return 1
    if catalog_row.get("interjection_tier") == 2 and catalog_row.get("resume_protocol") in _RESUME_PROTOCOLS:
        return 2
    return 3


def task_interjection_tier(installation, host):
    """Tier a correction to a task on this installation and host can use.

    The planner's view and inject_task_message both call this, so the tier the
    Bot sees is the tier inject accepts. Only local and SSH hosts have a mailbox
    the runner reads. The runner sends no SIGINT on Windows, so tier 2 there is 3.
    """
    adapter = installation.get("adapter") if isinstance(installation, dict) else None
    tier = interjection_tier_of(ADAPTER_CATALOG.get(adapter))
    host = host if isinstance(host, dict) else {}
    if tier == 3 or host.get("transport") not in {"local", "ssh"}:
        return 3
    if tier == 2 and os_family(host.get("os")) == "win":
        return 3
    return tier


def stdin_protocol_for(adapter):
    row = ADAPTER_CATALOG.get(adapter or "")
    if interjection_tier_of(row) != 1:
        return None
    return row.get("stdin_protocol")


def resume_protocol_for(adapter):
    row = ADAPTER_CATALOG.get(adapter or "")
    if interjection_tier_of(row) != 2:
        return None
    return row.get("resume_protocol")


def session_protocol_for(adapter):
    """Launch channel for one catalog adapter. Not an interjection tier."""
    return declared_session_protocol(ADAPTER_CATALOG.get(adapter or ""))


def permission_bridge_for(adapter):
    """Chat bridge a catalog row may advertise, or None.

    Same shape as the protocol helpers: an unregistered name does not
    advertise a channel, and undeclared adapters keep today's ignore behavior.
    """
    row = ADAPTER_CATALOG.get(adapter or "")
    if not isinstance(row, dict):
        return None
    bridge = row.get("permission_bridge")
    if bridge == runner.PERMISSION_BRIDGE_CHAT:
        return bridge
    return None


def secret_env_name(adapter):
    cat = ADAPTER_CATALOG.get(adapter, {})
    if cat.get("secret_env"):
        return cat["secret_env"]
    return "CODEX_API_KEY" if adapter == "codex" else "ANTHROPIC_API_KEY"


# Extensible executor catalog. Adding a new CLI = add one DATA row here
# (operator edit, never AI). `argv` is the parameter template appended after the
# probed binary; the real argv is [resolved_binary, *argv]. This is the single
# source of truth for: which CLIs are discoverable, how they are invoked, their
# default per-installation concurrency, and their default routing priority
# (lower = preferred). SIS boundary: argv is ALWAYS system-generated from this
# table — the model can never define what command runs.
ADAPTER_CATALOG = {
    "qwen-claude": {"binary": "qwen-claude", "argv": ["-p"], "concurrency": 2, "priority": 10,
                    "use_when": "通用重活默认首选：取数据/联网检索/画图/写文档/研究报告。能联网、知识新，不确定派谁时用它。"},
    # interjection_tier 1 + stdin_protocol: online inject on a protocol this
    # runner implements. Other adapters omit both and stay on tier 3
    # (cancel + redispatch). A tier number without a registered protocol
    # does not count.
    "claude-code": {"binary": "claude", "argv": ["-p", "--permission-mode", "acceptEdits", "--allowed-tools", "Read", "Write"], "concurrency": 1, "priority": 20,
                    "use_when": "改代码、编辑本地文件。不擅长取金融数据或画图。",
                    "interjection_tier": 1, "stdin_protocol": "claude-stream-json"},
    # Same declaration shape as claude-code. argv stays the stored `exec -`
    # invocation; the runner switches that to app-server when this protocol is set.
    "codex": {"binary": "codex", "argv": ["exec", "-"], "concurrency": 1, "priority": 30,
              "use_when": "跑代码/命令，以及用内置 image_gen 出图（画插画/海报）。取数据/研究报告仍首选 qwen-claude。",
              "interjection_tier": 1, "stdin_protocol": "codex-app-server"},
    # Tier 2, not tier 1. Gemini CLI 0.62.0 has no input-format and no
    # mid-turn stdin inject. Headless is a non-TTY stdin prompt.
    # `--session-id` is the derived attempt id; a later turn is `--resume`
    # of that same id after SIGINT. `--skip-trust` is the headless workspace
    # gate (exit 55 without it), not tool approval: no --yolo, no
    # --approval-mode. `--resume` and `--session-id` together are rejected
    # by the CLI, so the runner never passes both.
    "gemini": {"binary": "gemini", "argv": ["--skip-trust"], "concurrency": 1, "priority": 35,
               "use_when": "通用编码与改文件。补一句不能插进正在跑的回合，会打断这一轮再在同一会话里继续。工具确认不会自动通过。",
               "interjection_tier": 2, "resume_protocol": "cli-session-resume",
               "secret_env": "GEMINI_API_KEY"},
    # Bundled fallback. Priority 40 is below every user-installed CLI above.
    # argv is the 1.0.3 print-mode invocation; the prompt is stdin, never an argument.
    "pi": {"binary": str(bundled_pi_path()), "argv": [
               "--print", "--no-session", "--tools", "read,write,edit,grep,find,ls",
               "--offline", "--no-extensions", "--no-skills", "-e", str(PI_GUARD_PATH)],
           "concurrency": 1, "priority": 40,
           "use_when": "内置兜底：没有更优先的已安装 CLI 时，在工作目录内读写文件。不能跑命令。",
           "pinned": True, "sha256": _pi_binary_sha(), "guard_sha256": PI_GUARD_SHA256,
           "extra_env": dict(_PI_EXTRA_ENV), "secret_env": "OPENAI_API_KEY", "models_json": True},
    # Session channel, not tier 1 or tier 2. No stdin inject, no --resume,
    # no approval flag. Discover skips this row until a binary named
    # xpp-acp exists; install and self-check are unchanged for the rows above.
    # The runner speaks ACP on stdin because session_protocol is registered,
    # not because the adapter name is special.
    "acp": {"binary": "xpp-acp", "argv": [], "concurrency": 1, "priority": 50,
            "use_when": "ACP 会话：把一句话交给会说 ACP 的执行器，并拿回这一轮的原文。工具许可会发到对话里等你明确同意或拒绝，不会自动同意；提问和文件请求也不会自动答应。不能往正在跑的回合里插话。",
            "session_protocol": ACP, "permission_bridge": runner.PERMISSION_BRIDGE_CHAT},
}


PROFILE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
PROFILE_ENV_KEYS = frozenset(("CLAUDE_CONFIG_DIR", "CODEX_HOME"))
MAX_PROFILE_ENV_ITEMS = 16
MAX_PROFILE_ENV_VALUE_CHARS = 4096


def installation_id(adapter, host_id, profile=None):
    """The ONE canonical installation namespace, shared by the device configure,
    the cloud approve and the cloud report so all three derive the same id from
    (adapter, profile, host). Without a profile it is byte-identical to the
    historical adapter + "-" + host_id."""
    if not profile:
        return adapter + "-" + host_id
    if not isinstance(profile, str) or not PROFILE_PATTERN.fullmatch(profile):
        raise Fault("invalid_installation_profile", 400,
                    advice="profile must match ^[a-z0-9][a-z0-9-]{0,31}$")
    return adapter + "-" + profile + "-" + host_id


def validate_profile_env(env):
    """Per-installation operator env: a closed whitelist of profile-location
    variables only. Secrets go through secret_refs/vault; control-plane and
    process-hijacking variables (PANGPANG_*, PATH, PYTHONPATH, ...) are never
    acceptable no matter who asks."""
    if not isinstance(env, dict) or len(env) > MAX_PROFILE_ENV_ITEMS:
        raise Fault("invalid_installation_env", 400,
                    advice="env must be an object of at most %d entries" % MAX_PROFILE_ENV_ITEMS)
    for key, value in env.items():
        if (key not in PROFILE_ENV_KEYS or not isinstance(value, str) or not value
                or "\x00" in value or len(value) > MAX_PROFILE_ENV_VALUE_CHARS):
            raise Fault("invalid_installation_env", 400,
                        advice="env keys are limited to %s; secrets belong in the credential vault"
                               % sorted(PROFILE_ENV_KEYS))


MAX_EXTRA_ARGV_ITEMS = 16
MAX_EXTRA_ARGV_ITEM_CHARS = 1024
MAX_EXTRA_ARGV_TOTAL_CHARS = 4096
_EXTRA_ARGV_DENY = ("yolo", "dangerously", "bypass", "full-auto", "no-sandbox",
                    "approve-for-me", "skip-permissions", "danger-full-access")


def validate_extra_argv(adapter, extra_argv):
    """Operator-owned extra argv appended after the catalog argv. Shape is
    necessary but not sufficient: permission/sandbox-disabling tokens are
    rejected outright (the narrow tool grant a catalog row advertises must not
    be widened through this field), and for the codex app-server protocol every
    exec-only flag is rejected because the protocol rewrite DROPS it — storing
    a flag the launched process never receives would be a silent lie. For the
    same reason bare tokens are rejected there: the rewrite repositions them
    before the app-server subcommand, and the tokens the rewrite drops outright
    ('-', '{prompt}' templates) are rejected too."""
    if not isinstance(extra_argv, list) or len(extra_argv) > MAX_EXTRA_ARGV_ITEMS:
        raise Fault("invalid_extra_argv", 400,
                    advice="extra_argv must be a list of at most %d strings" % MAX_EXTRA_ARGV_ITEMS)
    total = 0
    for item in extra_argv:
        if not isinstance(item, str) or "\x00" in item or not item or len(item) > MAX_EXTRA_ARGV_ITEM_CHARS:
            raise Fault("invalid_extra_argv", 400,
                        advice="extra_argv entries must be non-empty plain strings")
        total += len(item)
        if any(marker in item.lower() for marker in _EXTRA_ARGV_DENY):
            raise Fault("invalid_extra_argv", 400,
                        advice="permission/sandbox-disabling flags are not allowed in extra_argv: %s" % item)
    if total > MAX_EXTRA_ARGV_TOTAL_CHARS:
        raise Fault("invalid_extra_argv", 400,
                    advice="extra_argv exceeds %d characters in total" % MAX_EXTRA_ARGV_TOTAL_CHARS)
    row = ADAPTER_CATALOG.get(adapter or "") or {}
    if row.get("stdin_protocol") == CODEX_APP_SERVER:
        dropped = runner._CODEX_EXEC_ONLY_BOOL_FLAGS | runner._CODEX_EXEC_ONLY_VALUE_FLAGS
        index = 0
        while index < len(extra_argv):
            item = extra_argv[index]
            name = item.partition("=")[0]
            if name in dropped:
                raise Fault("invalid_extra_argv", 400,
                            advice="'%s' is a codex exec-only flag: the app-server protocol this "
                                   "installation runs drops it and the process never receives it; "
                                   "trust the workspace instead (git init or codex config trust_level)" % name)
            if name == "--listen":
                raise Fault("invalid_extra_argv", 400,
                            advice="'--listen' is owned by the app-server bridge (stdio only); "
                                   "any other transport is rejected at launch, so storing it here would only defer the failure")
            if item == "-" or "{prompt}" in item:
                raise Fault("invalid_extra_argv", 400,
                            advice="'%s' is dropped by the app-server protocol rewrite before launch "
                                   "and the process never receives it; storing it here would be a "
                                   "silent lie" % item)
            if not item.startswith("-"):
                raise Fault("invalid_extra_argv", 400,
                            advice="'%s' is a bare token: the app-server protocol rewrite repositions it "
                                   "before the app-server subcommand, silently changing the codex "
                                   "subcommand; write values as '--flag=value' or attach them to a known "
                                   "value flag instead" % item)
            if name in runner._CODEX_VALUE_FLAGS and "=" not in item:
                index += 2
                continue
            index += 1


class Execution:
    def __init__(self, config, data):
        self.config, self.data = config, Path(data)
        self.broker = None
        # Set by Service from --deployment-level. None until then: do not treat
        # an Execution built for validation as an L0 declaration.
        self.deployment_level = None
        self.hosts = {h["id"]: h for h in config.get("hosts", [])}
        self.installations = {e["id"]: e for e in config.get("installations", [])}
        self.resources = {r["key"]: r for r in config.get("resources", [])}
        for r in self.resources.values():
            for verb in ("acquire_argv", "release_argv"):
                if not isinstance(r.get(verb), list) or not r[verb] or "--force" in r[verb]:
                    raise Fault("invalid_resource_command", 400)
        for h in self.hosts.values():
            if h.get("transport") not in {"local", "ssh", "connector"}:
                raise Fault("invalid_transport", 400)
            if h["transport"] == "ssh":
                # an ssh host's OS is load-bearing — it decides remote
                # command construction (PowerShell vs POSIX) and path case semantics.
                # Require a KNOWN OS (exact token via os_family, not a prefix) at config
                # validation (fail fast) rather than mis-execute later.
                if os_family(h.get("os")) is None:
                    raise Fault("ssh_host_os_required", 400)
                if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*", h.get("target", "")):
                    raise Fault("invalid_ssh_target", 400)
                root_pattern = r"[A-Za-z]:[\\/][a-zA-Z0-9_./\\ -]+" if self.windows(h) else r"/[a-zA-Z0-9_./-]+"
                if not re.fullmatch(root_pattern, h.get("runner_root", "")) or ".." in re.split(r"[\\/]", h["runner_root"]):
                    raise Fault("invalid_runner_root", 400)
                if not re.fullmatch(r"[a-zA-Z0-9_/.:\\ -]+", h.get("python", "python3")):
                    raise Fault("invalid_remote_python", 400)
        for e in self.installations.values():
            if e.get("adapter") not in ADAPTER_CATALOG:
                raise Fault("invalid_adapter", 400,
                            advice="adapter rows come from the built-in catalog plus custom rows in %s; "
                                   "a removed or renamed adapters.json requires restoring it or removing "
                                   "the installations that reference its adapters" % (self.data / "adapters.json"))
            if not isinstance(e.get("argv"), list) or not e["argv"] or not all(isinstance(a, str) and "\x00" not in a for a in e["argv"]):
                raise Fault("invalid_argv", 400)
            if "env" in e:
                validate_profile_env(e["env"])
            if "extra_argv" in e:
                validate_extra_argv(e.get("adapter"), e["extra_argv"])
            # executor capacity is expressed by the generic
            # max_concurrency, not a product-level qwen resource special-case. A
            # real external device lock may still be configured via resource_keys,
            # but qwen no longer *requires* one.
            if self.hosts[e["host_id"]]["transport"] == "ssh" and e.get("secret_refs"):
                raise Fault("managed_cli_credentials_require_connector", 400)
            for key in e.get("resource_keys", []):
                if key not in self.resources:
                    raise Fault("unknown_resource", 400)

    def ssh(self, host):
        return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", host["target"]]

    @staticmethod
    def windows(host):
        return os_family(host.get("os")) == "win"

    def remote(self, host, arguments):
        if self.windows(host):
            # Encoded PowerShell command keeps shell metacharacters in values
            # inert. The remote Python runner receives prompts through stdin.
            code = "& " + " ".join("'" + a.replace("'", "''") + "'" for a in arguments)
            encoded = base64.b64encode(code.encode("utf-16le")).decode()
            return self.ssh(host) + ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        return self.ssh(host) + [shlex.quote(a) for a in arguments]

    def directory(self, host, execution_id):
        if not re.fullmatch(r"exec-[a-f0-9]{32}", execution_id):
            raise Fault("invalid_execution_id", 400)
        if host["transport"] == "local":
            return str(self.data / "runs" / execution_id)
        return host["runner_root"].rstrip("/") + "/" + execution_id

    def prepare(self, host, directory, spec=None):
        from .store import assert_no_txn_io
        assert_no_txn_io("execution.prepare") # filesystem writes / remote bootstrap
        if host["transport"] == "local":
            d = Path(directory)
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            if spec is not None:
                # Idempotent write for start-retry: a
                # lost/partial first start leaves this attempt's run dir behind, and
                # _try_start re-invokes prepare for the SAME attempt (same
                # execution_id, same spec). An exclusive "x" create would then raise
                # FileExistsError on every retry and wrongly drive the still-launching
                # attempt to failed. The run dir is unique per execution_id, so an
                # existing spec.json is THIS attempt's own spec — write-once, then reuse.
                path = d / "spec.json"
                try:
                    with open(path, "x", encoding="utf-8") as f:
                        f.write(encode(spec))
                except FileExistsError:
                    if json.loads(path.read_text(encoding="utf-8")) != spec:
                        raise Fault("execution_spec_conflict")
            return
        if self.windows(host) and not host.get("runner_root_acl_verified"):
            raise Fault("windows_runner_acl_not_verified")
        # Ship every helper runner.py may import on the remote: a Linux host with a
        # usable systemd --user scope imports linux_job at launch.
        helpers = {name: Path(runner.__file__).with_name(name + ".py").read_text(encoding="utf-8")
                   for name in ("windows_job", "linux_job")}
        payload = {"directory": directory, "runner": Path(runner.__file__).read_text(encoding="utf-8"),
                   "spec": spec, **helpers}
        # Base64 avoids nested quote rewriting by Windows' native argv parser.
        bootstrap = "import base64;exec(base64.b64decode('" + base64.b64encode(BOOTSTRAP.encode()).decode() + "'))"
        self._run(self.remote(host, [host.get("python", "python3"), "-c", bootstrap]), payload)

    @staticmethod
    def _run(argv, payload=None):
        from .store import assert_no_txn_io
        assert_no_txn_io("execution._run")
        try:
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                result = subprocess.run(argv, input=None if payload is None else encode(payload).encode(),
                                        stdout=out, stderr=err, timeout=30)
                out.seek(0)
                raw = out.read(12 * 1024 * 1024 + 1)
        except (OSError, subprocess.TimeoutExpired):
            raise Fault("transport_unavailable") from None
        if result.returncode:
            raise Fault("runner_rejected_request")
        if len(raw) > 12 * 1024 * 1024:
            raise Fault("runner_response_too_large")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise Fault("invalid_runner_response") from None

    def call(self, host, action, directory, payload=None):
        if host["transport"] == "local":
            argv = [sys.executable, runner.__file__, action, directory]
        else:
            argv = self.remote(host, [host.get("python", "python3"), directory + "/runner.py", action, directory])
        return self._run(argv, payload)

    def inspect_file(self, host, path, action="hash", data=None):
        if host["transport"] == "connector":
            return self.broker.request(host["id"], "file", {"path": path, "action": action, "data": data})
        if host["transport"] == "local":
            try:
                runner.check_control_separation(host.get("allowed_roots", []), [self.data])
            except ValueError:
                raise Fault("workspace_overlaps_control_plane", 403) from None
        directory = str(self.data / "file-tools") if host["transport"] == "local" else host["runner_root"] + "/file-tools"
        self.prepare(host, directory)
        payload = {"path": path, "allowed_roots": host.get("allowed_roots", [])}
        if data is not None:
            payload["data"] = data
        return self.call(host, action, directory, payload)

    def probe(self, installation_id):
        e = self.installations[installation_id]
        h = self.hosts[e["host_id"]]
        if h["transport"] == "connector":
            return self.broker.request(h["id"], "probe", {"installation_id": installation_id})
        directory = str(self.data / "probe") if h["transport"] == "local" else h["runner_root"] + "/probe"
        self.prepare(h, directory)
        # Explicit L0 is the operator declaration for the process-group tier.
        # Do not write l0_process_group_consent here, and do not set the flag
        # when the deployment level is absent.
        if host_consented(h) or self.deployment_level == "L0":
            e = dict(e, allow_pgroup_cooperative=True)
        return self.call(h, "probe", directory, e)

    def spec(self, task):
        e = self.installations[task["installation_id"]]
        h = self.hosts[e["host_id"]]
        spec = {"execution_id": task["execution_id"], "spec_digest": task["spec_digest"],
                "attempt_uuid": task.get("attempt_uuid"),
                "argv": list(e["argv"]) + list(e.get("extra_argv", [])),
                "prompt": task["prompt"], "prompt_mode": e.get("prompt_mode", "stdin"),
                "cwd": task.get("cwd", e["cwd"]), "allowed_roots": h.get("allowed_roots", []), "inputs": task["inputs"],
                "control_roots": [str(self.data)] if h["transport"] == "local" else [h["runner_root"]],
                "outputs": task.get("outputs", []), "required_outputs": task.get("required_outputs", []),
                "timeout_s": task.get("timeout_s", e.get("timeout_s", 3600)),
                "env_allowlist": e.get("env_allowlist", []),
                "extra_env": dict(e.get("env", {}), **e.get("extra_env", {})),
                "secret_refs": e.get("secret_refs", {}),
                "vault_path": str(self.data / "vault") if h["transport"] == "local" else None,
                "resources": [self.resources[k] for k in e.get("resource_keys", [])]}
        pins = pinned_digests(e)
        if pins:
            spec["pinned_digests"] = pins
        # Present for a current consent field or an explicit L0 declaration, so the
        # runner can select the process-group tier. Absent otherwise, so a spec is
        # unchanged when the flag is not L0 and the host has not consented.
        if host_consented(h) or self.deployment_level == "L0":
            spec["allow_pgroup_cooperative"] = True
        # The supervision the route admitted, frozen at submit: the runner refuses
        # to launch under anything weaker. Absent for pre-upgrade tasks (unchanged).
        if task.get("supervision_floor"):
            spec["supervision_floor"] = task["supervision_floor"]
        # Launch channel only. Not a task field: the session id is derived from
        # execution_id inside the runner. Absent for every adapter that did not
        # declare a protocol this runner implements.
        protocol = stdin_protocol_for(e.get("adapter"))
        if protocol:
            spec["stdin_protocol"] = protocol
        # Same shape as stdin_protocol: a launch channel, not a task field.
        # The session id is derived from execution_id inside the runner.
        resume = resume_protocol_for(e.get("adapter"))
        if resume:
            spec["resume_protocol"] = resume
        # Same shape: a launch channel, not a task field and not a tier.
        # Absent unless the catalog row names a registered session protocol.
        session = session_protocol_for(e.get("adapter"))
        if session:
            spec["session_protocol"] = session
        # Same shape: a capability, not a task field and not an argv flag.
        # Absent unless the catalog row names the chat bridge.
        bridge = permission_bridge_for(e.get("adapter"))
        if bridge:
            spec["permission_bridge"] = bridge
        return spec

    def start(self, task):
        host = self.hosts[task["host_id"]]
        if host["transport"] == "connector":
            return self.broker.start(task)
        if host["transport"] == "local":
            try:
                runner.check_control_separation(host.get("allowed_roots", []), [self.data])
            except ValueError:
                raise Fault("workspace_overlaps_control_plane", 403) from None
        directory = self.directory(host, task["execution_id"])
        self.prepare(host, directory, self.spec(task))
        return self.call(host, "start", directory)

    def poll(self, task, *, tail=False):
        host = self.hosts[task["host_id"]]
        if host["transport"] == "connector":
            return self.broker.poll(task)
        directory = self.directory(host, task["execution_id"])
        if host["transport"] == "local":
            # in-process observation — no subprocess spawn per poll. The
            # runner's inspect is a pure, read-hardened function; a failed read is
            # reported as 'no receipt' (observation failure), never as a fact.
            return runner.inspect(Path(directory), include_tail=tail)
        return self.call(host, "inspect", directory)

    def cancel(self, task):
        host = self.hosts[task["host_id"]]
        if host["transport"] == "connector":
            return self.broker.cancel(task)
        return self.call(host, "cancel", self.directory(host, task["execution_id"]))

    def inject(self, task, message_uuid, text, *, interrupt_id=None):
        """Append one user sentence to a running attempt's steer mailbox.

        `interrupt_id` is the tier-2 interrupt row in the same file. Local and
        SSH use the runner that already owns the process. Connector has no
        steer command; the caller treats that as tier 3.
        """
        host = self.hosts[task["host_id"]]
        if host["transport"] == "connector":
            raise Fault("interjection_unsupported")
        payload = {"uuid": message_uuid, "text": text}
        if interrupt_id is not None:
            payload["interrupt"] = interrupt_id
        return self.call(host, "inject", self.directory(host, task["execution_id"]), payload)

    def copy(self, artifact, host_id, destination):
        # Explicitly authorized transfer. Exclusive create, no in-place overwrite.
        source = self.read_artifact(artifact)
        # R7: never trust the source device's self-reported digest field. Recompute
        # the hash (and length) from the ACTUAL decoded bytes BEFORE sending them to
        # the target — a compromised source could return "old digest + new bytes",
        # and the pre-fix code only noticed AFTER writing the tampered file to the
        # destination. Validating the bytes here means tampered content is never
        # written at all.
        artifact_bytes(artifact, source)
        target = self.inspect_file(self.hosts[host_id], destination, "write", source["data"])
        if target["digest"] != artifact["digest"]:
            raise Fault("transfer_digest_mismatch")
        return target

    def read_artifact(self, artifact):
        host = self.hosts[artifact["host_id"]]
        execution_id = artifact.get("snapshot_execution_id")
        if execution_id:
            payload = {"execution_id": execution_id, "digest": artifact["digest"]}
            if host["transport"] == "connector":
                return self.broker.request(host["id"], "artifact_read", payload)
            directory = self.directory(host, execution_id)
            return self.call(host, "artifact_read", directory, payload)
        return self.inspect_file(host, artifact["path"], "read")
