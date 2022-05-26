欢迎关注 chaosblade 项目！这篇文章将带你快速掌握 chaosblade 工具的使用。

# 下载 chaosblade

获取 chaosblade 最新的 [release](https://github.com/chaosblade-io/chaosblade/releases) 包，目前支持的平台是 linux/amd64 和 darwin/64，下载对应平台的包。

下载完成后解压即可，无需编译。


# 使用 chaosblade
进入解压后的文件夹，可以看到以下内容：
```
├── bin
│   ├── chaos_fuse
│   ├── chaos_os
│   ├── nsexec
│   └── strace
├── blade
└── lib
│    ├── cplus
│    └── sandbox
├── logs
└── yaml
```

其中 blade 是可执行文件，即 chaosblade 工具的 cli，混沌实验执行的工具。执行 `./blade help` 可以查看支持命令有哪些：
```
An easy to use and powerful chaos engineering experiment toolkit

Usage:
  blade [command]

Available Commands:
  create      Create a chaos engineering experiment
  destroy     Destroy a chaos experiment
  help        Help about any command
  prepare     Prepare to experiment
  revoke      Undo chaos engineering experiment preparation
  status      Query preparation stage or experiment status
  version     Print version info

Flags:
  -d, --debug   Set client to DEBUG mode
  -h, --help    help for blade

Use "blade [command] --help" for more information about a command.
```
## 执行你的第一个混沌实验
我们拿 CPU 满载(CPU 使用率 100%) 演练场景举例（！！**注意，在不清楚影响面的情况下，切勿在生产系统机器上执行**），执行以下命令实施实验：
```
./blade create cpu fullload
```

执行结果返回：
```
{"code":200,"success":true,"result":"7c1f7afc281482c8"}
```

通过 `top` 命令查看 CPU 使用率：
```
CPU usage: 93.79% user, 6.20% sys, 0.0% idle
```

此时命令已经生效，**停止混沌实验**，执行：
```
./blade destroy 7c1f7afc281482c8 
```

返回以下结果，表示停止实验成功:
```
{"code":200,"success":true,"result":"command: cpu fullload --debug false --help false"}
``` 

再去观察 CPU 情况，CPU 负载已回到正常状态：
```
CPU usage: 6.36% user, 4.74% sys, 88.88% idle 
```
一次 CPU 满载演练完成。

## 你的第二个混沌实验
这次实验，我们演练 Dubbo 应用，我们的需求是 consumer 调用 com.alibaba.demo.HelloService 服务下的 hello 接口延迟 3 秒。接下来我们下载所需要的 Dubbo Demo： 
 
[dubbo-provider](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/demo/dubbo-provider-1.0-SNAPSHOT.jar)  
[dubbo-consumer](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/demo/dubbo-consumer-1.0-SNAPSHOT.jar)

下载完成后，执行以下命令启动应用，注意必须先启动 `dubbo-provider`，然后再启动 `dubbo-consumer`:
```
# 启动 dubbo-provider
nohup java -Djava.net.preferIPv4Stack=true -Dproject.name=dubbo-provider -jar dubbo-provider-1.0-SNAPSHOT.jar > provider.nohup.log 2>&1 &

# 稍等 2 秒，然后启动 dubbo-consumer
nohup java -Dserver.port=8080 -Djava.net.preferIPv4Stack=true -Dproject.name=dubbo-consumer -jar dubbo-consumer-1.0-SNAPSHOT.jar > consumer.nohup.log 2>&1 &
```
访问 `http://localhost:8080/hello?msg=world`，返回以下信息，表示启动成功：
```
{
    msg: "Dubbo Service: Hello world"
}
```

接下来我们要使用 blade 工具进行混沌实验，在执行实验前，我们需要先执行 prepare 命令，挂载所需要的 java agent：
```
./blade prepare jvm --process dubbo.consumer
```
返回以下结果，表示实验准备成功：
```
{"code":200,"success":true,"result":"e669d57f079a00cc"}
```
我们开始实施混沌实验，我们的需求是 consumer 调用 `com.alibaba.demo.HelloService` 服务下的 `hello` 接口延迟 3 秒。
我们执行 `./blade create dubbo delay -h` 命令查看 dubbo 调用延迟的命令用法：
``` 
Usage:
  blade create dubbo delay

Flags:
      --appname string      The consumer or provider application name
      --consumer            To tag consumer role experiment.
  -h, --help                help for delay
      --methodname string   The method name in service interface
      --offset string       delay offset for the time
      --process string      Application process name
      --provider            To tag provider experiment
      --service string      The service interface
      --time string         delay time (required)
      --version string      the service version

Global Flags:
  -d, --debug   Set client to DEBUG mode
```

调用 `com.alibaba.demo.HelloService` 服务下的 `hello` 接口延迟 3 秒，我们执行以下命令：
```
./blade create dubbo delay --time 3000 --service com.alibaba.demo.HelloService --methodname hello --consumer --process dubbo.consumer
```
返回以下结果，表示执行成功；访问 `http://localhost:8080/hello?msg=world` 验证是否延迟 3 秒。
```
{"code":200,"success":true,"result":"ec695fee1e458fc6"}
```
对实施实验的命令进行解析：
* `--time`: 3000，表示延迟 3000 ms；单位是 ms
* `--service`: com.alibaba.demo.HelloService， 表示调用的服务
* `--methodname`: hello，表示服务接口方法
* `--consumer`: 表示演练的是 dubbo consumer
* `--process`: dubbo.consumer，表示对哪个应用进程实施混沌实验

停止当前延迟的混沌实验，再次访问 url 验证是否恢复正常：
```
./blade destroy ec695fee1e458fc6
```

不尽兴的话，我们再实施调用刚才那个服务抛异常，执行 `./blade create dubbo throwCustomException -h` 命令查看帮助：
```
Throw custom exception with --exception option

Usage:
  blade create dubbo throwCustomException

Aliases:
  throwCustomException, tce

Flags:
      --appname string      The consumer or provider application name
      --consumer            To tag consumer role experiment.
      --exception string    Exception class inherit java.lang.Exception (required)
  -h, --help                help for throwCustomException
      --methodname string   The method name in service interface
      --process string      Application process name
      --provider            To tag provider experiment
      --service string      The service interface
      --version string      the service version

Global Flags:
  -d, --debug   Set client to DEBUG mode
```
和刚才延迟命令参数差不多，因为相同的参数是演练 dubbo 所需要的，不同的是没有了 `--time`，多了个 `--exception` 参数。
我们模拟调用刚才的服务抛 `java.lang.Exception` 异常：
```
./blade create dubbo throwCustomException --exception java.lang.Exception --service com.alibaba.demo.HelloService --methodname hello --consumer --process dubbo.consumer
```
返回以下结果，表示实验执行成功；访问 `http://localhost:8080/hello?msg=world` 验证是否异常
```
{"code":200,"success":true,"result":"09dd96f4c062df69"}
```
停止此次试验，再次访问请求，验证是否恢复：
```
./blade destroy 09dd96f4c062df69 
```

最后，我们撤销刚才的实验准备，即卸载 Java Agent：
```
./blade revoke e669d57f079a00cc
```
如果找不到之前执行 prepare 返回的 UID 的话，执行 `./blade status --type prepare` 命令查询：
```
{
        "code": 200,
        "success": true,
        "result": [
                {
                        "Uid": "e669d57f079a00cc",
                        "ProgramType": "jvm",
                        "Process": "dubbo.consumer",
                        "Port": "59688",
                        "Status": "Running",
                        "Error": "",
                        "CreateTime": "2019-03-29T16:19:37.284579975+08:00",
                        "UpdateTime": "2019-03-29T17:05:14.183382945+08:00"
                }
        ]
}
```

# FAQ
### 如何获取最新版本
chaosblade 每次发布，相关的 changelog 和新版的包都会同步到 RELEASE ，可在此地址下载。

### Windows 平台是否有支持计划
暂无支持计划，不过欢迎大家提相关支持的 issue，社区会根据大家的需求来决定是否支持。

### 执行 blade 命令报错： exec format error 或 cannot execute binary file 
这个问题是由于 chaosblade 包和运行的平台不兼容造成的，请提 [ISSUE](https://github.com/chaosblade-io/chaosblade/issues)来告知我们您遇到的问题，issue 中标注下载的 chaosblade 包版本和操作系统版本信息。
