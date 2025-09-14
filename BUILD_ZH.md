# ChaosBlade 构建指南

## 概述

ChaosBlade 是一个强大的混沌工程平台，支持在 Mac、Linux 或 Windows 平台上编译各种项目组件。本文档提供了如何构建 ChaosBlade 项目的详细说明。

## 系统要求

### 系统要求
- **Git 版本**: >= 1.8.5
- **Go 版本**: 支持 Go modules
- **操作系统**: macOS (Darwin)、Linux、Windows

### 依赖项
- Go 编译器
- Git
- Make
- 可选：Docker 或 Podman（用于容器化构建）

## 版本管理

### 自动版本检测
ChaosBlade 支持从 Git Tags 自动获取版本号：
- 如果存在 Git Tag，将自动提取版本号（移除 v 前缀）
- 如果不存在 Git Tag，将使用默认版本 1.7.4 或环境变量 `BLADE_VERSION`

### 手动版本设置
```bash
export BLADE_VERSION=1.8.0
make build
```

## 构建目标

### 基础构建

#### 1. 当前平台构建
```bash
# 构建 CLI 工具
make build

# 构建所有组件
make build_all
```

#### 2. 特定平台构建
```bash
# 为特定平台构建所有组件
make darwin_amd64
make darwin_arm64
make linux_amd64
make linux_arm64
make windows_amd64

# 为特定平台构建特定组件
make linux_amd64 MODULES=cli
make linux_amd64 MODULES=cli,os,java
make linux_amd64 MODULES=all
```

#### 3. 单独组件构建
```bash
# 为当前平台构建单独组件
make cli          # 仅构建 CLI 工具
make os           # 构建操作系统实验场景
make cloud        # 构建云平台实验场景
make middleware   # 构建中间件实验场景
make java         # 构建 Java 实验场景
make cplus        # 构建 C/C++ 实验场景
make cri          # 构建 CRI 实验场景
make kubernetes   # 构建 Kubernetes 实验场景
make nsexec       # 构建 nsexec（仅 Linux）
make upx          # 使用 UPX 压缩二进制文件
make check_yaml   # 下载检查规范 YAML 文件
```

#### 4. 构建准备和工具
```bash
# 从 Git 生成版本信息
make generate_version

# 同步 go.mod 依赖项与 Makefile 分支配置
make sync_go_mod

# 准备构建环境（清理和创建目录）
make pre_build

# 打包构建产物
make package

# 清理所有构建产物
make clean
```

### 组件列表

| 组件 | 描述 | 备注 |
|------|------|------|
| `cli` | 命令行工具 | 核心 CLI 工具 |
| `os` | 操作系统实验场景 | 基础资源实验场景 |
| `cloud` | 云平台实验场景 | 云服务实验场景 |
| `middleware` | 中间件实验场景 | 中间件服务实验场景 |
| `java` | Java 实验场景 | JVM 相关实验场景 |
| `cplus` | C/C++ 实验场景 | C/C++ 应用程序实验场景 |
| `cri` | 容器运行时实验场景 | CRI 相关实验场景 |
| `kubernetes` | Kubernetes 实验场景 | K8s 相关实验场景 |
| `nsexec` | 命名空间执行器 | 仅 Linux，支持跨平台编译 |
| `check_yaml` | 检查规范文件 | 下载检查规范 YAML 文件 |

## 构建过程

### 1. 构建前准备
```bash
make pre_build
```
- 生成版本信息
- 清理和创建构建目录
- 设置平台特定的环境变量

### 2. 组件构建
每个组件都有独立的构建过程，包括：
- 克隆或更新源代码仓库
- 执行平台特定的构建命令
- 将构建产物复制到目标目录

### 3. 打包
构建完成后，自动生成平台特定的压缩包：
- `chaosblade-{version}-{platform}_{arch}.tar.gz`

## 平台特定构建

### macOS (Darwin)
```bash
# AMD64 架构
make darwin_amd64

# ARM64 架构（Apple Silicon）
make darwin_arm64
```

