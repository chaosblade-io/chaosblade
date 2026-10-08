**用例名称** 配置文件权限异常 导致 Pod_文件权限异常

## 故障现象
1. 应用配置文件权限被改为不可读（`chmod 000`），应用读取时报 `Permission denied`
2. 日志目录被改为不可写，应用无法落盘日志（进程继续运行但日志丢失）
3. 应用启动失败、或运行中读取配置失败导致功能降级
4. 模拟运维误操作、镜像构建权限错误、ConfigMap 挂载权限异常场景

> **与「文件被删除/移走」的区别**：`mv` 让文件消失（应用报 `No such file`），
> `chmod` 让文件在但读不了（应用报 `Permission denied`）。二者触发的应用错误分支
> 不同 —— 很多应用对「文件不存在」有默认值兜底，对「权限拒绝」却直接崩溃。
> ChaosBlade 的 `file chmod` 是主机级 action，K8s 场景下容器内文件需用本用例的 kubectl-native 方式。

## 资源准备
1. 确认目标 Pod 正常运行，且已知要篡改的文件/目录在容器内的绝对路径
2. **确认容器内进程不是以 root 运行** —— 这是本用例能否生效的决定性前提：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- id
   ```
   `uid=0(root)` 时 `chmod 000` **不产生任何效果**（root 绕过权限检查），此用例不适用，
   改用「文件被移走」类用例。非 root（如 `uid=1000`）才继续。
   - **靶可得性**：集群既有常驻靶全部以 root 运行时（常见——默认镜像 USER root），需带外预建
     专用非 root 靶：Deployment 单副本 + `securityContext.runAsUser/runAsGroup: 1000` + 镜像选
     节点缓存仓（见 node-cached-images.md）；容器内可写路径用 `/tmp/<app>/`（`/opt` 等
     root-owned 目录 uid 1000 无法 mkdir），启动脚本建目录 + 写配置文件（644）+ 循环读取
     （每 10s cat 一次并记 `CONFIG_OK`/`CONFIG_DENIED` 时间戳行到 app.log——轮询循环同时
     覆盖演练步骤第 3 步的「触发应用重读配置」）。
3. 确认目标文件**不是** ConfigMap/Secret 挂载进来的 —— 那类挂载点是只读 tmpfs，
   `chmod` 会报 `Read-only file system`。用 `kubectl exec <pod> -- mount` 查看挂载类型，
   或改选应用自己写入的路径（日志目录、数据目录）。
4. 记录原始权限（恢复必需）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -la <目标路径>
   ```
   记下 `-rw-r--r--` 这样的权限串，或用数字形式 `stat -c '%a' <目标路径>`（BusyBox 的 stat 可能不支持 `-c`）

## 演练步骤
1. 完成资源准备的三项检查（非 root、非只读挂载、已记录原权限）
2. **先武装定时恢复，再注入**（恢复动作 = 容器内文件 `chmod`——属主自身执行，非 API 平面写，
   不走 recovery-carrier 标准件；定时器宿主 = 目标容器自身，与注入合并为**单载荷**在同一
   `sh -c` 内零间隙下发：武装 → 写变更标记 → 变更权限 → 取证。若拆分两次执行，武装与注入
   间隔必须 ≤60s，且武装后发生任何修复须先停旧定时器再全额重武装——见 SKILL.md 安全红线
   「故障窗口完整」）：

   ⚠️ 顶层裸 `( sleep … ) &` 会被命令守卫拦截（unknown_binary: `(`），exec-form 通道也不
   解释裸后台语法——载荷必须包一层 `sh -c '<载荷>'`。

   **方向 A —— 配置文件不可读**（应用读配置失败，载荷示例）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c '
     ( sleep <recovery-seconds>; chmod <原始权限数字> <配置文件路径>; echo PERMS_RESTORED >> /tmp/perm.evd ) >/tmp/perms-restore.log 2>&1 &
     echo PERMS_CHANGED >> /tmp/perm.evd
     ls -l <配置文件路径> >> /tmp/perm.evd
     chmod 000 <配置文件路径>
     ls -l <配置文件路径> >> /tmp/perm.evd'
   ```

   **方向 B —— 日志目录不可写**（应用无法落盘，进程仍存活）：同形态，末两行改为
   `chmod 555 <日志目录路径>` + `ls -ld <日志目录路径> >> /tmp/perm.evd`。`555` 保留可读
   可执行、去掉写权限 —— 比 `000` 更贴近真实的「目录权限配错」场景，且不影响应用遍历该目录。

   **为什么容器内定时器就够**：恢复命令是属主对自己的文件 chmod——非 root 属主可 chmod 自己
   拥有的文件，**不受当前权限位限制**（000 后照样能改回 644）；幂等（chmod 到原值重复执行是
   no-op），不设 pidfile、迟到触发无害；武装失败则不注入。容器重启则文件系统回镜像初始态、
   定时器进程消失，故障自消（记录为「故障导致重启」而非注入失效）。

   参数说明：
   - `chmod 000`：完全不可访问（owner/group/other 全部无权限）
   - `chmod 555`：可读可执行、不可写
   - `chmod 444`：只读（连执行都去掉，对目录会导致无法 cd 进去）
   - **不要用 `-R` 递归**：递归改整个目录树会让恢复变得不可靠（每个文件原权限可能不同），
     只改单个目标文件/目录
   - `<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
     （= duration + grace，见 SKILL.md 双数窗口契约）——载荷内 sleep 定时器以它武装，
     让框架在观察窗终点主动派发的恢复先于自治到期落地

