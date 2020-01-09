![logo](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/doc/image/chaosblade-logo.png)  

# Chaosblade: An Easy to Use and Powerful Chaos Engineering Toolkit
[![Build Status](https://travis-ci.org/chaosblade-io/chaosblade.svg?branch=master)](https://travis-ci.org/chaosblade-io/chaosblade)
[![codecov](https://codecov.io/gh/chaosblade-io/chaosblade/branch/master/graph/badge.svg)](https://codecov.io/gh/chaosblade-io/chaosblade)
![license](https://img.shields.io/github/license/chaosblade-io/chaosblade.svg)
## Introduction

Chaosblade is an experimental tool that follows the principles of Chaos Engineering and is used to simulate common fault scenarios, helping to improve the recoverability of faulty systems and the fault tolerance of faults.

Chaosblade is Alibaba's internal MonkeyKing open source project. It is based on Alibaba's nearly ten years of fault testing and practice, combining the best ideas and practices of the Group's businesses.

Chaosblade can be compiled and run directly, and the cli command prompt makes it easier to perform chaos engineering experiments. Currently supported experimental areas include os, java, docker and kubernetes, for example, filling disk, killing the process, network delay, Dubbo, MySQL, Servlet and custom class methods of Java application class delay or exception, kill container, kill Pod and so on. You can execute `blade create -h` command to view

中文使用文档：https://chaosblade-io.gitbook.io/chaosblade-help-zh-cn/

## CLI Command

Chaosblade's cli tool is a blade that can be used directly after downloading or compiling. The list of blade commands is as follows:
* **prepare**: alias is p, preparation before the chaos engineering experiment, such as drilling Java applications, you need to attach the java agent. For example, to drill an application whose application name is business, execute `blade p jvm --process business` on the target host. If the attach is successful, return the uid for status query or agent revoke.
* **revoke**: alias is r, undo chaos engineering experiment preparation before, such as detaching java agent. The command is `blade revoke UID`
* **create**: alias is c, create a chaos engineering experiment. The command is `blade create [TARGET] [ACTION] [FLAGS]`. For example, if you implement a Dubbo consumer call xxx.xxx.Service interface delay 3s, the command executed is `blade create dubbo delay --consumer --time 3000 -- Service xxx.xxx.Service`, if the injection is successful, return the experimental uid for status query and destroy the experiment.
* **destroy**: alias is d, destroy a chaos engineering experiment, such as destroying the Dubbo delay experiment mentioned above, the command is `blade destroy UID`
* **status**: alias s, query preparation stage or experiment status, the command is `blade status UID` or `blade status --type create`

Use the `blade help [COMMAND]` command to view help


## Experience Demo
Download the chaosblade demo image and experience the use of the blade toolkit
  
![demo.gif](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/release/chaosblade-demo-0.0.1.gif)  

Download image command：
```bash
docker pull registry.cn-hangzhou.aliyuncs.com/chaosblade/chaosblade-demo:latest
```

Run the demo container：
```bash
docker run -it registry.cn-hangzhou.aliyuncs.com/chaosblade/chaosblade-demo:latest
```

After entering the container, you can read the README.txt file to implement the chaos experiment, Enjoy it.

## Compile
Install [Golang](https://golang.org/doc/install) first, then download the project to `GOPATH`:
```bash
go get github.com/chaosblade-io/chaosblade
```
This project was downloaded to the `GOPATH/src/github.com/chaosblade-io/chaosblade` directory. You can execute `go env` command to view the `GOPATH` value. 

Go to the project root directory(`GOPATH/src/github.com/chaosblade-io/chaosblade`) and execute compile:
```bash
make
```

If you compile the Linux package on the Mac operating system, you can do:
```bash
make build_linux
```

If you compile the chaosblade image, you can do:
```bash
make build_image
```

Compilation process:
* Create the compilation result folder target and chaosblade version directory in the project root directory, and the compiled file is placed in the target/chaosblade-[version] directory.
* Download the third-party package [jvm-sandbox](https://github.com/alibaba/jvm-sandbox/releases) required by Java Application Chaos Experiment to the cache folder (target/cache)
* Download chaosblade java agent and tools.jar (for attaching jvm), jar package for implementing Java chaos experiments to cache folder (target/cache)
* Unzip the JVM-SANDBOX package to the target/chaosblade-[version]/lib directory; copy the chaosblade java agent jar to the JVM-SANDBOX module directory (target/chaosblade-[version]/lib/sandbox/module)
* Compile the blade (cli command tool) to the target/chaosblade-[version] directory, and other programs needed to implement the chaos experiment will be compiled into the target/chaosblade-[version]/bin directory.
* Compile is complete, you can enter the target/chaosblade-[version] directory, you can use the blade toolkit.

clean compilation:
```bash
make clean
```

## Contributing
We welcome every contribution, even if it is just punctuation. See details of [CONTRIBUTING](CONTRIBUTING.md)

## Bugs and Feedback
For bug report, questions and discussions please submit [GitHub Issues](https://github.com/chaosblade-io/chaosblade/issues).

Contact us: chaosblade.io.01@gmail.com

Gitter room: [chaosblade community](https://gitter.im/chaosblade-io/community)


## Component Architecture 
![component.png](https://user-images.githubusercontent.com/3992234/58927455-2f8fe080-8781-11e9-9a5e-4e251b1e50f9.png)

* Cli contains create, destroy, status, prepare, revoke, version commands
* Relevant chaos experiment data is stored locally using SQLite (under the chaosblade directory)
* Create and destroy commands are used to create or destroy chaos experiments
* Prepare and revoke commands are used to prepare or revoke experimental environment，such as attaching jvm-sandbox
* Chaos experiment and environment preparation record can be queried by status command


## Executor Project
* [chaosblade-exec-jvm](https://github.com/chaosblade-io/chaosblade-exec-jvm): chaosblade executor for Java Applications


## Ecosystem Architecture
![ecosystem.png](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/doc/image/ecosystem.png)


## License
Chaosblade is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full license text.
