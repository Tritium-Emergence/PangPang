"""Device-local operator consent for the L0 process-group supervision tier.

python -m pangpang.consent --data DATA [--host ID] show|accept|revoke

The weaker `posix-pgroup-cooperative` tier (macOS /
Linux without a cgroup scope) runs only on a host whose operator has read and
accepted the CURRENT risk text. The consent is one field on the host entry of the
device's own installation config, so it is covered by policy_digest (changing it
invalidates earlier probes) and reaches the runner through the spec. The cloud /
web cannot write it: it is recorded only by this interactive local tool, while
the service that owns the database is stopped. The control plane's side of the
agreement is the separate, explicit `--deployment-level L0` declaration.

Changing RISK_VERSION invalidates every recorded consent.
"""
import argparse
import sys
from pathlib import Path

CONSENT_KEY = "l0_process_group_consent"
RISK_VERSION = "l0-pgroup-risk-v2"
RISK_TEXT = """\
L0 进程组协作档 —— 风险说明（{version}）

启用后，这台主机上没有 cgroup/Job 强监管的执行器（macOS、无 systemd --user 的 Linux）
将以“进程组协作档”（posix-pgroup-cooperative）运行。请确认你理解并接受：

1. 这是自用受信（L0）档，不是隔离。执行器 CLI 以你的同一账号运行，可读写你账号能访问的
   一切：胖胖控制目录、Vault、设备令牌、CLI API key；拿到设备令牌即可冒充本设备提交回执。
2. 本档信任本机 CLI 及其项目启动配置（hooks、MCP、插件、工作区里的脚本），不提供已核验的
   只读保证，也不做权限闭合。
3. 收尾只证明“CLI 自己的进程组已清空”。通过 setsid / 双 fork 脱离进程组的后代不会被发现
   或终止，它们可能继续修改工作区、占用端口/GPU/共享资源；系统不会因此保持占用。
   这并不少见：Node 的 detached 子进程（setsid）就会脱离；各 CLI 的实际覆盖尚未逐一实测。
   本档没有 cgroup 那样的运行时长兜底：若 runner 被强杀，组内进程与脱离者都可能无限期
   运行，并持有注入其环境变量的密钥。
4. 回执中的 process_tree_confirmed=true 在本档仅表示上述组作用域已清空，并附
   supervision=posix-pgroup-cooperative、termination_evidence=pgroup-empty；
   它不是“全部后代已结束”的证明。
5. 仅适用于 L0 自用部署；L1/L2 与客户发布不会接受本档。网页操作员与设备操作员
   未被系统独立验证为同一人。
"""


def host_consented(host):
    """True iff this host entry carries consent to the CURRENT risk text."""
    return (host or {}).get(CONSENT_KEY) == RISK_VERSION


def _open(data):
    """Take the owning service's lock (it must be stopped: one DB writer) and open
    its store. Coordinator data holds bots.db, a paired connector connector.db."""
    from .__main__ import ProcessLock
    from .store import Store
    for db, lock in (("connector.db", "connector.lock"), ("bots.db", "coordinator.lock")):
        if (data / db).exists():
            try:
                handle = ProcessLock(data / lock)
            except OSError:
                raise SystemExit("服务正在运行：请先停止胖胖服务/连接器，再记录或撤回同意。") from None
            return handle, Store(data / db)
    raise SystemExit("数据目录中没有 bots.db 或 connector.db：请先启动一次服务或完成配对。")


def _target(config, host_id):
    hosts = [h for h in config.get("hosts", []) if h.get("transport") in {"local", "ssh"}]
    if host_id:
        hosts = [h for h in hosts if h.get("id") == host_id]
    if len(hosts) != 1:
        ids = ", ".join(h.get("id", "?") for h in config.get("hosts", []) if h.get("transport") in {"local", "ssh"})
        raise SystemExit("请用 --host 指定唯一主机（可选：%s）" % (ids or "无"))
    return hosts[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="L0 process-group tier: device-local operator consent")
    parser.add_argument("action", choices=["show", "accept", "revoke"])
    parser.add_argument("--data", help="coordinator or connector data directory")
    parser.add_argument("--host", help="host id (required when the config has several local/ssh hosts)")
    args = parser.parse_args(argv)
    print(RISK_TEXT.format(version=RISK_VERSION))
    if args.action == "show" and not args.data:
        print("--config 文件部署：在 host 条目加入 \"%s\": \"%s\"。" % (CONSENT_KEY, RISK_VERSION))
        return 0
    if not args.data:
        parser.error("--data is required for accept/revoke")
    from .store import Fault
    lock, store = _open(Path(args.data).resolve())
    try:
        try:
            config = store.get("settings", "managed")["config"]
        except Fault:
            raise SystemExit("尚无受管配置：请先启动一次服务或完成配对。") from None
        host = _target(config, args.host)
        state = "已同意（当前版本）" if host_consented(host) else "未同意" if CONSENT_KEY not in host else "同意已过期"
        print("主机 %s：%s" % (host["id"], state))
        if args.action == "show":
            return 0
        if args.action == "accept":
            if not sys.stdin.isatty():
                raise SystemExit("同意只能由操作员在交互终端中记录。")
            if input("已阅读并接受以上风险，请输入版本号 %s 确认: " % RISK_VERSION).strip() != RISK_VERSION:
                raise SystemExit("未确认，配置未改变。")
            host[CONSENT_KEY] = RISK_VERSION
        else:
            host.pop(CONSENT_KEY, None)
        store.put("settings", {"id": "managed", "config": config})
        print("已记录。重启服务后生效；云端请对该主机的执行器重新“探测安装”。")
        return 0
    finally:
        store.close()
        lock.file.close()


if __name__ == "__main__":
    raise SystemExit(main())
