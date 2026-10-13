# PangPang

**The Bot That Manages Your AI.**  
**Let AI manage AI, so people can focus on goals again.**

[中文](README.zh.md) · [Install](docs/INSTALL.md) · [Model providers](docs/MODEL-PROVIDERS.md) · [Security](SECURITY.md) · [Deployment & recovery](docs/RUNBOOK.md) · [Docs map](docs/INDEX.md)

---

## AI is great at getting work done. So why are you busier than ever?

Claude Code writes code, Codex analyzes projects, local models handle everyday tasks. A single agent can produce in minutes what used to take a human hours; several agents can work at the same time.

But **AI now generates results far faster than any human can review, coordinate and accept them**.

Code was written — who checked it didn't break anything? Research is done — who verified the evidence? Three agents are working at once — who knows which one finished and which is still waiting? A machine dropped offline — did the task run at all, and can it be retried safely?

**AI hasn't removed the hard problems. It has moved the difficulty from production to coordination, review, integration and acceptance.**

So you own more and more AI, while you yourself become the project manager, the IT department and the human message queue.

We already have plenty of capable agents — a hall full of virtuosos. What's missing is not one more, smarter hire, but someone to conduct them.

**That is PangPang: a bot dedicated to managing AI.**

## 01 · Not another agent — AI that manages AI

PangPang does not try to replace Claude Code, Codex or the models you have already deployed.

It stands outside those execution tools: it understands goals, organizes work, assigns execution, tracks progress, and brings results and real state back to you.

| Who | Responsible for |
| --- | --- |
| **You** | Setting goals, defining boundaries, approving consequential actions, making final decisions |
| **PangPang (the supervisor Bot)** | Managing long-running work, coordinating agents and devices, handling dependencies and failures, assembling results and evidence |
| **Executor agents** | Using their own models, tools and skills to do the actual work: research, coding, writing, testing |

A task no longer belongs to a chat window that can be closed at any moment — it belongs to a durable, trackable **Work**.

**You manage goals, PangPang manages work, agents do the execution.**

## 02 · What is actually different about a long-lived work bot?

Imagine two jobs.

**Job A: ongoing AI-industry content.** You want to track news long-term, collect material, prepare topics, and have them reviewed when needed. Scheduled runs repeat only after your approval; the actual research and writing is handed to suitable executors.

**Job B: maintaining a software project.** Codex changes code on one machine while another executor runs tests on a second. When a test fails, PangPang keeps the work and its dependency state; when results come back, it links them to the original work.

These tasks share three hard problems:

**Work must persist.** Ending a chat, switching models or restarting a service must never erase what the task was or how far it got.

**Progress must be verifiable.** An agent saying "done" is not proof of completion. You need to separate model replies, process state, execution receipts, file artifacts and real business acceptance.

**Failures must not be guesswork.** If a machine drops offline and the outcome is unconfirmed, the system should record "uncertain" — not pretend success, and not blindly re-run an operation that may have side effects.

PangPang builds a durable control plane around exactly these problems: it stores Work, dependencies, checkpoints, execution facts and artifacts, so you can come back, review, continue or correct the same piece of work.

> **Chats may end. Work should not disappear.**

## 03 · Your existing AI, models and computers — organized into one working system

You may have a local Qwen, cloud GPT or Claude; a Windows workstation, a Linux GPU server and a Mac; and several different CLI agents.

Finishing a task should not start with deciding which terminal to open, which agent to launch, and where to hand messages between windows.

PangPang organizes these capabilities through a coordinator and device connectors:

- **Long-lived Bots** keep a role, stable preferences and work scope; they are not bound to any specific model.
- **Work and Task** record work, phases, dependencies, deadlines, checkpoints and execution receipts.
- **Executors** run tasks with existing CLI agents — PangPang does not rebuild coding or research capabilities inside itself.
- **Hosts / Connectors** let tasks run on the machine that has the files, tools and permissions they need.
- **Artifacts** link outputs back to the work, for review, follow-up and delivery.

```text
                      You
                goals · decisions · authority
                      │
                      ▼
             PangPang · supervisor Bot
        understand · coordinate · write back state
                      │
          durable Work / Task / Receipt
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Codex      Claude Code     Pi / others
          │           │           │
      workstation   remote PC    local/cloud models
          └───────────┼───────────┘
                      ▼
              results · files · evidence
                      │
                      ▼
            PangPang archives & delivers
```

