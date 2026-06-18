![logo](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/doc/image/chaosblade-logo.png)

# Chaosblade: An Easy to Use and Powerful Chaos Engineering Toolkit
[![Build Status](https://travis-ci.org/chaosblade-io/chaosblade.svg?branch=master)](https://travis-ci.org/chaosblade-io/chaosblade)
[![Financial Contributors on Open Collective](https://opencollective.com/chaosblade/all/badge.svg?label=financial+contributors)](https://opencollective.com/chaosblade) [![codecov](https://codecov.io/gh/chaosblade-io/chaosblade/branch/master/graph/badge.svg)](https://codecov.io/gh/chaosblade-io/chaosblade)
![license](https://img.shields.io/github/license/chaosblade-io/chaosblade.svg)
[![CII Best Practices](https://bestpractices.coreinfrastructure.org/projects/5032/badge)](https://bestpractices.coreinfrastructure.org/projects/5032)

中文版 [README](README_CN.md)  
Wiki: [DeepWiki](https://deepwiki.com/chaosblade-io/chaosblade-for-deepwiki)

## 🔥🔥🔥 Major UPDATE：Chaosblade Agent Released - Blade AI
<img width="2752" height="1252" alt="image" src="https://github.com/user-attachments/assets/f17ddb79-23aa-4632-89da-72c22379b5c4" />
Blade AI serves as the intelligent agent layer within the ChaosBlade ecosystem: at the foundational level, it invokes ChaosBlade to execute fault injection; at the upper level, it incorporates orchestration capabilities such as intent understanding, security auditing, effect verification, safe recovery, and structured reporting—thereby transforming fault drills from a process of "manually writing commands" into one completed through "conversational interaction."


For detailed information, please refer to: [Blade AI README](https://github.com/chaosblade-io/chaosblade/blob/feature/blade-ai/blade-ai/README_en.md)

* Release: [blade-ai-v0.1.0](https://github.com/chaosblade-io/chaosblade/releases/tag/blade-ai-v0.1.0)
* Code Branch: blade-ai-v0.1.0

## Introduction

ChaosBlade is an Alibaba open source experimental injection tool that follows the principles of chaos engineering and chaos experimental models to help enterprises improve the fault tolerance of distributed systems and ensure business continuity during the process of enterprises going to cloud or moving to cloud native systems.

Chaosblade is an internal open source project of MonkeyKing. It is based on Alibaba's nearly ten years of failure testing and drill practice, and combines the best ideas and practices of the Group's businesses.

ChaosBlade is not only easy to use, but also supports rich experimental scenarios. The scenarios include:
* Basic resources: such as CPU, memory, network, disk, process and other experimental scenarios;
* Java applications: such as databases, caches, messages, JVM itself, microservices, etc. You can also specify any class method to inject various complex experimental scenarios;
* C ++ applications: such as specifying arbitrary methods or experimental lines of code injection delay, tampering with variables and return values;
* container: such as killing the container, the CPU in the container, memory, network, disk, process and other experimental scenarios;
* Cloud-native platforms: For example, CPU, memory, network, disk, and process experimental scenarios on Kubernetes platform nodes, Pod network and Pod itself experimental scenarios such as killing Pods, and container experimental scenarios such as the aforementioned Docker container experimental scenario;

Encapsulating scenes by domain into individual projects can not only standardize the scenes in the domain, but also facilitate the horizontal and vertical expansion of the scenes. By following the chaos experimental model, the chaosblade cli can be called uniformly. The items currently included are:
* [chaosblade](https://github.com/chaosblade-io/chaosblade): Chaos experiment management tool, including commands for creating experiments, destroying experiments, querying experiments, preparing experimental environments, and canceling experimental environments. It is the execution of chaotic experiments. Tools, execution methods include CLI and HTTP. Provides complete commands, experimental scenarios, and scenario parameter descriptions, and the operation is simple and clear.
* [chaosblade-spec-go](https://github.com/chaosblade-io/chaosblade-spec-go): Chaos experimental model Golang language definition, scenes implemented using Golang language are easy to implement based on this specification.
* [chaosblade-exec-os](https://github.com/chaosblade-io/chaosblade-exec-os): Implementation of basic resource experimental scenarios.
* [chaosblade-exec-docker](https://github.com/chaosblade-io/chaosblade-exec-docker): Docker container experimental scenario implementation, standardized by calling the Docker API.
* [chaosblade-exec-cri](https://github.com/chaosblade-io/chaosblade-exec-cri): Container experimental scenario implementation, standardized by calling the CRI.
* [chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator): Kubernetes platform experimental scenario is implemented, chaos experiments are defined by Kubernetes standard CRD method, it is very convenient to use Kubernetes resource operation method To create, update, and delete experimental scenarios, including using kubectl, client-go, etc., and also using the chaosblade cli tool described above.
* [chaosblade-exec-jvm](https://github.com/chaosblade-io/chaosblade-exec-jvm): Java application experimental scenario implementation, using Java Agent technology to mount dynamically, without any access, zero-cost use It also supports uninstallation and completely recycles various resources created by the Agent.
* [chaosblade-exec-cplus](https://github.com/chaosblade-io/chaosblade-exec-cplus): C ++ application experimental scenario implementation, using GDB technology to implement method and code line level experimental scenario injection.
* [chaosblade-box](https://github.com/chaosblade-io/chaosblade-box): Possessing chaos engineering platform and resilience testing platform capabilities.For more information on the resilience testing platform capabilities, see the [main2](https://github.com/chaosblade-io/chaosblade-box/tree/main2) branch.
  
## Quick Start

This guide helps you run your first fault injection on Kubernetes in under 5 minutes using ChaosBlade. We'll inject a CPU stress fault into a Pod as a minimal example.

### Prerequisites

Before you begin, make sure you have:

- [ ] **kubectl access** to a running Kubernetes cluster (`kubectl cluster-info` returns successfully)
- [ ] A **target namespace** with at least one running Pod (default: `default`)
- [ ] **ChaosBlade installed**:
  - [ ] The [chaosblade](https://github.com/chaosblade-io/chaosblade/releases) CLI toolkit downloaded and extracted (the `blade` binary is on your `PATH`)
  - [ ] The [chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator/releases) deployed to your cluster (required for Kubernetes scenarios)

Install the operator with Helm:

```shell script
helm install chaosblade-operator chaosblade-operator-<version>.tgz --namespace chaosblade --create-namespace
```

Verify the operator is running:

```shell script
kubectl get pods -n chaosblade
```

### Inject Your First Fault (CPU Stress)

Run a cpu full-load fault against a Pod in the `default` namespace:

```shell script
blade create k8s pod-cpu fullload   --cpu-percent 80   --kubeconfig ~/.kube/config   --names <pod-name>   --namespace default
```

If the injection succeeds, ChaosBlade returns a JSON result containing an experiment `uid`. Save this `uid` to check status or destroy the experiment later:

```json
{"code":200,"success":true,"result":"<experiment-uid>"}
```

### Check the Experiment Status

```shell script
blade status <experiment-uid>
```

### Recover (Destroy the Fault)

Always recover after your drill to restore the target to normal:

```shell script
blade destroy <experiment-uid>
```

That's it! You've completed a full inject-verify-recover cycle. To explore more scenarios, run `blade create k8s -h` or see [Chaos Engineering Practice under Cloud Native](CLOUDNATIVE.md).

## CLI Command
You can download the latest chaosblade toolkit from [Releases](https://github.com/chaosblade-io/chaosblade/releases) and extract it and use it. If you want to inject Kubernetes related fault scenarios, you need to install [chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator/releases). For detailed Chinese usage documents, please see [chaosblade-help-zh-cn ](https://chaosblade-io.gitbook.io/chaosblade-help-zh-cn/).

chaosblade supports CLI and HTTP invocation methods. The supported commands are as follows:
* **prepare**: alias is p, preparation before the chaos engineering experiment, such as drilling Java applications, you need to attach the java agent. For example, to drill an application whose application name is business, execute `blade p jvm --process business` on the target host. If the attach is successful, return the uid for status query or agent revoke.
* **revoke**: alias is r, undo chaos engineering experiment preparation before, such as detaching java agent. The command is `blade revoke UID`
* **create**: alias is c, create a chaos engineering experiment. The command is `blade create [TARGET] [ACTION] [FLAGS]`. For example, if you implement a Dubbo consumer call xxx.xxx.Service interface delay 3s, the command executed is `blade create dubbo delay --consumer --time 3000 --Service xxx.xxx.Service`, if the injection is successful, return the experimental uid for status query and destroy the experiment.
* **destroy**: alias is d, destroy a chaos engineering experiment, such as destroying the Dubbo delay experiment mentioned above, the command is `blade destroy UID`
* **status**: alias s, query preparation stage or experiment status, the command is `blade status UID` or `blade status --type create`
* **server**: start the web server, expose the HTTP service, and call chaosblade through HTTP requests. For example, execute on the target machine xxxx: `blade server start -p 9526` to perform a CPU full load experiment:` curl "http://xxxx:9526/chaosblade?cmd=create%20cpu%20fullload" `

Use the `blade help [COMMAND]` or `blade [COMMAND] -h` command to view help

## Experience Demo
Download the chaosblade demo image and experience the use of the blade toolkit

![demo.gif](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/release/chaosblade-demo-0.0.1.gif)

Download image command：
```shell script
docker pull chaosbladeio/chaosblade-demo
```
Run the demo container：
```shell script
docker run -it --privileged chaosbladeio/chaosblade-demo
```
After entering the container, you can read the README.txt file to implement the chaos experiment, Enjoy it.

## Cloud Native
[chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator) The project is a chaos experiment injection tool for cloud-native platforms. It follows the chaos experiment model to standardize the experimental scenario and defines the experiment as Kubernetes CRD Resources, mapping experimental models to Kubernetes resource attributes, and very friendly combination of chaotic experimental models with Kubernetes declarative design. While relying on chaotic experimental models to conveniently develop scenarios, it can also well integrate Kubernetes design concepts, through kubectl or Write code to directly call the Kubernetes API to create, update, and delete chaotic experiments, and the resource status can clearly indicate the execution status of the experiment, and standardize Kubernetes fault injection. In addition to using the above methods to perform experiments, you can also use the chaosblade cli method to execute kubernetes experimental scenarios and query the experimental status very conveniently. For details, please read the chinese document: [Chaos Engineering Practice under Cloud Native](CLOUDNATIVE.md)

## Compile
See [BUILD.md](BUILD.md) for the details.

## Bugs and Feedback
For bug report, questions and discussions please submit [GitHub Issues](https://github.com/chaosblade-io/chaosblade/issues).

You can also contact us via:
* Dingding group (recommended for chinese): 23177705
* Slack group: [chaosblade-io](https://join.slack.com/t/chaosblade-io/shared_invite/zt-f0d3r3f4-TDK13Wr3QRUrAhems28p1w)
* Gitter room: [chaosblade community](https://gitter.im/chaosblade-io/community)
* Email: chaosblade.io.01@gmail.com
* Twitter: [chaosblade.io](https://twitter.com/ChaosbladeI)

## Contributing
We welcome every contribution, even if it is just punctuation. See details of [CONTRIBUTING](CONTRIBUTING.md). For the promotion ladder of specific community participation students, see： ([Contributor Ladder](https://github.com/chaosblade-io/community/blob/main/Contributor_Ladder.md))

## Business Registration
The original intention of our open source project is to lower the threshold for chaos engineering to be implemented in enterprises, so we highly value the use of the project in enterprises. Welcome everyone here [ISSUE](https://github.com/chaosblade-io/chaosblade/issues/32). After registration, you will be invited to join the corporate mail group to discuss the problems encountered by Chaos Engineering in the landing of the company and share the landing experience.

## Contributors

### Code Contributors

This project exists thanks to all the people who contribute. [[Contribute](CONTRIBUTING.md)].
<a href="https://github.com/chaosblade-io/chaosblade/graphs/contributors"><img src="https://opencollective.com/chaosblade/contributors.svg?width=890&button=false" /></a>

## License
Chaosblade is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full license text.