### Linux
```bash
# AMD64 架构
make linux_amd64

# ARM64 架构
make linux_arm64
```

### Windows
```bash
# AMD64 架构
make windows_amd64
```

## 容器化构建

### Docker 镜像构建
```bash
# 构建 Linux AMD64 镜像
make build_linux_amd64_image

# 构建 Linux ARM64 镜像
make build_linux_arm64_image

# 使用特定模块构建
make build_linux_amd64_image MODULES=cli,os
make build_linux_arm64_image MODULES=all
```

### 镜像推送
```bash
# 推送到容器镜像仓库
make push_image
```

### 容器运行时配置
构建系统自动检测可用的容器运行时：
- **Docker**（默认）
- **Podman**（如果 Docker 不可用）

您可以手动指定容器运行时：
```bash
# 明确使用 Docker
make nsexec CONTAINER_RUNTIME=docker

# 明确使用 Podman
make nsexec CONTAINER_RUNTIME=podman
```

## 跨平台编译

### nsexec 跨平台编译
nsexec 组件支持从 macOS 到 Linux 的跨平台编译：

#### 自动编译器检测
系统按以下顺序自动检测可用的跨平台编译工具链：
1. `musl-gcc`（用于 amd64）
2. `/usr/local/musl/bin/musl-gcc`（用于 amd64）
3. `x86_64-linux-musl-gcc`（用于 amd64）
4. `aarch64-linux-musl-gcc`（用于 arm64）
5. `gcc`（两种架构的备用选项）
6. `aarch64-linux-gnu-gcc`（用于 arm64）
7. `container`（如果未找到合适的编译器）

#### 容器化编译
如果没有合适的跨平台编译工具链，可以使用容器进行编译：
```bash
# 使用 Docker
make nsexec CONTAINER_RUNTIME=docker

# 使用 Podman
make nsexec CONTAINER_RUNTIME=podman
```

### 使用容器进行跨平台构建
```bash
# 使用 musl 容器构建 Linux AMD64
make cross_build_linux_amd64_by_container

# 使用 ARM 容器构建 Linux ARM64
make cross_build_linux_arm64_by_container
```

### UPX 二进制压缩
构建系统支持 UPX 压缩以获得更小的二进制文件大小：
```bash
# 使用 UPX 压缩二进制文件（仅 Linux 和 Windows）
make upx

# UPX 在支持的平台的打包过程中自动应用
```

**UPX 支持的平台：**
- Linux（amd64、arm64）
- Windows（amd64）

**安装：**
- macOS: `brew install upx`
- Ubuntu/Debian: `apt-get install upx-ucl`
- CentOS/RHEL: `yum install upx`

## 构建配置

### 环境变量
- `GOOS`: 目标操作系统（linux、darwin、windows）
- `GOARCH`: 目标架构（amd64、arm64）
- `BLADE_VERSION`: 版本号（从 Git tags 自动检测或手动设置）
- `CONTAINER_RUNTIME`: 容器运行时（docker/podman，自动检测）
- `MODULES`: 要构建的组件列表（逗号分隔）
- `GOPATH`: Go 工作空间路径（用于容器化构建）

### 构建标志
- `CGO_ENABLED=0`: 禁用 CGO 进行静态链接
- `GO111MODULE=on`: 启用 Go modules
- 静态链接标志: `-ldflags="-s -w"`（剥离调试信息和符号）
- 版本注入: `-X` 标志用于嵌入版本信息

### 版本信息注入
构建过程自动将版本信息注入到二进制文件中：
- 来自 Git tags 或环境的版本号
- 构建环境（`uname -mv`）
- 构建时间戳
- 组件特定的版本信息

## 构建产物

### 目录结构
```
target/
└── chaosblade-{version}-{platform}_{arch}/
    ├── bin/           # 可执行文件
    ├── lib/           # 库文件
    └── yaml/          # 配置文件
```

