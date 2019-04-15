![logo](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/doc/image/chaosblade-logo.png)  

# Chaosblade: 一个简单易用且功能强大的混沌实验实施工具

## 项目简介
Chaosblade 是遵循混沌工程（Chaos Engineering）原理的实验工具，用于模拟常见的故障场景，帮助提升分布式系统的可恢复性和对故障的容错性。

Chaosblade 是内部 MonkeyKing 对外开源的项目，其建立在阿里巴巴近十年故障测试和演练实践基础上，结合了集团各业务的最佳创意和实践。

Chaosblade 可直接编译运行，cli 命令提示使执行混沌实验更加简单。目前支持的演练场景有操作系统类的 CPU、磁盘、进程、网络，Java 应用类的 Dubbo、MySQL、Servlet 和自定义类方法延迟或抛异常等以及杀容器、杀 Pod，具体可执行 `blade create -h` 查看：

## 使用文档
Chaosblade 的 cli 工具是 blade，下载或编译后可直接使用。blade 命令列表如下：

* prepare：简写 p，混沌实验前的准备，比如演练 Java 应用，则需要挂载 java agent。要演练应用名是 business 的应用，则在目标主机上执行 `blade p jvm --process business`。如果挂载成功，返回挂载的 uid，用于状态查询或者撤销挂载使用。
* revoke：简写 r，撤销之前混沌实验准备，比如卸载 java agent。命令是 `blade revoke UID`
* create: 简写是 c，创建一个混沌演练实验，指执行故障注入。命令是 `blade create [TARGET] [ACTION] [FLAGS]`，比如实施一次 Dubbo consumer 调用 xxx.xxx.Service 接口延迟 3s，则执行的命令为 `blade create dubbo delay --consumer --time 3000 --service xxx.xxx.Service`，如果注入成功，则返回实验的 uid，用于状态查询和销毁此实验使用。
* destroy：简写是 d，销毁之前的混沌实验，比如销毁上面提到的 Dubbo 延迟实验，命令是 `blade destroy UID`
* status：简写 s，查询准备阶段或者实验的状态，命令是 `blade status UID` 或者 `blade status --type create`

以上命令帮助均可使用 `blade help [COMMAND]` 查看，也可查看[新手指南](https://github.com/chaosblade-io/chaosblade/wiki/%E6%96%B0%E6%89%8B%E6%8C%87%E5%8D%97)，快速上手使用。

## Demo 体验
下载 chaosblade demo 镜像体验 blade 工具的使用。
  
![demo.gif](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/release/chaosblade-demo-0.0.1.gif)  

下载镜像：
```bash
docker pull registry.cn-hangzhou.aliyuncs.com/chaosblade/chaosblade-demo:latest
```

启动镜像：
```bash
docker run -it registry.cn-hangzhou.aliyuncs.com/chaosblade/chaosblade-demo:latest
```

进入镜像之后，可阅读 README.txt 文件实施混沌实验，Enjoy it。

## 本地编译
在项目根目录下执行以下命令进行编译:
```bash
make
```

如果在 mac 操作系统上编译 linux 包，可执行：
```bash
make build_linux
```

如果编译 chaosblade 镜像，可执行：
```bash
make build_image
```

编译过程解析：
* 在项目根目录下创建编译结果文件夹 target 和 chaosblade 版本目录，编译后的文件放在 target/chaosblade-[version] 目录下
* 下载 Java 应用混沌实验所需要的第三方包 [jvm-sandbox](https://github.com/alibaba/jvm-sandbox/releases) 至编译缓存文件夹中（target/cache）
* 下载 chaosblade java agent 和 tools.jar(用于挂载 jvm)，用于实施 Java 混沌实验的 jar 包至编译缓存文件夹（target/cache）
* 解压 JVM-SANDBOX 包至 target/chaosblade-[version]/lib 目录；拷贝 chaosblade java agent jar 到 JVM-SANDBOX 模块目录（target/chaosblade-[version]/lib/sandbox/module）
* 编译 blade（cli 命令工具）到 target/chaosblade-[version] 目录，实施混沌实验所需要的其他程序会编译到 target/chaosblade-[version]/bin 目录下
* 编译完成，可以进入 target/chaosblade-[version] 目录，即可使用 blade 工具


清除编译后文件:
```bash
make clean
```

## 组件架构
![component.png](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/doc/image/component.png)

* Cli 包含 create、destroy、status、prepare、revoke、version 6 个命令
* 相关混沌实验数据使用 SQLite 存储在本地（chaosblade 目录下）
* Create 和 destroy 命令调用相关的混沌实验执行器创建或者销毁混沌实验
* Prepare 和 revoke 命令调用混沌实验准备执行器准备或者恢复实验环境，比如挂载 jvm-sandbox
* 混沌实验和混沌实验环境准备记录都可以通过 status 命令查询

## 场景覆盖图
![ecosystem.png](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/doc/image/ecosystem.png)
