# Security Policy

PangPang 能调用真实模型、真实 CLI、真实文件和真实电脑，因此安全边界优先于“Agent 看起来更自主”。

## 当前安全模型

### 对外陈述里已经核对过的默认行为

这些是代码现在的行为，文档不得写成更强的保证：

- 新的对话 Work 在创建时写入 `grant_write=true`。这是 workspace 写授权，不是操作系统权限。只读必须另行设置。
- L0（个人同账号）没有把执行器隔离开。同一 OS 账号可以绕过应用层路径检查直接读 Vault。以 L0 启动是接受这项残余风险，不是隔离验收。
- `web_search` 默认出站。无 key 时查询词发往 Bing / DuckDuckGo；有 `TAVILY_API_KEY` 时优先 Tavily。`PANGPANG_SEARCH_PROVIDER=off` 才关闭。见下方出站清单。
- unknown 不是“人在聊天里说已经停了”就能收口的状态。模型或操作者的口头陈述不是 OS 内核证据。unknown 只在确认的终态回执或已记录的 operator resolution 之后收口，且不因此自动重跑可能有副作用的任务。
- 没有自动业务验收引擎。文件存在、摘要相符、模型说完成，都不是业务正确。

### 模型不是 authority

LLM、Jev、Executor stdout、Web search 内容都只能提出/提供信息，不能自行建立：

- 用户授权；
- Bot/Work ownership；
- secret 读取权限；
- CLI API key 的目标 endpoint；
- workspace 扩权；
- execution terminal truth；
- unknown 资源释放；
- receipt 身份；
- 路由硬约束的豁免。

上述边界由服务端 deterministic validation、operator action、设备 policy、OS/process facts 与 fenced receipt 决定。

Jev 的 typed API 把 `state`（含不可信用户/执行器文本）与固定 `questions` 字段分开，但**这不是“模型免疫 prompt injection”的声明**。安全性质来自更低一层：Jev 的 `EXECUTE` / task-result `ACCEPT` 最多允许继续尝试一个已经通过 deterministic eligibility 的路径，绝不能建立 authority；commit 时 Python 仍重验。task-result gate 在 provider timeout / 429 / invalid response / missing key 时一律回到 planner，`fail_open:true` 属于 1.0 禁止配置。

### Secret

不要把 API key、device token、private key 或 Web access key 放进：

- chat；
- issue / PR；
-普通日志；
- argv；
- README / config sample；
-测试 fixture；
-发布 ZIP。

云端模型 secret 存在 Vault；设备 secret 走设备侧存储/专用输入路径。源码 release builder 会拒绝已知 runtime DB / Vault / master key / secret/state 文件。

当前 Vault 的 Fernet `master.key` 与 `secrets.db` 位于同一个**受 OS 账号 ACL/0700 保护的私有 data tree**。这能降低“只拿到数据库文件”的泄露风险，但**不声明**能抵抗整个 data directory / backup 被复制，也不抵抗 L0 同账号恶意进程；那两类威胁必须靠受限 OS identity / sandbox（L1/L2）或未来可选的 OS-backed key wrapping。不要把“有加密”误写成“完整 data tree 被盗仍安全”。

Data root 的最终路径组件不能是 POSIX symlink 或 Windows reparse-point/junction；检查发生在 canonical `resolve()` 之前。Linux/macOS 还验证当前 effective UID ownership 并强制目录 `0700`、`master.key` `0600`；Windows 通过 System32 绝对路径工具和 numeric SID 收紧 ACL。父路径中的正常 OS symlink 允许存在。

如果 secret 已经出现在共享位置，**删除文本不等于撤销泄露**。应立即 rotate / revoke；设备身份泄露时重新配对。

### Artifact harvesting

Executor-produced files are untrusted input. Auto-harvest must stay inside allowed roots and register only bounded regular files.

Current invariants include:

- no symlink/junction/FIFO/device/socket artifacts;
- no directory-symlink traversal;
- final-file no-follow/non-blocking open where supported + descriptor `fstat`;
- per-file / total / file-count / scan-count / wall-clock limits;
- pre-run cwd snapshot failure disables fallback rather than treating old files as new;
- artifact registration does not waive digest/path revalidation on later read/download.

Do not “fix” a missing artifact by widening allowed roots or enabling symlink traversal.

### Artifact HTTP serving

Artifact content is untrusted even after registration. Download/copy/read-evidence paths verify the actual decoded bytes (base64 validity, size bound, SHA-256, recorded size) rather than trusting a device-reported digest.

Only a finite non-active MIME allowlist may render inline on the authenticated admin origin: raster images, common audio/video formats, and PDF. SVG/HTML/JS and every unlisted type are forced to attachment with `application/octet-stream` and a sandbox CSP.

### Delivery evidence immutability

Where available, newly harvested artifacts are copied into a digest-addressed execution snapshot. Later read/download/copy should prefer the snapshot so mutable workspace paths cannot silently rewrite an earlier delivery.

`read_evidence` verifies actual bytes and digest outside Store transactions. “Bytes verified” is not equivalent to semantic/business validation of PDF, Office, image, or other binary content.

### Model / Executor content rendering

Current React chat Markdown rendering builds React elements/text nodes from a bounded tokenizer and applies URL protocol allowlisting (`safeUrl`); it does not use `dangerouslySetInnerHTML`. Raw model, Web-search, or Executor content must never be inserted into the page as trusted HTML.

Rendering safety is still not an authorization boundary: a visually convincing link/table/message cannot grant tool permission or execution authority.

### Routine standing authority

A scheduled Routine is standing future write authority. Model-created or model-edited routines therefore remain pending/inactive until an authenticated operator approves the exact current digest of instruction, schedule, timezone, and owner Bot policy.

Any model edit or owner-policy drift invalidates the old approval. Resume, run-now, scheduler materialization, and startup migration all fail closed on missing/mismatched approval.

### Authorization propagation

Conversation/Work identity is not authority. Each user RootRequest carries a server-owned authorization snapshot. Linking, coalescing, delegation and task submission may only intersect restrictions; they cannot restore write permission or relax required host/installation constraints.

A read-only request linked to an older writable Work must remain read-only.

### Control data vs execution workspace

Executor workspace must not equal, contain or be contained by the control-data/runner root after realpath resolution. The local default is a sibling `<data>-workspace`.

This is **not an OS sandbox**. A malicious CLI running as the same OS account may still bypass application path checks and access files directly. High-security deployments require a restricted execution identity or verified sandbox; the current repository does not claim that boundary.

### Executor supervision scope (L0 cooperative tiers)

