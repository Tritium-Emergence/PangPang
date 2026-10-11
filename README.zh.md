# PangPang

**The Bot That Manages Your AI.**  
**让 AI 管理 AI，让人重新专注于目标。**

[English](README.md) · [安装指南](docs/INSTALL.md) · [模型接入](docs/MODEL-PROVIDERS.md) · [安全说明](SECURITY.md) · [部署与恢复](docs/RUNBOOK.md) · [文档地图](docs/INDEX.md)

---

## AI 已经很会干活了。为什么你反而更忙？

Claude Code 可以写代码，Codex 可以分析项目，本地模型可以处理日常工作。一个 Agent 能在几分钟里生成过去需要人花几小时完成的内容；多个 Agent 还能同时开工。

但 **AI 生成结果的速度，已经远远超出了人类逐项检查、协调和验收的速度**。

代码写出来了，谁确认没有破坏旧功能？研究完成了，谁检查结论的依据？三个 Agent 同时工作，谁知道哪个已经完成、哪个还在等别人？一台电脑断线了，任务到底有没有执行，能不能安全重试？

**AI 没有消灭所有难题，而是把难点从“生产”推向了“协调、检查、集成和验收”。**

于是，你拥有越来越多的 AI，自己却成了项目经理、IT 运维和人肉消息队列。

我们已经有很多能干活的“诸葛亮”。缺的不是再招一个更聪明的，而是有人把他们组织起来。

**这就是 PangPang：一个专门管理 AI 的 Bot。**

## 01 · 不是再造 Agent，而是让 AI 负责管理 AI

PangPang 不试图取代 Claude Code、Codex 或你已经部署的模型。

它站在这些执行工具之外，负责理解目标、组织工作、分配执行、跟踪进展，并把结果与真实状态带回给你。

| 谁 | 负责什么 |
| --- | --- |
| **你** | 提出目标、确定边界、批准重要操作、做最终决策 |
| **PangPang（主管 Bot）** | 管理长期工作、协调 Agent 与设备、处理依赖和异常、整理结果与证据 |
| **执行 Agent** | 使用自身的模型、工具和技能，完成研究、编码、写作、测试等具体任务 |

一个任务不再属于某个随时可能关掉的聊天窗口，而是属于一项可以持续追踪的 **Work**。

**你管理目标，PangPang 管理工作，Agent 负责执行。**

## 02 · 一个长期工作的 Bot，究竟有什么不同？

想象你有两项工作。

**工作 A：持续制作 AI 行业内容。** 你希望长期跟踪消息、收集资料、准备选题，并在需要时请你审阅。定时运行可以经过你批准后重复执行；研究、写作等具体工作可交给合适的执行器。

**工作 B：维护一个软件项目。** Codex 在一台电脑上修改代码，另一个执行器在另一台电脑上运行测试。测试失败时，PangPang 保存工作和依赖状态；结果回来后，继续把它们关联到原来的工作。

这些任务有三个共同难点：

**工作要持续存在。** 聊天结束、模型换了、服务重启，都不应抹掉任务是什么、已经做到哪一步。

**过程必须能核实。** Agent 说“做完了”不是完成证明。需要分清模型回复、进程状态、执行回执、文件产物和真正的业务验收。

**失败不能靠猜。** 如果电脑断线，系统尚不能确认执行结果，就应该记录“不确定”，而不是假装成功，也不应该贸然重新执行可能有副作用的操作。

PangPang 围绕这些问题建立长期控制面：保存 Work、依赖、checkpoint、执行事实和产物，使你能够回来查看、继续或纠正同一项工作。

> **聊天可以结束，工作不应该消失。**

## 03 · 已有的 AI、模型与电脑，组织成一个工作系统

你可能有本地 Qwen、云端 GPT 或 Claude；有 Windows 工作站、Linux GPU 服务器和 Mac；也有不同的 CLI Agent。

不应该为了完成一件事，先想清楚去哪个终端打开哪个 Agent，再在几个窗口之间手动传递消息。

PangPang 通过协调器和设备连接器组织这些能力：

