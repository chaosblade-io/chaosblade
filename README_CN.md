![logo](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/doc/image/chaosblade-logo.png)

# ChaosBlade: 一个简单易用且功能强大的混沌实验实施工具
[![Build Status](https://travis-ci.org/chaosblade-io/chaosblade.svg?branch=master)](https://travis-ci.org/chaosblade-io/chaosblade)
[![codecov](https://codecov.io/gh/chaosblade-io/chaosblade/branch/master/graph/badge.svg)](https://codecov.io/gh/chaosblade-io/chaosblade)
![license](https://img.shields.io/github/license/chaosblade-io/chaosblade.svg)


## 项目介绍
ChaosBlade 是阿里巴巴开源的一款遵循混沌工程原理和混沌实验模型的实验注入工具，帮助企业提升分布式系统的容错能力，并且在企业上云或往云原生系统迁移过程中业务连续性保障。

Chaosblade 是内部 MonkeyKing 对外开源的项目，其建立在阿里巴巴近十年故障测试和演练实践基础上，结合了集团各业务的最佳创意和实践。

ChaosBlade 不仅使用简单，而且支持丰富的实验场景，场景包括：
* 基础资源：比如 CPU、内存、网络、磁盘、进程等实验场景；
* Java 应用：比如数据库、缓存、消息、JVM 本身、微服务等，还可以指定任意类方法注入各种复杂的实验场景；
* C++ 应用：比如指定任意方法或某行代码注入延迟、变量和返回值篡改等实验场景；
* Docker 容器：比如杀容器、容器内 CPU、内存、网络、磁盘、进程等实验场景；
* 云原生平台：比如 Kubernetes 平台节点上 CPU、内存、网络、磁盘、进程实验场景，Pod 网络和 Pod 本身实验场景如杀 Pod，容器的实验场景如上述的 Docker 容器实验场景；

将场景按领域实现封装成一个个单独的项目，不仅可以使领域内场景标准化实现，而且非常方便场景水平和垂直扩展，通过遵循混沌实验模型，实现 chaosblade cli 统一调用。目前包含的项目如下：
* [chaosblade](https://github.com/chaosblade-io/chaosblade)：混沌实验管理工具，包含创建实验、销毁实验、查询实验、实验环境准备、实验环境撤销等命令，是混沌实验的执行工具，执行方式包含 CLI 和 HTTP 两种。提供完善的命令、实验场景、场景参数说明，操作简洁清晰。
* [chaosblade-spec-go](https://github.com/chaosblade-io/chaosblade-spec-go): 混沌实验模型 Golang 语言定义，便于使用 Golang 语言实现的场景都基于此规范便捷实现。
* [chaosblade-exec-os](https://github.com/chaosblade-io/chaosblade-exec-os): 基础资源实验场景实现。
* [chaosblade-exec-docker](https://github.com/chaosblade-io/chaosblade-exec-docker): Docker 容器实验场景实现，通过调用 Docker API 标准化实现。
* [chaosblade-exec-cri](https://github.com/chaosblade-io/chaosblade-exec-cri): 容器实验场景实现，通过调用 CRI 标准化实现。
* [chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator): Kubernetes 平台实验场景实现，将混沌实验通过 Kubernetes 标准的 CRD 方式定义，很方便的使用 Kubernetes 资源操作的方式来创建、更新、删除实验场景，包括使用 kubectl、client-go 等方式执行，而且还可以使用上述的 chaosblade cli 工具执行。
* [chaosblade-exec-jvm](https://github.com/chaosblade-io/chaosblade-exec-jvm): Java 应用实验场景实现，使用 Java Agent 技术动态挂载，无需任何接入，零成本使用，而且支持卸载，完全回收 Agent 创建的各种资源。
* [chaosblade-exec-cplus](https://github.com/chaosblade-io/chaosblade-exec-cplus): C++ 应用实验场景实现，使用 GDB 技术实现方法、代码行级别的实验场景注入。

## 使用文档
你可以从 [Releases](https://github.com/chaosblade-io/chaosblade/releases) 地址下载最新的 chaosblade 工具包，解压即用。如果想注入 Kubernetes 相关故障场景，需要安装 [chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator/releases)，详细的中文使用文档请查看 [chaosblade-help-zh-cn](https://chaosblade-io.gitbook.io/chaosblade-help-zh-cn/)。

chaosblade 支持 CLI 和 HTTP 两种调用方式，支持的命令如下：
* prepare：简写 p，混沌实验前的准备，比如演练 Java 应用，则需要挂载 java agent。例如要演练的应用名是 business，则在目标主机上执行 `blade p jvm --process business`。如果挂载成功，返回挂载的 uid，用于状态查询或者撤销挂载。
* revoke：简写 r，撤销之前混沌实验准备，比如卸载 java agent。命令是 `blade revoke UID`
* create: 简写是 c，创建一个混沌演练实验，指执行故障注入。命令是 `blade create [TARGET] [ACTION] [FLAGS]`，比如实施一次 Dubbo consumer 调用 xxx.xxx.Service 接口延迟 3s，则执行的命令为 `blade create dubbo delay --consumer --time 3000 --service xxx.xxx.Service`，如果注入成功，则返回实验的 uid，用于状态查询和销毁此实验使用。
* destroy：简写是 d，销毁之前的混沌实验，比如销毁上面提到的 Dubbo 延迟实验，命令是 `blade destroy UID`
* status：简写 s，查询准备阶段或者实验的状态，命令是 `blade status UID` 或者 `blade status --type create`
* server：启动 web server，暴露 HTTP 服务，可以通过 HTTP 请求来调用 chaosblade。例如在目标机器xxxx上执行：`blade server start -p 9526`，执行 CPU 满载实验：`curl "http:/xxxx:9526/chaosblade?cmd=create%20cpu%20fullload"`

以上命令帮助均可使用 `blade help [COMMAND]` 或者 `blade [COMMAND] -h` 查看，也可查看[新手指南](https://github.com/chaosblade-io/chaosblade/wiki/%E6%96%B0%E6%89%8B%E6%8C%87%E5%8D%97)，或者上述中文使用文档，快速上手使用。

## 快速体验
如果想不下载 chaosblade 工具包，快速体验 chaosblade，可以拉取 docker 镜像并运行，在容器内体验。
![demo.gif](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/release/chaosblade-demo-0.0.1.gif)

操作步骤如下：
下载镜像：
```bash
docker pull chaosbladeio/chaosblade-demo
```

启动镜像：
```bash
docker run -it --privileged chaosbladeio/chaosblade-demo
```

进入镜像之后，可阅读 README.txt 文件实施混沌实验，Enjoy it。

## 面向云原生
[chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator) 项目是针对云原生平台所实现的混沌实验注入工具，遵循混沌实验模型规范化实验场景，把实验定义为 Kubernetes CRD 资源，将实验模型映射为 Kubernetes 资源属性，很友好地将混沌实验模型与 Kubernetes 声明式设计结合在一起，在依靠混沌实验模型便捷开发场景的同时，又可以很好的结合 Kubernetes 设计理念，通过 kubectl 或者编写代码直接调用 Kubernetes API 来创建、更新、删除混沌实验，而且资源状态可以非常清晰地表示实验的执行状态，标准化实现 Kubernetes 故障注入。除了使用上述方式执行实验外，还可以使用 chaosblade cli 方式非常方便的执行 kubernetes 实验场景，查询实验状态等。具体请阅读：[云原生下的混沌工程实践](CLOUDNATIVE.md)

## 编译
此项目采用 golang 语言编写，所以需要先安装最新的 golang 版本，最低支持的版本是 1.11。Clone 工程后进入项目目录执行以下命令进行编译：
```shell script
make
```
如果在 mac 系统上，编译当前系统的版本，请执行：
```shell script
make build_darwin
```
如果想在 mac 系统上，编译 linux 系统版本，请执行：
```shell script
make build_linux
```
也可以选择性编译，比如只需要编译 cli、os 场景，则执行：
```shell script
make build_with cli os
# 如果是 mac 系统，执行
make build_with cli os_darwin
# 如果是 mac 系统，想选择性的编译 linux 版本的 cli，os，则执行：
ARGS="cli os" make build_with_linux
```

Arch Linux 安装 [chaosblade-bin](https://aur.archlinux.org/packages/chaosblade-bin/)
```bash
yay -S chaosblade-bin
```

## 缺陷&建议
欢迎提交缺陷、问题、建议和新功能，所有项目（包含其他子项目）的问题都可以提交到[Github Issues](https://github.com/chaosblade-io/chaosblade/issues)

你也可以通过以下方式联系我们：
* 钉钉群（推荐）：23177705
* Gitter room: https://gitter.im/chaosblade-io/community
* 邮箱：chaosblade.io.01@gmail.com
* Twitter: chaosblade.io

## 参与贡献
我们非常欢迎每个 Issue 和 PR，即使一个标点符号，如何参加贡献请阅读 [CONTRIBUTING](CONTRIBUTING.md) 文档，或者通过上述的方式联系我们。具体社区参与同学的晋升者阶梯，参见： ([晋升者阶梯](https://github.com/chaosblade-io/community/blob/main/Contributor_Ladder_CN.md))

## 企业登记
我们开源此项目的初衷是降低混沌工程在企业中落地的门槛，所以非常看重该项目在企业的使用情况，欢迎大家在此 [ISSUE](https://github.com/chaosblade-io/chaosblade/issues/32) 中登记，登记后会被邀请加入企业邮件组，探讨混沌工程在企业落地中遇到的问题和分享落地经验。

## 场景大图
![experiments landscape](https://user-images.githubusercontent.com/3992234/72340872-eb47c400-3703-11ea-830f-062e117c2e95.png)

## 项目生态
![ecosystem](https://user-images.githubusercontent.com/3992234/72410783-429d7100-37a4-11ea-8314-540560f8a54f.png)

## 未来规划
* 增强云原生领域场景
* Golang 应用混沌实验场景
* NodeJS 应用混沌实验场景
* 故障演练控制台
* 完善 ChaosBlade 各项目的开发文档
* 完善 ChaosBlade 工具的英文文档

## License
ChaosBlade 遵循 Apache 2.0 许可证，详细内容请阅读 [LICENSE](LICENSE)
