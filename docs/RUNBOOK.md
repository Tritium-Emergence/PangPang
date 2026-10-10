# PangPang — 部署、运行与恢复 Runbook

日期：2026-09-24  
适用：当前 main

## 1. 部署前先决定模型形态

### A. Cloud/API 模型 endpoint

如果 endpoint 本身支持并发，默认 Scheduler 可直接使用：

```json
{
  "scheduler": {
    "model_max_concurrency": 4,
    "reserved_user_slots": 1,
    "main_bot_max_active_turns": 2,
    "main_bot_reserved_user_slots": 1,
    "service_bot_max_active_turns": 1
  }
}
```

### B. 单流本地 Qwen

如果实际是单 GPU/单服务、长上下文 single-stream 最快，推荐：

```json
{
  "scheduler": {
    "model_max_concurrency": 1,
    "reserved_user_slots": 0,
    "main_bot_max_active_turns": 1,
    "main_bot_reserved_user_slots": 0,
    "service_bot_max_active_turns": 1
  }
}
```

不要为了“逻辑并发”制造多个大上下文 prefill / KV 争用。

## 2. Coordinator 首次启动

要求：

- Python 3.11+
- Node.js >=22.19
- 固定私有 data directory

```bash
python launch.py cloud --data /private/pangpang-cloud

# 默认本地 Executor workspace 会使用同级：
# /private/pangpang-cloud-workspace
```

只检查、不监听。`launch.py` 用 `parse_known_args` 把 `--check` 转给 `python -m pangpang`。它不是只读查询：首次本机登记仍会执行。任一能力为 `blocked` 或 `unknown` 时退出码为 1；`unconfigured` 为 0。启动时同样打印这份自检，并继续服务。

```bash
python3 launch.py cloud --data /private/pangpang-cloud --check
```

首次交互启动会显示 Web access key。保存到密码管理器。

打开：

```text
http://127.0.0.1:8790
```

配置：

- OpenAI-compatible API base；
- model id；
- API key；
- 必要 generation settings。

系统只有在真实 tool call → tool result → continuation probe 成功后才保存模型设置。

### 非交互服务启动

首次非交互部署必须安全提供：

```text
PANGPANG_TOKEN
```

不要把 token 写进 Git、聊天、issue 或普通日志。

### 环境变量旧名迁移（XIAN_PANGPANG_* → PANGPANG_*）

GA 重命名去掉了 `XIAN_` 前缀。仅以下**有升级现场证据**的变量在本发布周期内兼容读取旧名（旧名仅在新名完全缺失时生效，命中时打一条只含变量名、不含值的告警）：

| 现名 | 兼容的旧名 |
| --- | --- |
| `PANGPANG_TOKEN` | `XIAN_PANGPANG_TOKEN` |
| `PANGPANG_DEVICE_TOKEN` | `XIAN_PANGPANG_DEVICE_TOKEN` |
| `PANGPANG_PI_WORKER` | `XIAN_PANGPANG_PI_WORKER` |
| `PANGPANG_LOG_LEVEL` | `XIAN_PANGPANG_LOG_LEVEL` |
| `PANGPANG_SEMANTIC_GATE_URL` | `XIAN_PANGPANG_SEMANTIC_GATE_URL` |

其余变量（`PANGPANG_PI_NODE_BUNDLE`、`PANGPANG_PI_BUN_BUNDLE`、`PANGPANG_SEARCH_PROVIDER`）**没有可核实的旧名契约，不做任何回退**：设置 `XIAN_PANGPANG_PI_NODE_BUNDLE` 之类不会生效，运行时 bundle 选择器只认现名，避免陈旧值悄悄改变执行形态。

约定：**新名一旦出现（哪怕是空字符串）就以新名为准，绝不回退旧名**。显式空值表示 operator 主动清空；此时旧名即使有效也不会被采用（token 场景下走既有的 Vault/交互路径，非交互则直接报错退出），不会被静默复活。

## 3. HTTPS 反向代理

Coordinator 默认只监听 loopback。正式远程访问：

```bash
python launch.py cloud \
  --data /private/pangpang-cloud \
  --origin https://bot.example.com
```

要求：

- 同机可信 HTTPS reverse proxy；
- SSE 不缓冲；
- request body limit 足够；
- 保留正确 Host/origin；
- 不直接暴露 `127.0.0.1:8790` 的明文替代端口到公网。

Caddy 示例：

```text
bot.example.com {
    reverse_proxy 127.0.0.1:8790 {
        flush_interval -1
    }
}
```

## 4. Optional API keys

### Web search

`web_search` 默认即可用，无需任何 key：无 key 时走 `cn.bing.com` / `html.duckduckgo.com` 互备链；`PANGPANG_SEARCH_PROVIDER=off` 可整体关闭出站，取值 `tavily|bing|duckduckgo` 可强制单一 provider。

```text
TAVILY_API_KEY
```

配置后 `web_search` 优先使用 Tavily，失败自动回落到无 key 链。

### Jev semantic layer

```text
TYPESAFE_API_KEY
```

当前有三条彼此独立的用途：

