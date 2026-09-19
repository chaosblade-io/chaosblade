**用例名称** 配置文件权限异常 导致 Pod_文件权限异常

**故障现象**：
1. 应用配置文件权限被改为不可读（`chmod 000`），应用读取时报 `Permission denied`
2. 日志目录被改为不可写，应用无法落盘日志（进程继续运行但日志丢失）
3. 应用启动失败、或运行中读取配置失败导致功能降级
4. 模拟运维误操作、镜像构建权限错误、ConfigMap 挂载权限异常场景

> **与「文件被删除/移走」的区别**：`mv` 让文件消失（应用报 `No such file`），
> `chmod` 让文件在但读不了（应用报 `Permission denied`）。二者触发的应用错误分支
> 不同 —— 很多应用对「文件不存在」有默认值兜底，对「权限拒绝」却直接崩溃。
> ChaosBlade 的 `file chmod` 是主机级 action，K8s 场景下容器内文件需用本用例的 kubectl-native 方式。

**资源准备**：
1. 确认目标 Pod 正常运行，且已知要篡改的文件/目录在容器内的绝对路径
2. **确认容器内进程不是以 root 运行** —— 这是本用例能否生效的决定性前提：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- id
   ```
   `uid=0(root)` 时 `chmod 000` **不产生任何效果**（root 绕过权限检查），此用例不适用，
   改用「文件被移走」类用例。非 root（如 `uid=1000`）才继续。
3. 确认目标文件**不是** ConfigMap/Secret 挂载进来的 —— 那类挂载点是只读 tmpfs，
   `chmod` 会报 `Read-only file system`。用 `kubectl exec <pod> -- mount` 查看挂载类型，
   或改选应用自己写入的路径（日志目录、数据目录）。
4. 记录原始权限（恢复必需）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -la <目标路径>
   ```
   记下 `-rw-r--r--` 这样的权限串，或用数字形式 `stat -c '%a' <目标路径>`（BusyBox 的 stat 可能不支持 `-c`）

**演练步骤**：
1. 完成资源准备的三项检查（非 root、非只读挂载、已记录原权限）
2. **先武装定时恢复，再注入**（载体 Pod 武装形态——直接以顶层 `( sleep … ) &` 后台子 shell
   派发会被命令守卫拦截（unknown_binary: `(`），载体内 `sh -c` 载荷同时解决 exec-form 通道
   不解释裸后台语法的问题；到期自动将权限改回资源准备第 4 步记录的原始值，补齐自恢复能力；
   载体 Pod 为多副本时无法可靠终止定时器，故不设 pidfile——恢复命令幂等（chmod 到原值
   重复执行是 no-op），迟到触发无害；武装失败则不注入。还原脚本 = 一条
   `kubectl exec <pod-name> -n <namespace> -- chmod <原始权限数字> <目标路径>`，落盘形态按
   recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定（Phase 2 无 base64 生成器，勿留
   <restore-b64> 占位符——下式为旧契约历史形态示例，勿套用）；载体 SA 需目标命名空间 pods/exec create 权限——从载体远程
   exec 目标 Pod 执行 chmod，注入前先在载体验证（⚠️ can-i 子资源假阴性陷阱：
   `kubectl auth can-i create pods/exec` 对子资源报 no 但实际可 exec，必须用
   `--subresource` 旗标形态）：
   `kubectl exec <载体Pod> -n <载体ns> -- kubectl auth can-i create pods --subresource=exec -n <namespace>`）：
   ```bash
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-perms.sh; ( sleep <duration>; sh /tmp/blade-restore-perms.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-perm[s]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
   随后按要模拟的故障方向二选一注入：

   **方向 A —— 配置文件不可读**（应用读配置失败）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- chmod 000 <配置文件路径>
   ```

   **方向 B —— 日志目录不可写**（应用无法落盘，进程仍存活）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- chmod 555 <日志目录路径>
   ```
   `555` 保留可读可执行、去掉写权限 —— 比 `000` 更贴近真实的「目录权限配错」场景，
   且不影响应用遍历该目录。

   参数说明：
   - `chmod 000`：完全不可访问（owner/group/other 全部无权限）
   - `chmod 555`：可读可执行、不可写
   - `chmod 444`：只读（连执行都去掉，对目录会导致无法 cd 进去）
   - **不要用 `-R` 递归**：递归改整个目录树会让恢复变得不可靠（每个文件原权限可能不同），
     只改单个目标文件/目录

3. 触发应用重读配置（视应用而定：发送 reload 信号、等待轮询周期、或重启单个副本），
   观察应用是否报错

**注入验证**：
1. 确认权限已改变：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -la <目标路径>
   ```
   方向 A 应显示 `----------`，方向 B 应显示 `dr-xr-xr-x`
2. 确认应用**实际读不到** —— 这是效果判据，不能只看权限位。以容器内进程的身份验证：
   ```bash
   # 方向 A：应报 Permission denied
   kubectl exec <pod-name> -n <namespace> -- cat <配置文件路径>
   # 方向 B：应报 Permission denied（尝试在目录里创建文件）
   kubectl exec <pod-name> -n <namespace> -- touch <日志目录路径>/probe
   ```
   **若命令仍然成功**，说明进程是 root（回到资源准备第 2 步复查）或该路径不是应用实际使用的路径。
3. 检查应用日志是否出现 `Permission denied`、配置加载失败、日志写入失败等错误
4. 确认容器**未被重启**（`kubectl get pod` 的 RESTARTS 列）—— 若应用因此崩溃退出，
   容器重启会让文件系统回到镜像初始状态，故障自动消失，此时应记录为「故障导致重启」而非「注入失效」

**注入恢复**：
1. 等待 `<duration>` 到期后武装的定时器自动将权限改回原始值。如需提前恢复，Agent 直接幂等
   重执行（**用资源准备第 4 步记录的值**，不要凭猜；定时器迟到触发无害——chmod 到原值
   重复执行是 no-op）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- chmod <原始权限数字> <目标路径>
   ```
   常见原值：配置文件 `644`，日志目录 `755`，可执行文件 `755`
2. 若容器已重启，文件系统已回到镜像初始状态，**无需也不应再执行 chmod** ——
   此时容器内的权限就是原始权限，多余的 chmod 反而可能改错
3. 触发应用重读配置，确认恢复正常

**恢复验证**：
1. 确认权限已恢复：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -la <目标路径>
   ```
2. 确认应用能正常读写：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat <配置文件路径>
   kubectl exec <pod-name> -n <namespace> -- touch <日志目录路径>/probe
   kubectl exec <pod-name> -n <namespace> -- rm -f <日志目录路径>/probe
   ```
3. 确认应用日志不再出现 Permission denied

**基准事实**：
- **根因**：容器内配置文件/日志目录权限被改为不可读/不可写，应用以非 root 身份运行时无权访问
- **必现现象**：`ls -la` 显示权限位已改；容器内 `cat`/`touch` 报 `Permission denied`；
  应用日志出现配置加载失败或日志写入失败；进程通常不退出（区别于文件被删除导致的启动失败）
- **不生效的前提**：容器内进程是 root（绕过权限检查）、或目标路径是 ConfigMap/Secret 的只读挂载点
