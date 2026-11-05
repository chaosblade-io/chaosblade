**用例名称** 数据静默篡改 导致 Pod_磁盘IO异常

## 故障定位
持续型故障——被篡改的文件内容存在即故障存活（脏数据不会自行还原），贯穿整个
故障窗口；写回注入前备份（定时器还原/提前恢复）即内容逐字节复原、自动恢复。

**注入手段**：容器内 exec 改写文件内容（kubectl-native）。ChaosBlade 无「文件内容
篡改」维度的原生动作（本形态对标 Chaos Mesh IOChaos 的 mistake 注入），本用例不用 blade。

**核心判别特征——「静默」**：与同目录 `文件系统IO返回错误`（EIO，读写报错、日志有
Input/output error）**相反**，数据静默篡改的读取**成功返回（RC=0）但内容是错的**——
应用无任何错误信号却读到脏数据，即静默数据损坏（silent data corruption）。「读得到、
读不报错、但读错了」是本用例区别于其它 IO 异常的签名。

**只有 path A（容器内 exec）一条路径**：篡改与还原都在容器内对文件内容读写
（`head`/`cp`/`cat`），不经宿主写守卫；恢复 = 写回注入前备份的原始内容（容器内 `cp`，
无需宿主 `rm`）。故与 `Pod_磁盘空间使用率过高_inode耗尽` 同族手段链——容器内
背景 subshell 定时还原，靶须为有 shell 的容器（distroless/scratch 无 `cp`/`cat`/`sh` 不可演练）。

**爆炸半径（默认形态零外部）**：默认篡改**演练自建的 canary 数据文件**（drill-owned），
机制层直读自证「读到脏数据且无错误」，应用层无感知（判据以机制层直读为准，同
`文件系统IO返回错误` 的纯 error 设备默认形态）；仅当靶有一个**可安全备份+还原的真实
业务数据/配置文件**、且需要观察应用层读到脏数据的影响时，才篡改真实文件——必须先备份、
恢复写回，**还原失败即真实数据损坏（风险自负，非默认形态）**。

`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 应用读取文件**成功**（无 I/O error、无 errno），但读到的是被篡改的脏数据——「静默」损坏
2. 数据一致性校验失败：文件 `md5sum`/`cksum` 与注入前基线不符（内容变了、读取路径健康）
3. 若靶为真实业务数据/配置文件：应用行为异常（加载到错误配置、算出错误结果、上层校验和
   报错），但**底层文件系统无任何错误**
4. 与 `文件系统IO返回错误` 的区别：**无** EIO、**无** Input/output error 日志——读取完全「健康」，只是内容错了

## 资源准备
1. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（多容器/临时容器混存时
   `kubectl exec` 必须显式 `-c <容器名>`）
2. **容器工具探测（决定 path A 可行性）**——需要 shell + `cp`/`cat` + 内容改写工具 + checksum：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'command -v sh; command -v cp; command -v cat; command -v head; command -v md5sum; echo PROBE_DONE'
   ```
   典型靶：`sh`/`cp`/`cat`/`head` 全有 → path A 可行。`md5sum` 缺失时用 `cksum` 或
   `wc -c` + 内容比对替代；distroless/scratch 镜像（无 shell）→ 本用例不可演练，须换有 shell 的靶
3. **篡改落点判定（决定爆炸半径，必做）**：
   - **默认（零外部）**：在靶可写目录新建 canary 文件 `<file>` = `<data-path>/.drill_canary.dat`
     （`<data-path>` 取靶**实际存在**的专属数据盘挂载路径——须先 `ls -ld` 确认存在再落点，不预设；
     无专属盘的通用靶（echo/busybox 类）直接用容器内 `/tmp`，爆炸半径同样零外部。
     注：`/var/lib/data` 是某些 PVC 挂载靶的专属数据盘路径、**非通用默认**，对无此挂载的靶探测它会 exit=2 空转），
     写入已知基线内容
   - **真实文件形态（可选，风险自负）**：选定一个应用会读取、且可安全备份还原的数据/配置文件
     `<target-file>`，记录其基线 checksum——还原失败即真实数据损坏，非默认
