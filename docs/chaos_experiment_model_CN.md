遵循此模型，可以简单明了的执行一次混沌实验，控制实验的最小爆炸半径。并且可以方便快捷的扩展新的实验场景或者增强现有场景。[chaosblade](https://github.com/chaosblade-io/chaosblade) 和 [chaosblade-exec-jvm](https://github.com/chaosblade-io/chaosblade-exec-jvm) 工程都根据此模型实现。

# 模型定义

在给出模型之前先讨论实施一次混沌实验明确的问题：
* 对什么做混沌实验
* 混沌实验实施的范围是是什么
* 具体实施什么实验
* 实验生效的匹配条件有哪些

举个例子：一台 ip 是 10.0.0.1 机器上的应用，调用 com.example.HelloService@1.0.0 Dubbo 服务延迟 3s。根据上述的问题列表，先明确的是要对 Dubbo 组件混沌实验，实施实验的范围是 10.0.0.1 单机，对调用 com.example.HelloService@1.0.0 服务模拟 3s 延迟。 明确以上内容，就可以精准的实施一次混沌实验，抽象出以下模型：

<img width="572" alt="fault-injection model" src="https://user-images.githubusercontent.com/3992234/55319674-fd73b100-54a7-11e9-8a8c-15d0fb8f2758.png">

* Target：实验靶点，指实验发生的组件，例如 容器、应用框架（Dubbo、Redis、Zookeeper）等。
* Scope：实验实施的范围，指具体触发实验的机器或者集群等。
* Matcher：实验规则匹配器，根据所配置的 Target，定义相关的实验匹配规则，可以配置多个。由于每个 Target 可能有各自特殊的匹配条件，比如 RPC 领域的 HSF、Dubbo，可以根据服务提供者提供的服务和服务消费者调用的服务进行匹配，缓存领域的 Redis，可以根据 set、get 操作进行匹配。
* Action：指实验模拟的具体场景，Target 不同，实施的场景也不一样，比如磁盘，可以演练磁盘满，磁盘 IO 读写高，磁盘硬件故障等。如果是应用，可以抽象出延迟、异常、返回指定值（错误码、大对象等）、参数篡改、重复调用等实验场景。

回到上述的例子，可以叙述为对 Dubbo 组件（Target）进行故障演练，演练的是 10.0.0.1 主机（Scope）的应用，调用 com.example.HelloService@1.0.0 （Matcher）服务延迟 3s（Action）。

伪代码可以写成：
```java
Toolkit.
    // 实验靶点
    dubbo.
    // 范围，此处是主机
    host("1.0.0.1").
    // 组件匹配器，消费者还是服务提供者
    consumer().
    // 组件匹配器，服务接口
    service("com.example.HelloService").
    // 组件匹配器，1.0.0 接口版本
    version("1.0.0").
    // 实验场景，延迟 3s
    delay(3000);
```

# chaosblade 模型实现

## chaosblade cli 调用
针对上述例子，chaosblade 调用命令是： 
```
blade create dubbo delay --time 3000 --consumer --service com.example.HelloService --version 1.0.0
```
* `dubbo`: 模型中的 target，对 dubbo 实施实验。
* `delay`: 模型中的 action，执行延迟演练场景。
* `--time`: 模型中 action 参数，指延迟时间。
* `--consumer`、`--service`、`--version`：模型中的 matchers，实验规则匹配器。

**注：** 由于 chaosblade 是在单机执行的工具，所以混沌实验模型中的 scope 默认为本机，不再显示声明。

## chaosblade 模型结构图

为了有个更加直观的认识，我们先通过以下的模型结构图来大致看一下模型之间的关系。核心接口模型是：ExpModelCommandSpec，由它引申出来的是ExpActionCommandSpec和ExpFlagSpec这两个接口。其中，ExpModelCommandSpec已有的具体实现有：cpu、network、disk等；ExpActionCommandSpec则是如cpu下的fullload之类的；ExpFlagSpec是各类自定义参数，比如--timeout。更加详细的模型定义说明请见后续小节。

![模型简图](https://user-images.githubusercontent.com/3992234/56200214-ecfb3300-6070-11e9-9c33-a318eb305bd9.png)

## chaosblade 模型定义
```go
type ExpModelCommandSpec interface {
	// 组件名称
	Name() string

	// 支持的场景列表
	Actions() []ExpActionCommandSpec

	// ...
}
```
**注：** 一个组件混沌实验模型的定义，包含组件名称和所支持的实验场景列表。
```go
type ExpActionCommandSpec interface {
	// 演练场景名称
	Name() string

	// 规则匹配器列表
	Matchers() []ExpFlagSpec

	// Action 参数列表
	Flags() []ExpFlagSpec

	// Action 执行器
	Executor(channel Channel) Executor

    // ...
}
```
**注：** 一个实验场景 action 的定义，包含场景名称，场景所需参数和一些实验规则匹配器
```go
type ExpFlagSpec interface {
    // 参数名
	FlagName() string

    // 参数描述
	FlagDesc() string

    // 是否需要参数值
	FlagNoArgs() bool

    // 是否是必要参数
	FlagRequired() bool
}
```
**注：** 实验匹配器定义。

## chaosblade 模型具体实现
拿 network 组件举例，network 作为混沌实验组件，目前包含网络延迟、网络屏蔽、网络丢包、DNS 篡改演练场景，则依据模型规范，具体实现为：
```go
type NetworkCommandSpec struct {
}

func (*NetworkCommandSpec) Name() string {
	return "network"
}

func (*NetworkCommandSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&DelayActionSpec{},
		&DropActionSpec{},
		&DnsActionSpec{},
		&LossActionSpec{},
	}
}
```

network target 定义了 `DelayActionSpec`、`DropActionSpec`、`DnsActionSpec`、`LossActionSpec` 四种混沌实验场景，其中 `DelayActionSpec` 定义如下：

```go
type DelayActionSpec struct {
}

func (*DelayActionSpec) Name() string {
	return "delay"
}

func (*DelayActionSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "local-port",
			Desc: "Port for external service",
		},
		&exec.ExpFlag{
			Name: "remote-port",
			Desc: "Port for invoking",
		},
		&exec.ExpFlag{
			Name: "exclude-port",
			Desc: "Exclude one local port, for example 22 port. This flag is invalid when --local-port or remote-port is specified",
		},
		&exec.ExpFlag{
			Name:     "device",
			Desc:     "Network device",
			Required: true,
		},
	}
}

func (*DelayActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "time",
			Desc:     "Delay time, ms",
			Required: true,
		},
		&exec.ExpFlag{
			Name: "offset",
			Desc: "Delay offset time, ms",
		},
	}
}

func (*DelayActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkDelayExecutor{channel}
}
```
* `DelayActionSpec` 包含 2 个场景参数和 4 个规则匹配器。

# 总结
通过以上事例，可以看出此模型简单、易实现，并且可以覆盖目前已知的实验场景。后续可以对此模型进行完善，成为一个混沌实验标准。

# 附录 A

应用级别通用的故障场景：
* 延迟
* 异常
* 返回特定值
* 修改参数值
* 重复调用
* try-catch 块异常


# 文档贡献者
[@xcaspar](https://github.com/xcaspar)  
[@Cenyol](https://github.com/Cenyol)
