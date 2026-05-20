# BLADE AI — Usage

**Languages:** [中文](USAGE.md) | English

A complete reference for install, configuration, commands, API, and best practices. If this is your first time, start with [README_en.md](../README_en.md) for the quick start. For architecture and design, see [INTRODUCTION_en.md](INTRODUCTION_en.md).

## Table of contents

- [Install](#install)
- [Uninstall](#uninstall)
- [First-time setup](#first-time-setup)
- [Conversational TUI](#conversational-tui)
- [Structured CLI](#structured-cli)
- [Three injection modes in detail](#three-injection-modes-in-detail)
- [Fault scenarios](#fault-scenarios)
- [Recovery & fallback](#recovery--fallback)
- [Server mode & API](#server-mode--api)
- [Configuration](#configuration)
- [TS TUI environment variables](#ts-tui-environment-variables)
- [Common pitfalls cheat sheet](#common-pitfalls-cheat-sheet)
- [Development guide](#development-guide)

---

## Install

### One-liner (recommended)

When no version is given, the installer queries the GitHub Releases API and resolves the latest `blade-ai-v*` tag. Pin an older version with `--version` or `BLADE_AI_VERSION`:

```bash
# macOS / Linux — install latest (default; auto-resolves the newest release)
curl -fsSL https://chaosblade.io/install-agent.sh | bash

# Pin a specific version
curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --version 0.1.0

# Same via env var (good for Dockerfiles / CI)
BLADE_AI_VERSION=0.1.0 curl -fsSL https://chaosblade.io/install-agent.sh | bash
```

The script detects your platform from `uname -m`, downloads the matching `tar.gz` from the `chaosblade-io/chaosblade` `blade-ai-v<version>` Release, extracts it to `~/.blade-ai/versions/blade-ai-v<version>/`, creates a `~/.local/bin/blade-ai` symlink, and appends `~/.local/bin` to your shell rc PATH (tagged `# blade-ai` so `uninstall.sh` can clean it precisely).

Four platforms are supported: `linux-amd64` / `linux-arm64` / `darwin-amd64` / `darwin-arm64`. **Windows is currently unsupported** — `release-blade-ai.yml` does not produce a Windows binary; `install.ps1` exits with a clear "not yet supported" message pointing users at WSL2 / building from source.

Prebuilt archives are **self-contained**: bundled Python runtime + ChaosBlade v1.8.0 binary + all skill files; extract and run. No system Python or extra dependencies — particularly useful in jump-box / bastion-host environments.

### Download from a GitHub Release directly

If the `chaosblade.io` redirect is not yet configured, or you want offline distribution:

```bash
VERSION=0.1.0
PLATFORM=darwin-arm64    # linux-amd64 / linux-arm64 / darwin-amd64 / darwin-arm64
URL="https://github.com/chaosblade-io/chaosblade/releases/download/blade-ai-v${VERSION}"

curl -fSLO "${URL}/blade-ai-${PLATFORM}.tar.gz"
curl -fSLO "${URL}/checksums.txt"
sha256sum -c --ignore-missing checksums.txt    # verify
tar -xzf "blade-ai-${PLATFORM}.tar.gz"
sudo ln -sf "$PWD/blade-ai/blade-ai" /usr/local/bin/blade-ai
```

### Build from source

```bash
git clone https://github.com/chaosblade-io/chaosblade.git
cd chaosblade/blade-ai
make dev      # install dev deps
make build    # PyInstaller bundle into dist/blade-ai/
./dist/blade-ai/blade-ai version
```

> The current release channel **distributes only via GitHub Releases** — no PyPI (`pip install blade-ai`) or npm (`@blade-ai/tui`) anymore. If you need either, `make build` and then `python -m build` (wheel) / `cd tui && npm publish` to your private registry.

## Uninstall

`uninstall.sh` / `uninstall.ps1` are uploaded next to `install.*` in every `blade-ai-v<version>` release; invocation style mirrors install.

### macOS / Linux — one-liner (recommended)

```bash
# Full uninstall (binary + config + memory + skills + logs)
#
# Note: under curl | bash, stdin is not a tty so the interactive
# y/N prompt is disabled. Uninstall is destructive — you must pass
# --force to confirm explicitly.
curl -fsSL https://chaosblade.io/uninstall-agent.sh | bash -s -- --force

# Dry-run first to see the plan (--dry-run doesn't need --force; it
# never deletes anything anyway)
curl -fsSL https://chaosblade.io/uninstall-agent.sh | bash -s -- --dry-run

# Remove binary + PATH but keep ~/.blade-ai/ user data
curl -fsSL https://chaosblade.io/uninstall-agent.sh | bash -s -- --force --keep-config

# Remove one specific version (other versions and symlink kept in multi-version setups)
curl -fsSL https://chaosblade.io/uninstall-agent.sh | bash -s -- --force --version 0.1.0
```

If the `chaosblade.io` redirect is not yet configured, fetch the script directly from a GitHub Release:

```bash
VERSION=0.1.0
curl -fsSL "https://github.com/chaosblade-io/chaosblade/releases/download/blade-ai-v${VERSION}/uninstall.sh" | bash -s -- --force
```

### macOS / Linux — local script invocation (after install)

```bash
# Default: full uninstall (interactive y/N prompt in a real terminal)
bash <path>/uninstall.sh

# Show the plan but do not delete
bash <path>/uninstall.sh --dry-run

# Remove binary + PATH, but keep user data
bash <path>/uninstall.sh --keep-config

# Remove only one specific version
bash <path>/uninstall.sh --version 0.1.0

# CI-friendly: skip y/N
bash <path>/uninstall.sh --force
```

### Windows

```powershell
# Full uninstall
.\uninstall.ps1

# Keep config
.\uninstall.ps1 -KeepConfig

# Safety check: only proceed if the manifest version matches 0.1.0
.\uninstall.ps1 -Version 0.1.0

# Show plan
.\uninstall.ps1 -DryRun
```

A backup is always taken before mutating shell rc / Windows registry (`~/.zshrc.blade-ai-uninstall.bak` / `~/.blade-ai/path-backup.txt`), so a misclick is recoverable. `--keep-config` only removes install metadata (`install-manifest.json` + `receipt.json`), leaving config intact for a clean reinstall.

### Using a China mirror

```bash
# One-liner respects a custom download base
BLADE_AI_MIRROR=https://your-mirror.example.com/releases/download \
  curl -fsSL https://chaosblade.io/install-agent.sh | bash

# Latest-version resolution can also be redirected to a self-hosted GitHub API mirror (rare)
BLADE_AI_MIRROR_API=https://your-mirror.example.com/api/releases \
  curl -fsSL https://chaosblade.io/install-agent.sh | bash
```

---

## First-time setup

On the first `blade-ai` launch, the TUI runs a 5-step onboarding wizard. Each step asks one thing, offers a sensible default, and masks sensitive input:

1. **LLM API key** — paste your key (works with Alibaba Cloud Bailian and any OpenAI-compatible endpoint); echo is masked
2. **Model selection** — pick the reasoning model (recommend `qwen-max-latest`, `qwq-32b`, or any deep-reasoning-capable model)
3. **Cluster config** — auto-scans `~/.kube/` for kubeconfigs; pick defaults for cluster + namespace
4. **Permission mode** — pick the default permission mode (confirm / auto / plan); confirm is recommended for day-to-day use
5. **Environment doctor** — auto-detects four things:
   - ✓ Local Blade binary (`~/.blade-ai/vendor/blade`)
   - ✓ K8s connectivity (`kubectl cluster-info`)
   - ✓ ChaosBlade Operator install (`kubectl get pods -n chaosblade`)
   - ✓ Skill file integrity (hash check on `~/.blade-ai/skills/`)

If the ChaosBlade Operator is missing, the wizard offers to install it (Helm recommended, `kubectl apply` fallback) — or skip without blocking; subsequent injection attempts will prompt again.

Config is written to `~/.blade-ai/config.json`. The agent greets you immediately and enters the chat loop — no restart needed.

---

## Conversational TUI

Running `blade-ai` drops you straight into the chat UI.

### Three permission modes (cycle with `Shift+Tab`)

| Mode | Behavior | Use case |
|------|----------|----------|
| 🔒 Confirm (default) | Show the plan and wait for human approval before executing | Daily drills; prevent fat-finger mistakes |
| ⚡ Auto | Skip the confirmation gate, execute directly | CI/CD pipelines, trusted test environments |
| 📋 Plan | Read-only — produces the plan but never runs an injection | Plan reviews, capability exploration, learning |

The footer bar shows the current mode in real time (green = confirm / yellow = auto / gray = plan) along with the active cluster + namespace.

### Six conversational intents

The agent recognizes six distinct kinds of chat input, each routed differently:

| Intent | User says | Agent does |
|--------|-----------|-----------|
| Inject fault | "Inject 80% CPU pressure" | Runs the full pipeline (plan → safety → confirm → execute → verify) |
| Capability probe | "What can you do?" | Lists capabilities, guides the user toward specifics |
| Fuzzy intent | "Show me how the system holds up under pressure" | Multi-turn conversation to narrow the intent |
| Recover | "Recover that last experiment" | Runs the recover graph |
| Query | "What experiments are active right now?" | Queries and answers directly |
| Exit | "Bye" / "Exit" | Leaves the TUI |

### Slash command palette

A keyboard-friendly companion to free-form chat:

| Command | Function |
|---------|----------|
| `/faults` | List every supported fault type, grouped by Pod / Workload / Service / Node / Storage |
| `/skills` | List installed skill packs with metadata |
| `/skills info <name>` | Detail view (scenarios supported, prerequisites, dependencies) |
| `/skills search <keyword>` | Fuzzy search against the skill marketplace |
| `/recover` | Recover a specific task, or the most recent experiment |
| `/config` | View / edit config (API key, model, cluster, permission mode) |
| `/history` | Drill history + experiment records |
| `/help` | Help text (keybindings + commands + config summary) |
| `/exit` | Leave the TUI |

### Keybindings

| Key | Function |
|-----|----------|
| `Shift+Tab` | Cycle permission mode |
| `Ctrl+C` | Interrupt the current operation (triggers safe recovery during an active injection) |
| `Ctrl+R` | Search command history |
| `Ctrl+O` | Toggle "constrain height" mode (long-output overflow handling) |
| `\` + Enter | Multi-line input mode |
| `@` | Trigger K8s resource auto-completion (Deployment / Service / Pod names) |

---

## Structured CLI

For scripting and CI/CD integration — a deterministic execution path.

### Three injection modes compared

| Mode | Flag | LLM involvement | Speed | When to use |
|------|------|-----------------|-------|-------------|
| Natural-language | `-i "description"` | Yes (plan + execute + verify) | Slow 15–30s | Exploration, fuzzy intent, **label-selector support** |
| Structured params | `--scope/--target/--action ...` | Yes (execute + verify only) | Medium 8–15s | Parameters are known |
| Direct | `--scope ... --direct` | No (only verification stage) | Fast 2–5s | CI/CD, automation, fast injection |

### Constraints

- `--direct` and `--input` are mutually exclusive
- `--direct` requires the full set of structured params (`scope` + `target` + `action` + `target-name`; Pod/Container scope also needs `namespace`)
- `--stream` only works with natural-language mode
- Node-level scenarios do not need `--namespace`

### Common flags

| Flag | Short | Description | Example |
|------|-------|-------------|---------|
| `--scope` | - | Scope: node / pod / container | `--scope pod` |
| `--target` | - | Fault target: cpu / mem / network / disk / process / pod | `--target cpu` |
| `--action` | - | Fault action: fullload / load / delay / loss / fill / burn / kill / stop / delete / dns | `--action fullload` |
| `--target-name` | `-n` | **Resource name** (Pod name / Node name), **not** a label selector | `-n "accounting-6fbdb464c7-qn2vr"` |
| `--namespace` | `--ns` | K8s namespace | `--namespace cms-demo` |
| `--params` | `-p` | Key=value parameters, comma-separated; bare keys are boolean flags | `-p "cpu-percent=80"` |
| `--duration` | `-d` | Duration in seconds (maps to blade `--timeout`, default 60) | `-d 600` |
| `--direct` | - | Skip LLM and execute directly | `--direct` |
| `--kubeconfig` | - | Path to kubeconfig | `--kubeconfig ~/.kube/config` |
| `--confirm` | - | Require human confirmation before injecting | `--confirm` |
| `--input` | `-i` | Natural-language description | `-i "inject CPU fault"` |

### Parameter mapping

| CLI flag | Maps to blade arg | Notes |
|----------|------------------|-------|
| `--target-name` / `-n` | `--names` | **Always** a resource name, never a label selector |
| `-p "key=value"` | `--key value` | Key/value pair |
| `-p "flag"` (bare key) | `--flag` | Boolean flag |
| `-d 600` | `--timeout 600` | Automatically appended |

> **`-n/--target-name` vs label selectors**
>
> `-n` maps to ChaosBlade's `--names` flag, which expects an **exact resource name** (e.g. Pod name `accounting-6fbdb464c7-qn2vr`), not a label selector (e.g. `opentelemetry.io/name=accounting`).
>
> - **Pod scope**: `-n` is the Pod name; blade emits `--names <pod-name>`
> - **Node scope**: `-n` is the Node name; blade emits `--names <node-name>`
> - **Locating Pods by label selector**: natural-language mode only — the agent converts the label to a `--labels` parameter via LLM
>
> To get the Pod name:
> ```bash
> kubectl get pods -n cms-demo -l "opentelemetry.io/name=accounting" \
>   --kubeconfig ~/.kube/config -o jsonpath='{.items[*].metadata.name}'
> ```

---

## Three injection modes in detail

### Natural-language mode (recommended; supports label selectors)

```bash
# Inject CPU fullload into a service (the agent converts "accounting service" into a label selector)
blade-ai inject -i "inject CPU fullload at 80% into the accounting service in the cms-demo namespace" \
  --kubeconfig ~/.kube/config

# Fuzzy intent — the agent will ask clarifying questions
blade-ai inject -i "let's see how the system holds up under pressure" \
  --kubeconfig ~/.kube/config
```

### Structured-parameter mode (uses Pod names)

```bash
# CPU pressure
blade-ai inject \
  --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 \
  --kubeconfig ~/.kube/config

# Memory pressure
blade-ai inject \
  --scope pod --target mem --action load \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "mode=ram,mem-percent=90" -d 600 \
  --kubeconfig ~/.kube/config

# Network latency
blade-ai inject \
  --scope pod --target network --action delay \
  -n "payment-5d979b947f-mht6v" --namespace cms-demo \
  -p "time=3000,interface=eth0" -d 600 \
  --kubeconfig ~/.kube/config
```

### Direct mode (`--direct`, zero LLM calls, fastest)

```bash
blade-ai inject \
  --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 --direct \
  --kubeconfig ~/.kube/config
```

> **Direct path vs LLM path**
>
> Both paths share the exact same safety review and verification flow; they only differ in how the plan is produced:
>
> | Path | Planning method | LLM involvement | Latency | Determinism | When to use |
> |------|-----------------|-----------------|---------|-------------|-------------|
> | Direct | Deterministic skill activation + parameter assembly | Zero LLM calls | Controlled | Predictable | CI/CD pipelines, scheduled tasks |
> | LLM | LLM understands intent → matches skill → generates plan | LLM inference | Hundreds of ms to seconds | Flexible but non-deterministic | Interactive debug, fuzzy intent |

Direct path exists because LLM planning has two problems in automation: latency is unbounded and output is non-deterministic. Direct is zero-LLM end-to-end — bounded latency, predictable result — suitable for embedding in CI/CD pipelines.

---

## Fault scenarios

The built-in `k8s-chaos-skills` skill pack covers **19 fault scenarios** across 5 layers.

### Pod layer (8 scenarios)

| Scenario | Root cause | ChaosBlade command | Verification |
|----------|-----------|-------------------|--------------|
| CPU over-utilization | Tight loop / high concurrency | `pod-cpu fullload` | `kubectl top pod` CPU% ≥ target |
| OOM (memory anomaly) | Memory pressure | `pod-mem load` | Pod OOMKilled event or memory% target |
| Disk space over-utilization | Log / data accumulation | `pod-disk fill` | In-Pod `df -h` ≥ target |
| Disk IO over-utilization | Abnormal IO load | `pod-disk burn` | `iostat` %util rises |
| Network latency | Service-call timeout | `pod-network delay` | Target Pod latency ≥ configured |
| Network packet loss | Communication loss | `pod-network loss` | Loss rate ≥ configured percent |
| DNS failure | Resolution failure | `pod-network dns` | DNS timeout / failure |
| Image pull failure | Missing image / wrong tag | `kubectl patch` | Pod ImagePullBackOff state |

### Workload layer (3 scenarios)

| Scenario | Root cause | Method | Verification |
|----------|-----------|--------|--------------|
| Replica scaled down | Human error | `kubectl scale --replicas=0` | Pod count drops, endpoints shrink |
| HPA at max | Resource saturation | `pod-cpu fullload` triggers scale-out | HPA REPLICAS == MAXPODS |
| DaemonSet incomplete scheduling | Node unschedulable | `kubectl cordon` | DESIRED != READY; missing Pod on target node |

### Service layer (1 scenario)

| Scenario | Root cause | Method | Verification |
|----------|-----------|--------|--------------|
| Load-balancer anomaly | Backend unreachable | `pod-network loss` / `process kill` | Endpoints IP list shrinks; requests 502/503 |

### Node layer (5 scenarios)

| Scenario | Root cause | Method | Verification |
|----------|-----------|--------|--------------|
| Node CPU over-utilization | Anomalous process | `node-cpu fullload` | `kubectl top node` CPU% ≥ target |
| Node memory over-utilization | Anomalous process | `node-mem load` | `kubectl describe node` MemoryPressure=True |
| Node disk IO over-utilization | Abnormal IO load | `node-disk burn` | Node iowait rises; app r/w latency increases |
| Container runtime disk over-utilization | Logs / tmpfiles pile up | `node-disk fill` | `df -h` partition > 85%; Pods may be Evicted |
| Node unavailable | Node down | `kubectl cordon` + taint | Node NotReady; Pods migrate |

### Storage layer (1 scenario)

| Scenario | Root cause | Method | Verification |
|----------|-----------|--------|--------------|
| PVC Pending | StorageClass misconfig | `kubectl patch pvc` StorageClass | PVC status=Pending; events show "storageclass not found" |

### Standard ChaosBlade scenario commands

#### Pod CPU over-utilization (`pod / cpu / fullload`)
```bash
blade-ai inject -i "inject CPU fullload at 80% into the accounting service in cms-demo" --kubeconfig ~/.kube/config
blade-ai inject --scope pod --target cpu --action fullload -n "accounting-xxx" --namespace cms-demo -p "cpu-percent=80" -d 600 --kubeconfig ~/.kube/config
blade-ai inject --scope pod --target cpu --action fullload -n "accounting-xxx" --namespace cms-demo -p "cpu-percent=80" -d 600 --direct --kubeconfig ~/.kube/config
```

#### Pod OOM memory anomaly (`pod / mem / load`)
```bash
blade-ai inject --scope pod --target mem --action load -n "accounting-xxx" --namespace cms-demo -p "mode=ram,mem-percent=90" -d 600 --kubeconfig ~/.kube/config
```

#### Pod disk space (`pod / disk / fill`)
```bash
blade-ai inject --scope pod --target disk --action fill -n "accounting-xxx" --namespace cms-demo -p "path=/tmp,size=10240" -d 600 --kubeconfig ~/.kube/config
```
> `size` must be a pure integer in MB; `10g` and `10240m` are not accepted.

#### Pod disk IO (`pod / disk / burn`)
```bash
blade-ai inject --scope pod --target disk --action burn -n "accounting-xxx" --namespace cms-demo -p "path=/tmp,read,write" -d 600 --kubeconfig ~/.kube/config
```
> `read` and `write` are boolean flags — pass them as bare keys in `-p` (no `=value`).

#### Pod Terminating / process hang (`pod / process / stop`)
```bash
blade-ai inject --scope pod --target process --action stop -n "accounting-xxx" --namespace cms-demo -p "process=java" -d 600 --kubeconfig ~/.kube/config
```

#### Pod network latency (`pod / network / delay`)
```bash
blade-ai inject --scope pod --target network --action delay -n "payment-xxx" --namespace cms-demo -p "time=3000,interface=eth0" -d 600 --kubeconfig ~/.kube/config
```

#### Pod network packet loss (`pod / network / loss`)
```bash
blade-ai inject --scope pod --target network --action loss -n "checkout-xxx" --namespace cms-demo -p "percent=60" -d 600 --kubeconfig ~/.kube/config
```
> Prefer `local-port` / `remote-port` to scope the blast radius; avoid all-port loss.

#### Pod DNS failure (`pod / network / dns`)
```bash
blade-ai inject --scope pod --target network --action dns -n "cart-xxx" --namespace cms-demo -p "domain=example.com,ip=1.1.1.1" -d 600 --kubeconfig ~/.kube/config
```

#### Node CPU over-utilization (`node / cpu / fullload`)
```bash
blade-ai inject --scope node --target cpu --action fullload -n "cn-hongkong.10.0.1.101" -p "cpu-percent=90" -d 600 --kubeconfig ~/.kube/config
```
> Node scope does not take `--namespace`.

#### Node memory over-utilization (`node / mem / load`)
```bash
blade-ai inject --scope node --target mem --action load -n "cn-hongkong.10.0.1.101" -p "mode=ram,mem-percent=95" -d 600 --kubeconfig ~/.kube/config
```

#### Node disk IO over-utilization (`node / disk / burn`)
```bash
blade-ai inject --scope node --target disk --action burn -n "cn-hongkong.10.0.2.69" -p "path=/tmp,read,write" -d 600 --kubeconfig ~/.kube/config
```

#### Container runtime disk over-utilization (`node / disk / fill`)
```bash
blade-ai inject --scope node --target disk --action fill -n "cn-hongkong.10.0.1.101" -p "path=/var/log,percent=90" -d 600 --kubeconfig ~/.kube/config
```
> Prefer `percent` (e.g. `percent=90`) to reliably trigger DiskPressure (>85%).

### kubectl operation scenarios (5; natural-language mode only)

These use `kubectl` rather than ChaosBlade under the hood and **do not** support structured params or `--direct`:

```bash
# Node unavailable
blade-ai inject -i "cordon node cn-hongkong.10.0.1.101" --kubeconfig ~/.kube/config

# Workload replicas scaled down
blade-ai inject -i "scale the accounting service in cms-demo to 0 replicas" --kubeconfig ~/.kube/config

# DaemonSet incomplete scheduling
blade-ai inject -i "cordon a node so the DaemonSet can't schedule a Pod onto it" --kubeconfig ~/.kube/config

# PVC Pending — wrong StorageClass
blade-ai inject -i "patch a PVC to point at a non-existent StorageClass to simulate misconfiguration" --kubeconfig ~/.kube/config

# Pod image pull failure
blade-ai inject -i "simulate an image pull failure on the frontend service in cms-demo" --kubeconfig ~/.kube/config
```

---

## Recovery & fallback

```bash
# Recover through blade-ai (two-layer verification)
blade-ai recover --task-id <task-id> --kubeconfig ~/.kube/config

# Force-clean residual experiments (fallback after process crash)
blade-ai recover --task-id <task-id> --force --kubeconfig ~/.kube/config

# Last-resort: recover via raw blade (when blade-ai itself is unavailable)
kubectl exec <tool-pod> -n chaosblade -- blade destroy <blade-uid> --kubeconfig=~/.kube/config
```

Recovery has its own two-layer verification:

- **Layer 1**: `blade destroy <uid>` returns success
- **Layer 2**: kubectl polls until the target resource looks normal (CPU/memory back down, endpoints restored, image pull succeeds)

For abnormal scenarios (process crash, lost blade UID), `--force` deletes the ChaosBlade CR directly from K8s, bypassing the `blade` binary — a fallback, not the normal path.

---

## Server mode & API

Start a FastAPI HTTP API for multi-team sharing or upstream platform integration:

```bash
blade-ai-server                   # FastAPI service on default port 8000
blade-ai-server --port 8089       # custom port
blade-ai-server --host 0.0.0.0    # listen externally
```

### REST + SSE endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/inject` | POST | Submit a fault-injection request |
| `/api/v1/inject/stream` | POST (SSE) | Inject + real-time streaming output |
| `/api/v1/recover` | POST | Recover a specific task |
| `/api/v1/confirm` | POST | Approve / reject an injection plan |
| `/api/v1/tasks/{id}/stream` | GET (SSE) | Real-time tracking of task progress |
| `/api/v1/metric/{id}` | GET | Task metrics (timeline, durations, token consumption) |
| `/api/v1/skills` | GET | List installed skills |
| `/api/v1/sessions` | POST | Create a TUI session (used by TS TUI) |
| `/api/v1/sessions/{id}` | DELETE | Destroy a session (called on CLI exit) |
| `/api/v1/sessions/{id}/state` | GET | Fetch session metadata |
| `/api/v1/sessions/{id}/turn` | POST (SSE) | Unified SSE turn endpoint (TS TUI core) |
| `/api/v1/sessions/{id}/interrupt` | POST | Interrupt response (confirm / cancel / answer) |
| `/api/v1/sessions/{id}/cancel` | POST | Cancel current turn (triggered by Ctrl+C) |
| `/api/v1/health` | GET | Health check + protocol-version negotiation |

### SSE event types

| Event | Description |
|-------|-------------|
| `token` | LLM token-by-token output |
| `thinking` | Reasoning trace, separate from token events; the client may show or hide it |
| `tool_start` / `tool_end` | Tool invocation start / end |
| `node_start` / `node_end` | Graph node enter / exit |
| `confirm` | Awaiting human approval |
| `result` | Final result |
| `error` | Exception |

### Protocol versioning

The `X-Blade-Protocol-Version` response header carries the protocol version. The TUI bundle locks `TUI_PROTOCOL_VERSION` at compile time and prints a non-fatal warning on mismatch at boot.

### HTTP examples

```bash
# List skills
curl http://localhost:8089/api/v1/skills

# Inject
curl -X POST http://localhost:8089/api/v1/inject \
  -H "Content-Type: application/json" \
  -d '{
    "fault_type": "pod-cpu-high",
    "target_type": "pod",
    "target_name": "my-pod",
    "namespace": "default"
  }'

# Stream task progress over SSE
curl -N http://localhost:8089/api/v1/tasks/task-xxx/stream

# Health
curl http://localhost:8089/api/v1/health
```

---

## Configuration

All settings are managed through `blade-ai config`. Precedence (highest first):

```
init args > ~/.blade-ai/config.json > BLADE_AI_* env vars > code defaults
```

```bash
# LLM
blade-ai config set api_key "sk-xxx"                   # API key
blade-ai config set api_base_url "https://..."         # API base URL
blade-ai config set model_name "qwen-max-latest"       # Model name

# Cluster
blade-ai config set kubeconfig "/path/to/kubeconfig"   # Kubeconfig path
blade-ai config set default_namespace "cms-demo"       # Default namespace

# Safety
blade-ai config set safety_blacklist_namespaces "kube-system,kube-public,production"
blade-ai config set confirmation_required true          # Require human confirmation

# Runtime
blade-ai config set mode local                          # local / server
blade-ai config set server_port 8000                    # Server port

# Inspect current config
blade-ai config show
```

### Persistent data

All under `~/.blade-ai/`:

| Path | Contents |
|------|----------|
| `config.json` | User config |
| `logs/` | Runtime logs |
| `skills/` | Skill files (override embedded defaults) |
| `memory/sessions/` | Per-task Session Memory |
| `memory/experiments/` | Cross-task Operational Memory |
| `vendor/blade` | Embedded ChaosBlade binary |

---

## TS TUI environment variables

Only affect TUI process behavior (not persisted to `config.json`) — used to override the default loader, force a language, debug SSE, etc.:

| Variable | Effect |
|----------|--------|
| `BLADE_AI_SERVER` | Connect to a specific remote server URL; skip local spawn |
| `BLADE_AI_TUI=legacy` | Fall back to the legacy Python TUI (prompt_toolkit + Rich) |
| `BLADE_AI_TUI=ts` | Force the TS TUI; fail if the bundle is not found (no silent fallback) |
| `BLADE_AI_TUI_BIN` | Explicit path to the TUI bundle (`.js` or executable shim) |
| `BLADE_AI_PYTHON` | Python interpreter used to spawn the server in embedded mode |
| `BLADE_AI_LANG` | Force language: `zh` or `en`; default auto-detects from `LC_ALL` / `LANG` |
| `BLADE_AI_DEBUG=1` | Dump SSE protocol-parse errors to stderr |
| `BLADE_AI_MIRROR` | Installer download base URL (default: GitHub Releases) |
| `NO_COLOR` | Disable themed colors (follows the [no-color.org](https://no-color.org) convention) |

---

## Common pitfalls cheat sheet

| # | Pitfall | Correct usage |
|---|---------|---------------|
| 1 | Passing a label selector to `-n` in structured / direct mode | `-n` is a Pod name; use natural-language mode for label selectors |
| 2 | `node-disk fill` `size` format | Pure integer MB: `size=10240`; do NOT use `size=10g` |
| 3 | Passing `--namespace` for Node scope | Node scope skips namespace; the CLI does not need it |
| 4 | `--target mem --action fullload` | `pod-mem` uses `load`; only `cpu` uses `fullload` |
| 5 | Network fault with no port filter | Prefer `local-port` / `remote-port` |
| 6 | How to pass boolean flags | Bare key: `-p "path=/tmp,read,write"`; do NOT write `read=true` |
| 7 | Mixing `--direct` and `--input` | Mutually exclusive; pick one mode |
| 8 | `--direct` without `--namespace` | Pod/Container scope must supply a namespace |
| 9 | Forgetting `npm run build` after editing TS | Must regenerate `tui/dist/cli.js` or PyInstaller will ship the stale bundle |
| 10 | `blade status --kubeconfig` errors | v1.8.0 does not accept the flag; pass via `KUBECONFIG` env var instead |

---

## Development guide

### Python backend

```bash
git clone https://github.com/chaosblade-io/chaosblade.git
cd chaosblade/blade-ai
make dev      # install dev deps (pytest, ruff, mypy)
make test     # run tests
make build    # PyInstaller standalone binary into dist/blade-ai/
```

Main directories:

| Path | Contents |
|------|----------|
| `src/chaos_agent/agent/` | LangGraph StateGraph, nodes, Router |
| `src/chaos_agent/cli/` | Typer CLI entrypoint (the `blade-ai` command) |
| `src/chaos_agent/server/` | FastAPI + SSE server (the `blade-ai-server` command) |
| `src/chaos_agent/tools/` | Tool implementations (blade / kubectl / guard / skills) |
| `src/chaos_agent/skills/` | Skill loader (Tier 1/2/3) |
| `src/chaos_agent/memory/` | Three-layer memory system |
| `src/chaos_agent/config/` | Configuration (pydantic-settings) |
| `skills/` | SKILL.md files for the 19 fault scenarios |
| `tests/` | Pytest test suites (80+ files) |

### TS TUI

The TUI render layer lives in `tui/` and talks HTTP + SSE to the Python backend. Requires Node 22+:

```bash
cd tui
npm install
npm run typecheck
npm run build       # → dist/cli.js (Python uses this bundle to launch TS TUI)
npm run dev         # tsx watch — rebuild on source change
npm test            # vitest

# Headless smoke tests (no terminal needed)
node scripts/smoke-i18n.mjs       # locale detection
node scripts/smoke-slash.mjs      # parser + registry + handler dispatch
node scripts/smoke-reducer.mjs    # action ↔ state transitions
```

> **Important**: after changing TS source, you MUST `npm run build` to regenerate `tui/dist/cli.js`, or `blade-ai` will load the stale bundle (the Python side launches the TUI via `tui/dist/cli.js`).

### Release process

Fully driven by `chaosblade/.github/workflows/release-blade-ai.yml`:

```bash
# 1) Bump the 3 version strings (CI verifies)
#    blade-ai/pyproject.toml
#    blade-ai/tui/package.json
#    blade-ai/src/chaos_agent/__init__.py

# 2) Commit and push the tag
git tag blade-ai-v0.1.0
git push origin blade-ai-v0.1.0
```

CI pipeline:

| Stage | Content |
|-------|---------|
| `verify-versions` | Cross-check the 3 version strings against the tag |
| `build-tui` | npm install (--ignore-scripts) → patch-package → typecheck → tsup bundle → vitest, upload `tui-bundle` artifact (`cli.js` + `package.json`) |
| `build` | 4-platform native matrix: linux-amd64 (ubuntu-latest) / linux-arm64 (ubuntu-24.04-arm) / darwin-amd64 (macos-latest + python.org universal2 Python + Rosetta) / darwin-arm64 (macos-latest). Each downloads its matching chaosblade tarball → PyInstaller bundle; macOS ad-hoc codesign |
| `release` | Collect the 4 archives + `checksums.txt` → publish a GitHub Release |

End-to-end ≈25 minutes, producing 4 platform binaries. **Neither PyPI nor npm publishing is wired up in the current config**; distribution is entirely through GitHub Releases + the `install.sh` one-liner.

---

## Feedback & contributing

- **Issues**: [github.com/chaosblade-io/chaosblade/issues](https://github.com/chaosblade-io/chaosblade/issues) (please prefix the title with `[blade-ai]`)
- **DingTalk group**: 23177705
- **Email**: chaosblade.io.01@gmail.com

PRs welcome — see [CONTRIBUTING.md](../../CONTRIBUTING.md).