4. 记录注入前基线（恢复验证的定量对照）：`<file>` 的 `md5sum`、字节大小、内容摘要
5. 确认目标目录存在且当前用户可写（`ls -ld <data-path>` 先验存在，典型靶 uid=0、`/tmp` 1777；
   探测不存在的挂载路径只会空转 exit=2，落点前务必确认目录已在）

## 演练步骤
1. 记录注入前基线（恢复验证的定量对照）：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'md5sum <file>; wc -c <file>'
   ```
   记录：Pod RESTARTS 基线、`<file>` 的 md5 与字节数（恢复对照用「内容逐字节复原」而非粒度粗的「文件存在」）

2. 写入 canary 基线内容并**备份原始内容**（默认形态；真实文件形态改为 `cp -p <target-file> <target-file>.bak`）：
   ```bash
   # 载荷零 $ 字符（wiz 通道对 $ 语法拒斥）：基线内容用 printf 固定串，不用 $(...) 命令替换
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'printf "drill-baseline-canary-content-v1\n" > <file>; cp -p <file> <file>.bak; md5sum <file> <file>.bak'
   ```
   - `<file>` 与 `<file>.bak` 的 md5 此刻必须一致（备份 = 原始内容的还原源）
   - 备份文件 `<file>.bak` 与证据文件 `<evd>`（`<data-path>/.drill_canary.evd`）**分开**：恢复 `cp` 写回不覆盖证据

3. 注入 —— **武装定时还原 → 篡改内容 → 自证标记**，严格串行于单个载荷
   （定时器先于篡改武装——篡改后若武装失败，脏数据将无人还原；定时器 `cp` 写回幂等，
   到期自动还原并写还原标记）：
   ```bash
   # <size> 取 <file> 原始字节数（等长篡改更贴近位翻转/错误写入的真实形态）
   # <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     '( sleep <recovery-seconds>; cp <file>.bak <file>; echo DATA_RESTORED >> <evd> ) >/dev/null 2>&1 & \
      head -c <size> /dev/urandom > <file>; \
      echo DATA_CORRUPTED > <evd>'
   ```
   - **篡改必须保持文件「可读且无错」**（这是「静默」的本质）：`head -c <size> /dev/urandom > <file>`
     用随机字节等长覆写，`cat` 读取成功（RC=0）而内容全变——**不要** `truncate` 到 0（可能被应用识别为空文件）、
     **不要**删除文件（那是文件丢失/IO返回错误，不是静默篡改）、**不要** `chmod 000`（那是权限故障）
   - 容器无 `/dev/urandom` 或需模拟语义级损坏时，可改用固定错误串覆写：`printf "CORRUPTED-WRONG-VALUE\n" > <file>`
   - **武装与篡改必须在同一条命令内**：门禁按单条命令判定「时间界定时器 + 配对逆操作（写回备份）」——
     拆成「先单独武装」「再单独篡改」两条时各自都缺另一半而分别被拒
   - 载荷必须单次下发：拆成多条独立 kubectl exec 时武装与篡改间隔不可控且违反紧邻原则；
     `&&` 连两条 kubectl 会让第二条沦为第一条 exec 载荷的死参数
   - 链条是**自证的**：`DATA_CORRUPTED`（篡改时刻写入）、`DATA_RESTORED`（定时器还原后写入）
     落盘证据文件 `<evd>`，验证阶段 `cat` 取证，不受故障窗口是否已关闭的时序约束。标记不得为缩短命令省略

   倒计时从武装时刻起算：武装与篡改在同一载荷内严格串行（无侵蚀间隙）；武装后发生任何修复
   须先停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'pkill -f "sleep.*; cp .*\.ba[k] " 2>/dev/null; true'
   ```
   （模式串末字符加 `[]` 防自杀——载体 sh -c 命令行含模式串时 pkill -f 会把自己杀掉；
   容器无 pkill 时**不得用 `pidof`+`kill` 杀 sleep 替代停表**——杀掉 sleep 会让其所属 subshell 立即
   往下执行 `cp` 提前还原（侵蚀窗口）而非取消定时器；旧表既无法安全停止，重武装会叠加双表致更早的
   旧表先到期侵蚀窗口，故 pkill 缺失时不重武装，如实上报缩短的窗口或改人工带外恢复）

