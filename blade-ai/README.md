<h1 align="center">BLADE AI</h1>

<!-- Repo-internal links and images use Markdown syntax, not HTML tags. The
     hosting platform rewrites relative paths in Markdown to /blob/ but leaves
     paths inside raw HTML alone — those resolve to /raw/, which serves bytes
     instead of a rendered page, so <a href> lands on source text and <img src>
     shows nothing. Centring is not worth an unreadable page. -->

English · [简体中文](README.zh-CN.md)

<p align="center">
  <strong>Run chaos experiments in plain language — safely, verifiably, and with guaranteed recovery.</strong>
</p>

<p align="center">
  A Kubernetes &amp; host chaos-engineering agent. Describe a fault in natural language;
  BLADE AI plans it, screens it through rule-based safety gates, injects it via
  <a href="https://github.com/chaosblade-io/chaosblade">ChaosBlade</a>, verifies the
  effect actually took hold, and recovers deterministically — every run completing the
  full <em>intent → safety → injection → verification → recovery</em> loop.
</p>

[![Apache 2.0 License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](NOTICE) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-brightgreen.svg) ![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-orange.svg) ![88 scenarios](https://img.shields.io/badge/scenarios-88-9cf.svg)

[Why BLADE AI](#why-blade-ai) · [How it works](#how-it-works) · [Quick start](#quick-start) · [Scenarios](#fault-scenarios) · [Interfaces](#four-interfaces) · [Safety](#safety) · [Architecture](#architecture) · [Full usage](docs/USAGE.md)

---

## Why BLADE AI

Every agent is fundamentally the same thing: a ReAct loop (reason / act / observe) wrapped around a general-purpose LLM. Since the base is identical, the only things that differ are **tools** and **context** — so a "general agent + skills" can, in principle, do chaos engineering too. Reasoning is fixed by the model and no engineering can move it; only *acting* and *observing* are ours to shape. A vertical agent pushes those to the extreme for one domain — and does the three things a general agent + skills **can't do reliably**:

- 🎯 **Determinism, not one big loop** — the drill's order is graph structure, not something the model decides. Intelligence lives in three separate loops (Plan / Execute / Verify), and each phase has its own guard: planning may look but never touch, execute is the only phase allowed to mutate. A general agent has one loop, so one permission set — mutation is either allowed everywhere or nowhere.
- 🛡️ **Controllability over a hallucinating model** — when a target can't be injected, LLMs "cleverly" switch to another one. BLADE AI freezes the approved target at confirmation and screens every call against it: *the method may change, the identity may not*.
- ♻️ **Irreversibility — clusters have no undo** — every experiment carries a mandatory timeout that self-destructs even if the agent crashes. And since you can't undo, you must know what changed: BLADE AI snapshots the environment before injecting and diffs it after verifying, so you see what the fault touched **beyond its target**. A general agent confirms the target broke; only a diff shows the blast radius.

The generic plumbing — memory compaction, progressive skill loading — isn't the point; a general agent has that too. The point is turning "it runs" into "it runs stably and safely." Vibe coding has hugely raised development throughput, but shipping something *stable* still needs resilience testing that used to require a senior SRE. BLADE AI exists to lower that barrier — to make chaos engineering safe and simple enough that anyone can do it.

> In one line: a general agent makes "being able to do it" ubiquitous; a vertical agent makes "doing it stably and safely" possible.

---

## How it works

BLADE AI orchestrates the whole drill lifecycle as a deterministic LangGraph state machine. Both entry forms — free-form natural language and structured CLI parameters — are parsed into the same fault intent, and every run follows the identical ordered spine after the same `safety_check`.

![Three-phase ReAct pipeline: Plan, Safety Check, Confirm Gate, Execute, Verify, plus an independent Recover subgraph](assets/pipeline.png)

| Stage | Responsibility | Key design |
| --- | --- | --- |
| **Phase 1 · Plan** | Understand intent, match skills, generate the fault plan | `FULL` prompt; read-only planning tools — cannot call `blade_create` |
| **Safety Check** | Namespace blacklist, conflict detection, target validity, blast-radius score | Pure rule engine, no LLM in the path |
| **Confirm Gate** | Human authorization before anything is injected | Dynamic node-level `interrupt()`, resumed with `Command(resume=…)` |
| **Baseline** | Capture pre-injection metrics and an environment snapshot | Verification becomes a before/after comparison, not a threshold guess |
| **Phase 2 · Execute** | Invoke ChaosBlade / kubectl to inject | `MINIMAL` prompt; every tool call screened by the target-drift guard |
| **Phase 3 · Verify** | Two-layer effect verification | L1 deterministic `blade_status` · L2 LLM semantic judgement (`VERIFICATION`) |
| **Side-effect Detect** | Diff the post-drill state against the snapshot | Surfaces impact beyond the target |
| **Recover** | Independent, separately-compiled recovery graph | Its own ReAct loop + two-layer verification + `--force` fallback |

Every super-step is checkpointed by `thread_id = task_id`, so a crashed or interrupted run resumes at the exact node it left off. Per-phase loop caps (`100 / 100 / 60`) and a global `recursion_limit` of `500` bound runaway behaviour.

---

## Quick start

### 1. Install

macOS / Linux:

```bash
# Latest version
curl -fsSL https://chaosblade.io/install-agent.sh | bash

# Pin a version
curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --version 0.6.0
```

The prebuilt bundle embeds the Python runtime, the ChaosBlade binary, and all skill files — unpack and run, zero dependencies. It supports linux-amd64 / linux-arm64 / darwin-amd64 / darwin-arm64. The installer performs SHA256 verification, PATH setup, and receipt recording automatically.

> Windows has no prebuilt bundle yet — use the bash installer under WSL2.

### 2. Prerequisites

- **kubectl** configured and able to reach the target cluster
- **ChaosBlade Operator** deployed to the cluster (`kubectl get pods -n chaosblade`)
- An **LLM API key** (OpenAI-compatible endpoint, DashScope by default)

### 3. Configure

```bash
blade-ai config set llm_api_key sk-xxx        # set API key
blade-ai config set model_name qwen3.7-plus   # set model (default: qwen3.7-plus)
blade-ai config                               # show all config
```

Config priority: init args > `~/.blade-ai/config.json` > environment variables (`BLADE_AI_*`) > defaults. To harden the namespace blacklist (empty by default), set `BLADE_AI_SAFETY_BLACKLIST_NAMESPACES=kube-system,...`.

### 4. Your first injection

```bash
# Natural-language mode
blade-ai inject -i "inject 80% CPU pressure into my-pod in the default namespace for 120s"

# Structured mode (CI/CD-friendly)
blade-ai inject --scope pod --target cpu --action fullload \
  -n "app=myapp" --namespace default \
  -p "cpu-percent=80" -d 120

# List available scenarios
blade-ai list

# Recover
blade-ai recover --task-id task-xxx
```

Full command reference: [docs/USAGE.md](docs/USAGE.md).

---

## Fault scenarios

Three built-in skill packs cover **88 fault scenarios**. Each pack is one `SKILL.md` plus a catalogue of scenario files under `references/catalogue/`:

### k8s-chaos-skills — 61 scenarios

| Layer | Examples |
| --- | --- |
| **Pod** | CPU fullload, CPU throttling, OOM, disk fill, high disk IO, packet loss, network latency, Pending, ContainerCreating, CrashLoopBackOff, Terminating, image-pull failure, evicted & rebuilt, process kill, deletion … |
| **Container** | CPU fullload, packet loss, deletion, process anomaly |
| **Node** | High CPU, high memory, high disk IO, low disk space, unreachable (100% loss), maintenance, network failure |
| **Workload / Service** | Replica scale-down, HPA maxed out, DaemonSet scheduling anomaly, Service call failure, Service load-balancer anomaly |

### host-chaos-skills — 18 scenarios

CPU fullload, memory / cache hogging, disk fill, high disk IO, packet loss / DNS hijack / port occupation, process kill / hang / count spike, file deletion / tampering / handle exhaustion, clock skew, systemd service stop, syscall latency / return-value tampering.

### python-app-chaos-skills — 9 scenarios

HTTP latency / error, MySQL latency / error, Redis latency / error / return-value tampering, Kafka error, gRPC latency.

> Add a scenario file under a pack's `references/catalogue/`, or drop a new `SKILL.md` to add a whole pack — Server mode hot-reloads changes automatically (watchdog + 500 ms debounce).

---

## Four interfaces

| Interface | Entry point | Best for |
| --- | --- | --- |
| **CLI** | `blade-ai inject` / `recover` / `list` / `metric` / `config` | Command-line ops, CI/CD pipelines |
| **TUI** | `blade-ai` (interactive terminal) | Day-to-day ops, watching progress live |
| **HTTP API** | `POST /api/v1/inject`, `POST /api/v1/inject-stream` (SSE) | Platform integration, external systems |
| **Python SDK** | `from chaos_agent.l4 import L4ResilienceAgent` | Programmatic calls, test-platform integration |

### Two run modes

The same agent core backs both modes; `blade-ai config set mode` switches which side calls the graph — `AgentRunner` in-process (local) or `AgentClient` over HTTP (server).

```bash
# Local mode (default) — agent runs in-process, zero network overhead
blade-ai config set mode local

# Server mode — centralized FastAPI control, CLI/TUI connect remotely
blade-ai server                                        # terminal 1: start server (default 0.0.0.0:8089)
blade-ai server --host 127.0.0.1 --port 9000           # bind address / port explicitly
blade-ai server --port 0 --ready-stdout                # OS-allocated port; prints "BLADE_AI_READY port=N"
blade-ai config set mode server http://localhost:8089  # terminal 2: switch to server
```

---

## Safety

Safety is not a single check but five progressive layers. An injection only reaches the cluster after clearing every one of them:

![Five-layer defense in depth: Safety Check, Confirmation Gate, Per-phase Screeners, ToolGuard, Loop Max & Timeout](assets/safety-layers.png)

1. **Safety Check** — a pure rule engine (no LLM): configurable namespace blacklist, conflict detection against live ChaosBlade CRDs, target validity, and a multi-dimensional blast-radius score.
2. **Confirmation Gate** — a dynamic, data-driven `interrupt()` that pauses for human approve/reject; critical operations cannot proceed without it.
3. **Per-phase guards** — each phase that lets the LLM pick tools has its own guard with its own red line: planning is denied every mutating call, execute is screened against the frozen approved target (*method may change, identity may not*, including escapes hidden inside `sh -c`), verify and recover are limited to the connected environment.
4. **ToolGuard** — a fail-closed command whitelist plus a dangerous-pattern blacklist (`rm -rf`, `| bash`, `$(…)` …); everything runs exec-form, so pipes and substitutions are inert. It sits at the execution entry point, so commands from *every* phase pass through it.
5. **Loop Max & Timeout** — per-phase loop caps, a global recursion limit, and a mandatory timeout that self-destructs the experiment even if the agent dies. When no duration is specified, a per-fault-type floor (default 300s) is injected; an explicitly stated duration is honored verbatim and never silently amended.

Those five gate what *reaches* the cluster. One more answers what no gate can: **did it stay inside the blast radius you approved?** The environment is snapshotted before injection and diffed after verification — restarts, evictions, OOM kills, endpoint removals, HPA scaling and more. Verifying that the target broke is easy; proving nothing *else* did is what makes a drill safe to repeat.

### The third verdict: `unverified`

Verification can go wrong in two very different ways: the fault effect is provably absent (counter-evidence → `failed`), or the **observation channel itself** was down — kubectl auth expired, metrics queries timed out. Reporting the latter as a failure would claim evidence nobody observed. So a run can also end `unverified`: the command was issued, the conclusion is simply not knowable right now.

`unverified` tasks stay in the recoverable list — re-destroying is idempotent. Fix the observation channel first (the result's `observation_failures` field classifies each failed probe as `auth` / `transient` / `unknown`), then re-check or simply recover. In batch summaries it renders as `?`: `✓` claims success, `✗` claims failure, and "cannot tell" is neither.

---

## Architecture

BLADE AI is layered: entry adapters on top, a unified LangGraph orchestration core, a capabilities layer, and shared infrastructure. All three call paths (Local in-process, Server over HTTP+SSE, SDK) converge on the same compiled graph.

![Functional architecture map: ten layers from entry interfaces, through the orchestration engine, safety guards, domain semantics, execution, memory, model connectivity and observability, down to the real fault surface](assets/architecture-layers.svg.png)

*Click the image to open the full-resolution original for detail.*

A single `AgentState` (organized by lifecycle: identity / intent / planning / safety / confirmation / execution / verification / recovery / loop-control / results / memory) is the source of truth; natural-language and structured inputs are planned by the LLM and every run converges at `safety_check`; the Recover graph is compiled independently with its own ReAct loop; and SSE streaming (token / tool / confirm / result …) threads through nodes → FastAPI → TUI as the unified real-time channel. Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Tech stack

| Layer | Technology |
| --- | --- |
| Agent orchestration | LangGraph (StateGraph) · LangChain |
| Fault injection | ChaosBlade · kubectl |
| Backend | FastAPI · Typer · pydantic-settings |
| TUI | TypeScript · Ink · React |
| Storage | aiosqlite (Checkpointer) · PostgreSQL (optional) |
| Observability | OpenTelemetry · Prometheus · SSE |
| LLM | OpenAI-compatible endpoints (DashScope / DeepSeek / Zhipu …) |

---

## Development

```bash
make install     # runtime + dev dependencies
make test        # run tests
make server      # start the server
make build       # PyInstaller standalone binary
make build-tui   # build the TUI frontend
```

Python backend tests: `uv run pytest tests/ -v` · TUI frontend tests: `cd tui && npm test`

---

## Relationship with ChaosBlade

BLADE AI is part of the [ChaosBlade](https://github.com/chaosblade-io/chaosblade) ecosystem. ChaosBlade is the injection engine (CLI + Operator); BLADE AI is its intelligent agent layer:

- **ChaosBlade** owns *how to inject* — running concrete commands like `blade create k8s pod-cpu fullload`.
- **BLADE AI** owns *whether to inject, whether it worked, and how to recover* — intent understanding, safety review, effect verification, deterministic recovery.

They are complementary, not competing. BLADE AI calls ChaosBlade underneath and adds LLM orchestration plus safety guardrails on top.

---

## License

[Apache 2.0](NOTICE) — Copyright 2026 ChaosBlade Authors.
