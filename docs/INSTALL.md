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
  --cloud https://YOUR.DOMAIN \
  --workspace /data/projects \
  --data /var/lib/pangpang-connector
```

**③ MacBook（执行机 B）**

```bash
python -m pangpang.cli connector \
  --cloud https://YOUR.DOMAIN \
  --workspace ~/work \
  --data ~/Library/Application\ Support/pangpang-connector
```

`YOUR.DOMAIN` 必须是浏览器和 Connector 的 Python 都信任的 HTTPS 名称。局域网 IP 上的 `https://` 不是可复现的安装步骤：没有一张被 Python 信任的证书时，Connector 会报 `connector_tls_untrusted` 并拒绝连接。明文 `http://` 只允许本机 loopback。步骤见 [HTTPS.md](HTTPS.md)。

要点：

- 执行机的 `--cloud` 都指向同一台协调器。协调器默认只绑 loopback，跨机器访问必须先按 [HTTPS.md](HTTPS.md) 配好可信 HTTPS 反向代理；明文服务不得暴露公网。
- 连接器只主动出站连接协调器，任何一台机器都不需要开 SSH 或入站端口。
- 每台执行机首次允许本机执行需要显式确认（交互终端输入 `y`，或 `--allow-local-exec`）。
- 配对完成后，任务由 PangPang 按设备能力与授权派发；撤销设备见下文「行为边界」。

## 扩展执行器目录（adapters.json，phase 1：手工双端放置）

内置执行器目录（`ADAPTER_CATALOG`）不可被配置覆盖或删除。operator 可以在**协调器与连接器的数据目录各放一份** `<data>/adapters.json`，为目录追加自定义 CLI 行——新增一个通用 stdin 执行器不再需要改代码，但**两端都要由 operator 同步放置同一文件**（broker 自动下发是后续版本）。

- 格式：JSON 对象，键为自定义 adapter 名（小写字母数字连字符），值为 `{binary, argv, concurrency, priority, use_when, secret_env?, extra_env?}`。`binary` 为 PATH 中的可执行文件名，`argv` 为参数模板（字符串数组），`concurrency` 1–64，`priority` 0–1000，`use_when` 为模型可见的派单说明。
- **SIS 边界不变**：argv 永远由 operator 定义，模型与任何 API 都没有目录写入路径。
- 自定义行**禁止**携带 `pinned`、`stdin_protocol`、`session_protocol`、`resume_protocol`、`interjection_tier`、`permission_bridge`、`models_json` 等内置协议/信任字段；未知字段一律拒启。自定义 CLI 只承诺通用 stdin 任务的可执行与回执，mid-turn inject、会话恢复、权限桥是内置 runner 的显式协议能力，加数据行不会自动获得。
- 启动时严格校验（JSON 合法性、大小/条数/argv 长度上限、env 键格式、文件属主与权限）；任何违规**拒启**并指明文件、行号与字段。
- **设备是 argv 的唯一来源**：start 下发 payload 不含 argv，connector 执行只用设备自己 installation 记录里的 argv；云端与设备 catalog 不一致不会"悄悄执行另一条命令"，也只影响云端侧调度元数据（priority/use_when/concurrency）。设备侧 installation 的任何变更由既有 `policy_digest` 合同拦截（`local_policy_changed_probe_and_resubmit`）。
- 设备侧修改 `adapters.json` 的 argv **不影响已注册的 installation**：argv 在 configure 时已固化进 installation 记录，要重新 configure 才生效。两端目录漂移的正确处置仍是两端重新放置同一文件并重新探测/注册。
- **删除或改名 `adapters.json` 是 fail-closed**：若存量 installation 引用自定义 adapter，服务将拒启（`invalid_adapter`，错误信息指向该文件路径）；恢复文件或移除对应 installation 即可。

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

## 数据目录

源码检出（目录旁有 `launch.py`）的默认数据目录是仓库里的 `data-v2`（连接器是 `connector-data`），与启动时的当前目录无关。

正式安装（平台 wheel 或冻结包）的默认目录与当前目录无关：

| 系统 | 协调器 | 连接器 |
|---|---|---|
| Linux | `$XDG_DATA_HOME/pangpang`，未设置时为 `~/.local/share/pangpang` | 同级的 `pangpang-connector` |
| macOS | `~/Library/Application Support/pangpang` | 同级的 `pangpang-connector` |
| Windows | `%LOCALAPPDATA%\pangpang` | 同级的 `pangpang-connector` |

显式 `--data` 始终优先。如果当前目录里已经有带数据库、Vault 或进程锁的旧相对 `data-v2` / `connector-data`，正式安装的启动会停下来并打印两条路径：不会把旧目录搬进新位置，也不会另建一个看起来是空白新用户的目录。

从别的目录启动时，只检查当前目录下的那个相对路径。找不到、而稳定目录里也还没有数据时，程序会先向标准错误打印一行，说明即将在稳定目录创建新的空数据。已有数据若不在当前目录，先不要继续，用 `--data` 指向旧目录的绝对路径：

```bash
python -m pangpang.cli cloud --data /old/repo/data-v2
python -m pangpang.cli connector --data /old/repo/connector-data
```

`python -m pangpang.cli` 是已安装环境的入口，不创建 `.venv`，也不用 `npm ci --omit=dev` 删掉开发依赖。`python launch.py` 仍是源码 / Release zip 的引导入口，会准备仓库内 `.venv`，并在没有自带 Bun worker 时用 `npm ci --omit=dev` 安装生产用 Node 依赖。

## 升级与回退

升级前先停止协调器和连接器，再做一次停机一致备份。只复制 `bots.db` 或 `connector.db` 不是回滚点：Vault 的 `master.key` 与 `secrets.db` 必须在同一份清单里。版本号相同也不表示两份代码是同一可回滚产物，回滚按构建时的 commit / tag 与发布资产的 SHA256 核对。

```bash
python -m pangpang.cli backup --data /absolute/data --dest /absolute/backup --confirm
python -m pangpang.cli restore --backup /absolute/backup --data /absolute/restore-drill --confirm
```

非交互环境不带 `--confirm` 会直接退出，不写文件。进程锁被占用、磁盘空间不足、缺少 Vault 时也会退出，不留下一份声称成功的备份。清单里若仍有未终结任务、unknown、SSH 主机、尚未 acknowledged 的连接器 `local_run`，或 semantic gate 不是 `off`，备份可以留下，但退出码为 3，升级不得继续。本命令不迁移这些记录。`adapters.json` 若存在会一并写入清单，其中的执行器环境与 Vault 同样敏感。

恢复先把校验过的文件写到目标旁边的暂存目录，再次核对后才改名进入目标。中途失败会留下原来的数据目录，不留下半份 Vault。演练可以指向一份空目录或已停止的同一数据目录，并核对访问密钥与设备身份仍在。旧二进制会拒绝已升到 v2 的库；回滚是旧程序加上这份升级前的备份，不是只看 `--version`。

服务示例在 `deploy/`。`pangpang-cloud.service` 与 `pangpang-connector.service` 是系统单元，`User=` 和 `Group=` 都是 `pangpang`。启用前先创建该用户和组；不要删掉这两行，否则进程会以 root 运行。连接器若要读写某人的工作区，把这两行改成那个账号，不要改成 root。按 pip 安装后的 `python -m pangpang.cli` 启动，数据目录是绝对路径。示例里没有密钥。非交互首次启动另需环境变量 `PANGPANG_TOKEN`（至少 24 位）；本机执行器的非交互确认用已有的 `--allow-local-exec`。
