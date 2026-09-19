<h1 align="center">BLADE AI</h1>

<!-- 仓库内的链接与图片使用 Markdown 语法而非 HTML 标签：托管平台会把 Markdown 里的
     相对路径重写为 /blob/，但不处理 HTML 标签里的路径 —— 那些会落到 /raw/，返回原始
     字节而非渲染页面，于是 <a href> 跳到源码文本、<img src> 什么都不显示。
     居中排版不值得用「页面打不开」来换。 -->

[English](README.md) · 简体中文

<p align="center">
  <strong>用自然语言做混沌演练 —— 安全、可验证、且保证能恢复。</strong>
</p>

<p align="center">
  面向 Kubernetes 与主机的混沌工程智能代理。你用自然语言描述一个故障，BLADE AI 负责规划它、
  通过纯规则安全门禁做审查、经由 <a href="https://github.com/chaosblade-io/chaosblade">ChaosBlade</a>
  注入、验证故障是否真的生效，并确定性地恢复 —— 每一次运行都走完整的
  <em>意图 → 安全 → 注入 → 验证 → 恢复</em> 闭环。
</p>

[![Apache 2.0 License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](NOTICE) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-brightgreen.svg) ![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-orange.svg) ![88 scenarios](https://img.shields.io/badge/scenarios-88-9cf.svg)

[为什么是 BLADE AI](#为什么是-blade-ai) · [工作原理](#工作原理) · [快速开始](#快速开始) · [故障场景](#故障场景) · [四种接口](#四种接口) · [安全防护](#安全防护) · [架构](#架构) · [完整用法](docs/USAGE.md)

---

## 为什么是 BLADE AI

所有 Agent 本质上是同一样东西：一个套在通用 LLM 外面的 ReAct 循环（推理 / 行动 / 观察）。既然底座相同，差异就只剩下**工具**和**上下文** —— 所以「通用 Agent + 技能」原则上也能做混沌工程。推理由模型决定、工程无法撼动；能被我们塑造的只有**行动**和**观察**。垂类 Agent 就是把某一个领域的行动与观察做到极致 —— 做的是通用 Agent + 技能**做不可靠的三件事**：

- 🎯 **确定性，而非一个大循环** —— 演练顺序是固定的图结构，不由模型决定。智能被约束进三个独立循环（规划 / 执行 / 验证），且每个阶段各有自己的守卫：规划只能看、绝不能动，执行是唯一被允许变更的阶段。通用 Agent 只有一个循环，所以只有一套权限 —— 变更要么处处允许，要么处处禁止。
- 🛡️ **摁住会幻觉的模型** —— 当一个目标注入不进去，LLM 会「机智地」换一个。BLADE AI 在确认时冻结已批准目标，并拿它审查每一次调用：*方式可变，身份不可变*。
- ♻️ **集群没有撤销键** —— 每个实验都携带一个强制超时，到期即使 Agent 崩溃也会自毁。而既然无法撤销，你就必须知道改了什么：BLADE AI 在注入前给环境拍快照、验证后做差分，让你看到这次故障**在目标之外**碰到了什么。通用 Agent 确认的是"目标坏了"；只有差分能看到爆炸半径。

那些通用管道 —— 记忆压缩、技能渐进加载 —— 不是重点，通用 Agent 也有。重点是把「能跑起来」变成「能稳定、安全地跑」。Vibe coding 已极大提升了开发吞吐，但要交付*稳定*的东西，仍需要原本资深 SRE 才能做的韧性测试。BLADE AI 就是为了降低这道门槛 —— 让混沌工程足够安全、足够简单，简单到任何人都能做。

> 一句话：通用 Agent 让「能做」变得无处不在；垂类 Agent 让「稳定且安全地做」成为可能。

---

## 工作原理

BLADE AI 用一个确定性的 LangGraph 状态机编排演练的全生命周期。自然语言与结构化参数两种入口形式都被解析为同一份故障意图，此后每一次运行都走完全相同的有序骨架，并在同一个 `safety_check` 处接受安全审查。

![三阶段 ReAct 链路：规划、安全审查、确认门禁、执行、验证，以及独立的恢复子图](assets/pipeline.png)

| 阶段 | 职责 | 关键设计 |
| --- | --- | --- |
| **Phase 1 · 规划** | 理解意图、匹配技能、生成故障计划 | `FULL` Prompt；只读规划工具 —— 调不到 `blade_create` |
| **Safety Check** | 命名空间黑名单、冲突检测、目标有效性、爆炸半径打分 | 纯规则引擎，链路中无 LLM |
| **Confirm Gate** | 注入前的人工授权 | 动态、节点级 `interrupt()`，以 `Command(resume=…)` 恢复 |
| **Baseline** | 采集注入前指标与环境快照 | 验证变成前后对比，而不是拿阈值猜 |
| **Phase 2 · 执行** | 调用 ChaosBlade / kubectl 注入 | `MINIMAL` Prompt；每次工具调用都过目标漂移护栏 |
| **Phase 3 · 验证** | 两层效果验证 | L1 确定性 `blade_status` · L2 LLM 语义判断（`VERIFICATION`） |
| **Side-effect Detect** | 用演练后的状态与快照做差分 | 暴露目标之外的影响 |
| **Recover** | 独立编译的恢复图 | 自带 ReAct 循环 + 两层验证 + `--force` 降级路径 |

每一个 super-step 都以 `thread_id = task_id` 做检查点，因此崩溃或被中断的运行能从它离开的那个节点精确续跑。分阶段的循环上限（`100 / 100 / 60`）与全局 `recursion_limit`（`500`）为失控行为兜底。

---

## 快速开始

### 1. 安装

macOS / Linux：

```bash
# 安装最新版本
curl -fsSL https://chaosblade.io/install-agent.sh | bash

# 指定版本
curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --version 0.6.0
```

预编译包内嵌 Python 运行时、ChaosBlade 二进制和全部技能文件，解压即用、零依赖。支持 linux-amd64 / linux-arm64 / darwin-amd64 / darwin-arm64 四个平台。安装脚本自动完成 SHA256 校验、PATH 配置和 receipt 记录。

> Windows 暂未提供预编译包，可通过 WSL2 使用 bash 安装脚本。

### 2. 前置条件

- **kubectl** 已配置且能访问目标集群
- **ChaosBlade Operator** 已部署到集群（`kubectl get pods -n chaosblade`）
- **LLM API Key**（OpenAI 兼容接口，默认 DashScope）

### 3. 配置

```bash
blade-ai config set llm_api_key sk-xxx        # 设置 API Key
blade-ai config set model_name qwen3.7-plus   # 设置模型（默认：qwen3.7-plus）
blade-ai config                               # 查看全部配置
```

配置优先级：初始化参数 > `~/.blade-ai/config.json` > 环境变量（`BLADE_AI_*`）> 默认值。命名空间黑名单默认为空，如需收紧可设置 `BLADE_AI_SAFETY_BLACKLIST_NAMESPACES=kube-system,...`。

### 4. 第一次故障注入

```bash
# 自然语言模式
blade-ai inject -i "给 default 命名空间的 my-pod 注入 80% CPU 压力，持续 120 秒"

# 结构化模式（CI/CD 友好）
blade-ai inject --scope pod --target cpu --action fullload \
  -n "app=myapp" --namespace default \
  -p "cpu-percent=80" -d 120

# 查看可用故障场景
blade-ai list

# 恢复故障
blade-ai recover --task-id task-xxx
```

完整命令参考见 [docs/USAGE.md](docs/USAGE.md)。

---

## 故障场景

内置 3 个技能包，覆盖 **88 个故障场景**。每个技能包由一个 `SKILL.md` 加上 `references/catalogue/` 下的一组场景文件构成：

### k8s-chaos-skills —— 61 个场景

| 层级 | 场景示例 |
| --- | --- |
| **Pod** | CPU 满载、CPU Throttling、OOM、磁盘空间打满、磁盘 IO 过高、网络丢包、网络延迟、Pending、ContainerCreating、CrashLoopBackOff、Terminating、镜像拉取失败、被驱逐重建、进程杀死、被删除 …… |
| **Container** | CPU 满载、网络丢包、被删除、进程异常 |
| **Node** | CPU 使用率过高、内存使用率过高、磁盘 IO 过高、磁盘空间不足、不可用（网络全丢包）、维护、网络故障 |
| **Workload / Service** | 副本缩容、HPA 上限、DaemonSet 调度异常、Service 调用失败、Service 负载均衡异常 |

### host-chaos-skills —— 18 个场景

CPU 满载、内存占用 / 缓存占用、磁盘空间填充、磁盘 IO 高负载、网络丢包 / DNS 劫持 / 端口占用、进程杀死 / 假死 / 数量飙升、文件删除 / 篡改 / 句柄耗尽、时钟偏移、Systemd 服务停止、syscall 延迟 / 返回值篡改。

### python-app-chaos-skills —— 9 个场景

HTTP 延迟 / 异常、MySQL 延迟 / 异常、Redis 延迟 / 异常 / 返回值篡改、Kafka 异常、gRPC 延迟。

> 在某个技能包的 `references/catalogue/` 下添加一个场景文件，或放入一个新的 `SKILL.md` 就能扩展出一整个技能包 —— Server 模式会自动热加载（watchdog + 500ms 去抖）。

---

## 四种接口

| 接口 | 入口 | 适用场景 |
| --- | --- | --- |
| **CLI** | `blade-ai inject` / `recover` / `list` / `metric` / `config` | 命令行操作、CI/CD 流水线 |
| **TUI** | `blade-ai`（交互式终端） | 日常运维、实时查看注入进度 |
| **HTTP API** | `POST /api/v1/inject`、`POST /api/v1/inject-stream`（SSE） | 平台集成、外部系统对接 |
| **Python SDK** | `from chaos_agent.l4 import L4ResilienceAgent` | 编程式调用、测试平台集成 |

### 两种运行模式

同一份 Agent 内核支撑两种模式；`blade-ai config set mode` 切换的是由哪一侧来调用 Graph —— 同进程的 `AgentRunner`（Local），还是走 HTTP 的 `AgentClient`（Server）。

```bash
# Local 模式（默认）—— Agent 同进程直连，零网络开销
blade-ai config set mode local

# Server 模式 —— FastAPI 集中管控，CLI/TUI 远程连接
blade-ai server                                        # 终端 1：启动 Server（默认 0.0.0.0:8089）
blade-ai server --host 127.0.0.1 --port 9000           # 显式指定监听地址 / 端口
blade-ai server --port 0 --ready-stdout                # 端口交由系统分配，就绪后打印 "BLADE_AI_READY port=N"
blade-ai config set mode server http://localhost:8089  # 终端 2：切换到 Server
```

---

## 安全防护

安全不是单点校验，而是五层递进。注入必须逐层通过后才能真正落到集群：

![五层纵深防御：安全审查、确认门禁、分阶段守卫、ToolGuard、循环上限与超时](assets/safety-layers.png)

1. **Safety Check** —— 纯规则引擎（无 LLM）：可配置的命名空间黑名单、与线上 ChaosBlade CRD 的冲突检测、目标有效性校验、多维爆炸半径打分。
2. **Confirmation Gate** —— 动态、数据驱动的 `interrupt()`，暂停等待人工批准 / 拒绝；关键操作没有它无法推进。
3. **分阶段守卫** —— 每个让 LLM 自己挑工具的阶段都有自己的守卫，红线各不相同：规划阶段拒绝一切变更调用，执行阶段拿冻结的已批准目标审查每次调用（*方式可变 · 身份不可变*，包括藏在 `sh -c` 里的逃逸），验证与恢复只能碰当前连接的环境。
4. **ToolGuard** —— fail-closed 的命令白名单加危险模式黑名单（`rm -rf`、`| bash`、`$(…)` ……）；一切以 exec-form 执行，管道与命令替换从构造上就失效。它挂在执行入口，所以**每个**阶段的命令都要过它。
5. **Loop Max & Timeout** —— 分阶段循环上限、全局递归上限，以及强制超时，到期即使 Agent 已经死掉也会自毁实验。未指定时长时按故障类型注入默认下限（默认 300 秒）；显式声明的时长原样执行、绝不偷偷改写。

这五层管的是"什么能**到达**集群"。还有一层回答的是任何准入门禁都答不了的问题：**它有没有待在你批准的爆炸半径里？** 环境在注入前被拍下快照、验证后做差分 —— 容器重启、驱逐、OOM、端点摘除、HPA 扩容等等。验证"目标坏了"很容易；能证明"别的没坏"，才敢把这次演练再跑一遍。

---

## 架构

BLADE AI 采用分层设计：顶层是接入适配器，中间是统一的 LangGraph 编排核心，其下是能力层与共享的基础设施层。三条调用路径（Local 同进程、Server HTTP+SSE、SDK）最终都汇聚到同一份编译后的 Graph。

![功能架构大图：从交互界面域、编排引擎域、安全守卫域、领域语义域、执行能力域、上下文与记忆域、模型连接域、可观测域、持久化与配置域，到真实故障面的十层功能域](assets/architecture-layers.svg.png)

*点击图片可查看高清原图。*

单一 `AgentState`（按生命周期组织：身份 / 意图 / 规划 / 安全 / 确认 / 执行 / 验证 / 恢复 / 循环控制 / 结果 / 记忆）是唯一真相源；自然语言与结构化输入均经 LLM 规划，并在 `safety_check` 处汇合；Recover 图完全独立编译、拥有自己的 ReAct 循环；SSE 流式事件（token / tool / confirm / result ……）贯穿节点 → FastAPI → TUI，是统一的实时反馈通道。完整设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 技术栈

| 层 | 技术 |
| --- | --- |
| Agent 编排 | LangGraph (StateGraph) · LangChain |
| 故障注入 | ChaosBlade · kubectl |
| 后端 | FastAPI · Typer · pydantic-settings |
| TUI | TypeScript · Ink · React |
| 存储 | aiosqlite (Checkpointer) · PostgreSQL（可选） |
| 观测 | OpenTelemetry · Prometheus · SSE |
| LLM | OpenAI 兼容接口（DashScope / DeepSeek / 智谱等） |

---

## 开发

```bash
make install     # 安装运行 + 开发依赖
make test        # 运行测试
make server      # 启动 Server
make build       # PyInstaller 打包独立二进制
make build-tui   # 构建 TUI 前端
```

Python 后端测试：`uv run pytest tests/ -v` · TUI 前端测试：`cd tui && npm test`

---

## 与 ChaosBlade 的关系

BLADE AI 是 [ChaosBlade](https://github.com/chaosblade-io/chaosblade) 生态的一部分。ChaosBlade 是故障注入引擎（CLI + Operator），BLADE AI 是它的智能代理层：

- **ChaosBlade**：负责“怎么注入” —— 执行 `blade create k8s pod-cpu fullload` 等具体命令。
- **BLADE AI**：负责“该不该注入、注入了没有、怎么恢复” —— 意图理解、安全审查、效果验证、确定性恢复。

两者是互补而非竞争关系。BLADE AI 底层调用 ChaosBlade，上层增加 LLM 编排和安全护栏。

---

## License

[Apache 2.0](NOTICE) —— Copyright 2026 ChaosBlade Authors。