`process_tree_confirmed=true` means only that this attempt may release its claims and commit its terminal state. Its source is named by the other fields — a runner cleanup proof (`supervision` + `termination_evidence`: the chosen tier's own scope is empty), an explicit `not_launched`, or an `operator_resolution`. It is **not** proof that every descendant has ended:

- `windows-job-object` covers every process in the Job;
- `linux-cgroup-cooperative` (`cgroup-v2-populated-zero`) covers descendants that stayed in the scope's cgroup, including accidental setsid/double-fork children;
- `posix-pgroup-cooperative` (`pgroup-empty`) covers only processes still in the CLI's process group. A setsid/double-fork descendant escapes it and may keep writing the workspace or using shared resources.

Neither cooperative tier resists an adversarial same-account payload, and none is access isolation. Both are admitted only at deployment level L0. Omitting `--deployment-level` on a personal POSIX start (macOS, Linux) that is not `--control-only` and was not given `--origin` infers that L0 declaration; `--origin` is evidence the coordinator is reached from another machine, so L0 is not inferred. Linux uses the cgroup cooperative tier when a systemd --user scope is available, otherwise the process group (same as macOS). On Windows there is no default. An invalid value is a configuration error. An explicit L0 on this coordinator admits the process-group tier without a recorded consent field. A connector Execution has no deployment level, so that tier on a connector device still needs the current consent recorded on the device. The supervision admitted at routing is frozen on the task (`supervision_floor`); the runner refuses — before acquiring resources or spawning the CLI — any tier that cannot confirm its scope or is weaker than that floor. Leaving the deployment level undeclared blocks new local launches of that tier even with a stale cooperative probe. A `transport: ssh` host row still loads and is listed by inventory. A new launch on that host is refused with `ssh_executor_retired` and is not sent to local or Connector. An in-flight SSH attempt becomes `unknown` and needs operator resolution. `macos-pgroup-postverify` is never admitted. A receipt for a run that executed the CLI commits only with a known label, that label's evidence and at least the frozen floor; unknown or contradictory labels never commit. Cloud and connector share this one predicate, and a launched attempt is judged by its frozen facts, not by a later deployment-level change. Supervision fields are operator-only and never reach the model. Starting at L0, declared or inferred, accepts that residual risk. L1/L2 deployments, and any deployment that serves people other than its operator, must not rely on these tiers.

### Exactly-once launch fencing

`start.claim` is written and fsynced before spawning a supervisor; `supervisor.claim` prevents duplicate run entry. Lost ACKs or retries must never authorize a second launch. If a process dies after claim but before actual execution, fail closed into reconciliation/unknown rather than taking over the claim automatically.

### Execution fail-closed

`unknown` execution 不自动重试、不自动释放冲突 workspace，**也不 settle 其 Dependency**。Observation failure 不能伪造为 unknown；unknown 只有在有 confirmed terminal receipt / operator evidence 后才收口。

Task terminal state 必须接受当前 attempt fencing，包括 `execution_id + spec_digest + attempt_uuid` 与必要 process/receipt identity。

### Configuration publication

Mutable installation/device configuration is prepared outside Store transactions, revalidated under the commit lock, persisted durably, and only then published to in-memory routing/execution state via outermost `after_commit` callbacks. Rollback must never leave a ghost in-memory config.

Every coordinator write of `settings/managed` goes through `Service.commit_managed_config` (connector side: `LocalSetup.save`). The first write in a transaction is a compare-and-set against the revision this process published, so a row changed behind a running process fails with `state_conflict` instead of being overwritten; `GET /api/v1/diagnostics` reports `config_stale`. Vault side effects follow the commit: a new secret is removed if the config does not commit, and an old secret is deleted only after the durable config stops referencing it. Bot records are not cached: instruction edits apply without a restart.

Concurrent local installation edits fail with conflict rather than silently overwriting each other. Device reports are checked against server-authored command parameters and host ownership before publication.

### Login and pairing rate limits

The coordinator binds loopback only. `/api/v1/login` (5 failures / 60 s) and `/api/v1/pair` (10 requests / 60 s, successful ones included) are keyed by the TCP peer address. Behind a reverse proxy or the self-use LAN TLS bridge that peer is always the proxy, so each limit is one **global** bucket: anyone who can reach the entry can exhaust it and block new logins (existing 12 h sessions are unaffected) for up to 60 s. This is deliberate fail-closed behaviour; `X-Forwarded-For` is not trusted and PROXY protocol is not supported. The supported LAN entry is a trusted HTTPS reverse proxy with its own connection and login-rate limits (RUNBOOK); `scripts/tls-bridge.py` is a self-use helper excluded from the public snapshot.

### Connector trust

Connector receipt 说明“受信设备如此报告”，不是独立证明设备未被攻陷。设备账号、OS ACL、CLI 自身权限仍属于部署安全边界。

## 出站网络清单（egress inventory）

除下表外，自有代码不向任何端点发送数据；**没有遥测、使用统计或自动更新检查**（此声明限定本仓库自有代码；各 CLI 与模型 provider 自身的网络行为由其条款约束）。

| 组件 | 端点 | 默认状态 | 触发条件 | 发送内容 |
|---|---|---|---|---|
| 模型推理 | 31 个 provider 的默认 `baseUrl` 见 `pangpang/model_catalog.json`，operator 可覆盖 | 未配置模型前完全关闭 | 每次模型调用 | 会话 state、prompt、工具结果（按 provider 协议） |
| Semantic gate / continuation routing | `https://api.typesafe.ai/v1/systemone`，可用 `PANGPANG_SEMANTIC_GATE_URL` 覆盖 | `mode=off`；continuation routing 默认 ON，无 `TYPESAFE_API_KEY` 时由 operator 自己配置的模型在本地路由（不新增出站），再失败则确定性降级为新 Work | gate 判定 / sticky-tab 续接 | 结构化 `state` + 固定 `questions`，不含 secret |
| Web search | 有 `TAVILY_API_KEY` 时优先 `https://api.tavily.com/search`；无 key 或失败时走 `https://cn.bing.com/search`、`https://html.duckduckgo.com/html/`（互为 failback） | **默认开启**（无 key 链仅发送查询词）；`PANGPANG_SEARCH_PROVIDER=off` 可整体关闭出站 | bot 显式调用搜索工具 | 查询词（URL 编码 query string，不含会话上下文） |
| pi 二进制获取 | `https://github.com/earendil-works/pi/releases/download/v1.0.3/...` | **平台 wheel 与带 `runtime/SHIPPED.json` 的 Release 不下载**（二进制已在包内，启动只核对 SHA256；不符则拒绝启动，不替换）。源码检出且没有该标记时，首次登记本机执行器且 PATH 无已知 CLI、或 connector discover 时，仍可能一次性下载 | 一次性下载 | 无（纯下载）；archive 与 binary SHA256 双固定。没有自动更新 |
| Bun 运行时获取 | `https://github.com/oven-sh/bun/releases/download/bun-v1.4.2/...` | **用户安装与启动不下载**。仅维护者在构建机准备带 runtime 的发行包时下载 | 构建期 | zip SHA256 对照官方 SHASUMS256.txt，解出的 `bun`/`bun.exe` 再对照源码钉。pip 与 npm 都没有 postinstall 下载 |
| Connector 设备 | operator 配置的 coordinator 公网地址 | 配对后设备**仅出站**连接 coordinator | 设备在线/回执 | 设备身份 + 执行回执，TLS |

## 给审计者的说明（auditor notes）

以下代码形态容易被误读为后门，逐条说明：

- **SSH host rows**：`transport: ssh` still loads so an old config can be listed. There is no `BOOTSTRAP` constant, no `remote()`, and no PowerShell `-EncodedCommand` channel. A new SSH launch is refused with `ssh_executor_retired` and is not rerouted to local or Connector. An in-flight SSH attempt becomes `unknown` and needs operator resolution.
- **codex 默认 argv**（catalog 为 `["exec", "--sandbox", "workspace-write", "-"]`，`runner.py` 把它改写成 `app-server`，并把 `--sandbox` 的值放到 `thread/start.sandbox`）：默认档位是 `workspace-write`。该档位可以在任务 workspace 内写文件；workspace 之外的写入仍被这个沙箱拒绝（plan 不附加 writable root，并丢弃 `--add-dir`）。没有审批 flag 时 `approvalPolicy` 是 `never`。默认 argv **不会**带上 `sandbox=danger-full-access`。只有存储的 argv 含 `--dangerously-bypass-approvals-and-sandbox` 且没有另行指定 `--sandbox` 时才会发送 `danger-full-access`。operator 要启用完整绕过，就把该 flag 写进该 installation 或 profile 的存储 argv；它不是 catalog 默认，`extra_argv` 也会拒绝这个 token。没有 `--sandbox` 的已存储 argv 在 plan 里仍不发送 sandbox，线程保持 read-only。升级迁移按当时已存储的 argv 授予授权摘要：已有 installation 保留自己的 argv，摘要不变，因此不会因为这次 catalog 默认变化而重新要求批准。重新 configure 会复制当前 catalog argv，授权摘要随之改变。安全边界是 runner 与 deployment level，不是 CLI 弹窗；见上文 “Executor supervision scope”。
- **约 380KB 的 `pangpang/web/app.js`**：由 `web-src/` 构建的压缩 React bundle（构建指引见 README）；审计请读 `web-src/` 源码。
- **本机 CLI adapter 名**：安装模板的内部别名（operator 可编辑），不对应任何隐藏端点。
- **常驻 runner 模板**：每次执行在自己的 `exec-<32hex>` 目录写入 `runner.py`，随该目录生命周期存在；不存在跨执行共享的常驻进程模板。

## 数据清单（data inventory）

- **Coordinator data 目录**（`--data`，POSIX `0700` / Windows ACL 收紧）：`bots.db`（Store：受管配置、Bot/Work/Task、receipt、设备与命令状态、probe、artifact 元数据）、`coordinator.lock`；Vault：`master.key`（`0600`）+ `secrets.db`（Fernet 加密，与 DB 同 tree——见 “Secret” 节的边界声明）。
- **Connector data 目录**：`connector.db` + 设备侧 Vault（device private key 等）。
- **执行目录** `exec-<32hex>`：`runner.py`、`output.log`（默认 4 MiB 上限，超限置 `receipt.truncated`；注入执行器的 secret 经 Redactor 脱敏后才落盘；UI 只读最后 16 KiB）、`receipt.json`（≤1 MiB）。
- **Artifacts**：harvest 文件 + digest 寻址的执行快照（delivery evidence 不可变性一节）。
- **Workspace**：默认 sibling `<data>-workspace`，与控制数据分离。
- **应用日志**：仅 stdout（无日志文件），由部署方自行收集；保留与备份属于 operator。
- **清除**：停止服务后删除对应 data 目录即清除全部状态；单个 secret 用 revoke/rotate，设备用解绑（下节）。无自动过期删除。

## Connector 解绑与设备撤销

1. Coordinator 侧：`POST /api/v1/devices/<host_id>/revoke`（Web UI 设备页等价）。此后该设备重连与命令下发一律 `device_revoked` 403；**revoked ≠ offline**，重启 connector 不能恢复，必须重新配对。
2. 设备侧：删除 connector data 目录（`connector.db`、设备 Vault、device private key）。
3. 怀疑设备或 token 泄露：revoke + rotate 模型 key + 重新配对；删除文本不等于撤销泄露（见 “Secret” 节）。

## Reporting a vulnerability

请使用仓库的 GitHub **Private Vulnerability Reporting**（Security 页 → “Report a vulnerability”）提交私有安全报告；该入口未启用时，通过仓库 owner 的私有渠道报告。不要把：

- 可利用细节；
- 真实凭据；
- device token/private key；
- 用户数据；
- 完整 runtime DB；

贴到普通 issue。

## 报告中应包含

可以安全提供：

- 受影响 commit/tag；
- OS / Python / Node；
- 受影响组件；
- 最小复现步骤；
- 预期 vs 实际；
- 是否涉及跨 Work/Bot/Host 越权；
- 是否涉及重复副作用 / stale receipt / unknown 释放；
- 已脱敏日志。

不要附带真实 secret。

## Scope priorities

优先级最高的漏洞包括：

- 未授权执行真实 CLI/写文件；
- 跨 Bot/Work 读取私有内容；
- secret 泄露；
-模型或外部文本扩大 authority；
- receipt/attempt 混淆导致错误 terminal transition；
- unknown/quarantine 被绕过；
- workspace canonicalization 绕过；
- device identity 冒用；
- release artifact 带入 runtime secret/state。

## Supported versions

当前仍处于快速开发期。安全修复以 current main 与最新正式 Release 为准；旧 release 不保证回移所有架构改动。