- **长期 Bot** 保存岗位、稳定偏好和工作范围，不与某个具体模型绑定。
- **Work 与 Task** 记录工作、阶段、依赖、期限、checkpoint 和执行回执。
- **Executor** 使用现有 CLI Agent 执行任务，不必在 PangPang 内重造编码或研究能力。
- **Host / Connector** 让任务可以在拥有所需文件、工具和权限的电脑上执行。
- **Artifact** 将产物关联回工作，便于查阅、继续处理和交付。

```text
                      你
                目标 · 决策 · 授权
                      │
                      ▼
             PangPang · 主管 Bot
            理解 · 协调 · 回写状态
                      │
            持久 Work / Task / Receipt
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Codex      Claude Code     Pi / 其他
          │           │           │
       工作站        远程电脑       本地/云端模型
          └───────────┼───────────┘
                      ▼
               结果 · 文件 · 证据
                      │
                      ▼
             PangPang 归档与交付
```

这张图表示工作分工，并不意味着每一种 Agent、模型、设备和协议组合都已完成实机兼容验收。

## 04 · 不靠“超级长聊天”记住一切

更长的上下文并不等于更可靠的长期工作。

聊天里可能混着过时的方案、被否决的决定、工具日志和无关讨论。直接把整段历史转交给下一个 Agent，不但昂贵，还可能把错误信息和错误指令一并带过去。

PangPang 遵循一个原则：

**小而干净的活跃上下文，大而持久的外部状态。**

长期 Bot 留在外层，负责目标、协调与状态；需要真正执行时，向新的执行器交付有界的任务说明——目标、约束、相关证据和验收要求，而不是把整段脏聊天灌进去。

执行完成后，收回的是结果、产物和回执。真实工作状态由服务端保存，不能由模型的一句话改写。

这就是 **薄主管、厚执行器**：

> **共享目标和结果，隔离推理上下文；让擅长管理的 Bot 管事，让擅长执行的 Agent 干活。**

## 05 · PangPang 已经具备什么？

**长期工作管理。** 持久保存 Bot、Work、Task 和依赖；支持工作继续、取消传播、checkpoint，以及服务重启后的状态核对。

**设备与执行器协作。** 通过 Connector 将真实电脑接入协调器，按设备能力、位置和授权约束分配任务；执行器保留自身工具与技能栈。

**定时工作。** 支持 Routine。模型创建或修改的定时工作不会直接获得长期执行权限，必须经过操作者对当前准确版本的批准。

**执行结果与文件回收。** 将执行回执、文本结果和文件产物关联到来源任务；文件访问和回收受到路径、摘要、大小及类型等约束。

**断线和故障恢复。** 不把失联误当成任务失败，也不把未知状态伪装成成功；在缺少足够终态证据时保持依赖等待，防止危险的重复派发。

**Web 控制界面。** 从浏览器对话、配置模型、查看工作与产物；提供移动端布局适配。

**多模型协议。** 全局对话模型可配置 OpenAI Chat Completions、Responses、Anthropic Messages、Gemini API-key 模式，以及兼容的本地/第三方端点。模型目录和协议接入不等于所有供应商、模型或登录方式均已实机认证。

PangPang 当前重点是 **可靠地管理工作**，而不是宣称已经实现通用的自动质量判定、全平台沙箱或按每个阶段动态优化模型成本。

## 06 · 开始使用

当前为 **0.9.0 RC**，1.0 尚未发布。建议先用于受控环境，按[安装指南](docs/INSTALL.md)准备系统。

### 启动主管

需要系统 **Python 3.11+**，以及可访问的模型服务。使用包含平台运行时的正式组装包：

```bash
pip install pangpang
python -m pangpang.cli cloud
```

随后在本机浏览器打开：

**http://127.0.0.1:8790**

使用首次启动提供的访问密钥登录，并在设置中配置全局对话模型。

平台 wheel 已包含经过摘要校验的 Bun Worker 与 Pi 兜底执行器；不要求另外安装 Node.js，但**不包含 Python 解释器**。不要把从源码目录直接构建的普通 wheel 与包含运行时的发行包混为一谈。

### 接入另一台电脑

在 Web 中准备设备配对，并在目标电脑上执行：