### 文件命名
- 可执行文件: `blade`（Linux/macOS）或 `blade.exe`（Windows）
- 压缩包: `chaosblade-{version}-{platform}_{arch}.tar.gz`
- YAML 规范文件: `chaosblade-{component}-spec-{version}.yaml`
- 检查规范: `chaosblade-check-spec-{version}.yaml`

### 构建缓存
构建系统使用缓存目录存储下载的依赖项：
- 缓存位置: `target/cache/`
- 缓存组件: 所有外部仓库（os、cloud、middleware 等）
- 缓存管理: 自动克隆/更新缓存的仓库

## 常用命令示例

### 快速开始
```bash
# 为当前平台构建
make build

# 构建所有组件
make build_all
```

### 特定平台构建
```bash
# 为 Linux AMD64 平台构建
make linux_amd64

# 为 macOS ARM64 平台构建
make darwin_arm64
```

### 组件选择构建
```bash
# 仅构建 CLI 和 OS 组件
make linux_amd64 MODULES=cli,os

# 构建所有组件
make linux_amd64 MODULES=all
```

### 容器镜像构建
```bash
# 构建并推送镜像
make build_linux_amd64_image
make build_linux_arm64_image
make push_image

# 使用特定模块构建镜像
make build_linux_amd64_image MODULES=cli,os
make build_linux_arm64_image MODULES=all
```

### 跨平台构建
```bash
# 使用容器构建 Linux 版本
make cross_build_linux_amd64_by_container
make cross_build_linux_arm64_by_container
```

### 工具命令
```bash
# 生成版本信息
make generate_version

# 同步依赖项
make sync_go_mod

# 压缩二进制文件
make upx

# 下载检查规范
make check_yaml

# 运行测试
make test

# 清理构建产物
make clean
```

## 故障排除

### 常见问题

#### 1. Git 版本过低
```bash
# 错误信息
ALERTMSG="please update git to >= 1.8.5"

# 解决方案
# Ubuntu/Debian
sudo apt-get update && sudo apt-get install git

# macOS
brew install git
```

#### 2. 缺少跨平台编译工具链
```bash
# Ubuntu/Debian
sudo apt-get install musl-tools gcc-aarch64-linux-gnu

# macOS
brew install FiloSottile/musl-cross/musl-cross
```

#### 3. 容器运行时问题
```bash
# 检查 Docker 状态
docker info

# 检查 Podman 状态
podman info

# 手动指定容器运行时
make nsexec CONTAINER_RUNTIME=docker
```

#### 4. UPX 压缩问题
```bash
# 检查是否安装了 UPX
which upx

# 在不同平台安装 UPX
# macOS
brew install upx

# Ubuntu/Debian
sudo apt-get install upx-ucl

# CentOS/RHEL
sudo yum install upx
```

#### 5. 跨平台编译问题
```bash
# 检查可用的交叉编译器
which musl-gcc
which aarch64-linux-gnu-gcc

# 安装交叉编译工具
# Ubuntu/Debian
sudo apt-get install musl-tools gcc-aarch64-linux-gnu

# macOS
brew install FiloSottile/musl-cross/musl-cross
```

#### 6. Git 版本问题
```bash
# 检查 Git 版本
git --version

# 如需要更新 Git
# Ubuntu/Debian
sudo apt-get update && sudo apt-get install git

# macOS
brew install git
```

### 清理构建产物
```bash
# 清理所有构建产物
make clean
```

## 测试

### 运行测试
```bash
# 运行所有测试
make test
```

### 测试覆盖率
测试生成覆盖率报告：
- 文件: `coverage.txt`
- 模式: `atomic`

## 帮助信息

### 查看帮助
```bash
make help
```

### 可用目标
```bash
# 查看所有可用的 make 目标
make -n help
```

## 相关链接

- [项目主页](https://github.com/chaosblade-io/chaosblade)
- [贡献指南](CONTRIBUTING.md)
- [代码风格指南](docs/code_styles.md)
- [发布流程](docs/release_process.md)
