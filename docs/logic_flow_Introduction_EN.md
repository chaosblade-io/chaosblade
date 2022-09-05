# Logical Process Introduction

After understanding the relevant model interface, we can continue to understand briefly how the models interact with each other. After entering a command such as `blade create cpu fullload`, how the system resolves into the corresponding model step by step, and finally executes to achieve the effect of pressure test load. Without further ado, see the following figure.

![流程简图V0.0.1](https://user-images.githubusercontent.com/3992234/56200113-bc1afe00-6070-11e9-82ef-860b68b14827.png)

## Cobra

It is first recommended to know about Cobra, an open source tool library for go that provides a simple interface to create a powerful modern CLI interface, similar to git or go tools. It is also an application for generating personal application frameworks, organizing system commands, subcommands, and related parameters. For more specific information about cobra see [official description here](https://github.com/spf13/cobra) to develop Cobra-based applications. Cobra is used in the Docker source code.

The above diagram begins with adding various commands, such as version, prepare, revoke, create, etc., in the project source code is based on cobra for implementation, and then the secondary commands and related parameters of the packaging logic.

## chaosblade Process Introduction

First of all, the program at the beginning, will add all kinds of basic commands, such as: version, prepare, revoke, create, etc., these commands in addition to create has a variety of secondary subcommands, the rest are the native Cobra command model, only one level of command with parameters to operate. These commands are relatively simple, the specific implementation can be seen directly in the source code, after familiar with the Cobra look at a glance to understand.

In addition to the project source code and documentation, another way to understand chaosblade usage is to get help information through the constant help prompt. For example:

```bash
blade help
blade create help
blade create cpu help
```

After you type blade overwhelmed, help all the way down will have surprises, thanks to the gods to provide easter eggs!

### Various create subcommands

The subcommands of create correspond in the previous model section: ExpModelCommandSpec interface. The main categories are as follows.

**OS commands** i.e. OS-level load commands, currently only supported for *nix systems, provide load commands for cpu, disk, network, mem, etc.

**DockerOS commands** i.e. OS-level load commands in Docker containers, this is basically consistent with the above OS commands, as you can also find from the source code, it just makes a wrapper around the OS command object, and then it is stuffed into the Docker command object.

And Jvm, k8s, etc., you can see that the name of this level of command is a noun, which echoes the meaning of the naming of the interface ExpModelCommandSpec, it is a target model operation object. In these model objects followed by the second-level subcommand is a variety of operational actions, such as fullload.

### Action under Model object

The interface model corresponding to this level 2 subcommand is ExpActionCommandSpec, such as cpu fullload, network delay and drop. It is a command that actually performs an action, as can be seen from its naming as a verb.

After the system resolves to this step, it will take the parameter ExpFlagSpec to call the corresponding command in the specific bin directory to realize the system load operation.

## Summary

This paper first shows the general operation flow of the system through a logical flow sketch. Then it briefly introduces the functions of Cobra library and its role in this project. After that, it introduces the operation flow from the command structure at one level, please refer to the source code implementation for more details.

## Reference

- [Cobra Official Description](https://github.com/spf13/cobra)

## Document Contributors
[@Cenyol](https://github.com/Cenyol)
[@Super-long](https://github.com/Super-long)