3. 触发应用重读配置（视应用而定：发送 reload 信号、等待轮询周期、或重启单个副本），
   观察应用是否报错

## 注入验证
1. 确认权限已改变：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -la <目标路径>
   ```
   方向 A 应显示 `----------`，方向 B 应显示 `dr-xr-xr-x`
2. 确认应用**实际读不到 / 写不进** —— 这是效果判据，不能只看权限位。以容器内进程的身份验证：
   ```bash
   # 方向 A：应报 Permission denied
   kubectl exec <pod-name> -n <namespace> -- cat <配置文件路径>
   # 方向 B：应输出 DENIED（以进程身份测目录写权限，等价 touch 但零残留、不创建文件）
   kubectl exec <pod-name> -n <namespace> -- sh -c 'test -w <日志目录路径> && echo WRITABLE || echo DENIED'
   ```
   **方向 A 若 cat 仍成功、方向 B 若输出 WRITABLE**，说明进程是 root（回到资源准备第 2 步复查）或该路径不是应用实际使用的路径。
3. 检查应用日志是否出现 `Permission denied`、配置加载失败、日志写入失败等错误
4. 确认容器**未被重启**（`kubectl get pod` 的 RESTARTS 列）—— 若应用因此崩溃退出，
   容器重启会让文件系统回到镜像初始状态，故障自动消失，此时应记录为「故障导致重启」而非「注入失效」

## 注入恢复
1. 等待 `<recovery-seconds>` 到期后武装的定时器自动将权限改回原始值。如需提前恢复，恢复执行者
   （Agent recover 阶段或人工带外）直接幂等重执行（**用资源准备第 4 步记录的值**，不要凭猜；
   定时器迟到触发无害——chmod 到原值重复执行是 no-op）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- chmod <原始权限数字> <目标路径>
   ```
   常见原值：配置文件 `644`，日志目录 `755`，可执行文件 `755`
2. 若容器已重启，文件系统已回到镜像初始状态，**无需也不应再执行 chmod** ——
   此时容器内的权限就是原始权限，多余的 chmod 反而可能改错
3. 触发应用重读配置，确认恢复正常

## 恢复验证
1. 确认权限已恢复：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -la <目标路径>
   ```
2. 确认应用能正常读写：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat <配置文件路径>
   kubectl exec <pod-name> -n <namespace> -- sh -c 'test -w <日志目录路径> && echo WRITABLE || echo DENIED'
   ```
3. 确认应用日志不再出现 Permission denied

## 基准事实
- **根因**：容器内配置文件/日志目录权限被改为不可读/不可写，应用以非 root 身份运行时无权访问
- **必现现象**：`ls -la` 显示权限位已改；容器内 `cat` 报 `Permission denied`、`test -w` 输出 `DENIED`；
  应用日志出现配置加载失败或日志写入失败；进程通常不退出（区别于文件被删除导致的启动失败）
- **不生效的前提**：容器内进程是 root（绕过权限检查）、或目标路径是 ConfigMap/Secret 的只读挂载点