## 注入验证
（本用例只读判据 `cat`/`md5sum`/`ls` 天然放行；篡改是**写操作**，verify 阶段 read-only 纪律会拒绝，
故篡改探针必须在注入载荷内或执行期完成，不可留到验证阶段）：
1. **效果主证——内容已篡改但读取无错误（「静默」签名）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'cat <file> >/dev/null && echo READ_OK; md5sum <file>; wc -c <file>'
   ```
   判据：`READ_OK` 出现（`cat` 成功、RC=0、**无** Input/output error）**且** md5 与基线不符
   = 脏数据被无错读到 = 静默损坏签名成立。二者缺一即手段串味（报 EIO 是 IO返回错误、md5 未变是篡改未生效）
2. **备份完好（恢复的前提）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'ls -l <file>.bak; md5sum <file>.bak'
   ```
   `<file>.bak` 存在且 md5 == 注入前基线（还原源未被篡改波及）
3. 白盒确认自证链齐备：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- cat <evd>
   ```
   含 `DATA_CORRUPTED`（含 `DATA_RESTORED` 即窗口已关，如实报告实际时长）
4. **与 IO返回错误的对照（必做）**：读取路径**不报** EIO/errno 5——若报错说明手段串到了错误注入，
   不是静默篡改，须重做
5. **持续性检查**——内容篡改是状态型故障，脏数据存活即故障存活：
   45s 后复查 `md5sum <file>` 仍是篡改值（未自行还原）、`<evd>` 尚无 `DATA_RESTORED`
6. （真实文件形态可选）应用层：观察应用读到脏配置/数据后的行为异常（错误结果、上层校验和报错），
   底层 fs 仍无错误

## 注入恢复
1. 主路径：定时器到期自动 `cp <file>.bak <file>` 写回原始内容 + 写 `DATA_RESTORED`（Agent 无需干预，
   自恢复不依赖 Agent 在线；Agent 提交结论即收尾。需提前收尾或定时器丢失时移交带外
   `blade-ai recover --task-id`，幂等——写回备份可重复执行、迟到重复无副作用）
2. 提前恢复（幂等：`cp` 备份写回可重复执行）——**必须拆成两次 exec：先停定时器，再写回+标记**：
   ```bash
   # (a) 停定时器：单独一次 exec，命令行内只有 pkill 模式串、无字面 .bak 目标
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'pkill -f "sleep.*; cp .*\.ba[k] " 2>/dev/null; true'
   # (b) 写回原始内容 + 写还原标记：另一次 exec
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'cp <file>.bak <file>; echo DATA_RESTORED >> <evd>; true'
   ```
   **禁止把 (a)(b) 合并进同一条 sh -c**：合并后执行恢复的 shell 自身命令行同时含 "sleep"（pkill 模式串里）
   与字面的 `cp <file>.bak <file>`（写回目标），`pkill -f` 会匹配到这个 shell 自己——末字符 `[k]` 的防自杀
   只挡得住模式串自身的 `.ba[k]`，挡不住同一命令行里第二个字面 `.bak`；pkill 先把自己 SIGTERM（退出码 143），
   后续 `cp` 与还原标记都不执行，脏数据残留、提前恢复失效。拆开后 (a) 的命令行无字面 `.bak`，`[k]` 守卫成立
   （机理同 `Pod_磁盘空间使用率过高_inode耗尽` 的 pkill 拆两次 exec）
   **pkill 缺失的降级路径（部分靶常见——如仅 `/bin/pidof`，无 pkill/ps/pgrep/killall）**：提前恢复退化为
   **只做 (b) 幂等写回**、跳过 (a)——**严禁用 `pidof`+`kill` 杀 `sleep` 子进程来「停定时器」**：杀掉 sleep
   会让其所属 subshell 立即往下执行 `cp <file>.bak <file>`，即在故障窗口内**提前还原**（侵蚀窗口）而非取消
   定时器，与 (a) 的意图正相反。未取消的定时器到期后自行再写回一次（幂等 no-op）并追加一条重复
   `DATA_RESTORED`，无残留、无副作用——故 pkill 缺失时提前恢复仍安全，只是不主动停表、任由旧表自然到期

## 恢复验证
1. **内容定量回落（主证）**：`md5sum <file>` == 注入前基线 md5（写回成功、内容逐字节复原）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'md5sum <file>; cat <file> >/dev/null && echo READ_OK'
   ```