1. **pre-commit semantic gate**：由 `semantic_gate.mode = off|shadow|enforce` 控制；Jev 只能收紧“语义是否足够明确”，`EXECUTE` 之后仍由 Python 做 authority / state / policy commit-time revalidation；
2. **Work continuity routing**：由 `semantic_gate.continuation_routing = true|false` 独立控制，默认 true；
3. **read/query Task result fast-accept**：由 `task_result_gate.mode = off|shadow|enforce` 控制。当前版本固定 fail-closed：provider unavailable / timeout / 429 / invalid response / missing key 都回到 planner，旧的 `fail_open:true` 配置启动即拒绝。

默认配置可以是：

```json
{
  "semantic_gate": {
    "mode": "off",
    "continuation_routing": true,
    "continuation_switch_threshold": 0.75,
    "correction_threshold": 0.85,
    "continuation_breaker_failures": 3,
    "continuation_breaker_cooldown_s": 30
  }
}
```

没有 `TYPESAFE_API_KEY` 或 `continuation_routing=false` 时，普通 Web `work_hint_id` **不会**建立 continuation：系统确定性 NEW（`route_source=unconfigured_new`）。这不算 outage，也不会触发 fallback approval。若确实需要确定性续接，应由受信内部/兼容入口使用显式 `work_id`。

只有 key 已配置、但 Jev 请求失败/timeout/circuit breaker open 时，才产生 `fallback` weak route。

Jev 永远不是 ownership / authorization / liveness / terminal execution truth 的 authority。

## 5. Model provider 注意：本地推理与云端 reasoning provider 不共享"关 reasoning"参数

current `normalize_profile()` 不按 host 或模型名注入任何 generation 参数；provider 特性由 Pi 目录映射与操作员显式选择决定。以下是**推荐显式配置**（来自 A/B 实测），不是自动默认。

### 本地 / vLLM Qwen

推荐显式配置：

```json
{
  "chat_template_kwargs": {
    "enable_thinking": true
  }
}
```

这是 chat-template 行为。生产 A/B 实测：关思考时模型即使看得见在跑的任务，也会不撤回就答“没有在跑”，因此建议开思考。开思考时 `output_tokens` 要给足（生产用 32768），否则 reasoning 可能烧满输出预算导致 content 为空的假截断。显式写了 `false` 的档案保持不变。

### 云端 reasoning endpoint

带独立 reasoning 预算的云端 provider 推荐显式配置：

```json
{
  "reasoning_effort": "medium"
}
```

实测：

- `none` 会破坏 multi-step tool discipline；
- `low` 仍偶发漂移；
- `medium` 是 current stable default。

这类 endpoint 会拒绝本地 vLLM 的 `chat_template_kwargs`，所以不要手工把两套参数混用。

这类 endpoint 应在设置里选择 `max_completion_tokens` 字段（旧的 pre-provider 档案在重配前保留历史字段行为）。不要改回 `max_tokens`：此类 provider 的 reasoning tokens 计入 completion budget，错误字段曾实测把 multi-step turn 在约 3900 combined tokens 处截断。

Coordinator 进程必须真的拿到对应 provider 的 API key。正式安装入口见 docs/INSTALL.md（`pip install pangpang` 或源码运行 `python launch.py cloud`）。


### Output token budget caveat

`output_tokens` 默认且最小为 **32768**，用于给 reasoning + 最终回答留足 completion budget。加载配置时，低于 32768 或缺字段的档案一律按 32768 运行（如旧示例配置的 1200），下次保存配置时写回；Pi 目录里自身输出上限更低的模型按该模型上限。需要更大时在「设置全局对话模型」的「输出上限」里调高。

同时：

- `context_window_tokens=40000` 或字节级 context 上限不证明“input + reasoning + output”适配端点真实窗口；
- 云端 reasoning endpoint 使用 `max_completion_tokens`；
- 提高输出上限本身不等于资源预算已验收，仍需按目标模型窗口跑长输出/多步工具现场测试。

## 6. 配对真实电脑

设备要求：

- Python 3.11+
- 出站 HTTPS
- 本机已有 CLI / 文件
- 一个明确授权的 workspace

不要求：

- Node/Pi
- 入站 SSH
- 公网 IP
- 端口映射

Web 中创建配对码后：

```bash
python launch.py connector \
  --cloud https://bot.example.com \
  --workspace /your/workspace \
  --data /private/pangpang-device
```

Windows：

```powershell
python launch.py connector --cloud https://bot.example.com --workspace C:\Work --data C:\PangPangData
```

后续必须复用同一 data directory 才能恢复同一个 device identity。


### Control-data / workspace separation

当前版本强制 control data 与 Executor workspace 分离。

例如：

```text
data      = C:\PangPangData
workspace = C:\Work
```

以下都拒绝：

- workspace == data；
- workspace 是 data 的父目录；
- workspace 是 data 的子目录；
- junction/symlink realpath 后发生上述重叠。

本地 cloud 未显式传 `--workspace` 时，默认使用同级 `<data>-workspace`。

**升级旧安装时**：如果曾把 data directory 自己当 workspace，升级后会看到 `workspace_overlaps_control_plane`。先核查 active/unknown task，再把 workspace 迁到独立目录；不要把 DB/Vault 整个复制进新 workspace。

这仍不是 OS sandbox。同一账号运行的任意 CLI 理论上仍可绕过应用层 path policy；正式高安全部署需要受限 OS identity / sandbox。
## 7. 接入 Claude Code / Codex 等本机 CLI 执行器

推荐走 Web / 对话式 onboarding，不日常手改 JSON。

原则：

