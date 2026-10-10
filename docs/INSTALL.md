# 安装与首次启动

## 包管理器（推荐）

需要系统 **Python 3.11+**。wheel 不包含 Python 解释器。

```bash
pip install pangpang
python -m pangpang.cli cloud
python -m pangpang.cli connector --cloud https://YOUR.DOMAIN --workspace /your/workspace
```

文档入口是 `python -m pangpang.cli`，与 `python launch.py cloud|connector` 同一角色。pip 也可能装上 `pangpang`（Windows 上是 `pangpang.exe`）。那个启动器只是方便入口；若杀毒软件拦截（常见于 TEMP 或 Scripts），用 `-m`。可选的 npm 包名也是 `pangpang`，只是去调用同一个 Python 入口。它没有 postinstall。

平台 wheel（以及 GitHub Release 发布的带 runtime 平台 zip）里已经放好：

- `runtime/pi-worker/`：Bun **1.4.2** 与打好的 worker bundle
- `runtime/pi/pi`（Windows 为 `pi.exe`）：pi **1.0.3**

支持的平台是同时有 Bun 与 pi 官方摘要的组合：Linux x86_64 / aarch64、macOS arm64 / x86_64、Windows x64 / arm64。启动时用包内 `runtime/SHIPPED.json` 和源码里的摘要表核对字节。缺文件、摘要不符、平台不符：在开始服务之前退出。不会下载替换，也不会在 Bun worker 缺失时改用系统 Node。没有自动更新。

pi 仍是 `ADAPTER_CATALOG` 里的普通兜底行（priority 40），不是另一套安装器，也不会预置用户 Bot。本机执行器第一次登记仍要你确认（交互终端输入 `y`，或 `--allow-local-exec`）。

从源码目录执行 `pip install .` 不会把上述二进制打进包里（它们不在 git 里）。这种安装缺少 `SHIPPED.json` 时会在启动前失败，而不是在安装阶段悄悄下载。请使用组装好的平台 wheel。

## 源码检出与平台中立 zip

- Python 3.11+（含 `venv` / `ensurepip`）
- Node.js `>=22.19.0` 与 `npm` —— 仅当 cloud 没有自带的 Bun worker 时需要
- 一个模型 endpoint（OpenAI-compatible / Anthropic / Gemini 原生协议均可，见 [MODEL-PROVIDERS.md](MODEL-PROVIDERS.md)）

`git clone` 本仓库，或解压 GitHub Release 发布的平台中立 zip。该 zip 不含 Bun/pi。ZIP 的 SHA256 随 GitHub Release 资产一同发布，安装前用 `sha256sum`（Windows：`certutil -hashfile <file> SHA256`）核对。

```bash
python3 launch.py cloud
```

`launch.py` 创建虚拟环境、安装 Python 与（在没有 Bun worker 时）npm 依赖并启动协调器。它不下载安装器、不提权、不改 shell 配置，也不传 `--deployment-level`、`--host` 或 `--port`。Python / Node 版本不满足时只打印对应平台的修复命令，不执行。

解压后的目录里如果有 `runtime/SHIPPED.json`，`launch.py` 与 `python -m pangpang.cli` 一样在服务前核对 Bun 与 pi，失败即退出。

只检查、不监听（非只读查询：首次本机登记仍会执行）：

```bash
python3 launch.py cloud --check
```

## 多机部署（主管 + 连接器）

主管只跑在一台机器上；其余需要接活的电脑各跑一个连接器。以「Windows 工作站当主管 + Linux GPU 服务器 + MacBook 当执行机」为例：

**① Windows 工作站（主管）**

```bash
pip install pangpang
python -m pangpang.cli cloud
```

浏览器打开 `http://127.0.0.1:8790` 登录并配置模型；在 Web 里创建设备配对。

**② Linux GPU 服务器（执行机 A）**

```bash
python -m pangpang.cli connector \
  --cloud https://192.168.1.10:8790 \
  --workspace /data/projects \
  --data /private/pangpang-device
```

**③ MacBook（执行机 B）**

```bash
python -m pangpang.cli connector \
  --cloud https://192.168.1.10:8790 \
  --workspace ~/work \
  --data ~/.pangpang-device
```

要点：

- 执行机的 `--cloud` 都指向同一台协调器。协调器默认只绑 loopback，跨机器访问必须先按 RUNBOOK 配好可信 HTTPS 反向代理；明文服务不得暴露公网。
- 连接器只主动出站连接协调器，任何一台机器都不需要开 SSH 或入站端口。
- 每台执行机首次允许本机执行需要显式确认（交互终端输入 `y`，或 `--allow-local-exec`）。
- 配对完成后，任务由 PangPang 按设备能力与授权派发；撤销设备见下文「行为边界」。

## 首次启动行为

1. 自检打印每项能力的状态与操作说明；任一 `blocked`/`unknown` 退出码为 1。
2. **本机执行器注册需要显式确认**：交互终端打印风险文本（workspace 根、非沙箱、注入 CLI API key）并要求输入 `y`；非交互环境必须传 `--allow-local-exec`，否则跳过注册并告警（fail-closed）。已注册过的机器不再询问。纯控制面部署用 `--control-only`，完全不注册本机执行器。
3. 源码检出且没有 `runtime/SHIPPED.json` 时：若 PATH 无已知 CLI（且非 `--control-only`），会一次性下载固定版本 Pi v1.0.3（GitHub release 资产），archive 与 binary SHA256 双固定，校验不过则不安装该文件。带 `SHIPPED.json` 的 wheel / 平台 zip **不走这条下载**；摘要不符直接拒绝启动。两种路径都不做后台更新。
4. Web 控制台绑定 loopback（默认 `127.0.0.1:8790`），需公网访问时置于可信 HTTPS 反向代理之后（RUNBOOK）。

## 行为边界（安装即知）

- 协调器只监听 loopback；`PANGPANG_TOKEN` 至少 24 位随机字符。
- 执行器 workspace 默认 sibling `<data>-workspace`，与控制数据分离；这不是 OS 沙箱（SECURITY.md）。
- 出站网络端点逐项列于 SECURITY.md「出站网络清单」；自有代码无遥测。
- 设备配对与撤销：`POST /api/v1/devices/<id>/revoke`，revoked 设备必须重新配对（SECURITY.md「Connector 解绑」）。

## 升级与回退

升级 = 检出新的 main / 解压新 ZIP 后用同一 `--data` 目录启动；Store 迁移在启动时执行。回退 = 换回旧代码 + 旧 `--data` 备份。数据库与 Vault 的备份、保留属于 operator（SECURITY.md「数据清单」）。