```bash
python -m pangpang.cli connector \
  --cloud https://YOUR.DOMAIN \
  --workspace /your/workspace \
  --data /private/pangpang-device
```

Windows：

```powershell
python -m pangpang.cli connector --cloud https://YOUR.DOMAIN --workspace C:\Work --data C:\PangPangData
```

### 三台机器，一支队伍

主管只需要跑在一台机器上，其余每台电脑用连接器接入：

| 机器 | 角色 | 命令 |
| --- | --- | --- |
| Windows 工作站 | 主管 + 浏览器界面 | `python -m pangpang.cli cloud` |
| Linux GPU 服务器 | 执行设备 | `python -m pangpang.cli connector --cloud https://YOUR.DOMAIN --workspace /data/projects --data /var/lib/pangpang-connector` |
| MacBook | 执行设备 | `python -m pangpang.cli connector --cloud https://YOUR.DOMAIN --workspace ~/work --data "$HOME/Library/Application Support/pangpang-connector"` |

每台设备首次连接前，先在 Web 里完成配对。连接器只主动连接 `https://` 或本机 loopback 的 `http://127.0.0.1`。局域网 IP 不是受支持的 HTTPS 入口。可信入口由操作者自己的代理提供，见 [docs/HTTPS.md](docs/HTTPS.md)。

首次允许本机执行需要显式确认。设备不需要开放入站 SSH 或公网端口；远程协调器必须按[部署与恢复指南](docs/RUNBOOK.md)配置可信的 HTTPS 入口，不应把默认明文服务直接暴露在公网。

配置好模型、设备和执行器后，你可以从这些要求开始：

- “给我建一个长期负责公众号工作的 Bot。”
- “让 Linux 那台机器上的执行器处理这个项目的测试。”
- “以后每天早上生成一份简报，需要的权限先让我确认。”
- “继续昨天的工作，先告诉我已经完成了什么、还有什么没确认。”
- “把产物收回来，别覆盖原来的文件。”

## 07 · 安全边界与当前状态

PangPang 能操作真实文件和真实机器。因此，比起展示“完全自主”，我们更在意边界是否清楚：

- 模型和执行器的输出均不能自行授予权限。
- 新的对话 Work 默认 `grant_write=true`。这允许执行器在自己的 workspace 里写文件，不是操作系统授权。只读必须显式关闭。
- 密钥由控制面管理；自定义 API 端点需要明确确认。
- 已批准的 Routine 一旦发生影响授权的修改，需要重新批准。
- 未经核实的设备报告不能直接变成最终执行事实。
- 文件路径限制**不是操作系统沙箱**。L0 下执行器与控制面是同一个 OS 账号，应用层路径限制挡不住这个账号直接读取 Vault。这项残余风险不是隔离验收。
- `web_search` 默认会把查询词发到机器之外（无 key 时是 Bing 与 DuckDuckGo，设置了 `TAVILY_API_KEY` 时优先 Tavily）。`PANGPANG_SEARCH_PROVIDER=off` 才会关闭。
- 聊天里的一句话，包括人说“已经完成”或“unknown 的进程已经停了”，不是内核证据，也不能把 unknown 收成成功。unknown 只在有确认的终态回执或已记录的 operator resolve 之后收口。没有自动业务验收引擎。
- 跨设备或远程部署应采用可信 HTTPS，并按运行手册限制执行权限。两份打印同一版本号的包不是同一可回滚产物；对照构建时写入的 commit 和发布资产的 SHA256。

**发行与验收状态（0.9.0 RC）：**

| 平台 | 当前边界 |
| --- | --- |
| **Linux** | 已有自动化验证及 Bun 包测试；完整真实场景验收仍需持续补齐 |
| **Windows** | 有安装和浏览器测试路径；真实 CLI / Connector 故障注入验收仍在进行 |
| **macOS** | 有平台代码与测试入口；尚不宣称完成全部实机验收 |