- 已登录 CLI 优先复用 CLI 自己的 OAuth/login；
- API-key 模式只通过专用 secret input path；
- 模型不能读取明文 key；
- 模型不能决定 key 发往哪个 endpoint；
- endpoint 变更需要 operator 明确确认；
- workspace 不能超出设备侧授权根；
- unknown CLI/version 不自动运行模型生成的安装脚本。

一次成功的 version probe 只证明 binary 可见，不等于登录、权限、写文件都正确。必须用小任务做真实 execution acceptance。


### connector 主机的 operator 注册入口

对话式 onboarding 之外，operator 有直接注册入口：Web 设备页会列出 connector 主机上**已探测未注册**的 CLI，点"注册执行器"即创建 setup_request；等价 HTTP 入口为 `POST /api/v1/setup/request`（需 session + Origin + `Idempotency-Key`，与其余变更端点同一幂等保护：同 key 同 body 重放返回首次结果，同 key 不同 body 409），body `{host_id, action:"configure", adapter, cwd?, auth?}`（凭据录入用 `action:"credential", installation_id`）。字段集合按 action 严格白名单，出现任何多余字段 400。请求进入与对话式路径相同的批准队列：operator 在待批准面板确认（endpoint 在批准时提供），设备执行 configure 并回报落库。

`adapter` 必须是**设备最近一次探测实际上报过**的 CLI（服务端对照设备回报的 inventory 校验，客户端 body 无法自报"已安装"；未探测过的主机先跑一次 discover）。服务端再验 `ADAPTER_CATALOG`，设备在 configure 时再 `shutil.which` 复核——三层各自 fail-closed。

SIS 边界不变：argv 仍由 `ADAPTER_CATALOG` 生成，该入口不接收模型提案，也不开放任何 argv 字段。`auth:"api_key"` 的注册落库后保持停用，需再发起 credential 请求在专用窗口录入密钥。

### Adapter catalog / use_when

当前执行器 command/default 定义集中在 operator-owned `ADAPTER_CATALOG`。不要通过聊天让模型登记任意 argv。

local installation 的配置 UI 现在可以覆盖：

- `priority`：1–999 严格整数，小者优先；留空恢复 catalog 默认；
- `use_when`：最多 500 字符；留空恢复 catalog 默认。

catalog 内各执行器（claude-code / codex 等）的能力描述只是默认提示；operator 自定义 `use_when` 才是该 installation 的 effective hint。current prompt 不再套固定品牌分工。

如果模型不确定安装实例，允许省略 `installation_id`；控制面按 effective priority + host/workspace/policy 选择。**use_when 只解决能力匹配，不做 current-health 预测**；真实可执行性仍由 start 阶段裁定。

connector installation 仍是 device-owned，不能在 cloud 端用这个通用配置 UI 修改。

### extra_argv（operator 追加参数）

installation 可配置 `extra_argv`：字符串数组，启动时追加在 catalog argv 之后。SIS 边界不变——argv 本体仍由 catalog 固定，模型不能提案该字段（`operator_owned_setup_field`），只能由 operator 在配置 UI 或 approve 时注入。

约束：

- 最多 16 项、单项 ≤1024 字符、总计 ≤4096 字符，空数组表示清除；
- 权限/沙箱解除类 token（`yolo`、`dangerously`、`bypass`、`full-auto`、`no-sandbox`、`approve-for-me`、`skip-permissions`、`danger-full-access`）一律 400——catalog 行声明的窄授权不能经此字段放宽；`--sandbox workspace-write` 这类合法档位仍可用，`--sandbox danger-full-access` 被值本身拦下；
- codex 走 app-server 协议：`--skip-git-repo-check`、`--json`、`--approve-for-me` 等 exec-only flag 在协议改写时会被**静默丢弃**，因此 extra_argv 直接 400 拒绝这些 flag，而不是存一个进程永远收不到的参数；`--listen` 由 app-server 桥独占（stdio），同样 400，不会存到启动时才炸。trust guard 的正确解法是给 workspace 建立可信目录（`git init` 或 codex config 的 `trust_level`），不是传 flag；
- cloud 端显示限制：connector installation 在 cloud 侧 argv 是占位 `["device-owned-command"]`，`extra_argv` 真实值只存在 device 端并在 device 侧启动时生效。

### Executor 产物

推荐 Executor 把交付文件写到：

```text
$XPP_OUTPUT_DIR
```

系统会自动收割该目录，不要求模型预先知道文件名。

如果 CLI 无视该变量、直接在 cwd 写 `png/pdf/docx/xlsx/pptx/csv/md/html/video/audio...`，系统还有 shallow “本次新建文件” fallback。

当前限制：

- 单文件 8 MiB；
- 最多 64 files；
- 总 256 MiB；
- 20k scan / 30s harvest_dir；
- `.xpp-out/<execution_id>/` 暂不自动清理。

若用户需要大视频/音频，当前 8 MiB read/download cap 是产品限制，不应承诺 inline 播放。

### Installation 配置编辑冲突

local installation 配置页支持 `cwd/max_concurrency/enabled/timeout_s/priority/use_when/extra_argv`。保存时常见 409：

- `installation_config_changed`：prepare 后同一 installation/host 已被其他操作修改；刷新当前值并人工合并，不要盲目覆盖；
- `installation_busy`：修改 cwd 时仍有 active/quarantined task；先让执行事实收口。

