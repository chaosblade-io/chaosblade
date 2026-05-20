# BLADE AI 使用文档

**语言:** 中文 | [English](USAGE_en.md)

完整的安装、配置、命令、API 速查与最佳实践。如果是第一次接触，先读 [README.md](../README.md) 了解快速开始；想了解架构与设计，读 [INTRODUCTION.md](INTRODUCTION.md)。

## 目录

- [安装](#安装)
- [卸载](#卸载)
- [首次配置](#首次配置)
- [对话式 TUI](#对话式-tui)
- [结构化 CLI](#结构化-cli)
- [三种注入模式详解](#三种注入模式详解)
- [故障场景速查](#故障场景速查)
- [恢复与降级](#恢复与降级)
- [Server 模式与 API](#server-模式与-api)
- [配置管理](#配置管理)
- [TS TUI 环境变量](#ts-tui-环境变量)
- [常见陷阱速查](#常见陷阱速查)
- [开发指南](#开发指南)

---

## 安装

### 一键脚本（推荐）

不传版本时脚本会自动查询 GitHub Releases 取最新的 `blade-ai-v*` tag。固定老版本可以传 `--version` 或 `BLADE_AI_VERSION`：

```bash
# macOS / Linux —— 装最新版（默认行为，自动 resolve 最新 release）
curl -fsSL https://chaosblade.io/install-agent.sh | bash

# 锁定指定版本
curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --version 0.1.0-alpha

# 通过 env 传同样的版本（适合 Dockerfile / CI）
BLADE_AI_VERSION=0.1.0-alpha curl -fsSL https://chaosblade.io/install-agent.sh | bash
```

脚本会自动按 `uname -m` 探测平台，从 `chaosblade-io/chaosblade` 的 `blade-ai-v<版本>` Release 下载对应 `tar.gz` 并解压到 `~/.blade-ai/versions/blade-ai-v<版本>/`，再创建 `~/.local/bin/blade-ai` 符号链接，并把 `~/.local/bin` 加入 shell rc 的 PATH（带 `# blade-ai` 标记，方便 `uninstall.sh` 精确清理）。

支持 4 个平台：`linux-amd64` / `linux-arm64` / `darwin-amd64` / `darwin-arm64`。**当前 Windows 暂不支持**（release-blade-ai.yml 不产 Windows 二进制；`install.ps1` 直接报错指引走 WSL2 / 源码构建）。

预编译包是**自包含**的：内嵌 Python 运行时 + ChaosBlade v1.8.0 二进制 + 全部技能文件，解压即用，无需 Python 或任何其他依赖。这特别适合堡垒机/跳板机等受限环境。

### 直接从 GitHub Release 下载

如果 `chaosblade.io` 域名跳转尚未配置，或想离线分发：

```bash
VERSION=0.1.0-alpha
PLATFORM=darwin-arm64    # linux-amd64 / linux-arm64 / darwin-amd64 / darwin-arm64
URL="https://github.com/chaosblade-io/chaosblade/releases/download/blade-ai-v${VERSION}"

curl -fSLO "${URL}/blade-ai-${PLATFORM}.tar.gz"
curl -fSLO "${URL}/checksums.txt"
sha256sum -c --ignore-missing checksums.txt    # 校验
tar -xzf "blade-ai-${PLATFORM}.tar.gz"
sudo ln -sf "$PWD/blade-ai/blade-ai" /usr/local/bin/blade-ai
```

### 源码构建

```bash
git clone https://github.com/chaosblade-io/chaosblade.git
cd chaosblade/blade-ai
make dev      # 安装开发依赖
make build    # PyInstaller 打包到 dist/blade-ai/
./dist/blade-ai/blade-ai version
```

> 当前发布通道**只通过 GitHub Releases 分发** —— 不再发 PyPI（`pip install blade-ai`）和 npm（`@blade-ai/tui`）。如果需要 pip / npm，自行 `make build` 后用 `python -m build` 打 wheel 或 `cd tui && npm publish` 推到私有 registry。

## 卸载

提供与 install.sh / install.ps1 对称的卸载脚本，按平台用对应版本：

### macOS / Linux

```bash
# 默认全删（含 ~/.blade-ai/ 下的配置/记忆/技能/日志）
bash <path>/uninstall.sh

# 看会做什么但不实际删（推荐先跑这个）
bash <path>/uninstall.sh --dry-run

# 删二进制 + PATH，保留用户数据
bash <path>/uninstall.sh --keep-config

# 仅删某一版（多版本场景下保留其它版本和符号链接）
bash <path>/uninstall.sh --version 0.1.0-alpha

# CI 友好：跳 y/N 确认
bash <path>/uninstall.sh --force
```

### Windows

```powershell
# 全删
.\uninstall.ps1

# 保留配置
.\uninstall.ps1 -KeepConfig

# 安全校验：仅当 manifest 记录的版本 = 0.1.0-alpha 时才执行
.\uninstall.ps1 -Version 0.1.0-alpha

# 看 plan
.\uninstall.ps1 -DryRun
```

每次修改 shell rc / 注册表前会自动写备份（`~/.zshrc.blade-ai-uninstall.bak` / `~/.blade-ai/path-backup.txt`），误删可手动还原。`--keep-config` 模式下只清安装元数据（`install-manifest.json` + `receipt.json`），方便你下次重装从头开始。

### 使用国内镜像

```bash
# 一键脚本支持自定义下载源
BLADE_AI_MIRROR=https://your-mirror.example.com/releases/download \
  curl -fsSL https://chaosblade.io/install-agent.sh | bash

# latest 解析也可以指向自建 GitHub API mirror（罕见）
BLADE_AI_MIRROR_API=https://your-mirror.example.com/api/releases \
  curl -fsSL https://chaosblade.io/install-agent.sh | bash
```

### 使用国内镜像

```bash
# 一键脚本支持自定义下载源
BLADE_AI_MIRROR=https://your-mirror.example.com/releases/download \
  curl -fsSL https://chaosblade.io/install-agent.sh | bash
```

---

## 首次配置

首次启动 `blade-ai` 时，TUI 会自动进入五步配置向导。每一步只问一件事，提供合理默认值，敏感信息回显掩码：

1. **LLM API Key** —— 输入 API Key（支持阿里云百炼、OpenAI 兼容接口），回显掩码保护
2. **模型选择** —— 选择推理模型（推荐 `qwen-max-latest`、`qwq-32b` 等支持深度推理的模型）
3. **集群配置** —— 自动扫描 `~/.kube/` 目录下的 kubeconfig 文件，选择默认集群和命名空间
4. **权限模式** —— 选择默认权限模式（确认 / 自动 / 计划），建议日常使用确认模式
5. **环境自检** —— 自动检测四项环境状态：
   - ✓ 本地 Blade 二进制（`~/.blade-ai/vendor/blade`）
   - ✓ K8s 集群连通性（`kubectl cluster-info`）
   - ✓ ChaosBlade Operator 部署状态（`kubectl get pods -n chaosblade`）
   - ✓ 技能文件完整性（`~/.blade-ai/skills/` 哈希校验）

如果检测到 ChaosBlade Operator 缺失，会主动询问是否现在安装，并提供两种安装方式（Helm 推荐，kubectl apply 降级）。用户也可以选择跳过，不阻塞配置流程 —— 后续尝试 K8s 注入时会再次提示。

配置完成后写入 `~/.blade-ai/config.json`，保存后 Agent 发出第一句问候即进入对话循环，无需退出后重新运行。

---

## 对话式 TUI

`blade-ai` 启动即进入 TUI 对话界面。

### 三种权限模式（`Shift+Tab` 切换）

| 模式 | 行为 | 适用场景 |
|------|------|---------|
| 🔒 确认模式（默认） | 注入前展示计划，等待人工确认后才执行 | 日常演练，防止误操作 |
| ⚡ 自动模式 | 跳过确认门，直接执行注入和恢复 | CI/CD 流水线、信任的测试环境 |
| 📋 计划模式 | 纯只读，不执行任何注入操作，只输出计划 | 方案评审、能力探索、学习场景 |

底部状态栏实时显示当前权限模式（绿色=确认/黄色=自动/灰色=计划），以及集群名、命名空间等环境上下文。

### 六种对话意图

Agent 能识别并区分六种不同的对话意图，每种有独立的处理路径：

| 意图类型 | 用户示例 | Agent 行为 |
|---------|---------|-----------|
| 故障注入请求 | "注入 CPU 压力 80%" | 走完整注入流程（规划 → 安全 → 确认 → 执行 → 验证） |
| 能力探索 | "你能做什么？" | 展示能力，引导逐步澄清 |
| 模糊意图引导 | "看看系统在压力下表现" | 多轮对话收敛意图 |
| 恢复请求 | "恢复刚才的实验" | Recover Graph |
| 查询请求 | "当前有哪些活跃实验？" | 直接查询并回答 |
| 退出 | "结束" | 退出 TUI |

### 斜杠命令体系

作为自然语言交互的补充，提供快速操作入口：

| 命令 | 功能 |
|------|------|
| `/faults` | 列出所有支持的故障注入类型，按 Pod/Workload/Service/Node/Storage 层级分组 |
| `/skills` | 列出已安装技能包及其元信息 |
| `/skills info <name>` | 展示指定技能的详细信息（支持场景、前置条件、依赖项） |
| `/skills search <keyword>` | 从技能市场搜索，支持模糊匹配 |
| `/recover` | 恢复指定任务或最近一次实验 |
| `/config` | 查看/修改配置（API Key、模型、集群、权限模式） |
| `/history` | 查看演练历史和实验记录 |
| `/help` | 帮助信息（快捷键 + 命令 + 配置摘要） |
| `/exit` | 退出 TUI |

### 快捷键

| 快捷键 | 功能 |
|-------|------|
| `Shift+Tab` | 循环切换权限模式 |
| `Ctrl+C` | 中断当前操作（注入期间触发安全恢复） |
| `Ctrl+R` | 历史命令搜索 |
| `Ctrl+O` | 切换约束高度模式（长输出溢出处理） |
| `\` + Enter | 多行输入模式 |
| `@` | 触发 K8s 资源自动补全（Deployment/Service/Pod 名） |

---

## 结构化 CLI

适合脚本化和 CI/CD 集成，提供确定性执行路径。

### 三种注入模式对比

| 模式 | 命令标志 | LLM 参与 | 速度 | 适用场景 |
|------|---------|---------|------|---------|
| 自然语言 | `-i "描述"` | 是（规划+执行+验证） | 慢 15-30s | 探索式、模糊意图、**支持 label 选择器** |
| 结构化参数 | `--scope/--target/--action...` | 是（执行+验证） | 中 8-15s | 参数明确 |
| 直接模式 | `--scope... --direct` | 否（仅验证阶段） | 快 2-5s | CI/CD、自动化、快速注入 |

### 约束规则

- `--direct` 不兼容 `--input`，两者互斥
- `--direct` 需要完整的结构化参数（scope + target + action + target-name；Pod/Container scope 还需 namespace）
- `--stream` 仅支持自然语言模式
- Node 级别场景 CLI 不要求 `--namespace` 参数

### 通用参数表

| 参数 | 缩写 | 说明 | 示例 |
|------|------|------|------|
| `--scope` | - | 作用范围：node/pod/container | `--scope pod` |
| `--target` | - | 故障目标：cpu/mem/network/disk/process/pod | `--target cpu` |
| `--action` | - | 故障动作：fullload/load/delay/loss/fill/burn/kill/stop/delete/dns | `--action fullload` |
| `--target-name` | `-n` | **资源名称**（Pod 名/Node 名），**不是** label 选择器 | `-n "accounting-6fbdb464c7-qn2vr"` |
| `--namespace` | `--ns` | K8s 命名空间 | `--namespace cms-demo` |
| `--params` | `-p` | Key=value 参数，逗号分隔；裸键为布尔标志 | `-p "cpu-percent=80"` |
| `--duration` | `-d` | 持续时间（秒），映射为 blade `--timeout`，默认 60 | `-d 600` |
| `--direct` | - | 跳过 LLM 直接执行 | `--direct` |
| `--kubeconfig` | - | kubeconfig 文件路径 | `--kubeconfig ~/.kube/config` |
| `--confirm` | - | 注入前需人工确认 | `--confirm` |
| `--input` | `-i` | 自然语言描述 | `-i "注入CPU故障"` |

### 参数映射规则

| CLI 参数 | 映射到 blade 命令 | 说明 |
|----------|-----------------|------|
| `--target-name` / `-n` | `--names` | **始终**映射为资源名称，不是 label 选择器 |
| `-p "key=value"` | `--key value` | 键值对参数 |
| `-p "flag"` (裸键) | `--flag` | 布尔标志 |
| `-d 600` | `--timeout 600` | 自动追加 timeout |

> **`-n/--target-name` 与 label 选择器的区别**
>
> `-n` 映射到 ChaosBlade 的 `--names` 参数，期望的是**精确的资源名称**（如 Pod 名 `accounting-6fbdb464c7-qn2vr`），而非 label 选择器（如 `opentelemetry.io/name=accounting`）。
>
> - **Pod 级别**：`-n` 传 Pod 名称，blade 生成 `--names <pod-name>`
> - **Node 级别**：`-n` 传节点名称，blade 生成 `--names <node-name>`
> - **label 选择器定位 Pod**：仅自然语言模式支持。Agent 通过 LLM 自动将 label 转为 `--labels` 参数
>
> 如何获取 Pod 名称：
> ```bash
> kubectl get pods -n cms-demo -l "opentelemetry.io/name=accounting" \
>   --kubeconfig ~/.kube/config -o jsonpath='{.items[*].metadata.name}'
> ```

---

## 三种注入模式详解

### 自然语言模式（推荐，支持 label 选择器）

```bash
# 对某服务注入 CPU 满载（Agent 自动将 "accounting服务" 转为 label 选择器）
blade-ai inject -i "对cms-demo命名空间中accounting服务注入CPU满载故障，负载80%" \
  --kubeconfig ~/.kube/config

# 模糊意图（Agent 会引导澄清参数）
blade-ai inject -i "看看系统在压力下表现" \
  --kubeconfig ~/.kube/config
```

### 结构化参数模式（使用 Pod 名称）

```bash
# 注入 CPU 压力
blade-ai inject \
  --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 \
  --kubeconfig ~/.kube/config

# 注入内存压力
blade-ai inject \
  --scope pod --target mem --action load \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "mode=ram,mem-percent=90" -d 600 \
  --kubeconfig ~/.kube/config

# 注入网络延迟
blade-ai inject \
  --scope pod --target network --action delay \
  -n "payment-5d979b947f-mht6v" --namespace cms-demo \
  -p "time=3000,interface=eth0" -d 600 \
  --kubeconfig ~/.kube/config
```

### 直接模式（`--direct`，零 LLM 调用，最快）

```bash
blade-ai inject \
  --scope pod --target cpu --action fullload \
  -n "accounting-6fbdb464c7-qn2vr" --namespace cms-demo \
  -p "cpu-percent=80" -d 600 --direct \
  --kubeconfig ~/.kube/config
```

> **Direct 路径 vs LLM 路径**
>
> 两条路径共享完全相同的安全审查和验证流程，差异仅在规划方式：
>
> | 路径 | 规划方式 | LLM 参与 | 延迟 | 确定性 | 适用场景 |
> |------|---------|---------|------|--------|---------|
> | Direct 路径 | 确定性技能激活 + 参数组装 | 零 LLM 调用 | 可控 | 结果可预测 | CI/CD 流水线、定时任务 |
> | LLM 路径 | LLM 理解意图 → 匹配技能 → 生成计划 | LLM 推理 | 数百ms到数秒 | 灵活但有不确定性 | 交互式调试、模糊意图 |

Direct 路径的存在是因为 LLM 规划在自动化场景中存在两个问题：一是延迟不可控，二是输出不确定。Direct 路径全程零 LLM 调用，延迟可控、结果可预测，适合嵌入 CI/CD 流水线。

---

## 故障场景速查

内置 `k8s-chaos-skills` 技能包，覆盖 **19 个故障场景**、5 个层级。

### Pod 层级（8 个场景）

| 场景 | 根因 | ChaosBlade 命令 | 验证方式 |
|------|------|-----------------|---------|
| CPU 使用率过高 | 死循环/高并发 | `pod-cpu fullload` | `kubectl top pod` CPU% ≥ 目标值 |
| OOM 内存异常 | 内存压力过大 | `pod-mem load` | Pod OOMKilled 事件或内存%达标 |
| 磁盘空间过高 | 日志数据积累 | `pod-disk fill` | Pod 内 `df -h` 使用率 ≥ 目标值 |
| 磁盘 IO 过高 | 异常 IO 占用 | `pod-disk burn` | `iostat` %util 升高 |
| 网络延迟 | 服务调用超时 | `pod-network delay` | 目标 Pod 延迟 ≥ 设定值 |
| 网络丢包 | 通信丢包 | `pod-network loss` | 丢包率 ≥ 设定百分比 |
| DNS 故障 | 域名解析异常 | `pod-network dns` | DNS 解析超时或失败 |
| 镜像拉取失败 | 镜像不存在/标签错误 | `kubectl patch` | Pod ImagePullBackOff 状态 |

### Workload 层级（3 个场景）

| 场景 | 根因 | 注入方式 | 验证方式 |
|------|------|---------|---------|
| 副本缩容 | 人为误操作 | `kubectl scale --replicas=0` | Pod 数量减少，服务端点缩减 |
| HPA 达到上限 | 资源饱和 | pod-cpu fullload 触发扩容 | HPA REPLICAS == MAXPODS |
| DaemonSet 不完全调度 | 节点不可调度 | `kubectl cordon` | DESIRED != READY，目标节点缺失 Pod |

### Service 层级（1 个场景）

| 场景 | 根因 | 注入方式 | 验证方式 |
|------|------|---------|---------|
| 负载均衡异常 | 后端不可达 | pod-network loss / process kill | Endpoints IP 列表缩减，请求 502/503 |

### Node 层级（5 个场景）

| 场景 | 根因 | 注入方式 | 验证方式 |
|------|------|---------|---------|
| Node CPU 过高 | 异常进程占用 | `node-cpu fullload` | `kubectl top node` CPU% ≥ 目标值 |
| Node 内存过高 | 异常进程占用 | `node-mem load` | `kubectl describe node` MemoryPressure=True |
| Node 磁盘 IO 过高 | 异常 IO 占用 | `node-disk burn` | 节点 iowait 升高，应用读写延迟增加 |
| 容器运行时磁盘过高 | 日志/临时文件堆积 | `node-disk fill` | `df -h` 分区 > 85%，Pod 可能 Evicted |
| 节点不可用 | 节点宕机 | `kubectl cordon` + taint | 节点 NotReady，Pod 迁移 |

### 存储层级（1 个场景）

| 场景 | 根因 | 注入方式 | 验证方式 |
|------|------|---------|---------|
| PVC Pending | 存储类配置错误 | `kubectl patch pvc` StorageClass | PVC status=Pending，Events 显示 storageclass not found |

### ChaosBlade 标准场景命令大全

#### Pod CPU 过高 (`pod / cpu / fullload`)
```bash
blade-ai inject -i "对cms-demo命名空间中accounting服务注入CPU满载故障，负载80%" --kubeconfig ~/.kube/config
blade-ai inject --scope pod --target cpu --action fullload -n "accounting-xxx" --namespace cms-demo -p "cpu-percent=80" -d 600 --kubeconfig ~/.kube/config
blade-ai inject --scope pod --target cpu --action fullload -n "accounting-xxx" --namespace cms-demo -p "cpu-percent=80" -d 600 --direct --kubeconfig ~/.kube/config
```

#### Pod OOM 内存异常 (`pod / mem / load`)
```bash
blade-ai inject --scope pod --target mem --action load -n "accounting-xxx" --namespace cms-demo -p "mode=ram,mem-percent=90" -d 600 --kubeconfig ~/.kube/config
```

#### Pod 磁盘空间 (`pod / disk / fill`)
```bash
blade-ai inject --scope pod --target disk --action fill -n "accounting-xxx" --namespace cms-demo -p "path=/tmp,size=10240" -d 600 --kubeconfig ~/.kube/config
```
> `size` 必须为纯正整数（单位 MB），禁止使用 `10g` 或 `10240m`。

#### Pod 磁盘 IO (`pod / disk / burn`)
```bash
blade-ai inject --scope pod --target disk --action burn -n "accounting-xxx" --namespace cms-demo -p "path=/tmp,read,write" -d 600 --kubeconfig ~/.kube/config
```
> `read` 和 `write` 是布尔标志，在 `-p` 中作为裸键传递（不带 `=值`）。

#### Pod Terminating / 进程挂起 (`pod / process / stop`)
```bash
blade-ai inject --scope pod --target process --action stop -n "accounting-xxx" --namespace cms-demo -p "process=java" -d 600 --kubeconfig ~/.kube/config
```

#### Pod 网络延迟 (`pod / network / delay`)
```bash
blade-ai inject --scope pod --target network --action delay -n "payment-xxx" --namespace cms-demo -p "time=3000,interface=eth0" -d 600 --kubeconfig ~/.kube/config
```

#### Pod 网络丢包 (`pod / network / loss`)
```bash
blade-ai inject --scope pod --target network --action loss -n "checkout-xxx" --namespace cms-demo -p "percent=60" -d 600 --kubeconfig ~/.kube/config
```
> 优先使用 `local-port` 或 `remote-port` 限制影响范围，避免全端口丢包。

#### Pod DNS 故障 (`pod / network / dns`)
```bash
blade-ai inject --scope pod --target network --action dns -n "cart-xxx" --namespace cms-demo -p "domain=example.com,ip=1.1.1.1" -d 600 --kubeconfig ~/.kube/config
```

#### Node CPU 过高 (`node / cpu / fullload`)
```bash
blade-ai inject --scope node --target cpu --action fullload -n "cn-hongkong.10.0.1.101" -p "cpu-percent=90" -d 600 --kubeconfig ~/.kube/config
```
> Node 级别不含 `--namespace`。

#### Node 内存过高 (`node / mem / load`)
```bash
blade-ai inject --scope node --target mem --action load -n "cn-hongkong.10.0.1.101" -p "mode=ram,mem-percent=95" -d 600 --kubeconfig ~/.kube/config
```

#### Node 磁盘 IO 过高 (`node / disk / burn`)
```bash
blade-ai inject --scope node --target disk --action burn -n "cn-hongkong.10.0.2.69" -p "path=/tmp,read,write" -d 600 --kubeconfig ~/.kube/config
```

#### Node 容器运行时磁盘过高 (`node / disk / fill`)
```bash
blade-ai inject --scope node --target disk --action fill -n "cn-hongkong.10.0.1.101" -p "path=/var/log,percent=90" -d 600 --kubeconfig ~/.kube/config
```
> 推荐使用 `percent` 参数（如 `percent=90`）以确保触发 DiskPressure（>85%）。

### kubectl 操作场景（5 个，仅自然语言模式）

以下场景底层使用 kubectl 操作而非 ChaosBlade，**不支持结构化参数和 `--direct` 模式**。

```bash
# Node 不可用
blade-ai inject -i "将节点cn-hongkong.10.0.1.101标记为不可调度" --kubeconfig ~/.kube/config

# workload 副本被缩容
blade-ai inject -i "将cms-demo命名空间中accounting服务缩容到0个副本" --kubeconfig ~/.kube/config

# DaemonSet 未完全调度
blade-ai inject -i "将节点标记为不可调度，使DaemonSet无法在该节点调度Pod" --kubeconfig ~/.kube/config

# PVC Pending 存储类配置错误
blade-ai inject -i "将PVC修改为不存在的StorageClass，模拟存储类配置错误" --kubeconfig ~/.kube/config

# Pod 镜像拉取失败
blade-ai inject -i "对cms-demo命名空间中frontend服务模拟镜像拉取失败" --kubeconfig ~/.kube/config
```

---

## 恢复与降级

```bash
# 通过 blade-ai 恢复（两层验证）
blade-ai recover --task-id <task-id> --kubeconfig ~/.kube/config

# 强制清理残留实验（进程崩溃后的降级路径）
blade-ai recover --task-id <task-id> --force --kubeconfig ~/.kube/config

# 通过 blade 直接恢复（blade-ai 不可用时的最终降级）
kubectl exec <tool-pod> -n chaosblade -- blade destroy <blade-uid> --kubeconfig=~/.kube/config
```

恢复有自己的两层验证：

- **Layer 1**：`blade destroy <uid>` 返回成功
- **Layer 2**：kubectl 轮询确认目标资源已恢复正常（CPU/内存使用率回落、Endpoints 恢复、镜像拉取成功）

进程崩溃 / blade UID 丢失等异常场景下，`--force` 会直接清理 K8s 中的 ChaosBlade CR，绕过 blade 二进制 —— 是兜底而非常规路径。

---

## Server 模式与 API

启动 FastAPI HTTP API 服务，供多团队共享或对接上游平台：

```bash
blade-ai-server                   # 启动 FastAPI 服务（默认端口 8000）
blade-ai-server --port 8089       # 指定端口
blade-ai-server --host 0.0.0.0    # 对外监听
```

### REST + SSE 接口

| 接口 | 方法 | 用途 |
|------|------|------|
| `/api/v1/inject` | POST | 提交故障注入请求 |
| `/api/v1/inject/stream` | POST (SSE) | 注入请求 + 实时流式输出 |
| `/api/v1/recover` | POST | 恢复指定任务 |
| `/api/v1/confirm` | POST | 确认/拒绝注入计划 |
| `/api/v1/tasks/{id}/stream` | GET (SSE) | 实时追踪任务执行进度 |
| `/api/v1/metric/{id}` | GET | 查询任务指标（时间线、耗时、token消耗） |
| `/api/v1/skills` | GET | 列出已安装技能 |
| `/api/v1/sessions` | POST | 创建 TUI session（TS TUI 使用） |
| `/api/v1/sessions/{id}` | DELETE | 销毁 session（CLI 退出时调用） |
| `/api/v1/sessions/{id}/state` | GET | 拉 session 元信息 |
| `/api/v1/sessions/{id}/turn` | POST (SSE) | 统一 SSE turn endpoint（TS TUI 核心接口） |
| `/api/v1/sessions/{id}/interrupt` | POST | 中断响应（confirm / cancel / answer） |
| `/api/v1/sessions/{id}/cancel` | POST | 取消当前 turn（Ctrl+C 触发） |
| `/api/v1/health` | GET | 健康检查 + 协议版本协商 |

### SSE 事件类型

| 事件 | 说明 |
|------|------|
| `token` | LLM 逐 token 输出 |
| `thinking` | 推理过程，独立于 token 事件，前端可选择展示或隐藏 |
| `tool_start` / `tool_end` | 工具调用开始 / 结束 |
| `node_start` / `node_end` | 图节点进入 / 退出 |
| `confirm` | 等待人工审批 |
| `result` | 最终结果 |
| `error` | 异常 |

### 协议版本

`X-Blade-Protocol-Version` 响应头携带协议版本号，TUI bundle 编译时锁定 `TUI_PROTOCOL_VERSION`，不匹配时启动显示非致命警告。

### HTTP 调用示例

```bash
# 列出技能
curl http://localhost:8089/api/v1/skills

# 故障注入
curl -X POST http://localhost:8089/api/v1/inject \
  -H "Content-Type: application/json" \
  -d '{
    "fault_type": "pod-cpu-high",
    "target_type": "pod",
    "target_name": "my-pod",
    "namespace": "default"
  }'

# SSE 实时追踪
curl -N http://localhost:8089/api/v1/tasks/task-xxx/stream

# 健康检查
curl http://localhost:8089/api/v1/health
```

---

## 配置管理

所有参数可通过 `blade-ai config` 管理，配置优先级：

```
初始化参数 > ~/.blade-ai/config.json > BLADE_AI_* 环境变量 > 代码默认值
```

```bash
# LLM 配置
blade-ai config set api_key "sk-xxx"                   # API Key
blade-ai config set api_base_url "https://..."         # API Base URL
blade-ai config set model_name "qwen-max-latest"       # 模型名称

# 集群配置
blade-ai config set kubeconfig "/path/to/kubeconfig"   # Kubeconfig 路径
blade-ai config set default_namespace "cms-demo"       # 默认命名空间

# 安全配置
blade-ai config set safety_blacklist_namespaces "kube-system,kube-public,production"
blade-ai config set confirmation_required true          # 是否需要人工确认

# 运行模式
blade-ai config set mode local                          # local / server
blade-ai config set server_port 8000                    # Server 端口

# 查看当前配置
blade-ai config show
```

### 持久化数据

集中在 `~/.blade-ai/` 目录：

| 路径 | 内容 |
|------|------|
| `config.json` | 用户配置 |
| `logs/` | 运行日志 |
| `skills/` | 技能文件（覆盖内嵌默认） |
| `memory/sessions/` | 单次任务的 Session Memory |
| `memory/experiments/` | 跨任务的 Operational Memory |
| `vendor/blade` | 内嵌 ChaosBlade 二进制 |

---

## TS TUI 环境变量

只影响 TUI 进程行为（不进 `config.json`），用于覆盖默认装载逻辑、强制语言、调试 SSE 等：

| 变量 | 作用 |
|------|------|
| `BLADE_AI_SERVER` | 连接到指定远端 server URL，跳过本地 spawn |
| `BLADE_AI_TUI=legacy` | 回退到旧版 Python TUI（prompt_toolkit + Rich） |
| `BLADE_AI_TUI=ts` | 强制使用 TS TUI；找不到 bundle 则失败（不静默降级） |
| `BLADE_AI_TUI_BIN` | 显式指定 TUI bundle 路径（`.js` 或可执行 shim） |
| `BLADE_AI_PYTHON` | 嵌入模式下用哪个 Python 解释器 spawn server |
| `BLADE_AI_LANG` | 强制语言：`zh` 或 `en`；缺省按 `LC_ALL` / `LANG` 自动判断 |
| `BLADE_AI_DEBUG=1` | 把 SSE 协议解析错误 dump 到 stderr |
| `BLADE_AI_MIRROR` | 安装脚本下载源（默认 GitHub Releases） |
| `NO_COLOR` | 关闭主题颜色（遵循 [no-color.org](https://no-color.org) 约定） |

---

## 常见陷阱速查

| # | 陷阱 | 正确做法 |
|---|------|---------|
| 1 | `-n` 传 label 选择器给结构化/direct 模式 | `-n` 传 Pod 名称；label 选择器用自然语言模式 |
| 2 | node-disk fill `size` 参数格式 | 纯正整数 MB：`size=10240`，禁止 `size=10g` |
| 3 | Node 级别传了 `--namespace` | Node scope 自动省略，CLI 也不要求 |
| 4 | `--target mem --action fullload` | `pod-mem` 用 `load`，只有 cpu 用 `fullload` |
| 5 | 网络故障未限制端口 | 优先用 `local-port`/`remote-port` 过滤 |
| 6 | 布尔标志传递方式 | 裸键：`-p "path=/tmp,read,write"`，禁止 `read=true` |
| 7 | `--direct` + `--input` 混用 | 两者互斥，选择一种模式 |
| 8 | `--direct` 缺少 `--namespace` | Pod/Container scope 必须提供 namespace |
| 9 | 改完 TS 源码忘记 `npm run build` | 必须重新生成 `tui/dist/cli.js`，否则 PyInstaller 打的还是旧 bundle |
| 10 | `blade status` 加 `--kubeconfig` 报错 | v1.8.0 不支持该参数，需通过 `KUBECONFIG` 环境变量传 |

---

## 开发指南

### Python 后端

```bash
git clone https://github.com/chaosblade-io/chaosblade.git
cd chaosblade/blade-ai
make dev      # 安装开发依赖（含 pytest、ruff、mypy）
make test     # 跑测试
make build    # PyInstaller 打包独立二进制（dist/blade-ai/）
```

主要目录：

| 路径 | 内容 |
|------|------|
| `src/chaos_agent/agent/` | LangGraph StateGraph、节点、Router |
| `src/chaos_agent/cli/` | Typer CLI 入口（`blade-ai` 命令） |
| `src/chaos_agent/server/` | FastAPI + SSE Server（`blade-ai-server` 命令） |
| `src/chaos_agent/tools/` | 工具实现（blade / kubectl / guard / skills） |
| `src/chaos_agent/skills/` | 技能加载器（Tier 1/2/3） |
| `src/chaos_agent/memory/` | 三层记忆系统 |
| `src/chaos_agent/config/` | 配置管理（pydantic-settings） |
| `skills/` | 19 个故障场景的 SKILL.md 文件 |
| `tests/` | Pytest 测试（80+ 文件） |

### TS TUI

TUI 渲染层在 `tui/` 子目录，通过 HTTP + SSE 与 Python 后端通信。需要 Node 22+：

```bash
cd tui
npm install
npm run typecheck
npm run build       # → dist/cli.js（Python 包通过这个 bundle 启动 TS TUI）
npm run dev         # tsx watch 模式，源码改动自动重建
npm test            # vitest

# 不需要终端的烟雾测试（headless）
node scripts/smoke-i18n.mjs       # locale 检测用例
node scripts/smoke-slash.mjs      # parser + registry + handler 分发
node scripts/smoke-reducer.mjs    # action ↔ state 转移
```

> **重要**：改完 TS 源码后必须 `npm run build` 重新生成 `tui/dist/cli.js`，否则 `blade-ai` 加载的还是旧 bundle（Python 端通过 `tui/dist/cli.js` 启动 TUI）。

### 发布流程

由 `chaosblade/.github/workflows/release-blade-ai.yml` 全自动驱动：

```bash
# 1) 同步 3 处版本字符串到目标版本（CI 守卫会比对）
#    blade-ai/pyproject.toml
#    blade-ai/tui/package.json
#    blade-ai/src/chaos_agent/__init__.py

# 2) 提交并打标签
git tag blade-ai-v0.1.0-alpha
git push origin blade-ai-v0.1.0-alpha
```

CI 流程：

| 阶段 | 内容 |
|------|------|
| `verify-versions` | 校验三处版本字符串与标签匹配 |
| `build-tui` | npm install (--ignore-scripts) → patch-package → typecheck → tsup bundle → vitest，上传 `tui-bundle` artifact（`cli.js` + `package.json`） |
| `build` | 4 平台矩阵 native build：linux-amd64 (ubuntu-latest) / linux-arm64 (ubuntu-24.04-arm) / darwin-amd64 (macos-latest + python.org universal2 Python + Rosetta) / darwin-arm64 (macos-latest)。各自下载对应 chaosblade tarball → PyInstaller 打包；macOS 走 ad-hoc codesign |
| `release` | 聚合 4 份归档 + `checksums.txt` 创建 GitHub Release |

整条流水线在 ~25 分钟内产出四平台可执行包。**当前不发 PyPI 和 npm**；分发完全通过 GitHub Releases + `install.sh` 一键脚本。

---

## 反馈与贡献

- **Issues**：[github.com/chaosblade-io/chaosblade/issues](https://github.com/chaosblade-io/chaosblade/issues)（请在标题加 `[blade-ai]` 前缀）
- **钉钉群**：23177705
- **邮箱**：chaosblade.io.01@gmail.com

欢迎 Issue 与 PR，详见 [CONTRIBUTING.md](../../CONTRIBUTING.md)。
