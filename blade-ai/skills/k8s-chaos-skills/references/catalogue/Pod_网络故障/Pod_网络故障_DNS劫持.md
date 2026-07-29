**用例名称** DNS劫持 导致 Pod_网络故障

**故障现象**：
1. Pod 对特定域名的解析被劫持到错误 IP 地址
2. 应用连接到非预期的服务端点，请求失败或返回异常数据
3. 与 DNS 解析失败（NXDOMAIN/超时）不同：域名仍可解析，但结果为错误 IP
4. 仅影响指定域名，其他域名解析正常

**资源准备**：
1. 确认目标应用已正常运行，且依赖特定域名进行外部服务调用
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认目标域名当前可正常解析到正确 IP

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 确认目标域名当前解析结果：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- nslookup <target-domain>
   ```
3. 使用 ChaosBlade 对目标 Pod 注入 DNS 劫持：
   ```bash
   blade create k8s pod-network dns \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --domain <target-domain> \
     --ip <错误IP地址> \
     --timeout 300 \
     --kubeconfig <kubeconfig-path>
   ```
   - `--domain`：要劫持的域名（必填）
   - `--ip`：劫持后指向的错误 IP（必填）
   - `--replace`：若域名已有本地解析记录，是否覆盖（默认不覆盖）
4. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在目标 Pod 内验证域名解析已被劫持：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- nslookup <target-domain>
   ```
   确认解析结果为注入的错误 IP 地址
2. 在目标 Pod 内尝试访问该域名，确认连接失败或返回异常：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 http://<target-domain>
   ```
3. 查看应用日志确认出现连接错误（connection refused/timeout/非预期响应）：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=20
   ```
4. 验证其他域名解析不受影响（确认故障范围可控）

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 等待 DNS 缓存刷新（通常即时生效）

**恢复验证**：
1. 在目标 Pod 内验证域名解析恢复正确：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- nslookup <target-domain>
   ```
   确认解析结果为正确 IP 地址
2. 在目标 Pod 内验证服务访问恢复正常
3. 确认应用日志不再出现连接错误

**基准事实**：
- **根因**：Pod 内 DNS 解析被 ChaosBlade 劫持，特定域名被解析到错误 IP 地址，导致应用连接到非预期端点
- **必现现象**：目标域名 nslookup 返回注入的错误 IP；应用对该域名的请求失败或返回异常；其他域名解析不受影响

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，用以下 kubectl 原生命令实现等效 Pod 级 DNS 劫持。
> 有两条路径，**优先用路径 A** —— 它只用控制面 kubectl，不依赖容器内有什么、也不依赖容器权限。

**路径 A：改 workload 的 `hostAliases`（推荐）**

`hostAliases` 是 Kubernetes 原生的 /etc/hosts 注入机制，由 kubelet 在创建容器时写入，
不需要容器内可写、不需要 root、不需要任何容器内工具。

前提条件：目标 Pod 由 Deployment / StatefulSet / DaemonSet 管理（能承受一次滚动重建）

注入命令：
```bash
# 1) 记录当前是否已有 hostAliases（恢复时要还原成这个样子）
kubectl get deployment <deployment-name> -n <namespace> \
  -o jsonpath='{.spec.template.spec.hostAliases}'

# 2) 注入劫持记录 —— 把域名指向一个不可达 IP（240.0.0.0/4 是保留段，必然不可达）
kubectl patch deployment <deployment-name> -n <namespace> --type=strategic -p \
  '{"spec":{"template":{"spec":{"hostAliases":[{"ip":"<错误IP>","hostnames":["<target-domain>"]}]}}}}'

# 3) 等滚动重建完成，新 Pod 才带上劫持记录
kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
```

恢复命令：
```bash
# 原本没有 hostAliases —— 整个字段移除
kubectl patch deployment <deployment-name> -n <namespace> --type=json -p \
  '[{"op":"remove","path":"/spec/template/spec/hostAliases"}]'

# 原本有 hostAliases —— 用第 1 步记录的原值覆盖回去
kubectl patch deployment <deployment-name> -n <namespace> --type=strategic -p \
  '{"spec":{"template":{"spec":{"hostAliases":<第1步记录的原值>}}}}'

kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
```

**路径 B：直接改容器内 `/etc/hosts`（受限，先验证前提）**

前提条件：容器内 `/etc/hosts` 必须**当前用户可写**。这一条经常不成立 ——
该文件由 kubelet 生成，属主是 `root` 且权限通常是 `644`，而多数生产镜像以非 root 用户运行
（如 UID 1200），此时写入会 `Permission denied`。**必须先验证，不要假定可写**：

```bash
kubectl exec <pod-name> -n <namespace> -- sh -c 'ls -l /etc/hosts; id -u; test -w /etc/hosts && echo WRITABLE || echo NOT_WRITABLE'
```
输出 `NOT_WRITABLE` 就改走路径 A。

注入命令：
```bash
kubectl exec <pod-name> -n <namespace> -- sh -c \
  'cp /etc/hosts /etc/hosts.bak && echo "<错误IP> <target-domain>" >> /etc/hosts'
```

恢复命令：
```bash
kubectl exec <pod-name> -n <namespace> -- sh -c \
  'cp /etc/hosts.bak /etc/hosts && rm -f /etc/hosts.bak'
```

注意事项：
- 路径 A 会触发滚动重建，故障在**新 Pod** 上生效，原 Pod 名会变 —— 注入后需重新获取 Pod 名
- 路径 A 的劫持随 Pod 生命周期持续，重启也不丢；路径 B 改的是容器内临时文件，
  容器一旦重启，kubelet 重新生成 /etc/hosts，修改**自动丢失**（这也算一种兜底恢复）
- 两条路径都无自动超时恢复，必须手动还原
- 若应用绕过 hosts 直连 DNS 解析器（自带 resolver 或 DNS 缓存），两条路径都可能不生效；
  这种情况要在 DNS 层面做（见 `Pod_网络故障_CoreDNS异常`）