This diagram shows the division of labor; it does not claim that every combination of agent, model, device and protocol has passed real-machine acceptance.

## 04 · Remembering everything is not a super-long chat's job

Longer context is not the same as more reliable long-term work.

A chat transcript mixes outdated plans, rejected decisions, tool logs and irrelevant discussion. Handing that whole history to the next agent is expensive — and it carries over bad information and bad instructions along with the good.

PangPang follows one principle:

**Small, clean active context; large, durable external state.**

The long-lived Bot stays in the outer layer, owning goals, coordination and state. When real work is needed, it hands a fresh executor a bounded task brief — goal, constraints, relevant evidence, acceptance criteria — instead of dumping the entire dirty chat into the prompt.

When execution finishes, what comes back is results, artifacts and receipts. The real work state is stored server-side and cannot be rewritten by a model's say-so.

This is the **thin supervisor, thick executor** design:

> **Share goals and outcomes, isolate reasoning context — let the Bot that is good at managing manage, and the agents that are good at executing execute.**

## 05 · What PangPang already does

**Long-running work management.** Bots, Work, Tasks and dependencies persist; work continues, cancellation propagates, checkpoints hold, and state reconciles after a service restart.

**Device and executor collaboration.** Connectors bring real computers into the coordinator; tasks are assigned under capability, location and authority constraints; executors keep their own tools and skill stacks.

**Scheduled work.** Routines are supported. A Routine created or modified by a model never gains standing execution authority directly — it requires the operator's approval of the exact current version.

**Results and files come back.** Execution receipts, text results and file artifacts are linked to their source task; file access and harvest are constrained by path, digest, size and type.

**Disconnect and failure recovery.** A lost connection is not treated as task failure, and unknown state is never dressed up as success; without sufficient terminal evidence, dependencies stay waiting and dangerous re-dispatch is prevented.

**Web control UI.** Chat, configure models, browse work and artifacts from a browser; mobile layout supported.

**Multi-protocol models.** The global conversation model supports OpenAI Chat Completions, Responses, Anthropic Messages, Gemini API-key mode, and compatible local/third-party endpoints. Catalog and protocol support are not a certification that every vendor, model or login method has been field-tested.

PangPang's current focus is **managing work reliably** — not claiming universal automatic quality judgment, full-platform sandboxing, or per-phase dynamic model-cost optimization.

## 06 · Getting started

The current release is **0.9.0 RC**; 1.0 is not out. Start in a controlled environment and follow the [install guide](docs/INSTALL.md).

### Launch the supervisor

Requires system **Python 3.11+** and access to a model service. Use the official assembled package with platform runtimes:

```bash
pip install pangpang
python -m pangpang.cli cloud
```

Then open **http://127.0.0.1:8790** in your local browser.

Sign in with the access key shown on first launch and configure the global conversation model in settings.

Platform wheels include digest-verified Bun Worker and the Pi fallback executor; Node.js is not required, but **the Python interpreter is not bundled**. Do not confuse a plain wheel built from a source directory with an assembled release package that contains runtimes.

### Connect another computer

Create a device pairing in the Web UI, then on the target machine:

```bash
python -m pangpang.cli connector \
  --cloud https://YOUR.DOMAIN \
  --workspace /your/workspace \
  --data /private/pangpang-device
```

Windows:

```powershell
python -m pangpang.cli connector --cloud https://YOUR.DOMAIN --workspace C:\Work --data C:\PangPangData
```

### Three machines, one team

The supervisor runs on one machine; every other machine joins with a connector:

| Machine | Role | Command |
| --- | --- | --- |
| Windows workstation | supervisor + browser UI | `python -m pangpang.cli cloud` |
| Linux GPU server | executor device | `python -m pangpang.cli connector --cloud https://YOUR.DOMAIN --workspace /data/projects --data /var/lib/pangpang-connector` |
| MacBook | executor device | `python -m pangpang.cli connector --cloud https://YOUR.DOMAIN --workspace ~/work --data "$HOME/Library/Application Support/pangpang-connector"` |

Pair each device in the Web UI before its first connect. Connectors only make outbound connections to `https://` or to loopback `http://127.0.0.1`. A bare LAN IP is not a supported HTTPS origin. The trusted entry is the operator's own proxy, described in [docs/HTTPS.md](docs/HTTPS.md).

First-time local execution requires explicit confirmation. Devices need no inbound SSH or public ports; a remote coordinator must sit behind a trusted HTTPS entry as described in the [deployment & recovery guide](docs/RUNBOOK.md) — never expose the default plaintext service to the public internet.

