Welcome to the chaosblade project! this article will take you through the chaosblade tool quickly.

# Download chaosblade

Get the latest [release](https://github.com/chaosblade-io/chaosblade/releases) package of chaosblade, currently supported by linux/amd64 and darwin/64, and download the package for the corresponding platform.

Just download and unzip it, no need to compile.

# Using chaosblade
When you enter the unzipped folder, you can see the following contents:
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

where blade is the executable file, the cli of the chaosblade tool, the tool for chaos experiments. Execute `. /blade help` to see what commands are supported:

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

## Perform your first chaos experiment
Let's take a CPU full (100% CPU usage) exercise scenario as an example (!!! **Note, do not execute on the production system machine without knowing the impact surface**), and execute the following command to execute the experiment:

```
./blade create cpu fullload
```

Execution results return:
```
{"code":200,"success":true,"result":"7c1f7afc281482c8"}
```

View CPU usage with the `top` command:
```
CPU usage: 93.79% user, 6.20% sys, 0.0% idle
```

At this point the command is in effect, **Stop Chaos Experiment** and execute;
```
./blade destroy 7c1f7afc281482c8 
```

The following result is returned to indicate the success of the stop experiment:
```
CPU usage: 6.36% user, 4.74% sys, 88.88% idle 
```

A CPU full load walkthrough is completed.

## Your second chaos experiment
For this experiment, we walk through the Dubbo application, and our requirement is that the consumer calls the hello interface under the com.alibaba.demo.HelloService service with a delay of 3 seconds. Next, we download the Dubbo demo we need. 

[dubbo-provider](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/demo/dubbo-provider-1.0-SNAPSHOT.jar)  
[dubbo-consumer](https://chaosblade.oss-cn-hangzhou.aliyuncs.com/demo/dubbo-consumer-1.0-SNAPSHOT.jar)

After downloading, execute the following command to start the application. Note that you must start `dubbo-provider` first, and then `dubbo-consumer`:

```
# Start dubbo-provider
nohup java -Djava.net.preferIPv4Stack=true -Dproject.name=dubbo-provider -jar dubbo-provider-1.0-SNAPSHOT.jar > provider.nohup.log 2>&1 &

# Wait 2 seconds, then start dubbo-consumer
nohup java -Dserver.port=8080 -Djava.net.preferIPv4Stack=true -Dproject.name=dubbo-consumer -jar dubbo-consumer-1.0-SNAPSHOT.jar > consumer.nohup.log 2>&1 &
```
Visit `http://localhost:8080/hello?msg=world` and return the following message indicating a successful start.
```
{
    msg: "Dubbo Service: Hello world"
}
```

Next, we will use the blade tool to perform chaos experiments. Before we can perform the experiments, we need to execute the prepare command to mount the required java agent.

```
./blade prepare jvm --process dubbo.consumer
```
The following results are returned to indicate successful experiment preparation:
```
{"code":200,"success":true,"result":"e669d57f079a00cc"}
```
We start implementing chaos experiments, and our requirement is that consumer calls to the `hello` interface under the `com.alibaba.demo.HelloService` service are delayed by 3 seconds.
We execute `. /blade create dubbo delay -h` command to see the command usage for the dubbo call delay:
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

Calling the `hello` interface under the `com.alibaba.demo.HelloService` service is delayed by 3 seconds and we execute the following command.
```
./blade create dubbo delay --time 3000 --service com.alibaba.demo.HelloService --methodname hello --consumer --process dubbo.consumer
```
The following result is returned, indicating successful execution; visit `http://localhost:8080/hello?msg=world` to verify that the delay is 3 seconds.
```
{"code":200,"success":true,"result":"ec695fee1e458fc6"}
```
Explanation of the order to perform the experiment：
* `--time`: 3000, indicates a delay of 3000 ms; the unit is ms
* `--service`: com.alibaba.demo.HelloService, indicating the called service
* `-methodname`: hello, indicating the service interface method
* `--consumer`: indicates that the walkthrough is a dubbo consumer
* `--process`: dubbo.consumer, indicating which application process to implement the chaos experiment on

Stop the chaos experiment with the current delay and visit the url again to verify that it is back to normal.
```
./blade destroy ec695fee1e458fc6
```

We can also implement a call to that service to throw an exception by running `. /blade create dubbo throwCustomException -h` command to see help：
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
The same parameters as the delay command are needed to walk through dubbo, but without the `--time` and with an additional `--exception` parameter.
We simulate the call to the service we just made by throwing the `java.lang.Exception` exception:
```
./blade create dubbo throwCustomException --exception java.lang.Exception --service com.alibaba.demo.HelloService --methodname hello --consumer --process dubbo.consumer
```
The following result is returned, indicating successful execution of the experiment; visit `http://localhost:8080/hello?msg=world` to verify if there is an exception.
```
{"code":200,"success":true,"result":"09dd96f4c062df69"}
```
Stop this trial, access the request again and verify that it is restored.
```
./blade destroy 09dd96f4c062df69 

```
Finally, we undo the preparation for the experiment we just did, i.e., uninstall the Java Agent.
```
./blade revoke e669d57f079a00cc
```
If you can't find the UID returned by the previous prepare, execute `. /blade status --type prepare` command to query.
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
### How to get the latest version
Every time chaosblade is released, the related changelog and the new version of the package will be synchronized to RELEASE, which can be downloaded at [this address](https://github.com/chaosblade-io/chaosblade/releases).

### Is there a support plan for the Windows platform
There is no support plan, but you are welcome to raise relevant support issues, the community will decide whether to support according to your needs.

### Executing the blade command reports an error: exec format error or cannot execute binary file 

This problem is caused by an incompatibility between the chaosblade package and the running platform. Please inform us about the problem by mentioning [ISSUE](https://github.com/chaosblade-io/chaosblade/issues), and mark the issue with the downloaded chaosblade package version and The operating system version information is indicated in the issue.