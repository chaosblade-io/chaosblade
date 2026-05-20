# BLADE AI

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Release](https://img.shields.io/github/v/release/chaosblade-io/chaosblade?filter=blade-ai-v*&label=blade-ai)](https://github.com/chaosblade-io/chaosblade/releases?q=blade-ai-v)

**Languages:** [中文](README.md) | English

> Kubernetes chaos-engineering AI agent — describe a fault in plain English (or Chinese), no need to memorize CLI flags.

BLADE AI is the orchestration layer on top of [ChaosBlade](https://github.com/chaosblade-io/chaosblade): the agent understands intent, runs four-layer safety review, drives the injection, verifies the effect, recovers cleanly, and produces a structured report — so a drill goes from "look up the right flag" to "talk to the agent".

## Documentation map

- **[Introduction → docs/INTRODUCTION_en.md](docs/INTRODUCTION_en.md)** — positioning, capability matrix, architecture, safety model, tech stack
- **[Usage → docs/USAGE_en.md](docs/USAGE_en.md)** — install, TUI, CLI, all 19 fault scenarios, server REST+SSE API, config

The rest of this file is the fastest path to a working install (≈5 min). Read the two longer docs above for "why it's designed this way" and the full capability surface.

---

## Install

The `release-blade-ai.yml` pipeline publishes self-contained binaries (bundled Python runtime + ChaosBlade binary + skill files; extract-and-run) for four platforms whenever a `blade-ai-v*` tag is pushed: `linux-amd64` / `linux-arm64` / `darwin-amd64` / `darwin-arm64`. **Windows is not yet supported.**

### One-liner (recommended)

When no version is given, the script queries the GitHub Releases API and resolves the latest `blade-ai-v*` tag automatically — no need to edit the script for each new release:

```bash
# macOS / Linux — install latest (default; auto-resolves the newest release)
curl -fsSL https://chaosblade.io/install-agent.sh | bash

# Pin a specific version (bare semver, no blade-ai-v prefix)
curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --version 0.1.0-alpha

# Same via env var (works through irm | iex / docker / CI)
BLADE_AI_VERSION=0.1.0-alpha curl -fsSL https://chaosblade.io/install-agent.sh | bash
```

> **Windows:** `install.ps1` is in place but the current release matrix does not ship a Windows binary; the script prints a clear "not yet supported" message and points you at WSL2 / building from source. Once a Windows matrix entry lands, `irm | iex` will work immediately with the same latest-version auto-resolution.

If the `chaosblade.io` redirect is not configured yet, fetch the installer directly from a GitHub Release:

```bash
# Fetch the install script straight from the GitHub Release
VERSION=0.1.0-alpha
curl -fsSL "https://github.com/chaosblade-io/chaosblade/releases/download/blade-ai-v${VERSION}/install.sh" | bash -s -- --version "${VERSION}"
```

### Download a prebuilt archive manually

Each release uploads 4 archives + `checksums.txt` to the `blade-ai-v<version>` Release:

| Platform | Archive |
|----------|---------|
| Linux x86_64 | `blade-ai-linux-amd64.tar.gz` |
| Linux ARM64 | `blade-ai-linux-arm64.tar.gz` |
| macOS Intel | `blade-ai-darwin-amd64.tar.gz` |
| macOS Apple Silicon | `blade-ai-darwin-arm64.tar.gz` |

```bash
VERSION=0.1.0-alpha
PLATFORM=darwin-arm64    # match your host
URL="https://github.com/chaosblade-io/chaosblade/releases/download/blade-ai-v${VERSION}/blade-ai-${PLATFORM}.tar.gz"
curl -fSLO "${URL}"
tar -xzf "blade-ai-${PLATFORM}.tar.gz"
./blade-ai/blade-ai version
# Add the blade-ai/ dir to PATH, or symlink blade-ai into /usr/local/bin
```

### Uninstall

Companion uninstall scripts mirror the install layout — use the right one for your OS:

```bash
# macOS / Linux — full uninstall by default (binary + config + PATH)
bash <path>/uninstall.sh

# Show the plan but do not delete anything
bash <path>/uninstall.sh --dry-run

# Remove the binary + PATH entry but keep ~/.blade-ai/ (config / memory / skills)
bash <path>/uninstall.sh --keep-config

# Remove a single version (in multi-version setups; symlink and other versions retained)
bash <path>/uninstall.sh --version 0.1.0-alpha
```

```powershell
# Windows (script is in place — works once install.ps1 ships a real binary)
.\uninstall.ps1                          # full uninstall
.\uninstall.ps1 -KeepConfig              # keep config dir
.\uninstall.ps1 -Version 0.1.0-alpha     # safety check: only proceed if manifest matches
.\uninstall.ps1 -DryRun                  # show the plan, no deletion
```

Every shell-rc / registry edit takes a sibling backup (`~/.zshrc.blade-ai-uninstall.bak` / `~/.blade-ai/path-backup.txt`), so a misclick is recoverable.

### Build from source

```bash
git clone https://github.com/chaosblade-io/chaosblade.git
cd chaosblade/blade-ai
make dev      # install dev deps
make build    # PyInstaller bundle into dist/blade-ai/
```

---

## Quick start

### First launch

```bash
blade-ai
```

The first run walks you through a 5-step onboarding wizard (similar feel to Claude Code's first launch):

1. **LLM API key** — supports Alibaba Cloud Bailian and any OpenAI-compatible endpoint; echo is masked
2. **Model selection** — recommend `qwen-max-latest`, `qwq-32b`, or anything that supports deep reasoning
3. **Cluster config** — auto-scans `~/.kube/` and picks defaults for cluster + namespace
4. **Permission mode** — confirm / auto / plan; recommend confirm for daily use
5. **Environment doctor** — verifies the Blade binary, K8s connectivity, ChaosBlade Operator install, skill files

The wizard writes `~/.blade-ai/config.json` and the agent enters the chat loop immediately — no restart needed.

### Your first injection

```
💬 You: inject 80% CPU pressure into the accounting service in cms-demo for 5 minutes

🤖 Agent:
  ⚡ Analyzing request...
  ▸ Safety check ✓ — cms-demo is not blacklisted, no overlapping experiments
  ▸ Plan generated ✓ — pod-cpu fullload, cpu-percent=80, timeout=300
  ▸ Waiting for human confirmation...  → user types yes
  ▸ Executing ✓ — ChaosBlade experiment created (uid: 4d2e...)
  ▸ Verifying effect ✓ — Layer 1: blade_status=Running; Layer 2: kubectl top pod CPU=82%
  ✅ Injection complete! Task ID: task-20260507-a1b2c3
```

No need to memorize `blade create k8s pod-cpu fullload --cpu-percent 80 --namespace cms-demo …` — just say what you want done.

### Three usage modes

```bash
# 1) Conversational TUI (recommended for interactive use)
blade-ai

# 2) Structured CLI (good for scripting)
blade-ai inject --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 --kubeconfig ~/.kube/config

# 3) Direct mode (zero LLM calls — best for CI/CD)
blade-ai inject --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 --direct --kubeconfig ~/.kube/config

# 4) Server mode (multi-team shared deployment)
blade-ai-server   # FastAPI + SSE, default port 8000
```

Full command reference, fault scenarios, and Server API live in **[docs/USAGE_en.md](docs/USAGE_en.md)**.

---

## Core capabilities

| Dimension | Description |
|-----------|-------------|
| **Intent understanding** | Describe a fault in natural language; agent matches a skill and assembles parameters |
| **Four-layer safety** | ToolGuard (command allowlist) → Safety Check (namespace blacklist) → Confirmation Gate (human-in-the-loop) → Loop Max (per-phase iteration caps) |
| **Fault injection** | Drives ChaosBlade to produce real failures in your K8s cluster |
| **Two-layer verification** | Layer 1 operation correctness (deterministic) + Layer 2 effect reality (semantic) |
| **Safe recovery** | Independent recover graph + `--force` fallback path + 3 branch outcomes (success / failure / lost) |
| **Structured reports** | Every drill emits a JSON report, suitable for audit and downstream pipelines |
| **Observability** | Real-time SSE streaming + token tracking + execution trace |

Supports **19 fault scenarios** across 5 layers: Pod / Workload / Service / Node / Storage. See [docs/USAGE_en.md#fault-scenarios](docs/USAGE_en.md#fault-scenarios) for the full list.

---

## Project layout

```
blade-ai/
├── README.md                  ← Chinese entry point
├── README_en.md               ← you are here
├── docs/
│   ├── INTRODUCTION.md / _en.md   ← project intro + architecture
│   └── USAGE.md / _en.md          ← full usage guide
├── pyproject.toml             ← Python package definition
├── blade-ai.spec              ← PyInstaller spec
├── Makefile                   ← dev / test / build
├── src/chaos_agent/           ← Python backend (LangGraph + FastAPI)
├── tui/                       ← TypeScript + Ink frontend (embedded into the PyInstaller bundle)
├── skills/                    ← fault-injection skill packs
├── scripts/                   ← install.sh / install.ps1 / uninstall.{sh,ps1}
└── tests/                     ← pytest suites
```

---

## Develop & release

### Local development

```bash
# Python backend
cd blade-ai
make dev          # install dev deps (pytest, ruff, mypy)
make test         # run tests
make build        # PyInstaller bundle

# TS TUI (standalone iteration)
cd tui
npm install
npm run dev       # tsx watch — rebuild on source change
npm test          # vitest
npm run typecheck

# After changing TS, you MUST `npm run build` to refresh tui/dist/cli.js,
# otherwise PyInstaller will package the stale bundle.
```

### Release

The release flow is fully automated by `chaosblade/.github/workflows/release-blade-ai.yml`:

```bash
# 1) Bump the 3 version strings in lockstep to the target version
#    pyproject.toml / tui/package.json / src/chaos_agent/__init__.py
# 2) Commit and push a tag
git tag blade-ai-v0.1.0-alpha
git push origin blade-ai-v0.1.0-alpha
```

CI will then:

1. **verify-versions** — compare the 3 version strings against the tag; fail on any drift
2. **build-tui** — typecheck → tsup bundle → vitest → upload `tui-bundle` artifact (contains `cli.js` + the `{"type":"module"}` marker `package.json`)
3. **build (4-platform matrix)** — download ChaosBlade v1.8.0 → PyInstaller bundle
   - linux/amd64: native build on ubuntu-latest (glibc 2.39 baseline)
   - linux/arm64: native build on ubuntu-24.04-arm
   - darwin/amd64: macos-latest (Apple Silicon host) + python.org universal2 Python + `arch -x86_64` Rosetta to produce an x86_64 bundle
   - darwin/arm64: native build on macos-latest + ad-hoc codesign
   - Each matrix entry uploads `blade-ai-<os>-<arch>.tar.gz`
4. **release** — collect the 4 archives + `checksums.txt` and publish a GitHub Release

End-to-end ≈25 minutes, producing 4 platform binaries. **No npm or PyPI publishing in the current configuration.**

---

## Feedback & contributing

- **Issues**: [github.com/chaosblade-io/chaosblade/issues](https://github.com/chaosblade-io/chaosblade/issues) (please prefix the title with `[blade-ai]`)
- **DingTalk group**: 23177705
- **Email**: chaosblade.io.01@gmail.com

PRs welcome — see [CONTRIBUTING.md](../CONTRIBUTING.md).

## License

Apache 2.0 — see [LICENSE](../LICENSE).
