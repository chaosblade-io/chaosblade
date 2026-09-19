# 节点缓存镜像事实档案（node-cached-images）

**定位**：容器级接线（sidecar 注入、调试载体、探针 Pod）的镜像选型环境事实——哪些镜像在本集群全节点常驻缓存、新 Pod 落任意节点免拉取。规划期消费本定案后**无需逐节点重新论证镜像覆盖面**。

---

## 定案：CNI 组件镜像全节点常驻缓存

terway-eniip DaemonSet（kube-system）全节点运行（ready == desired），其镜像及同 tag 的 CNI 组件镜像类（`acs/terway` 等）**在每个节点都有运行中容器——镜像必然已在节点本地缓存**。新 Pod 使用同 tag 镜像调度到任意节点都免拉取（kubelet 直接复用本地镜像层）。

**结构性依据**（为什么该事实可靠）：

- DaemonSet ready == desired，意味着每个节点都存在该镜像的运行中副本——镜像在节点缓存是 DaemonSet 语义的必然推论，不是巧合
- CNI DaemonSet 是集群网络面组件，其覆盖先于一切业务负载就绪、且持续常驻——覆盖稳定性高于任何业务 workload
- 与「某业务 Pod 恰好在某节点」式的偶然缓存不同，DaemonSet 覆盖是声明式保证

**复核出口**（至多单命令；复核通过即免推导）：

```bash
kubectl get ds terway-eniip -n kube-system -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}'
```

ready 数与 desired 数一致即定案成立；不一致时（节点池变动、DS 异常）才降级为逐节点探测。**勿用 `kubectl get ds -A` 全表扫描或逐节点 `crictl images` 来论证覆盖面**——单命令复核已含全部所需信息。

## 工具链事实

terway 类 CNI 镜像（busybox 系用户态）自带完整工具链：`socat` / `pgrep` / `ps` / `kill` / `grep` / `cat`——满足进程域（信号注入/状态取证）、网络域（socat 双端探针）接线需求；busybox `ps` 无 STAT 列，进程状态判据用 `/proc/<PID>/status` 的 State 字段（两步字面量形态）。

## 适用边界

- **适用于**：容器级接线的镜像选型——sidecar 注入（JSON patch 追加容器）、`kubectl debug` 载体、一次性探针 Pod
- **tag 一致性**：接线镜像必须与 DS 实际运行 tag 完全一致——同 repo 不同 tag 仍可能触发节点拉取
- **网络前提**：本集群 VPC 受限网络下 docker.io 公网镜像 `ImagePullBackOff`——公网镜像一律禁用，只选节点缓存镜像或 VPC registry 可达镜像
- **覆盖失效信号**：DS ready < desired、节点池扩容后新节点未就绪——此时本定案不适用，降级为逐节点探测