路径错误还可能返回 `cwd_outside_allowed_roots` / `workspace_overlaps_control_plane`。

配置保存采用 prepare→commit→after_commit publish，因此 outer HTTP transaction rollback 时内存配置也不应变化。若 DB 与 runtime config 不一致，视为 invariant violation。

前端有 per-installation dirty/saving draft；background refresh 不应覆盖未提交编辑。

### Artifact 下载/预览安全

管理员网页只 inline 预览有限 allowlist：PNG/JPEG/GIF/WebP/AVIF/BMP、常见 audio/video、PDF。SVG/HTML/JS/其他未知类型必须下载，不在 admin origin 直接渲染。

若下载/copy/read_evidence 报 `source_changed`，不要绕过 digest check：它表示实际 decoded bytes 的 SHA-256/size 与登记版本不一致，应检查 snapshot/source device 是否被修改或篡改。

## 8. qwen / 本地资源边界

不要混淆两个概念：

### Model plane

主管/子 Bot 调用的 LLM endpoint。是否并发由 Scheduler 配置决定。

### Executor plane

Claude Code / Codex 等本机 CLI 的 Task。是否并发由：

- installation `max_concurrency`
- workspace RW overlap
- resource policy
- active/unknown quarantine

共同决定。

如果多个进程共享一个本地 Qwen 服务，所有消费者必须遵守同一真实资源协议。DB 中的逻辑计数不能冒充跨进程物理锁。

## 9. 当前执行恢复语义

### Observation failure

例如：

- connector timeout；
- local receipt 临时不可读；
- OS inspection unavailable。

处理：

- 保持 Task active；
- 记录 observation failure / backoff；
- 不自动制造 `unknown`；
- 不释放 workspace claim。

### 真正 unknown

只有在：

- 没有有效 terminal receipt；
- 本 attempt 进程树已确认不存在；
- 又无法证明已经成功/失败/取消收口；

才可进入 unknown。

unknown 是显式安全状态，不是“稍后自动重试”的普通错误。**当前 unknown 不 settle Dependency：parent join 保持 pending**，系统继续 reconcile；只有 confirmed terminal receipt 或有证据的 operator resolve 才收口。

## 10. attempt / receipt fencing

Task terminal transition 必须匹配自己的 attempt：

```text
execution_id
+ spec_digest
+ attempt_uuid
+ 当前 task revision/attempt
+ 必要的 process-tree facts
```

禁止：

- bare PID 当 identity；
- stale receipt 覆盖新 attempt；
- retry 复用旧 receipt；
- “目录里没看到进程”就自动释放资源；
- operator 只改 cloud DB 假装设备已经停了。

## 11. 模型 transient failure

当前默认 retry delays：

```text
2s, 5s, 15s, 30s, 30s
```

适用于：

- model request failure；
- model timeout；
- rate limit。

retry 是 durable requeue：等待期间释放 model worker，不用 `sleep` 占槽。

如果最终预算耗尽，Turn/Work 进入可诊断状态；不要无限隐藏重试。

## 12. Work continuity v3 故障排查

### 症状 A：本来应该继续，却开了新 Work

检查：

1. browser 是否带 `work_hint_id`；
2. `stream_id` 是否稳定在当前 tab × bot；
3. `last_seen_event_id` 是否指向用户实际看见的最后 conversational message；
4. hint 是否仍属于该 Bot、状态是否 eligible；
5. `continuation_routing` 是否开启；
6. 如果配置了 Jev，查看 `route_source / route_quality`。

continuation classifier 未配置时，普通 Web hint 只代表 UI focus，系统确定性创建 NEW Work；这不是 provider failure。显式受信内部/兼容 work_id 仍可确定性续接。

### 症状 B：明显换话题却还留在 CURRENT

查看 `route_quality`：

- `default_continue`：Jev 正常运行，但 switch-away 证据未达到 threshold；
- `confident`：router 明确选择了当前 target；
- `fallback`：configured provider 失败，应被视为 weak route。

如果经常出现误留 CURRENT，应先看真实 route telemetry，再调整 `continuation_switch_threshold`，不要改成无界候选/全历史 prompt。

### 症状 C：多 tab 互相污染“上一条消息”

检查 `stream_id + last_seen_event_id`。v3 不允许用全局最近消息定义 adjacency。

### 症状 D：用户明确纠正“刚才那句不是这个项目”

correction 必须满足服务端 eligibility；接受后写 append-only `routing_correction`。原始消息不被改写，错误 assistant suffix 保留审计但从 effective context 排除。

禁止后台 mass-rebind 历史消息。

## 13. Web search 故障排查

### 症状：实时价格/新闻查询失败

检查：

- `PANGPANG_SEARCH_PROVIDER` 是否被设成 `off`（整体关闭）或某个单一 provider（该 provider 故障即整体失败）；
- provider 链是 Tavily（需 `TAVILY_API_KEY`）→ Bing → DuckDuckGo 依次 failback，返回的 `search_unavailable` detail 里有各 provider 的失败原因；
- `web_search` 是否在 Store transaction 外运行；若有人误把它包回 transaction，`assert_no_txn_io("web_search")` 应直接挡住；
- 网络是否可达；
- 请求是否属于轻量 search；
- 深度研究是否应改走 Executor。

