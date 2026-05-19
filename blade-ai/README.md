# BLADE AI

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Release](https://img.shields.io/github/v/release/chaosblade-io/chaosblade?filter=blade-ai-v*&label=blade-ai)](https://github.com/chaosblade-io/chaosblade/releases?q=blade-ai-v)

> Kubernetes 混沌工程智能代理 — 说人话就能注入故障，不用背命令。

BLADE AI 是 [ChaosBlade](https://github.com/chaosblade-io/chaosblade) 生态的智能代理层：底层调用 ChaosBlade 执行故障注入，上层增加意图理解、安全审查、效果验证、安全恢复和结构化报告等编排能力，让故障演练从"手写命令"变成"对话完成"。

## 文档导航

- **[介绍文档 → docs/INTRODUCTION.md](docs/INTRODUCTION.md)** — 项目定位、能力矩阵、架构设计、安全体系、技术栈
- **[使用文档 → docs/USAGE.md](docs/USAGE.md)** — 安装、TUI 使用、CLI 命令、19 个故障场景速查、Server 模式、API、配置

下文是最快路径，让你在 5 分钟内跑起来；想了解"为什么这样设计"或"全部能力"请进入上面两份文档。

---

## 安装

发布流水线 `release-blade-ai.yml` 会在 `blade-ai-v*` 标签推送时为五个平台产出自包含的可执行包（内嵌 Python 运行时、ChaosBlade 二进制、技能文件，解压即用）。

### 一键脚本（推荐）

```bash
# macOS / Linux —— 装最新版（chaosblade.io 域名生效后）
curl -fsSL https://chaosblade.io/install-agent.sh | bash

# Windows (PowerShell)
irm https://chaosblade.io/install-agent.ps1 | iex

# 装指定版本（标签里的 bare semver，无 blade-ai-v 前缀）
curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --version 0.1.0
```

如果 `chaosblade.io` 域名跳转尚未配置，可以直接从 GitHub Releases 下载脚本：

```bash
# 直接走 GitHub Release 下载脚本
VERSION=0.1.0
curl -fsSL "https://github.com/chaosblade-io/chaosblade/releases/download/blade-ai-v${VERSION}/install.sh" | bash -s -- --version "${VERSION}"
```

### 手动下载预编译包

每次发布会上传 5 份归档 + `checksums.txt` 到 `blade-ai-v<版本>` Release：

| 平台 | 归档名 |
|------|-------|
| Linux x86_64 | `blade-ai-linux-amd64.tar.gz` |
| Linux ARM64 | `blade-ai-linux-arm64.tar.gz` |
| macOS Intel | `blade-ai-darwin-amd64.tar.gz` |
| macOS Apple Silicon | `blade-ai-darwin-arm64.tar.gz` |
| Windows | `blade-ai-windows-x64.zip` |

```bash
VERSION=0.1.0
PLATFORM=darwin-arm64    # 按本机替换
URL="https://github.com/chaosblade-io/chaosblade/releases/download/blade-ai-v${VERSION}/blade-ai-${PLATFORM}.tar.gz"
curl -fSLO "${URL}"
tar -xzf "blade-ai-${PLATFORM}.tar.gz"
./blade-ai/blade-ai --version
# 把 blade-ai/ 目录加入 PATH，或软链 blade-ai 到 /usr/local/bin
```

### pip / npm

```bash
# pip：内嵌 TS TUI bundle，装完直接 blade-ai
pip install blade-ai==0.1.0

# npm：仅 TS TUI（适合远端已有 server 的场景），需 Node 22+
npm install -g @blade-ai/tui@0.1.0
BLADE_AI_SERVER=http://127.0.0.1:8080 blade-ai-tui
```

### 源码构建

```bash
git clone https://github.com/chaosblade-io/chaosblade.git
cd chaosblade/blade-ai
make dev      # 安装开发依赖
make build    # PyInstaller 打包到 dist/blade-ai/
```

---

## 快速开始

### 首次启动

```bash
blade-ai
```

首次启动会进入 5 步配置向导（对标 Claude Code 的初始化体验）：

1. **LLM API Key** — 支持阿里云百炼、OpenAI 兼容接口；输入回显掩码
2. **模型选择** — 推荐 `qwen-max-latest`、`qwq-32b` 等支持深度推理的模型
3. **集群配置** — 自动扫描 `~/.kube/`，选默认集群和命名空间
4. **权限模式** — 确认 / 自动 / 计划，日常推荐确认模式
5. **环境自检** — Blade 二进制、K8s 连通性、Operator 部署、技能完整性

完成后写入 `~/.blade-ai/config.json`，无需重启即进入对话循环。

### 第一次故障注入

```
💬 你: 帮我在 cms-demo 给 accounting 注入 CPU 压力 80%，持续 5 分钟

🤖 Agent:
  ⚡ 正在分析你的请求...
  ▸ 安全检查 ✓ — cms-demo 不在黑名单，无冲突实验
  ▸ 生成故障计划 ✓ — pod-cpu fullload, cpu-percent=80, timeout=300
  ▸ 等待人工确认...  → 用户输入 yes
  ▸ 执行注入 ✓ — ChaosBlade 实验创建成功 (uid: 4d2e...)
  ▸ 验证注入效果 ✓ — Layer1: blade_status=Running; Layer2: kubectl top pod CPU=82%
  ✅ 注入完成！任务 ID: task-20260507-a1b2c3
```

不需要记 `blade create k8s pod-cpu fullload --cpu-percent 80 --namespace cms-demo …` —— 说你想做什么就行。

### 三种使用形态

```bash
# 1) 对话式 TUI（推荐日常使用）
blade-ai

# 2) 结构化 CLI（适合脚本化）
blade-ai inject --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 --kubeconfig ~/.kube/config

# 3) Direct 模式（CI/CD，零 LLM 调用）
blade-ai inject --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 --direct --kubeconfig ~/.kube/config

# 4) Server 模式（多团队共享）
blade-ai-server   # 默认 8000 端口，FastAPI + SSE
```

详细命令、所有故障场景、Server API 见 **[docs/USAGE.md](docs/USAGE.md)**。

---

## 核心能力

| 维度 | 说明 |
|------|------|
| **意图理解** | 自然语言描述故障意图，自动匹配技能并生成执行计划 |
| **四层安全** | ToolGuard（命令白名单）→ Safety Check（命名空间黑名单）→ Confirmation Gate（人工确认）→ Loop Max（循环上限） |
| **故障注入** | 调用 ChaosBlade 在 K8s 集群中注入真实故障 |
| **两层验证** | Layer 1 操作正确性（确定性） + Layer 2 效果真实性（语义性） |
| **安全恢复** | 独立恢复链路 + `--force` 降级路径 + 三种分支结果 |
| **结构化报告** | 每次演练生成 JSON 报告，支持审计和外部系统集成 |
| **可观测性** | 实时 SSE 流式输出 + Token 追踪 + 执行追踪 |

支持 **19 个故障场景**，覆盖 Pod/Workload/Service/Node/Storage 5 个层级。完整列表见 [docs/USAGE.md#故障场景速查](docs/USAGE.md#故障场景速查)。

---

## 项目结构

```
blade-ai/
├── README.md                  ← 你正在看这里
├── docs/
│   ├── INTRODUCTION.md        ← 项目介绍与架构设计
│   └── USAGE.md               ← 完整使用文档
├── pyproject.toml             ← Python 包定义
├── blade-ai.spec              ← PyInstaller 配置
├── Makefile                   ← dev / test / build
├── src/chaos_agent/           ← Python 后端（LangGraph + FastAPI）
├── tui/                       ← TypeScript + Ink 前端（@blade-ai/tui）
├── skills/                    ← 故障注入技能包
├── scripts/                   ← install.sh / install.ps1
└── tests/                     ← Pytest 测试
```

---

## 开发与发布

### 本地开发

```bash
# Python 后端
cd blade-ai
make dev          # 安装开发依赖（pytest、ruff、mypy）
make test         # 跑测试
make build        # PyInstaller 打包

# TS TUI（独立调试）
cd tui
npm install
npm run dev       # tsx watch，源码改动自动重建
npm test          # vitest
npm run typecheck

# 改完 TS 源码必须 npm run build 重新生成 tui/dist/cli.js，
# 否则 PyInstaller 打的还是旧 bundle
```

### 发布

发布流程由 `chaosblade/.github/workflows/release-blade-ai.yml` 全自动驱动：

```bash
# 1) 同步 4 处版本字符串到目标版本
#    pyproject.toml / tui/package.json / src/chaos_agent/__init__.py
# 2) 提交并打 tag
git tag blade-ai-v0.1.0
git push origin blade-ai-v0.1.0
```

CI 会：

1. **verify-versions** — 比对 3 处版本与标签，不一致则失败
2. **build-tui** — typecheck → bundle → vitest → 烟雾测试，产出 `tui/dist/cli.js`
3. **build (5 平台矩阵)** — 下载 ChaosBlade v1.8.0 → PyInstaller 打包
   - Linux 用 manylinux2014 docker 保证 glibc 2.17 基线
   - macOS ad-hoc codesign
   - 每个矩阵产出 `blade-ai-<platform>.tar.gz` / `.zip`
4. **release** — 聚合产物 + `checksums.txt` 创建 GitHub Release
5. **publish-npm** — `@blade-ai/tui` 推到 npm
6. **publish-pypi** — `blade-ai` wheel + sdist 推到 PyPI（验证 wheel 内嵌 `cli.js`）

整条流水线在 `~25 分钟` 内产出五平台可执行 + 两个公网包。

---

## 反馈与贡献

- **Issues**：[github.com/chaosblade-io/chaosblade/issues](https://github.com/chaosblade-io/chaosblade/issues)（请在标题加 `[blade-ai]` 前缀）
- **钉钉群**：23177705
- **邮箱**：chaosblade.io.01@gmail.com

欢迎 Issue 与 PR，详见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## License

Apache 2.0，详见 [LICENSE](../LICENSE)。