Once models, devices and executors are configured, start with requests like:

- "Create a long-lived Bot responsible for my public-account content."
- "Have the executor on the Linux machine run the tests for this project."
- "Generate a briefing every morning from now on — ask me first when permissions are needed."
- "Continue yesterday's work; first tell me what is done and what is still unconfirmed."
- "Harvest the artifacts, and don't overwrite the original files."

## 07 · Security boundaries and current status

PangPang operates real files on real machines. So we care less about demonstrating "full autonomy" than about whether the boundaries are clear:

- Neither model nor executor output can grant itself permissions.
- A new conversation Work is created with `grant_write=true`. That lets an executor write inside its workspace. It is not an operating-system grant. Read-only work has to be set explicitly.
- The built-in Codex executor defaults to the `workspace-write` sandbox (`exec --sandbox workspace-write`). A coding task can write inside the task workspace. A write outside that workspace is still refused: the plan sends `sandbox=workspace-write`, adds no writable root, and drops `--add-dir`. This is the Codex sandbox tier, not an operating-system grant.
- Full bypass is not a default. Chat, a new profile, and `extra_argv` cannot enable it. A new profile copies the built-in catalog argv, which does not contain `--dangerously-bypass-approvals-and-sandbox`, and `extra_argv` rejects that flag. The operator path is a custom row in `<data>/adapters.json` whose argv includes the flag. That file cannot replace the built-in `codex` row, and a custom row cannot declare a stdin protocol, so the run is plain exec: the flag stays on the process command and is not rewritten by the Codex app-server plan.
- An installation already stored keeps its argv. The upgrade migration authorizes that stored command, so the installation keeps running without a new approval prompt. Its authorization digest does not change. Configuring it again copies the current catalog command and changes the digest. A stored command with no `--sandbox` still comes up read-only.
- Secrets are managed by the control plane; custom API endpoints require explicit confirmation.
- An approved Routine that changes in a way affecting its authority must be re-approved.
- An unverified device report never becomes a final execution fact directly.
- File-path restrictions are **not an OS sandbox**. At L0 the executor runs as the same OS account as the control plane and can read the Vault directly. That residual risk is not an isolation acceptance.
- `web_search` sends the query off the machine by default (Bing and DuckDuckGo when no key is set; Tavily when `TAVILY_API_KEY` is set). `PANGPANG_SEARCH_PROVIDER=off` is what turns that off.
- A sentence in chat, including a person's statement that something "finished" or that an unknown process "has stopped", is not kernel evidence and does not close an unknown attempt. Unknown closes only on a confirmed terminal receipt or a recorded manual resolve (`confirmed_never_started` or `confirmed_stopped`, with operator evidence). There is no automatic business-acceptance engine.
- Cross-device or remote deployments must use trusted HTTPS and restrict execution per the runbook. Two packages that print the same version are not the same rollback artifact; compare the baked commit and the release asset SHA256.

**Release and acceptance status (0.9.0 RC):**

| Platform | Current boundary |
| --- | --- |
| **Linux** | Automated verification and Bun package tests exist; full real-scenario acceptance is still being completed |
| **Windows** | Install and browser test paths exist; real CLI / Connector fault-injection acceptance is in progress |
| **macOS** | Platform code and test entry points exist; full real-machine acceptance is not claimed |

