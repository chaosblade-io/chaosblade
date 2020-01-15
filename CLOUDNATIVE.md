## Chaosblade Operator：在云原生场景下，将 Kubernetes 设计理解与混沌实验模型相结合标准化实现方案 

![chaosblade operator](https://user-images.githubusercontent.com/3992234/72435800-ed7c5200-37d9-11ea-9224-359d1d9103cb.png)

[chaosblade-operator](https://github.com/chaosblade-io/chaosblade-operator) 项目是针对 Kubernetes 平台所实现的混沌实验注入工具，遵循上述混沌实验模型规范化实验场景，把实验定义为 Kubernetes CRD 资源，将实验模型中的四部分映射为 Kubernetes 资源属性，很友好的将混沌实验模型与 Kubernetes 声明式设计结合在一起，依靠混沌实验模型便捷开发场景的同时，又可以很好的结合 Kubernetes 设计理念，通过 kubectl 或者编写代码直接调用 Kubernetes API 来创建、更新、删除混沌实验，而且资源状态可以非常清晰的表示实验的执行状态，标准化实现 Kubernetes 故障注入。除了使用上述方式执行实验外，还可以使用 chaosblade cli 方式非常方便的执行 kubernetes 实验场景，查询实验状态等。
遵循混沌实验模型实现的 chaosblade operator 除上述优势之外，还可以实现基础资源、应用服务、Docker 容器等场景复用，大大方便了 Kubernetes 场景的扩展，所以在符合 Kubernetes 标准化实现场景方式之上，结合混沌实验模型可以更有效、更清晰、更方便的实现、使用混沌实验场景。
下面通过一个具体的案例来说明 chaosblade-operator 的使用：对 cn-hangzhou.192.168.0.205 节点本地端口 40690 访问模拟 60% 的网络丢包。
**使用 yaml 配置方式，使用 kubectl 来执行实验**
```
apiVersion: chaosblade.io/v1alpha1
kind: ChaosBlade
metadata:
  name: loss-node-network-by-names
spec:
  experiments:
  - scope: node
    target: network
    action: loss
    desc: "node network loss"
    matchers:
    - name: names
      value: ["cn-hangzhou.192.168.0.205"]
    - name: percent
      value: ["60"]
    - name: interface
      value: ["eth0"]
    - name: local-port
      value: ["40690"]
```
执行实验：
```
kubectl apply -f loss-node-network-by-names.yaml
```
查询实验状态，返回信息如下（省略了 spec 等内容）：
```
~ » kubectl get blade loss-node-network-by-names -o json                                                            
{
    "apiVersion": "chaosblade.io/v1alpha1",
    "kind": "ChaosBlade",
    "metadata": {
        "creationTimestamp": "2019-11-04T09:56:36Z",
        "finalizers": [
            "finalizer.chaosblade.io"
        ],
        "generation": 1,
        "name": "loss-node-network-by-names",
        "resourceVersion": "9262302",
        "selfLink": "/apis/chaosblade.io/v1alpha1/chaosblades/loss-node-network-by-names",
        "uid": "63a926dd-fee9-11e9-b3be-00163e136d88"
    },
        "status": {
        "expStatuses": [
            {
                "action": "loss",
                "resStatuses": [
                    {
                        "id": "057acaa47ae69363",
                        "kind": "node",
                        "name": "cn-hangzhou.192.168.0.205",
                        "nodeName": "cn-hangzhou.192.168.0.205",
                        "state": "Success",
                        "success": true,
                        "uid": "e179b30d-df77-11e9-b3be-00163e136d88"
                    }
                ],
                "scope": "node",
                "state": "Success",
                "success": true,
                "target": "network"
            }
        ],
        "phase": "Running"
    }
}
```
通过以上内容可以很清晰的看出混沌实验的运行状态，执行以下命令停止实验：
```
kubectl delete -f loss-node-network-by-names.yaml
```
或者直接删除此 blade 资源
```
kubectl delete blade loss-node-network-by-names
```
还可以编辑 yaml 文件，更新实验内容执行，chaosblade operator 会完成实验的更新操作。

**使用 chaosblade cli 的 blade 命令执行**
```
blade create k8s node-network loss --percent 60 --interface eth0 --local-port 40690 --kubeconfig config --names cn-hangzhou.192.168.0.205
```
如果执行失败，会返回详细的错误信息；如果执行成功，会返回实验的 UID：
```
{"code":200,"success":true,"result":"e647064f5f20953c"}
```
可通过以下命令查询实验状态：
```
blade query k8s create e647064f5f20953c --kubeconfig config

{
  "code": 200,
  "success": true,
  "result": {
    "uid": "e647064f5f20953c",
    "success": true,
    "error": "",
    "statuses": [
      {
        "id": "fa471a6285ec45f5",
        "uid": "e179b30d-df77-11e9-b3be-00163e136d88",
        "name": "cn-hangzhou.192.168.0.205",
        "state": "Success",
        "kind": "node",
        "success": true,
        "nodeName": "cn-hangzhou.192.168.0.205"
      }
    ]
  }
}
```
销毁实验：
```
blade destroy e647064f5f20953c
```
除了上述两种方式调用外，还可以使用 kubernetes client-go 方式执行，具体可参考：[executor.go](https://github.com/chaosblade-io/chaosblade/blob/master/exec/kubernetes/executor.go) 代码实现。

通过上述介绍，可以看出在设计 ChaosBlade 项目初期就考虑了云原生实验场景，将混沌实验模型与 Kubernetes 设计理念友好的结合在一起，不仅可以遵循 Kubernetes 标准化实现，还可以复用其他领域场景和 chaosblade cli 调用方式。

详细的中文使用文档：https://chaosblade-io.gitbook.io/chaosblade-help-zh-cn/blade-create-k8s
