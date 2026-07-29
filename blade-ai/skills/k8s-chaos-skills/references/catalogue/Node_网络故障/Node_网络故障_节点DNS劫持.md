**用例名称** 节点DNS劫持 导致 Node_网络故障

**故障现象**：
1. 节点上所有 Pod 对特定域名的 DNS 解析被劫持到错误 IP
2. 多个应用同时出现对同一域名的连接异常
3. 与 Pod 级 DNS 劫持不同：影响范围为整个节点上所有容器
4. 模拟节点级 DNS 污染或中间人攻击场景

**资源准备**：
1. 确认目标节点名称及其上运行的依赖特定域名的工作负载
2. 确认目标域名当前可正常解析
3. 确认 ChaosBlade Operator 已部署（DaemonSet 通道）或具备节点 SSH 访问权限（SSH 通道）

**演练步骤**：
1. 确认目标节点名称和上面运行的 Pod：
   ```bash
   kubectl get pods -o wide --field-selector spec.nodeName=<node-name>
   ```
2. 确认目标域名当前解析正常：
   ```bash
   kubectl exec <该节点上的pod> -n <namespace> -- nslookup <target-domain>
   ```
3. 选择执行通道并注入节点级 DNS 劫持：

   **方式一：DaemonSet 通道**
   ```bash
   blade create k8s node-network dns \
     --names <node-name> \
     --domain <target-domain> \
     --ip <错误IP地址> \
     --timeout 300 \
     --kubeconfig <kubeconfig-path>
   ```

   **方式二：SSH 通道**
   ```bash
   blade create k8s node-network dns \
     --domain <target-domain> \
     --ip <错误IP地址> \
     --channel ssh \
     --ssh-host <node-ip> \
     --ssh-user root \
     --timeout 300
   ```
   - `--domain`：要劫持的域名（必填）
   - `--ip`：劫持后指向的错误 IP（必填）
4. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在该节点上的多个 Pod 内验证域名解析已被劫持：
   ```bash
   kubectl exec <pod-name-1> -n <namespace> -- nslookup <target-domain>
   kubectl exec <pod-name-2> -n <namespace> -- nslookup <target-domain>
   ```
   确认均解析到注入的错误 IP
2. 验证其他域名解析不受影响
3. 验证其他节点上的 Pod 解析正常（确认故障限定在目标节点）：
   ```bash
   kubectl exec <其他节点上的pod> -n <namespace> -- nslookup <target-domain>
   ```
4. 查看应用日志确认出现连接异常

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 或等待 `--timeout` 到期自动恢复

**恢复验证**：
1. 在目标节点上的 Pod 内验证域名解析恢复正确：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- nslookup <target-domain>
   ```
2. 确认应用日志不再出现连接错误
3. 确认业务调用恢复正常

**基准事实**：
- **根因**：节点宿主机级别 DNS 解析被劫持，特定域名被解析到错误 IP，影响该节点上所有容器的域名解析
- **必现现象**：该节点上所有 Pod 对目标域名解析结果为错误 IP；其他节点不受影响；多个应用同时出现连接异常

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效节点级 DNS 劫持。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择已验证可拉取且含 `chroot`/`sh` 的镜像；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

注入命令：
```bash
# 通过 kubectl debug node 修改宿主机 /etc/hosts 注入 DNS 劫持
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'cp /etc/hosts /etc/hosts.bak && echo "<错误IP> <target-domain>" >> /etc/hosts'
```

恢复命令：
```bash
# 还原宿主机 /etc/hosts
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'cp /etc/hosts.bak /etc/hosts && rm -f /etc/hosts.bak'
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
```

注意事项：
- 修改宿主机 /etc/hosts 对所有使用 glibc 的进程立即生效（包括节点上所有容器）
- 部分应用有 DNS 缓存（如 JVM），修改 hosts 后可能需等待缓存过期
- 与 ChaosBlade 不同，此方式无自动超时恢复，必须手动还原 hosts 文件