Passing automated tests does not mean every model, CLI, device, disconnect-recovery and cross-platform scenario has been verified. Shipped artifacts and verified scope are listed in [Releases](https://github.com/Tritium-Emergence/PangPang/releases) and the [runbook](docs/RUNBOOK.md).

## Local-model performance

After the first start, managed settings in the database are the live configuration. The startup file (`--config`, default `config/runtime.json`; the built-in empty config when that file is absent) is only the seed: the process uses it while `settings/managed` is missing, and the first managed write copies that in-memory config into the data directory. Local-exec consent, local host registration, and saving model settings each perform that write.

On every later start the process loads `settings/managed`. When the startup file sets `turn_budget` or `semantic_gate`, those two values replace the stored ones in memory for that process. That read does not write them back. A replacement is logged as `startup config replaced stored`. Every other key in the file, including `scheduler`, stays in the stored settings, and a differing key is logged as `startup config is not the live settings after the first start`.

With no stored `scheduler` object the process runs four simultaneous model conversations (`model_max_concurrency` 4, `reserved_user_slots` 1). An endpoint that accepts concurrent requests can keep that default. A **single-stream local Qwen**, where parallel long contexts reduce wall-clock performance, should use one conversation.

Set it in the Web UI. Open **Set global chat model** (or **Set up model**), then **Simultaneous model conversations**, an integer from 1 to 16, and save. The save stores `scheduler.model_max_concurrency` on the managed row. A value of 1 also stores `reserved_user_slots` as 0 so the next start accepts the config. A value above 1 keeps the stored `reserved_user_slots` when that integer is already in range, and otherwise stores 1. No other scheduler key is written. Restart the coordinator before the new limit runs. No environment variable sets this limit.

After the managed row exists, `main_bot_max_active_turns`, `main_bot_reserved_user_slots`, and `service_bot_max_active_turns` have no supported change path.

Executor capacity and workspace claims are a separate resource domain and must not be coupled back into planner resource locks.

**Do not detect the provider from the model name:** local and cloud inference services may share the same `qwen-*` name, yet their generation parameters are not interchangeable. New configurations do not auto-inject thinking/reasoning parameters: Pi-catalog models follow Pi's built-in mapping, and custom endpoints get their thinking format, reasoning level and `max_tokens`/`max_completion_tokens` field chosen explicitly in settings. Historical A/B evidence (see RUNBOOK) recommends enabling thinking for local Qwen and `reasoning_effort=medium` + `max_completion_tokens` for cloud reasoning endpoints, but that is a recommended explicit setting, not an automatic default; pre-provider profiles keep their historical field behavior until reconfigured.

## Optional services

- `TAVILY_API_KEY` is optional: it makes in-process `web_search` prefer Tavily. Without it `web_search` still works through the keyless Bing/DuckDuckGo chain.
- `TYPESAFE_API_KEY` enables optional Jev/SystemOne semantic decisions (an optional external closed API; missing/timeout/error must have a deterministic fallback path).

Neither service may become an authority for ownership, permissions, liveness, secrets, workspace safety or execution terminal state.

## Validation

Local gates are the source of truth:

```bash
# Linux/macOS/Windows: prepare a Python venv and use its interpreter
python -m pip install -r requirements.txt
npm ci --ignore-scripts
python -m unittest discover -s tests -p "test_*.py"
```

The full gate additionally covers Playwright browser tests (`npx playwright install chromium && npm run test:browser`) and the release build; builds require a clean committed checkout.

Passing automated tests does not mean real-model / real-CLI / TLS / multi-platform / long-context / disconnect-recovery have all been field-verified. A real release additionally requires destructive fault injection and a real-machine loop.

`pangpang/web/app.js` / `app.css` are single-file bundles built from `web-src/` via `npx vite build` (rebuildable and byte-comparable); audit the frontend by reading `web-src/`.

## 08 · Documentation and contributing

- [Install and first run](docs/INSTALL.md)
- [Model services and protocols](docs/MODEL-PROVIDERS.md)
- [Security model](SECURITY.md)
- [Deployment, recovery and operations](docs/RUNBOOK.md)
- [Docs index](docs/INDEX.md)
- [Contributing guide](CONTRIBUTING.md)

PangPang's original code and documentation are licensed under the **Apache License 2.0**; third-party components remain under their own licenses — see [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES). Contributions follow the [DCO](CONTRIBUTING.md). The PangPang name and logo are not licensed with the code — see [TRADEMARKS.md](TRADEMARKS.md).

## One more thing · Make your Mac mini great again

A VPS is somebody else's computer. You rent it month after month, your data sits in someone else's rack, and the day you stop paying, your "infrastructure" evaporates.

Your own hardware tells a different story. The Mac mini under your desk, the retired gaming PC, the old laptop with the cracked screen — modern consumer machines (Apple Silicon's unified memory especially) are genuinely good at running local models and real executors. They idle most of the day, they are already paid for, and they are physically yours.

PangPang turns that idle hardware into a workforce: one machine supervises, the others execute, and your files stay on machines you control. Web search sends the query off the machine by default; `PANGPANG_SEARCH_PROVIDER=off` turns that off. No per-agent monthly rent, no third party holding your files, nobody throttling your ambition.

**Make your Mac mini great again.**

---

### There will be more virtuosos, and they will keep getting cheaper.

The future will not lack AI that writes code, does research or analyzes data. What is genuinely hard is organizing a flood of fast-generated work into reliable, continuous, checkable results.

**PangPang does not want to be another virtuoso.**

**We want you to have your own conductor.**
