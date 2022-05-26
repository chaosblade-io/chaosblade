# 逻辑流程简介

了解完相关模型接口之后，在此之上我们可以继续简单的了解模型之间是如何进行交互，一条命令如：`blade create cpu fullload`输入回车之后，系统是如何一步步解析成对应的模型，并最终执行达到压测负载效果的。话不多说，看下图：

![流程简图V0.0.1](https://user-images.githubusercontent.com/3992234/56200113-bc1afe00-6070-11e9-82ef-860b68b14827.png)

## Cobra框架

首先建议了解一下Cobra，它是go的一个开源工具库，提供简单的接口来创建强大现代的CLI接口，类似于git或者go工具。同时，它也是一个应用，用来生成个人应用框架，组织系统命令、子命令以及相关参数，关于cobra更多具体信息详见[这里官方说明](https://github.com/spf13/cobra)，从而开发以Cobra为基础的应用。Docker源码中使用了Cobra。

上图中一开始的添加各种指令，诸如：version、prepare、revoke、create等，在项目源码中就是基于cobra进行实现的，然后再进行二级命令以及相关参数的封装逻辑。

## chaosblade 流程简介

首先，程序在一开始的时候，会添加各类基础命令，诸如：version、prepare、revoke、create等，这些命令除了create拥有多种二级子命令之外，其余都是原生的Cobra命令模型，只有一级命令搭配参数进行操作。这些命令相对来说都比较简单，具体实现可以直接看源码，在熟悉了Cobra之后看一眼就能明白。

除了项目源码和文档，另一个了解chaosblade用法的方式是通过不断的help提示来获取帮助信息。比如：

```bash
blade help
blade create help
blade create cpu help
```

在你输入blade之后不知所措的时候，一路help下去会有惊喜不断，感谢大神提供的彩蛋。

### 各类create子命令

create的子命令在前面的模型章节中对应于：ExpModelCommandSpec接口。主要有如下几类：

**OS命令** 即操作系统层级的负载命令，目前只支持*nix系统，提供了cpu、disk、network、mem等负载命令。

**DockerOS命令** 即Docker容器中的操作系统层级的负载命令，这个和上述的OS命令基本是保持一致的，从源码也可以发现，它只是对OS命令对象做了个封装，然后就塞进Docker命令对象中。

还有Jvm、k8s等，可以看出这一级命令名称是名词，也就是呼应了ExpModelCommandSpec这个接口的命名含义，它就是个目标model操作对象。在这些model对象后面的二级子命令才是各种操作action，如fullload。

### Model对象下的Action

接上Model子命令，这一级二级子命令对应的接口模型为：ExpActionCommandSpec。比如cpu fullload, network delay和drop等。属于真正执行操作的命令，从其命名为动词上可看出。

系统解析到这步之后，就会带上参数ExpFlagSpec去调用具体的bin目录下对应的命令，实现系统负载操作。

## 总结

本文首先通过逻辑流程简图，展示了系统大致的运作流程。然后简单介绍了Cobra库的功能及其在本项目中的作用。之后从命令的结构上，一级级介绍其运作流程，更多详细信息请参考源码实现。


## 参考

- [Cobra官方说明](https://github.com/spf13/cobra)


## 文档贡献者
[@Cenyol](https://github.com/Cenyol)