禁止在 search 失败时让模型把训练记忆伪装成“刚查到”。

## 14. 结果回灌与重复派活排查

成功 Task 的 execution_result 应含：

- terminal status；
- evidence/artifact refs；
- bounded `result_text`；
- untrusted notice。

如果 Bot 只看见 `succeeded` 却没有内容，说明运行的是缺少结果回传路径的旧版本，请升级。

如果 Pi 同一轮已经输出正文，并且工具全部属于**只读集合或 `checkpoint_work`**，正文应通过 interim delivery 对用户可见。`checkpoint_work` 只是当前 Work 的本地状态记录。若同轮包含 submit/cancel/delegate/link/send 等 consequential tool，正文在 outcome 前**不应**被 durable deliver；这是 RCB 安全边界。


### 耗时

若 execution_result 带 `timing`：

- `executor_s` = CLI receipt 实际运行时间；
- `total_s` = task 创建到当前结果的总时间。

用户问“花了多久”时以这两个字段为准。不要采用模型正文自行估时。

### 产物没有出现

依次检查：

1. receipt.artifacts 是否有记录；
2. Executor 是否写入 `$XPP_OUTPUT_DIR`；
3. 若写 cwd，是否是**本次新建**且扩展名在 fallback allowlist；
4. 文件是否 >8 MiB；
5. 是否 symlink/junction/FIFO/非 regular file；
6. 是否越出 allowed_roots；
7. receipt.output_warnings。

不要通过放宽 allowed_roots 或允许 symlink 来“修”收割失败。

### 交付文件后来被修改

新 artifact 会尽量在 execution run 下建立 digest-addressed snapshot。read/download/copy 优先走 snapshot，所以 workspace 原路径后来被改，不应改变已交付版本。

排查：

1. artifact 是否有 `snapshot_execution_id`；
2. 若没有，查看 `snapshot_error`；
3. snapshot 路径读出后 digest 不匹配应 fail-closed；
4. 历史 artifact 没有自动补 snapshot，仍走 live path + digest 校验。

### read_evidence 的语义

`read_evidence` 会真实读取 artifact bytes 并核对 digest。UTF-8 文本可分页返回；图片/PDF/Office 等 binary 只返回 metadata + “bytes verified”状态，**不能因为 hash 对了就声称内容已读懂/业务验收通过**。
### Streaming / interim 重复或闪烁

current contract：

1. model delta 是 UI-only ephemeral；
2. final/waiting/interim durable message 先落 Store；
3. backend 再发 delta `done`；
4. 同 turn 已有 persisted assistant message 时，前端不再渲染 streaming bubble。

因此若看到**重复正文**，检查 persisted assistant 的 `turn_id` 是否与 SSE turn 一致；若看到 done 时短暂视觉闪烁，应做 browser E2E，而不是把它当成 durable message 丢失。

## 15. Diagnostics

认证后可查看 diagnostics API，例如：

```bash
curl -s \
  -H "Authorization: Bearer $PANGPANG_TOKEN" \
  http://127.0.0.1:8790/api/v1/diagnostics
```

重点观察：

- running / queued turn；
- active / unknown task；
- oldest age；
- draining；
- deadline / backlog 异常；
- `capabilities`：web_search / Jev key / continuation routing / gate 模式是否真的可用（启动时同样会对每项“已配置但用不了”的能力打一行 `capability:` warning）；
- `config_stale`：DB 中的 `settings/managed` 不是本进程发布的版本（服务运行中被改过库）。此时 API 配置写入会以 `state_conflict` 失败而不是覆盖；重启服务加载新配置；
- `work_routing_constraints`：带 `required_host_id` / `required_installation_id` 的未结束 Work。此前，聊天里“提到”某个主机或执行器 ID（包括“不要再用 X”）会把 Work 钉在 X 上；这些字段没有来源记录，所以**不会自动清理**。确认某个 Work 是误钉后，停服务再按 ID 精确清除：

  ```bash
  python -m pangpang.routing_constraint show  --data <cloud-data>
  python -m pangpang.routing_constraint clear --data <cloud-data> --work <work-id>
  ```

  Routine 的 Work 及其派生子 Work 上的限制是经过审批的 standing authority，`show` 里标 `lineage=routine`，`clear` 会拒绝（`routine_authority_reapproval_required`）：要改就修改 Routine 授权并重新批准。

  被这类限制拒绝的派活返回 `work_routing_constraint`（不再伪装成 `requested_executor_not_available`），模型的 Work 视图里也能看到这两个只读字段。子 Work 在创建时继承限制，需逐个清。

设备在线状态是协调器内存里的观测（最近 90 秒内有 sync），不再每次心跳写库/发事件；协调器重启后设备在第一次 sync（约 2 秒）前显示离线。网页上的“在线/离线 · 截至 HH:MM:SS”是那一刻的快照，不会自己倒计时，点刷新或有任何新事件时更新。

不要把 diagnostics 读操作变成自动“修数据”的路径。

## 16. 优雅停止 / 重启

停止顺序应满足：

1. coordinator 进入 draining；
2. 不再 claim 新 model turn；
3. bounded drain；
4. 必要时 kill 当前受管 Pi process；
5. execution supervisor 事实仍由 durable receipt/process identity 收口；
6. 下次启动运行 recover/reconcile。

不要直接删 `bots.db` / `connector.db` / run directory 来“解决卡住”。