2. 自证链齐备：`cat <evd>` 含 `DATA_RESTORED`（`DATA_CORRUPTED` + `DATA_RESTORED` 双标记 = 注入与还原全程自证）
3. 读取仍无错误：`READ_OK` 出现（恢复后内容正确且读取路径健康）
4. 备份可清理：恢复确认后 `rm -f <file>.bak`（容器内删除，非宿主，允许）——或保留供审计
5. Pod 状态 Running、RESTARTS 与基线一致；爆炸半径全程受控（默认 canary 形态零外部影响）

## 基准事实
- **根因**：应用数据/配置文件内容被静默篡改（磁盘位翻转、错误写入、缓存污染、恶意改写），
  文件系统层无任何错误，应用读到脏数据而不自知
- **必现现象**：文件 `md5sum`/`cksum` 与基线不符；读取**成功返回（RC=0）无 I/O error**——
  「静默」是区别于 IO返回错误（EIO）的判别签名
- **方案说明**：kubectl-native 容器内 exec 改写文件内容（ChaosBlade 无此维度动作，对标 Chaos Mesh
  IOChaos mistake）。默认篡改演练自建 canary（爆炸半径零外部、应用层无感知，判据以机制层直读为准，
  同 IO返回错误 纯 error 设备默认形态）；真实业务文件形态须先备份、恢复写回，还原失败即真实数据损坏
  （风险自负，非默认）
- **恢复**：写回注入前备份（容器内 `cp`，幂等），path A 容器内自恢复定时器（`sleep` + `cp` 写回），
  与 `Pod_磁盘空间使用率过高_inode耗尽` 同族手段链——容器内 exec 不经宿主写守卫，恢复无需宿主 `rm`

## 注意事项
- **载荷零 `$` 字符**（wiz 通道对 `$` 语法拒斥）：篡改用 `head -c /dev/urandom` 或 `printf` 固定串，
  不用 `$(...)` 命令替换；验证 RC 用 `cat ... && echo READ_OK` 而非 `echo $?`
- **篡改必须保持文件「可读且无错」**：只改内容，不 truncate 到 0、不删文件、不 chmod——否则串味成
  文件丢失/IO返回错误/权限故障，丢失「静默」本质
- 备份文件 `<file>.bak` 与证据文件 `<evd>` 分开：恢复 `cp` 写回不覆盖证据；证据用追加 `>>`
- 提前恢复/重武装的 pkill 自杀防护同 `Pod_磁盘空间使用率过高_inode耗尽`：停定时器与写回**拆两次 exec**，模式串末字符加 `[]` 防自匹配；
  pkill 缺失时提前恢复只做 (b) 幂等写回、**严禁 `pidof`+`kill` 杀 sleep**（会提前还原侵蚀窗口而非停表）
- **path A 唯一**：容器内 exec 篡改/还原不经宿主写守卫；本用例**无 node 侧手段**（无需 chroot /host、
  不涉及 dmsetup）——与同目录 IO返回错误（node 侧 dmsetup）的手段路径完全不同
- distroless/scratch 无 shell 靶不可演练（无 `cp`/`cat`/`sh`）；容器无 `/dev/urandom` 时改用 `printf` 固定错误串覆写
