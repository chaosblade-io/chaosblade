# 宿主机 Timer 通道事实档案（host-timer-channel）

**定位**：Node 级故障与一切需要在宿主机武装 systemd transient timer 的场景（节点不可调度/污点/资源压力类）的通道环境事实——本集群节点宿主机的 timer 武装要件、凭证边界与载体形态。规划期消费本定案后**无需重新论证通道可行性与形态认知**（profile 支持性 / chroot 可行性 / systemctl 响应语义 / 载体落位 ns）。

---

## 定案：宿主机 systemd timer 通道三要素齐备

本集群节点宿主机具备 timer 武装全要件：`/usr/bin/kubectl`、`/etc/kubernetes/kubelet.conf`、`/usr/bin/systemd-run`——`kubectl debug node/<node> --profile=sysadmin --image=<terway-tag> -- chroot /host sh -c 'systemd-run --on-active=<N>s --unit=<name> ...'` 一条链路端到端可用。

**结构性依据**（为什么该事实可靠）：

- 本集群节点同构（同批次 ACK 节点池），任一 Ready 节点验证通过即可代表存量节点——节点池扩容引入新批次时以当次探测为准
- kubelet.conf 是 kubelet 与 API server 通信的常驻凭证，节点存活即在；systemd-run 属 systemd 基础组件（PID 1 直接管理 transient unit，不依赖 debug Pod 存活）
- terway 镜像节点缓存（见 node-cached-images.md）+ `--profile=sysadmin`（kubectl ≥ 1.23 beta，本集群客户端支持）——载体免拉取且具备宿主机访问权

## 凭证边界（载荷设计的硬约束）

kubelet.conf 凭证受 NodeRestriction 准入限制，**只能修改本节点 `spec.unschedulable`**：

- **放行**：`kubectl uncordon <node>`（patch spec.unschedulable）——timer 载荷可含
- **拒绝**：taints 增删（Forbidden）——timer 载荷只放 uncordon，**污点类注入的摘除归 `blade-ai recover` 主动兜底**；「timer 部分自治 + recover 补完」是通道凭证的必然形态，不是设计缺陷

## 通道形态事实

- **sysadmin profile 受支持**：无需论证支持性，带 profile 的 debug 直接可用（本集群 kubectl 客户端形态）
- **chroot /host + systemd 可达的判据**：探针执行 `chroot /host systemctl status <任意unit>` 返回 **exit 4（unit not found）即通道通**——这是真实 systemd 响应（区分于通道不可达的 exec error）；勿把 exit 4 误读为探针失败而重试
- **载体落位**：`kubectl debug node/` 不带 `-n` 时 node-debugger-* 载体落在**当前命名空间**（非 kube-system）——载体清理按 node-debugger 前缀扫全 ns 兜底；一次性 debug 载体命令结束后多自清
- **武装 receipt**：systemd-run 输出 `Running timer as unit: <name>.timer` / `Will run service as unit: <name>.service` 即武装成功

## 复核出口（单次探测即免推导）

武装前一次 debug 探针同时验证三要素存在性与 systemd 响应性；冲突预检（`systemctl status <unit>` exit 4 = 无同名 unit，可武装）可并入同一探针。**勿分别探测三要素、勿论证 profile/chroot 支持性、勿反复验证 systemd 响应**。

## 适用边界

- **适用于**：Node 级故障注入的定时自恢复武装、宿主机侧观测取证（两步法：持续 debug pod + exec）
- **不适用于**：容器内进程域定时器（容器内 sleep 循环形态）、API 平面恢复载体（recovery-carrier.md 标准件）
- **失效信号**：节点池扩容新批次、debug 载体镜像缓存失效（node-cached-images.md 边界）、systemd 异常——降级为单节点逐项探测