## 17. 升级

任何会改变 durable state semantics 的升级：

1. 停止新流量；
2. 核查 running / unknown / cancel_requested；
3. WAL checkpoint；
4. 备份完整 data directory + vault；
5. 记录当前 commit/tag；
6. 升级代码；
7. 启动后先跑 reconcile/diagnostics；
8. 再开放新工作。

**不要在新版本已经写入数据库后，直接启动旧二进制消费同一数据库。**

**启动契约升到 v3：** `supervision_floor` 进入签名契约。云端只把新任务派给已报告支持 v3 的连接器；未升级的设备收不到新任务（任务停在 starting，过期后 unknown）。先升级设备，或对这类 unknown 任务执行人工 resolve——云端能证明启动命令从未送出时直接收口为 `cancelled`，不再等设备回应。

**部署等级不再有默认值：** 升级后，此前依赖“默认 L0”的 Linux cgroup 执行器需要操作员显式加上 `--deployment-level L0` 才会继续被派活；否则选路报 `executor_cleanup_unsupported`。Windows Job 路径不受影响。在途任务按原冻结规则收口。

回退不只是 git revert：如果新版本已经发出外部副作用或改变 durable schema/状态机，必须先 drain/reconcile，必要时恢复一致快照。


### 发行溯源与回滚摘要核验

`pangpang --version` 与 self-check 的 `build:` 行显示构建时烘焙进包的 commit、source ref（`git describe`）与构建时间；源码检出没有 `_build.py` 时显式显示 `unknown`。

发布构建流程（commit→资产摘要的可核对关系）：

1. 从明确的 tag 构建（工作树必须干净：`PANGPANG_REQUIRE_BUILD_PROVENANCE=1` 下缺失 git 信息、tracked 改动或**未追踪源文件**（`git status --porcelain -uall` 非空，`.gitignore` 排除的构建产物不计）都拒绝出包；只有本地源码开发允许 `unknown`）；
2. 构建产物内的 `BUILD_COMMIT`/`BUILD_REF` 由构建钩子从该构建输入写入（git 查询固定钉在源码根目录，与构建命令的当前工作目录无关），运行时不访问 git；
3. 出包后立刻 `sha256sum dist/*.whl dist/*.zip`，把**每个资产的 SHA256 与对应 tag/commit 一起写进该 tag 的 Release 说明**——资产摘要不可能嵌进资产自身，这层对应关系只存在于 Release 记录；
4. 已发布资产不可变：不重打同版本覆盖用户正在校验的包；新构建身份走新 tag/补丁版；
5. 回滚 SOP：先对候选资产 `sha256sum` 与 Release 记录核对，再按记录的 commit/tag 切代码树——只按"0.9.0"版本号切树不可接受，同版本号可能对应不同 commit。


### Routine approval 升级语义

模型创建/修改的 Routine 代表**未来持续写权限**，不能只凭“当前 turn 里有用户消息”自动生效。current main 的模型路径会创建/保留 proposal，但 routine 处于 `pending + inactive`，operator 必须批准当前版本。

approval digest 绑定：

```text
instruction
schedule
timezone
owner Bot policy（含 instructions/model profile 等）
```

因此：

- 模型创建 routine → pending，不触发；
- operator 审阅并提交当前 digest → approved，可调度；
- 模型修改 instruction/schedule/timezone → 若与批准版本不一致，重新 pending；
- owner Bot 的长期指令/身份策略变化 → 旧 approval digest 失效；
- `resume` / `run_now` / scheduler materialization 都必须重新检查 approved + digest match；
- 旧库缺 approval 字段或 digest mismatch → 启动时迁移成 pending/inactive，保留 proposal，不静默删除。

operator 自己通过 authenticated HTTP/launch path 创建/编辑属于显式 operator action，可在该事务内批准当前版本。

## 18. 本地验证

```bash
python -m pip install -r requirements.txt
npm ci --ignore-scripts
python -m compileall -q pangpang launch.py
node --check pangpang/pi_worker.mjs
node --check pangpang/web/app.js
node --check pangpang/web/setup.js
python -m unittest discover -s tests -p "test_*.py" -v
npx playwright install chromium
npm run test:browser
```

测试夹具把测试主机定义为“L0 自用主机且已记录进程组风险同意”（`tests/test_bot_runtime.py`），所以执行器用例在 Windows Job Object、Linux cgroup scope、macOS / 无 cgroup Linux 的进程组档上都能实际派活。只在进程组档主机上，两个快速验收用例会写明原因跳过（该档按设计不走快速验收）。

正式投产前建议完成真实环境验收：

- 真实模型；
- 真实 CLI 登录；
- 真实 TLS；
- 真实长 Task；
- kill / restart / ACK loss；
- stale receipt；
- unknown operator resolve；
- 文件产物与 digest；
- local-Qwen 长上下文性能。

## 19. 数据与秘密

### Executor 隔离等级（L0 / L1 / L2）

控制面（bots.db / Vault / runner receipts）与 CLI Executor 是不同信任域。workspace/control-data path separation、固定 argv、env filter 都是真约束，但**不是 OS sandbox**：同一 OS 账号启动的 Executor 仍可能绕过应用层 path policy 直接读控制目录。