自动测试通过，不代表所有模型、CLI、设备、断网恢复和跨平台场景都已验证。具体发行物与验证范围以 [Releases](https://github.com/Tritium-Emergence/PangPang/releases) 及[部署手册](docs/RUNBOOK.md)为准。

## 本地模型 / 高性能配置

默认 Scheduler 面向“可并发模型 endpoint”（`model_max_concurrency=4`）。如果你的 endpoint 实际是一条**单流本地 Qwen**，不要让四个长上下文同时打进同一个 GPU 服务，推荐显式配置：

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

Executor 的 `max_concurrency`、workspace RW claim 和本地资源锁是另一套容量域，模型容量不能重新耦合到执行器资源锁。

**不要根据模型名判断 provider**：本地与云端推理服务可能用同样的 `qwen-*` 名称，但两套 generation 参数互不兼容。新配置不自动注入 thinking/reasoning 参数：Pi 目录模型走 Pi 内置映射，自定义 endpoint 在设置里显式选择 thinking 格式、reasoning 档位与 `max_tokens`/`max_completion_tokens` 字段。历史 A/B 经验（见 RUNBOOK）建议本地 Qwen 开思考、云端 reasoning endpoint 用 `reasoning_effort=medium` + `max_completion_tokens`，但这是推荐显式配置，不是自动默认；旧的 pre-provider 档案在重配前保留历史字段行为。

## 可选外部服务

- `TAVILY_API_KEY`：可选，让 Pi 内置 `web_search` 优先使用 Tavily；不配置时 `web_search` 仍可用（Bing/DuckDuckGo 无 key 互备链）。
- `TYPESAFE_API_KEY`：启用 Jev/SystemOne 相关语义判断（可选外部闭源 API；缺失、超时或报错时必须有确定性降级路径）。

任何外部语义服务都不能决定权限、ownership、真实 liveness、secret 访问、workspace 安全或 execution terminal state。

## 测试与发布

本地验证是事实源：

```bash
# Linux / macOS / Windows；先准备可用的 Python venv，并指定其中的解释器
python -m pip install -r requirements.txt
npm ci --ignore-scripts
python -m unittest discover -s tests -p "test_*.py"
```

完整门禁还包括 Playwright 浏览器用例（`npx playwright install chromium && npm run test:browser`）与发行构建；构建要求干净的已提交 checkout。

自动测试通过不等于真实模型、真实 CLI、TLS、多平台、长上下文和断网恢复已经全部现场验证。正式 Release 前还要跑 destructive fault injection 和真实机器闭环。

`pangpang/web/app.js` / `app.css` 是由 `web-src/` 经 `npx vite build` 构建的单文件 bundle（可重建并逐字节比对）；审计前端请读 `web-src/`。

## 08 · 文档与共建

- [安装与首次运行](docs/INSTALL.md)
- [模型服务与协议](docs/MODEL-PROVIDERS.md)
- [安全模型](SECURITY.md)
- [部署、故障恢复与运维](docs/RUNBOOK.md)
- [文档索引](docs/INDEX.md)
- [贡献指南](CONTRIBUTING.md)

PangPang 的原创代码和文档采用 **Apache License 2.0**；第三方组件遵循各自许可证，详见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)。贡献遵循 [DCO](CONTRIBUTING.md)。PangPang 的名称和标志不随代码许可证一同授权，详见 [TRADEMARKS.md](TRADEMARKS.md)。

## One more thing · 让你的 Mac mini 再次伟大

VPS 终究是别人家的电脑：按月交租，数据躺在别人的机房，停止付费的那天，你的“基础设施”就蒸发了。

自己的硬件是另一个故事。桌下那台 Mac mini、退役的游戏 PC、屏幕裂了的老笔记本——如今的消费级机器（尤其是统一内存的 Apple Silicon）跑本地模型和真实执行器绰绰有余。它们一天里大部分时间在闲置，钱已经付过了，而且物理上属于你。

PangPang 把这些闲置硬件变成一支队伍：一台主管，多台执行，数据不出你的家门——除非你允许。没有按 Agent 计数的月租，没有第三方握着你的文件，没有人给你的野心限速。

**让你的 Mac mini 再次伟大。**

---

### 诸葛亮会越来越多，也会越来越便宜。

未来不会缺能写代码、做研究、分析数据的 AI。真正困难的是，把大量快速产生的工作，组织成可靠、连续、可检查的结果。

**PangPang 不想成为又一个诸葛亮。**

**我们想让你有自己的刘备。**
