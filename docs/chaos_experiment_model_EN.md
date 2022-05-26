Following this model, it is straightforward to perform a chaos experiment and control the minimum explosion radius of the experiment.The [chaosblade](https://github.com/chaosblade-io/chaosblade) and [chaosblade-exec-jvm](https://github.com/chaosblade-io/chaosblade-exec-jvm) projects are implemented according to this model.

## Model Definition
Before giving the model discuss the problem of implementing a chaos experiment explicitly.

- What to do the chaos experiment on
- What is the scope of the chaos experiment
- What are the specific experiments to be performed
- What are the matching conditions for the experiment to be effective?
  
For example, an application on a 10.0.0.1 machine with an IP address of 10.0.0.1 calls the com.example.HelloService@1.0.0 Dubbo service with a latency of 3s. Based on the list of problems above, the first thing that is clear is that you want to experiment with Dubbo component chaos, implementing the experiment on a single 10.0.0.1 machine, calling com. example.HelloService@1.0.0 service to simulate a 3s delay. By specifying the above, you can precisely implement a chaos experiment by abstracting the following model:

<img width="572" alt="fault-injection model" src="https://user-images.githubusercontent.com/3992234/55319674-fd73b100-54a7-11e9-8a8c-15d0fb8f2758.png">

* Target: The target of the experiment, the component on which the experiment takes place, such as container, application framework (Dubbo, Redis, Zookeeper), etc.
* Scope: The scope of the experiment implementation, referring to the specific machine or cluster that triggers the experiment, etc.
* Matcher: Experiment rule matcher, according to the configured Target, define the relevant experiment matching rules, can be configured multiple. As each Target may have its own special matching conditions, for example, HSF and Dubbo in the RPC domain can be matched according to the services provided by the service provider and the services invoked by the service consumer, and Redis in the caching domain can be matched according to set and get operations.
* Action: refers to the specific scenario of the experiment simulation, Target is different, the implementation of the scenario is also different, for example, the disk, you can rehearse the disk full, disk IO read and write high, disk hardware failure, etc.. If it is an application, you can abstract the experimental scenarios such as delay, exception, return specified values (error codes, large objects, etc.), parameter tampering, repeated calls, etc.

Returning to the above example, it can be described as a failure drill for a Dubbo component (Target), for an application on host 10.0.0.1 (Scope), calling the com.example.HelloService@1.0.0 (Matcher) service with a 3s delay (Action).

The pseudo code can be written as follows：
```java
Toolkit.
    // Target
    dubbo.
    // Scope
    host("1.0.0.1").
    // Matcher 
    consumer().
    // Matcher
    service("com.example.HelloService").
    // Matcher
    version("1.0.0").
    // Action
    delay(3000);
```

# chaosblade mode implementation

## chaosblade cli call
For the above example, the chaosblade call command is:
```
blade create dubbo delay --time 3000 --consumer --service com.example.HelloService --version 1.0.0
```
- ```dubbo```: Target in the model, which performs experiments on dubbo.
- ```delay```: The action in the model that executes the delayed walkthrough scenario.
- ```--time```: action argument in the model, referring to the delay time.
- ```--consumer```, ```--service```, ```--version```: matchers in the model, experimental rule matchers.
  
**Note**: Since chaosblade is a tool that executes on a single machine, the scope in the chaos experiment model defaults to the local machine and no further declarations are shown.

## Chaosblade model structure diagram

In order to have a more intuitive understanding, let's take a general look at the relationship between the models through the following model structure diagram. The core interface model is ExpModelCommandSpec, from which the two interfaces ExpActionCommandSpec and ExpFlagSpec are derived. The ExpModelCommandSpec has specific implementations such as cpu, network, disk, etc.; the ExpActionCommandSpec is such as fullload under cpu, etc.; the ExpFlagSpec is a variety of custom parameters, such as --timeout. For more detailed model definitions, please see the subsequent subsections.

![模型简图](https://user-images.githubusercontent.com/3992234/56200214-ecfb3300-6070-11e9-9c33-a318eb305bd9.png)

## chaosblade model definition
```go
type ExpModelCommandSpec interface {
	// Component Name
	Name() string

	// List of supported scenarios
	Actions() []ExpActionCommandSpec

	// ... 
}
```
**Note**: Definition of a component chaos experiment model with component names and a list of supported experiment scenarios.
```
type ExpActionCommandSpec interface {
	// Experimental scene name
	Name() string

	// Rule Matcher List
	Matchers() []ExpFlagSpec

	// Action parameter List
	Flags() []ExpFlagSpec

	// Action Executor
	Executor(channel Channel) Executor

    // ...
}
```
**Note**: Definition of an experimental scenario action, including the scenario name, the parameters required for the scenario and some experimental rule matchers
```go
type ExpFlagSpec interface {
    // Parameter Name
	FlagName() string

    // Parameter Description
	FlagDesc() string

    // Whether parameter values are required
	FlagNoArgs() bool

    // Is it a required parameter
	FlagRequired() bool
}
```
**Note**: Experimental matcher definition.

## Chaosblade model implementation
Take the network component as an example, network as a chaos experiment component, currently contains network latency, network shielding, network packet loss, DNS tampering exercise scenarios, then according to the model specification, the specific implementation as follows.
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
The network target defines four chaotic experiment scenarios, DelayActionSpec, DropActionSpec, DnsActionSpec, and LossActionSpec, where DelayActionSpec is defined as follows.

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
* `DelayActionSpec` contains 2 scene parameters and 4 rule matchers.

## Summary
The above examples show that this model is simple, easy to implement, and can cover the currently known experimental scenarios. This model can be improved later to become a standard for chaotic experiments.

## Appendix A
Application-level generic failure scenarios.

* Delay
* Exceptions
* Returning a specific value
* Modifying a parameter value
* Repeated calls
* try-catch block exceptions

## Document Contributors
[@xcaspar](https://github.com/xcaspar)  
[@Cenyol](https://github.com/Cenyol)
[@Super-long](https://github.com/Super-long)