- **L0 — 个人 loopback 自用**：POSIX（macOS、Linux）上普通启动（未传 `--deployment-level`，且不是 `--control-only`，也没有 `--origin`）推断为这一档。`--origin` 表示会从另一台机器访问，不能据此推断个人 L0。Windows 必须用 `--deployment-level L0` **显式声明**（没有默认值；未声明时只放行 Windows Job 强监管）。Linux 在有 systemd --user scope 时用 cgroup 协作档，否则用进程组（与 macOS 相同）。服务只绑定 loopback，减少匿名远程入口；但同账号 Executor **没有被隔离**。网页/文件/模型输出里的 prompt injection 仍可能诱导 CLI 读取控制面。以 L0 启动（显式声明，或 POSIX 个人启动时推断）即接受这项残余风险，不能把它记作 isolation acceptance。
- **L1 — 对外开放前的最低隔离**：Executor 使用独立受限 OS identity，该账号对 bots.db / Vault / runner/control paths 无读权限，只能访问授权 workspace。公开 cloud 上不应同时保留同账号 local Executor；更合适的是 coordinator 与受限 connector/device 分离。
- **L2 — 敏感场景强隔离**：Executor 进入 container/sandbox 或独立 VM，只挂载 workspace。把 coordinator 与 CLI 放进同一个容器不形成内部安全边界。

公网/跨不可信输入开放前，至少现场证明：

1. Executor identity 读取 bots.db / Vault 被 OS 拒绝，同时 workspace task 正常；
2. reverse proxy 有 request-size / read-timeout / connection / login-rate limits，后端不能绕过代理直连；
3. TLS 是真实链路，不把 `--origin https://...` 当作 TLS 证明；
4. artifact download isolation、device report binding、routine standing-write approval 都经过现场验收。

### L0 执行器监管档

执行器能被派活，取决于探测到的监管档与控制端声明的部署等级。三档的**覆盖面不同**：

| 监管档（`supervision`） | 平台 | 收尾证据（`termination_evidence`） | 覆盖 | 准入 |
|---|---|---|---|---|
| `windows-job-object` | Windows | —（Job 为空） | Job 内全部后代 | 任意等级，包括未声明 |
| `linux-cgroup-cooperative` | Linux + `systemd --user` scope + cgroup.kill | `cgroup-v2-populated-zero` | 留在该 cgroup 子树内的后代，含意外 setsid/双 fork | 仅 L0。POSIX 个人启动省略 `--deployment-level`（且没有 `--origin`、不是 `--control-only`）即为 L0 |
| `posix-pgroup-cooperative` | macOS / 无 cgroup scope 的 Linux | `pgroup-empty` | **仅**仍在 CLI 进程组内的成员 | L0。POSIX（macOS、Linux）个人启动省略 `--deployment-level`（且没有 `--origin`、不是 `--control-only`）即为 L0；无 cgroup scope 时 Linux 与 macOS 一样用进程组。Windows 须显式 `--deployment-level L0`。本协调器上 L0 不另要风险同意。连接器没有部署等级，进程组档仍要在该设备上记录同意。`macos-pgroup-postverify` 不放行 |

`process_tree_confirmed=true` 的含义统一为：**本 attempt 可以释放占用并提交终态**。来源由其它字段区分：runner 收尾证明（`supervision` + `termination_evidence`，即所选监管档自身作用域已清空）、显式 `not_launched`、或人工 `operator_resolution`。它**不是**“全部后代已结束”的证明。三档都不抵抗同账号主动逃逸，也都不是访问隔离。提交时（控制端、连接器共用同一纯函数 `runner.receipt_proof_ok`）：有冻结档的 attempt，任一终态（`succeeded` / `failed` / `cancelled`，不论有无 `exit_code`）都视为运行过 CLI，必须带已知监管档及其应有的证据，且不弱于提交时冻结的 `supervision_floor`；标签未知或与证据矛盾一律不提交（任务 unknown，占用不释放）。只有显式声明来源的未启动回执（`not_launched`：runner 启动前拒绝/失败、设备拒绝、从未启动、排队中取消，各自只允许一种结局）与人工处置（只允许 `cancelled`）无须清理证据；缺字段不等于未启动。**已启动 attempt 按冻结规则收口，不受之后部署等级变化影响**；部署等级的变化只阻止新启动。模型看到的任务视图不含这些监管字段。

**启用进程组协作档（自用 macOS / 无 cgroup Linux）：**

1. 控制端为 L0：POSIX（macOS、Linux）个人启动不传 `--deployment-level`、且没有 `--origin`、不是 `--control-only`，即为 L0。Windows 用 `python -m pangpang --deployment-level L0 …`。Linux 有 systemd --user scope 时探测为 cgroup 协作档，否则为进程组（与 macOS 相同）。Linux 启动脚本不代写 L0。`--control-only` 不因平台变成 L0。`--origin` 也不推断 L0。
2. 本协调器在 L0（含 POSIX 个人启动推断的 L0）下不把同意当作额外门，也不写 `l0_process_group_consent`。L0 下本机探测为 `posix-pgroup-cooperative` 后即可派活（Linux 无 cgroup scope 时与 macOS 相同）。连接器进程的 Execution 没有部署等级，进程组档要在那台设备上记录当前同意（先停止连接器，再 `python -m pangpang.consent accept`），探测才会是这一档。`macos-pgroup-postverify` 始终拒绝。Windows 上省略 `--deployment-level` 的启动保持未声明。

**连接器的执行等级（per-device `execution_level`）：**

本机/SSH 执行器的准入档 = 本协调器的 `--deployment-level`。**连接器设备**另有一个服务端写入的可选字段 `host.execution_level`：

- 设置时**覆盖**继承——该设备按自己的 L0 / L1 / L2 准入，与云端的部署等级无关。
- 缺省时**继承**云端的 `--deployment-level`（这正是此前就有的契约：云端显式 `--deployment-level L0` 本就放行它的连接器）。

| 连接器 `execution_level` | 云端 `--deployment-level` | 生效档 |
|---|---|---|
| 未设置 | L0 | L0（兼容旧语义） |
| 未设置 | 无 | 无（fail-closed） |
| L0 | 无 / L1 / L2 | L0（设备级覆盖） |
| L1 / L2 | L0 | L1 / L2（阻止协作档） |

这个字段**只能**由云端已认证的操作员经 `POST /api/v1/hosts/{host_id}/execution-level`（body `{"level":"L0"}`，`null` 清除覆盖恢复继承）写入；设备自报永远写不到它。连接器用进程组档（`posix-pgroup-cooperative`）时仍要设备本机的 `pangpang.consent accept`——`execution_level=L0`（云端）＋设备同意（本机）是**双重确认**；连接器用 cgroup 档（`linux-cgroup-cooperative`）时不另需设备同意（cgroup.kill 自身即强证据）。

**官方 happy path（control-only Mac 云 + Win/Linux 连接器，不传 `--deployment-level`）：**

1. Mac 以 `--control-only --origin https://…` 启动（部署等级因此为“未声明”，不自动推断 L0——正确，公网默认 fail-closed）。
2. Web 配对码配对 Windows / Linux 两台连接器，二者 online。
3. Windows 连接器探测为 `windows-job-object`（强监管），任意等级直接可派——无需任何额外步骤。
4. Linux 连接器探测为 `linux-cgroup-cooperative`（或无 cgroup 时 `posix-pgroup-cooperative`）。给**这一台**设备设 `execution_level=L0`（上面的 POST）；若是进程组档，再在该设备 `pangpang.consent accept`。此后 Linux 连接器正常入选，**无需**把整个 Mac 云降到 `--deployment-level L0`（那会连带放行本机/SSH 的协作档，范围过宽）。


**该档的已知限制（启用即接受）：**

- 通过 setsid / 双 fork 脱离进程组的后代**不会被发现或终止**，可能继续修改工作区、占用端口/GPU/共享资源；回执仍会是 `pgroup-empty`。需要覆盖全部后代的任务不要放在该档设备上。
- 同账号 CLI 可读控制目录、Vault、设备令牌与 CLI API key，拿到设备令牌可冒充设备回执；信任本机 CLI 及其项目配置（hooks / MCP / 插件）；不提供已核验只读，不扩大 fast-accept（该档回执不走 fast-accept）。
- 选路时的监管档冻结为任务的 `supervision_floor`。启动时实际可用档弱于它（如探测为 cgroup、启动时只剩进程组）、或无法确认作用域（`macos-pgroup-postverify` 或裸 `posix-process-group`），runner 在获取资源、启动 CLI **之前**拒绝，任务按 `failed`（`supervision_unavailable`）收口，不静默降级。部署等级不再是 L0 后，本机/SSH 主机即使 probe 仍是协作档也不再接受新的进程组任务。
- 清理/释放失败、组无法确认为空、runner 死亡：沿用 v2 unknown + quarantine；人工 resolve 仍按 v2 记为 `cancelled`，已知结果只留在操作员可见的原始回执/记录里。
- 仅撤回某个等待用 `cancel_dependency`，不会退役设备、也不释放 unknown 占用。**重装 / 换 host 身份复用旧工作区、共享账号或 NAS 前，先解决旧身份上的 unknown 占用**；系统不会跨 host_id 识别同一块盘。
- 已退役设备的连接器仍会按退避重试同步（已知限制）。远程 configure / 凭据 / 文件接收的权限闭合尚未实现；网页操作员与设备操作员未被系统独立验证为同一人。
- 进程组档没有 cgroup `RuntimeMaxSec` 那样的兜底：runner 被强杀后，组内进程与脱离者都可能无限期运行，并持有注入环境变量的密钥。
- 脱离并不少见：Node `detached: true` 子进程会 setsid（已在 Linux 实测脱离；普通子进程与 shell `&` 后台留在组内）。claude / codex 等 CLI 各自工具起的后台进程是否留在 PGID 内**尚未验证**，启用该档前请自行确认。
- macOS 路径（kqueue 观察退出、不回收组长）尚无实机验证；Linux 路径已在无 systemd 的容器内以真实进程验证。

**是否公网可达由部署拓扑决定，不由 127.0.0.1 绑定决定。** 反向代理/隧道完全可以转发 loopback。VPN/私网能降低匿名攻击面，但不能替代 L1/L2 的 Executor 隔离。


云端：

- `bots.db` / durable state
- Vault encrypted DB
- `master.key`

设备：

- `connector.db`
- device identity
- local secret store
- run directories

这些都属于私有 runtime data，不能进入源码发行包。

如果 token / device private key / API key 曾出现在 issue、聊天、日志或共享文件，**删除文本不等于撤销泄露**：必须 revoke / rotate / re-pair。
