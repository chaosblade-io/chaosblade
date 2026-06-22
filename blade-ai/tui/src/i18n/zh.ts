/**
 * 中文 (简体) 字典。Key 命名跟 en.ts 完全对齐。
 *
 * 文案语气基调：
 *   - 跟 Claude Code / Qwen Code 一样，简短、不啰嗦、命令式
 *   - 错误 next-step 用动词开头："/status — 确认会话健康"
 *   - 标签用名词："任务" "耗时"
 *   - 不用感叹号；不用"请"
 */

import type { Dict } from "./index.js";

export const zh: Dict = {
  // -- 思考短语池（8s 循环） -----------------------------------------
  // blade-ai 领域专用：每条对应注入流程里的一个真实环节
  //（意图 → 安全 → 基线 → 执行 → 验证 → 回滚），轮播起来像 agent
  // 在自述当前在做什么，而不是通用 "思考中" 占位。
  "thinking.phrases": [
    "思考中",
    "评估爆炸半径",
    "检查安全约束",
    "巡检目标健康度",
    "翻阅技能手册",
    "挑选注入手法",
    "采集基线指标",
    "观测系统响应",
    "评估回滚路径",
    "起草故障方案",
  ],

  // -- LoadingIndicator 通用 -----------------------------------------
  "loading.esc_to_cancel": "esc 取消",
  "loading.thinking_label": "思考中",
  "loading.responding_label": "回答中",
  "loading.tokens_estimate": "约 {n} tokens",

  // -- Overflow / 高度约束 -----------------------------------------
  "overflow.more_lines": "（还有 {count} 行被折叠 · Ctrl+O 展开）",
  "overflow.show_more_hint": "Ctrl+O 展开被折叠的内容",

  // -- ThinkingMessage（折叠后的思考行） -----------------------------
  "thinking.collapsed": "思考用时 {duration}",

  // -- TurnUsageMessage（轮末 token 用量总结） -----------------------
  "turn.usage": "本轮共 {total} tokens（输入 {input} tokens，输出 {output} tokens）",

  // -- 错误标签 + 建议 ----------------------------------------------
  "error.init_failed.label": "初始化失败",
  "error.init_failed.suggestions": [
    "/status — 确认会话健康",
    "若 server 启动失败，按 Ctrl+C 重启 blade-ai",
  ],

  "error.cluster_unreachable.label": "集群不可达",
  "error.cluster_unreachable.suggestions": [
    "另开终端验证 ``kubectl get ns`` 能否工作",
    "/status — 查看当前 cluster + namespace",
    "/tasks — 先检查是否有进行中的任务待恢复",
  ],

  "error.stream_error.label": "流式错误",
  "error.stream_error.suggestions": [
    "/retry — 重发最后一条消息（流式中断通常是临时的）",
    "/clear — 状态卡住时清空 scrollback",
  ],

  "error.conversation_error.label": "对话错误",
  "error.conversation_error.suggestions": [
    "/clear — 清空 scrollback 重启对话",
    "/status — 确认会话元信息",
  ],

  "error.replay_failed.label": "回放失败",
  "error.replay_failed.suggestions": [
    "/recordings — 查看磁盘上实际存在的录像",
    "/replay <task_id> — 确认 task_id 拼写无误",
  ],

  "error.command_failed.label": "命令失败",
  "error.command_failed.suggestions": ["/help — 列出可用命令"],

  "error.session_expired.label": "会话过期",
  "error.session_expired.suggestions": [
    "重启 blade-ai —— server 已丢失会话（多半是 server 重启了）",
  ],

  // -- 错误展示通用 -------------------------------------------------
  "error.next_label": "下一步：",

  // -- Slash 命令描述 ----------------------------------------------
  "command.help.desc": "列出可用命令",
  "command.clear.desc": "清空当前会话的 scrollback",
  "command.exit.desc": "退出 blade-ai",
  "command.mode.desc": "设置信息密度 —— 从下方子命令中选一个",
  "command.mode.calm.desc": "calm：极简密度，仅保留关键信号",
  "command.mode.working.desc": "working：标准密度（默认），完整呈现工具输出",
  "command.mode.dense.desc": "dense：高密度，展开所有诊断细节",
  "command.permission.desc": "设置权限模式 —— 从下方子命令中选一个",
  "command.permission.auto.desc": "auto —— agent 直接注入不等确认，对下一次 /turn 生效",
  "command.permission.confirm.desc": "confirm —— 注入前弹出 ARMED/ABORTED 确认卡（默认）",
  "command.session.desc": "查看会话信息（cluster / namespace / model / mode）",
  "command.status.desc": "查看当前会话信息",
  "command.run.desc": "执行故障注入（等价于直接输入自然语言）",
  "command.plan.desc": "故障注入预览（Dry-Run）：跑意图澄清 + 计划 + 安全检查，不真正下发；非故障对话等价于 /run",
  "plan.usage": "用法：/plan <故障描述> — 例如 /plan 注入 cpu 故障到 node-1",
  // /run 不带参数时的提示
  "run.usage": "用法：/run <自然语言描述>",
  // -- /show /copy /rerun /expand（locator 命令组）-------------------
  "command.show.desc": "查看 locator 快照：/show E1 | T3",
  "command.copy.desc": "打印 locator 的可复制文本块：/copy E1 | T3",
  "command.rerun.desc": "回看实验原始描述供再发起：/rerun E1",
  "command.expand.desc": "展开工具调用的完整输出：/expand T1（也接受 /expand 1）",
  "locator.usage_show": "用法：/show <E#|T#>，例如 /show E1",
  "locator.usage_copy": "用法：/copy <E#|T#>，例如 /copy T3",
  "locator.usage_rerun": "用法：/rerun <E#>，例如 /rerun E1",
  "locator.usage_expand": "用法：/expand <T#>，例如 /expand T1",
  "locator.not_found": "找不到 locator '{loc}'。/show 仅能查看本会话已出现的 [E#]/[T#]。",
  "locator.rerun_not_experiment": "/rerun 只适用于实验（E#），工具调用请用 /expand。",
  "locator.expand_not_tool": "/expand 只适用于工具调用（T#），实验请用 /show。",
  "locator.copy_tool_header": "# {loc} {name} 输出（复制下方文本）",
  "locator.copy_experiment_header": "# {loc} 实验快照（复制下方 JSON）",
  "locator.rerun_hint": "[{loc}] 原始描述：{desc}\n复制上述文本作为下一条输入即可重新发起；将再次经过意图确认。",
  "command.tasks.desc": "列出最近的故障注入任务（可加 active / failed / all 过滤；可加数字限制条数）",
  "command.recordings.desc": "录像管理：list（默认） / export <task_id> <path>",
  "command.recordings.list.desc": "列出可用 /replay 的任务录像",
  "command.recordings.export.desc": "把指定 task_id 的录像导出为本地 JSONL 文件",
  "recordings.export_usage": "用法：/recordings export <task_id> <out_path>",
  "recordings.export_empty": "录像 {id} 为空，没有事件可导出",
  "recordings.export_exists": "目标已存在：{path}（拒绝覆盖；先移走或换路径）",
  "recordings.export_ok": "已导出 {events} 个事件 / {bytes} 字节 → {path}",
  "recordings.export_failed": "导出 {id} 失败：{err}",
  "command.replay.desc": "回放任务录像 — 传 task_id 即可（可选 speed 数字或 instant）",
  "command.doctor.desc": "查看诊断信息（server / 集群 / 版本 / 语言）",
  "command.retry.desc": "重发上一次自然语言对话（例如流断后恢复）",

  // -- /retry 运行期 -------------------------------------------------
  "retry.no_input": "暂无可重发的对话——先发一条消息",
  "retry.busy": "仍在流式接收中——等待当前回合结束或按 Esc 取消后再试",
  "retry.unavailable": "/retry 在当前上下文未启用",
  "retry.resubmitting": "重发：{input}",

  // -- 协议版本不匹配 ------------------------------------------------
  "protocol.mismatch": "协议版本不匹配——TUI={tui}，server={server}；部分事件可能渲染异常。可执行 `npm install -g @blade-ai/tui` 或 `pip install -U blade-ai` 升级。",

  // -- Slash 命令运行期错误 -----------------------------------------
  "command.handler_failed": "/{name} 执行失败：{msg}",
  // 流式中调到非 stream-safe 命令时的拦截提示（对齐 Python 的语气）
  "command.busy_block": "请等待当前任务结束或 Esc 中断后再使用此命令",

  // -- /help 分组标题（对齐 Python tui/commands.py 的 _GROUP_LABELS）-
  "help.group.general": "通用",
  "help.group.business": "业务",
  "help.group.skills": "技能",
  "help.group.dynamic": "技能",
  // 旧 group 标签保留 — 现仍被 boot 屏 / 启动卡片用作 section 标题；
  // 命令分组本身已迁到上面的 4 组，但 i18n key 不删避免 t() 找不到 key
  "help.group.session": "会话",
  "help.group.tasks": "任务",
  "help.group.history": "历史",
  "help.card.title": "命令",
  "help.card.tip": "提示：输入 / 后按 TAB 自动补全",

  // -- /doctor 输出 --------------------------------------------------
  "doctor.head": "诊断",
  "doctor.server": "server",
  "doctor.server_unreachable": "（不可达）",
  "doctor.cluster": "集群",
  "doctor.cluster_none": "（无）",
  "doctor.tui_version": "TUI 版本",
  "doctor.server_version": "server 版本",
  "doctor.protocol": "协议",
  "doctor.lang": "语言",
  "doctor.mode": "权限模式",
  "doctor.terminal_bg": "终端背景",
  "doctor.preflight": "环境自检",
  "doctor.fix.server_unreachable":
    "检查 blade-ai server 是否在运行，且能从上述 URL 访问；本地开发可重启 TUI 触发 server 自动 spawn。",
  "doctor.fix.protocol_mismatch":
    "重启 server 或升级 TUI，使两端协议版本一致；版本不一致可能导致事件解析异常。",
  "doctor.fix.preflight_unavailable":
    "环境自检接口未响应——重试 /doctor，或确认 server 是较新构建（旧版本可能没有 /api/v1/preflight）。",

  // -- Slash 命令输出 ----------------------------------------------
  "mode.usage_unknown": "未知模式 '{value}' — 应为 'auto' 或 'confirm'",
  "mode.usage_missing": "/permission 需要参数 — 请加 'auto' / 'confirm'（当前：{mode}）",
  "mode.already": "权限模式已是 {mode}",
  "mode.changed": "权限模式 → **{mode}**（下一次 /turn 生效）",

  // -- /mode（显示密度，calm/working/dense）---------------------------
  "display.usage_unknown": "未知密度 '{value}' — 应为 'calm' / 'working' / 'dense'",
  "display.usage_missing": "/mode 需要参数 — 请加 'calm' / 'working' / 'dense'（当前：{mode}）",
  "display.already": "信息密度已是 {mode}",
  "display.changed": "信息密度 → **{mode}**",

  "tasks.empty": "暂无任务 — 描述一段故障开始一个新任务",
  "tasks.head": "最近 {n} 个任务（共 {total}）：",
  "tasks.empty_filter": "共 {total} 条任务，无符合 [{filter}] 的项",
  "tasks.head_filter": "{filter} · 显示 {n}/{total}（总 {grand}）：",
  "tasks.failed": "拉取任务失败：{err}",

  // -- /review --------------------------------------------------------
  "command.review.desc": "查看任务详情卡片 — 传 task_id 或 E# locator（无参数则取最近一条）",
  "review.no_recent": "暂无可回顾的任务 — 先执行一次 /run 注入故障",
  "review.failed": "读取任务详情失败：{err}",
  "review.head": "▸ 任务详情 · {id}",
  "review.status_label": "状态",
  "review.fault_label": "故障",
  "review.phase_label": "阶段",
  "review.uid_label": "blade uid",
  "review.duration_label": "耗时",
  "review.created_label": "创建时间",

  // -- /experiments ---------------------------------------------------
  "command.experiments.desc": "列出技能目录中可注入的故障实验",
  "experiments.loading": "正在加载故障实验目录 …",
  "experiments.failed": "加载故障实验失败：{err}",
  "experiments.empty": "未发现任何故障实验 — 检查 /skills 目录是否包含 SKILL.md",
  "experiments.head": "故障实验目录（共 {total} 项）：",
  "experiments.fault_count_unit": "个用例",
  "experiments.card.title": "故障实验",
  "experiments.card.count": "共 {n} 项",
  "experiments.card.symptom_empty": "（未提供症状）",

  // -- /recover -------------------------------------------------------
  "command.recover.desc": "故障恢复 — 传 task_id / latest，或 list 子命令查看可恢复任务",
  "command.recover.list.desc": "列出尚处 injecting/injected 的任务",
  "recover.list_empty": "没有待恢复的任务（injecting/injected）",
  "recover.list_head": "待恢复任务（{n} 项）：",
  "recover.list_hint": "用 **/recover <task_id>** 触发恢复",
  "recover.list_failed": "查询待恢复任务失败：{err}",
  "recover.usage": "用法：**/recover <task_id|latest>** — 或用 /recover list 列出可恢复的任务",
  "recover.no_latest": "本会话尚无完成的任务可作为 latest — 先 /run 注入一次",
  "recover.busy": "仍在流式接收中 — 等待当前回合结束后再触发恢复",
  "recover.starting": "正在恢复任务 **{id}** … 这一步会真实调用集群，可能需要几十秒",
  "recover.failed": "恢复任务 {id} 失败：{err}",
  "recover.success_head": "✓ 任务 {id} 已恢复（{level}）",
  "recover.fail_head": "✗ 任务 {id} 恢复失败",
  "recover.targets_label": "目标",
  "recover.error_label": "原因",
  "recover.unknown_error": "未知错误",

  // -- /skills --------------------------------------------------------
  "command.skills.desc": "技能目录管理：list / show / reload / install / enable / disable",

  // -- /config --------------------------------------------------------
  "command.config.desc": "服务端配置读写（list / get / set / unset / path）",
  "command.config.list.desc": "列出全部可见配置（敏感字段已脱敏）",
  "command.config.get.desc": "查询单项配置：/config get <key>",
  "command.config.set.desc": "写入并触发热重载（部分键需重启生效）",
  "command.config.unset.desc": "删除配置项，使用默认值",
  "command.config.path.desc": "打印 ~/.blade-ai/config.json 的解析路径",
  "config.usage": "用法：\n  /config list                    — 列出全部\n  /config get <key>               — 查询单项\n  /config set <key> <value>       — 写入并热重载\n  /config unset <key>             — 恢复默认\n  /config path                    — 打印 config.json 路径",
  "config.head": "当前配置：",
  "config.path_tail": "config.json: {path}",
  "config.failed": "读取配置失败：{err}",
  "config.unset": "{key} 未设置（使用默认值）",
  "config.get_usage": "用法：/config get <key>",
  "config.set_usage": "用法：/config set <key> <value>",
  "config.set_ok": "已写入 {key} = {value}{tail}",
  "config.set_cold_tail": " · ⚠ 此键为冷配置，需重启 TUI 才能完全生效",
  "config.set_failed": "写入 {key} 失败：{err}",
  "config.unset_usage": "用法：/config unset <key>",
  "config.unset_ok": "已删除 {key}{tail}",
  "config.unset_noop": "{key} 本来就未设置，无需删除",
  "config.unset_failed": "删除 {key} 失败：{err}",

  // -- /memory --------------------------------------------------------
  "command.memory.desc": "TUI 会话记忆（show / clear / path）",
  "command.memory.show.desc": "查看当前 TUI 会话和最近任务",
  "command.memory.clear.desc": "删除当前 TUI 会话快照文件（不清空 graph 线程）",
  "command.memory.path.desc": "打印 memory_dir",
  "memory.usage": "用法：\n  /memory show     — 查看会话快照\n  /memory clear    — 删除会话文件\n  /memory path     — 打印 memory_dir",
  "memory.head": "TUI session: {sid}",
  "memory.cluster_label": "cluster",
  "memory.ns_label": "namespace",
  "memory.started_label": "started_at",
  "memory.status_label": "status",
  "memory.recent_tasks_head": "最近任务（{shown}/{total}）：",
  "memory.stats_head": "统计：",
  "memory.show_failed": "读取会话记忆失败：{err}",
  "memory.clear_ok": "已删除当前会话的快照文件",
  "memory.clear_noop": "当前会话没有快照可删（可能尚未持久化）",
  "memory.clear_failed": "删除会话快照失败：{err}",

  // -- /compact -------------------------------------------------------
  "command.compact.desc": "强制压缩当前会话上下文（节省 LLM 上下文 tokens）",
  "compact.busy": "仍在流式接收中——等待当前回合结束后再压缩",
  "compact.starting": "正在压缩当前会话上下文…",
  "compact.in_progress": "  LLM 摘要器运行中 —— 可能需要几秒",
  "compact.failed": "压缩失败：{err}",
  // ManualCompactIndicator: 整个 /compact 期间常驻的 spinner 行
  // （noop / strip / LLM 三条路径统一显示）。
  "compact.indicator_label": "正在压缩当前会话上下文…",
  "compact.indicator_meta": "（{elapsed} · esc 取消）",
  "compact.cancelled": "已取消压缩",
  "compact.noop": "上下文 {before} tokens，无需压缩（{layer}）",
  "compact.ok": "已压缩（{layer}）：{before} → {after} tokens（节省 {saved} / {pct}%）",

  // -- Memory compaction (live SSE event from PreReasoningHook) --
  // Phase 4: SSE 主通道转发 ``memory_compaction`` 事件，UI 在压缩期
  // 间用独立 spinner 替换 LoadingIndicator（互斥）；完成 / 失败时
  // 落一行历史记录。
  "compaction.indicator_label": "记忆压缩中",
  "compaction.indicator_meta": "({tokens} tokens · {elapsed})",
  "compaction.success_line":
    "✓ 已压缩 {messages} 条消息：{before} → {after} tokens · 节省 {saved}（{percent}%）· 用时 {duration}",
  "compaction.failure_line": "✗ 记忆压缩失败：{reason} · 用时 {duration}",
  "compaction.failure_unknown": "未知原因",

  // -- /model ---------------------------------------------------------
  "command.model.desc": "选择 LLM 模型（list / set）",
  "command.model.list.desc": "列出候选模型并标记当前激活",
  "command.model.set.desc": "切换活动模型（写入 config，需重启 server 才能生效）",
  "model.usage": "用法：\n  /model list           — 列出候选模型\n  /model set <id>       — 切换活动模型",
  "model.head": "当前模型：{active}",
  "model.base_url_label": "api_base_url",
  "model.list_tail": "用 **/model set <id>** 切换；下一次 /turn 自动生效",
  "model.custom_note": "自定义（不在内置候选清单内，但可正常使用）",
  "model.card.title": "模型",
  "model.card.count": "共 {n} 个",
  "model.card.tip": "提示：/model set <id> 切换 · 下一次 /turn 自动生效",
  "model.card.custom_section": "custom",
  "model.card.custom_note": "— 不在内置候选清单",
  "model.card.unset": "（未设置）",
  "model.failed": "读取模型列表失败：{err}",
  "model.set_usage": "用法：/model set <model-id>",
  "model.set_ok": "已写入 model_name = {id}{tail}",
  "model.set_restart_tail": " · ⚠ 需要重启 server 才能加载新模型",
  "model.set_failed": "切换模型 {id} 失败：{err}",
  "command.skills.list.desc": "按分类列出已加载的技能（与 /experiments 同源，仅显示统计）",
  "command.skills.show.desc": "查看单个技能的元数据 + 入口脚本：/skills show <name>",
  "command.skills.reload.desc": "重新扫描 skills_dir，刷新已加载的技能集合",
  "command.skills.install.desc": "从 git URL 或本地路径安装技能（不执行 setup 脚本）",
  "command.skills.enable.desc": "解禁先前 disable 过的技能",
  "command.skills.disable.desc": "禁用技能（保留文件，从注册表移除）",
  "skills.usage": "用法：\n  /skills list                — 列出技能\n  /skills show <name>         — 查看技能详情\n  /skills reload              — 重新扫描 skills_dir\n  /skills install <url|path>  — 安装技能（仅拷贝文件）\n  /skills enable <name>       — 解禁技能\n  /skills disable <name>      — 禁用技能",
  "skills.list_failed": "拉取技能列表失败：{err}",
  "skills.list_empty": "未发现任何技能",
  "skills.list_head": "技能分类（{n} 类，{total} 个用例）：",
  "skills.list_tail": "用 **/experiments** 查看每类下的具体故障注入用例",
  "skills.show_usage": "用法：/skills show <name>",
  "skills.show_failed": "读取技能 {name} 失败：{err}",
  "skills.show_head": "▸ 技能 · {name}",
  "skills.show_scripts_head": "脚本（{n}）：",
  "skills.reload_failed": "重新扫描技能失败：{err}",
  "skills.reload_head": "已重新扫描 {dir}（共 {total} 项）",
  "skills.reload_no_change": "  (无新增 / 移除)",
  "skills.reload_added": "  + 新增：{items}",
  "skills.reload_removed": "  - 移除：{items}",
  "skills.install_usage": "用法：/skills install <git-url|本地路径>",
  "skills.install_starting": "正在从 {source} 安装技能（仅拷贝文件，不执行任何脚本）…",
  "skills.install_failed": "安装失败：{err}",
  "skills.install_none": "未发现任何技能（缺少 SKILL.md 或验证失败）",
  "skills.install_head": "已安装 {n} 项技能：",
  "skills.install_next": "用 **/skills reload** 让其在 TUI 中生效",
  "skills.enable_usage": "用法：/skills enable <name>",
  "skills.enable_failed": "启用技能 {name} 失败：{err}",
  "skills.enable_noop": "{name} 当前未被禁用，无需操作",
  "skills.enable_ok": "已启用 {name}（用 /skills reload 立即生效）",
  "skills.disable_usage": "用法：/skills disable <name>",
  "skills.disable_failed": "禁用技能 {name} 失败：{err}",
  "skills.disable_noop": "{name} 已经处于禁用状态",
  "skills.disable_ok": "已禁用 {name}（用 /skills reload 刷新动态命令）",
  "command.skills.path.desc": "打印 skills_dir 的解析路径与候选优先级",
  "skills.path_head": "resolved: {dir}",
  "skills.path_candidates_head": "候选（按优先级）：",
  "skills.path_failed": "读取 skills_dir 失败：{err}",

  "recordings.empty": "本机暂无录像",
  "recordings.head": "{n} 个录像（最近修改在前）：",
  "recordings.use_replay": "用 **/replay <task_id>** 回放",
  "recordings.failed": "拉取录像列表失败：{err}",

  "replay.usage": "用法：**/replay <task_id> [speed]** — speed 是数字（默认 4x）或 'instant'",
  "replay.empty": "录像 {id} 为空",
  "replay.starting": "正在回放 **{id}** — {n} 个事件，速度 {speed} · esc 中止",
  "replay.done": "回放完成 · {converted} 个事件 · {skipped} 个跳过 · {duration}{tail}",
  "replay.aborted_tail": "（已中止）",
  "replay.failed": "回放 {id} 失败：{err}",
  "replay.unknown_command": "未知命令：/{name} — 试试 /help",

  "status.session": "会话 ID",
  "status.cluster": "集群",
  "status.namespace": "命名空间",
  "status.model": "模型",
  "status.mode": "权限模式",
  "status.created": "创建时间",
  "status.tasks": "任务数",
  "status.failed": "读取会话信息失败：{err}",
  "session.card.title": "会话",

  // -- Header chrome ------------------------------------------------
  "header.brand_tag": "（TS 预览版）",
  "header.commands_hint": "/help · /doctor · /mode · /exit",
  "header.connected_to": "已连接 {url}",
  "header.no_cluster": "(无集群)",
  "header.default_agent": "agent",

  // -- 输入框占位 ---------------------------------------------------
  "input.placeholder": "输入消息 · /help 查看命令",
  // 流式中替换普通 placeholder：提示 Enter 被锁，但仍允许提前撰写下一条
  "input.placeholder_streaming": "agent 输出中 — Enter 在本轮结束后发送",

  // -- AgentMessage 流式截断提示（仅 pending 状态显示，TURN_DONE 后落入
  // <Static>，scrollback 内可见完整文本） ----------------------------------
  "agent.truncated_earlier": "… 上方 {n} 行已折叠 · 轮结束后可在 scrollback 查看完整内容",

  // -- ResultCard 标签 ----------------------------------------------
  "result.label.fault": "故障类型",
  "result.label.uid": "Blade UID",
  "result.label.duration": "耗时",
  "result.label.summary": "效果摘要",
  "result.label.cause": "失败原因",
  "result.label.hint": "建议",
  "result.label.why_partial": "部分恢复原因",
  // v3 短 chip 标签（紧贴 bracket 用，全大写）
  "result.chip.success": "SUCCESS",
  "result.chip.partial": "PARTIAL",
  "result.chip.failed": "FAILED",
  "result.chip.unknown": "RESULT",
  // v3 卡片内分区标题
  "result.section.outcome": "执行结果",
  "result.section.effect": "效果验证",
  "result.section.recovery_notes": "恢复说明",
  "result.section.failure_analysis": "失败分析",
  "result.section.side_effects": "副作用",
  "result.label.target": "目标",
  "result.label.attempts": "尝试次数",
  "result.label.side_effect_item": "副作用",
  "result.side_effects_none": "未检测到连带影响",
  "result.attempts.label": "成功（自动重规划 {n} 次后）",
  "result.status.success": "故障注入成功",
  "result.status.partial": "部分恢复",
  "result.status.failed": "故障注入失败",
  "result.status.unknown": "结果",
  "result.status.success.recover": "故障恢复成功",
  "result.status.failed.recover": "故障恢复失败",
  "result.show_for_timeline": "/replay {id} instant 查看完整时间线",

  // -- Postmortem (T6) ---------------------------------------------
  "postmortem.title": "事后分析",
  "postmortem.saved_at": "完整 markdown: {path}",

  // -- PlanPreviewSection（注入计划 / 替代方案）--------------------------
  "plan_preview.title": "注入计划预览",
  "plan_preview.alternatives_title": "替代方案",

  // -- ConfirmMessage chrome ----------------------------------------
  "confirm.title": "确认意图",
  "confirm.body_empty": "（未收到 plan_summary）",
  "confirm.proceed": "执行",
  "confirm.refine": "重新描述",
  "confirm.answered": "已确认",
  "confirm.answered_rejected": "已取消",

  // -- ConfirmMessage Layer 1 (intent_confirm) ----------------------
  "confirm.intent.title": "确认故障意图",
  "confirm.intent.proceed": "提交意图",
  "confirm.intent.refine": "调整意图",

  // -- ConfirmMessage Layer 2 (confirmation_gate) -------------------
  "confirm.execution.title": "确认执行计划",
  "confirm.execution.proceed": "开始注入",
  "confirm.execution.cancel": "取消",
  "confirm.targetChange.chip": "DRIFT",
  "confirm.targetChange.title": "目标变更确认",
  "confirm.targetChange.preamble": "Agent 正在尝试操作与已批准不同的目标。",
  "confirm.targetChange.agentReason": "Agent 理由",
  "confirm.targetChange.agentReasonEmpty": "Agent 未提供理由",
  "confirm.targetChange.original": "原始目标",
  "confirm.targetChange.proposed": "Agent 提议目标",
  "confirm.targetChange.approve": "批准变更",
  "confirm.targetChange.reject": "拒绝",
  "confirm.planChange.chip": "PLAN",
  "confirm.planChange.title": "计划变更确认",
  "confirm.planChange.preamble": "Agent 在重新规划后发现原故障类型不可行，建议替换方案：",
  "confirm.planChange.reason": "变更原因",
  "confirm.planChange.original": "原故障类型",
  "confirm.planChange.proposed": "建议故障类型",
  "confirm.planChange.approve": "批准变更",
  "confirm.planChange.reject": "拒绝",

  // -- ConfirmMessage 字段标签 --------------------------------------
  "confirm.field.fault_type": "故障类型",
  "confirm.field.scope": "范围",
  "confirm.field.target": "目标",
  "confirm.field.action": "动作",
  "confirm.field.namespace": "命名空间",
  "confirm.field.labels": "标签选择器",
  "confirm.field.names": "目标资源",
  "confirm.field.params": "参数",
  "confirm.field.user_description": "用户描述",
  "confirm.field.skill": "技能",
  "confirm.field.plan_summary": "计划",
  "confirm.field.safety_status": "安全检查",
  "confirm.field.safety_reason": "安全说明",
  "confirm.field.intent_confidence": "解析置信度",
  "confirm.field.risk": "风险",
  "confirm.field.safety": "安全",

  // -- ConfirmMessage 前言 / 说明行 --------------------------------
  "confirm.intent.preamble": "已识别如下故障注入意图：",
  "confirm.execution.preamble": "请确认执行计划：",
  "confirm.generic.preamble": "请确认：",

  // -- v3 title chip 文案 (bracket chip 风格，简短全大写) ----------
  "confirm.intent.chip": "INTENT",
  "confirm.execution.chip": "EXECUTE",
  "confirm.generic.chip": "CONFIRM",

  // -- v3 卡片内分区标题 -------------------------------------------
  "confirm.section.decision_signals": "决策信号",
  "confirm.section.execution_plan": "执行计划",
  "confirm.section.safety_check": "安全检查",
  "confirm.section.parameters": "故障参数",
  "confirm.section.target_health": "目标健康",
  "confirm.section.conflicts": "冲突实验",
  "confirm.section.audit_trail": "决策溯源",
  "confirm.section.safety_score": "风险评分",
  // E10 — 多维度风险评分面板
  "safety_score.overall": "总分",
  "safety_score.blast_radius": "影响面",
  "safety_score.frequency": "重复度",
  "safety_score.time": "时段",
  "safety_score.topology": "拓扑",
  "safety_score.level.low": "低",
  "safety_score.level.medium": "中",
  "safety_score.level.high": "高",
  "safety_score.level.critical": "严重",
  // -- v3 额外字段标签
  "confirm.field.attempt": "尝试次数",
  "confirm.field.plan_path": "计划文件",
  "confirm.field.clarification_round": "澄清轮次",
  "confirm.field.intent_reasoning": "意图推理",
  "confirm.field.health_summary": "健康摘要",
  // 故障分类一行——把 L1 已识别的 fault_type + (scope/target/action)
  // 显式呈现，避免运维要靠 params 反推。"故障" 这个 label 与
  // confirm.field.fault_type（位于 L1 卡片，仅显示主类型）刻意区分：
  // 这里给的是完整三维语义，L1 那个只是类型本身。
  "confirm.field.fault": "故障",
  // 复杂任务标记——is_complex=true 时显示，避免 simple plan 出现
  // 冗余 "简单任务" 字样。颜色用 warn 提醒用户这是带正式计划文件的注入。
  "confirm.field.complexity": "复杂度",
  "confirm.complexity.complex": "复杂任务（已生成正式计划）",
  "confirm.attempt.label": "第 {n} 次尝试",
  "confirm.clarification.label": "已澄清 {n} 轮",
  "confirm.plan_saved": "已保存（{path}）· /show plan 查看",
  "confirm.field.conflicts": "冲突实验",
  "confirm.conflicts.hint": "/show experiments 查看详情",
  // 故障参数 / 目标健康两个 section 即使"没异常"也要常驻——
  // section 标题本身代表"我们查过了"，留个空值比直接隐藏更诚实
  //（否则用户可能以为 agent 没检查）。
  "confirm.field.health": "状态",
  "confirm.params.none": "—",
  "confirm.health.all_clear": "目标无异常",
  "confirm.health.not_run": "未执行检查",
  "confirm.field.feasibility": "可行性",
  "confirm.feasibility.all_clear": "注入可行",
  "confirm.feasibility.not_run": "未执行检查",
  "confirm.intent.low_conf_audit": "为何识别为此意图：",

  // -- Forge × Operator 重设计：banner + headline + answered chip --
  "confirm.intent.banner": "INTENT CHECK",
  "confirm.execution.banner": "EXECUTE · this hits production",
  "confirm.intent.headline": "软问：这是你想注入的故障吗？",
  "confirm.execution.headline": "硬问：真的对集群按下注入？",
  "confirm.armed_chip": "ARMED",
  "confirm.aborted_chip": "ABORTED",
  "confirm.armed_tail": "继续执行",
  "confirm.aborted_tail": "已停止",

  // -- ConfirmMessage 风险计 / 置信度 tier --------------------------
  "confirm.tier.low": "低",
  "confirm.tier.medium": "中",
  "confirm.tier.high": "高",
  "confirm.risk.runtime": "运行时确定",
  "confirm.risk.scope.labels": "标签匹配",
  "confirm.risk.scope.namespace": "整个 namespace",
  "confirm.risk.scope.percent": "百分比 {value}",

  // -- 低置信度警告 -------------------------------------------------
  "confirm.confidence.warn_strong": "强烈建议逐项核对",
  "confirm.confidence.warn_soft": "建议逐项核对",
  "confirm.confidence.warn_prod": "namespace 含 'prod' 字样，请确认非生产环境",

  // -- Safety badge -------------------------------------------------
  "confirm.safety.safe": "SAFE",
  "confirm.safety.warning": "WARNING",
  "confirm.safety.blocked": "BLOCKED",
  "confirm.safety.all_clear": "安全检查通过",

  // -- Select 组件提示 ---------------------------------------------
  "select.options.hint": "A-Z 跳转 · ↑↓ 选择 · Enter 确认 · Esc 取消",
  "select.feedback.hint": "Enter 发送 · Esc 返回选项",
  "select.feedback.placeholder": "告诉 agent 别的话…",

  // -- YesNoFeedbackSelect 通用默认标签（任何 是/否/自由文本 场景的兜底）
  "select.yesno.yes": "是",
  "select.yesno.no": "否",
  "select.yesno.feedback": "告诉我别的话…",

  // -- ConfirmMessage 选项标签 -------------------------------------
  "confirm.option.feedback": "告诉 agent 别的话…",

  // -- ConfirmMessage Plan Builder --------------------------------
  "confirm.plan_builder.title": "方案引导",
  "confirm.plan_builder.default_question": "请选择一项",
  "confirm.plan_builder.free_input": "自由输入",

  // -- SlashMenu 提示 -----------------------------------------------
  "slash.menu.hint": "↑↓ 选择 · Enter/Tab 应用 · Esc 取消",
  "slash.menu.empty": "（无匹配命令）",
  "slash.menu.more_above": "↑ 还有 {n} 条",
  "slash.menu.more_below": "↓ 还有 {n} 条",

  // -- Footer / 通用 -------------------------------------------------
  "footer.help_hint": "? 查看帮助",

  // -- 启动屏：欢迎卡片 ----------------------------------------------
  "welcome.welcome_back": "Welcome back!",
  "welcome.mode_label": "mode",
  "welcome.mode.auto": "自动",
  "welcome.mode.confirm": "确认",
  "welcome.tips_header": "Tips for getting started",
  "welcome.tip.describe": "描述故障场景，例如：\"对 default 命名空间的 nginx Pod 注入 CPU 满载\"",
  "welcome.tip.help": "使用 /help 查看所有可用命令",
  "welcome.tip.doctor": "使用 /doctor 检测运行环境",
  "welcome.tip.retry": "流式中断后用 /retry 重发上一次对话",
  "welcome.tip.mode": "按 Shift+Tab 切换权限模式（确认 ↔ 自动）",
  "welcome.runtime_header": "Runtime",
  "welcome.bottom_hint": "输入自然语言描述故障场景，或 /help 查看命令",

  // -- 启动屏：环境自检卡片 ------------------------------------------
  "boot.doctor.title": "环境自检",
  "boot.doctor.summary": "{passed}/{total} 通过",
  "boot.doctor.passed_short": "通过",
  "boot.doctor.fixes_header": "建议修复",
  "boot.doctor.captured_at": "自检于 {time}（/doctor 可重新跑）",
  "boot.doctor.unavailable": "preflight 端点不可用 —— server 版本陈旧或网络故障",

  // -- 启动屏：未完成任务卡片 ----------------------------------------
  "boot.pending.title": "未完成任务",
  "boot.pending.empty": "没有未执行完的任务",

  // -- 启动屏：进度提示文案（伴随 spinner） ---------------------------
  "boot.progress.spawning": "正在启动 blade-ai 后端...",
  "boot.progress.health": "等待后端就绪...",
  "boot.progress.session": "正在创建会话...",
  "boot.progress.preflight": "正在自检环境...",
  "boot.progress.tasks": "正在检查未完成任务...",

  // -- 退出屏：告别卡片 ---------------------------------------------
  // 内容与 Python TUI `tui/renderers/goodbye.py` 完全对齐，方便用户
  // 在两套 TUI 之间无缝切换。
  "goodbye.title": "再见",
  "goodbye.farewell": "感谢使用 blade-ai，期待下次再见",
  "goodbye.section.overview": "会话概览",
  "goodbye.section.activity": "活动统计",
  "goodbye.label.session_id": "会话 ID",
  "goodbye.label.duration": "持续时间",
  "goodbye.label.cluster_ns": "集群 / 命名空间",
  "goodbye.label.messages": "消息交互",
  "goodbye.label.injections": "故障注入",
  "goodbye.label.recoveries": "故障恢复",
  "goodbye.value.count": "{n} 次",
  "goodbye.cluster_auto": "(auto)",

  "common.none": "（无）",
  "common.unset": "（未设置）",
  "common.unknown": "（未知）",

  // -- Phase Stepper（5 步 todo list，inject turn 期间显示） --------
  // recovery 不在这里——恢复是独立流程，不会自动接续注入：
  //   * 注入 task_id 与恢复 task_id 不一定相同（用户可恢复任意任务）
  //   * 启动屏 PendingTasksCard 已经列出未完成任务
  //   * blade --timeout 提供时间兜底
  // 未来若加 recover 流程的 stepper，会作为 mode="recover" 单独维护。
  //
  // 五步对应真实 graph 节点（src/chaos_agent/agent/graph.py）：
  //   intent       → intent_clarification（澄清意图，含 Layer-1 confirm）
  //   agent_loop   → agent_loop（计划编排，phase1 工具）
  //   safety       → safety_check / confirmation_gate（Layer-2 confirm）
  //   execute      → baseline_capture / execute_loop / direct_execute
  //   verify       → verifier_loop
  "phase.stepper.title": "故障注入待办",
  "phase.label.intent": "意图识别",
  "phase.label.agent_loop": "计划编排",
  "phase.label.safety": "安全检查",
  "phase.label.execute": "故障注入",
  "phase.label.verify": "注入验证",

  // -- ToolMessage 卡片 chrome ---------------------------------------
  "tool.running": "执行中…",
  "tool.no_output": "（无输出）",
  "tool.more_lines": "… 还有 {n} 行",
  "tool.captured_in_confirm": "（输出已随下方确认卡片送达）",

  // -- WizardCard ---------------------------------------------------
  "wizard.step.welcome": "欢迎",
  "wizard.step.model": "模型",
  "wizard.step.api_url": "API URL",
  "wizard.step.api_key": "API Key",
  "wizard.step.kubeconfig": "Kubeconfig",
  "wizard.step.kube_context": "K8s Context",
  "wizard.step.permission": "权限模式",
  "wizard.step.summary": "确认保存",
  "wizard.welcome.title": "blade-ai 配置向导",
  "wizard.welcome.section": "你好",
  "wizard.welcome.body1": "8 步走完，blade-ai 就能跑起来。每步都有智能默认，按 Enter 接受。",
  "wizard.welcome.body2": "中间任何时候按 Esc 取消（不保存），← 返回上一步，1-8 数字键跳转已完成的步骤。",
  "wizard.welcome.fields_section": "你将配置",
  "wizard.model.title": "默认模型",
  "wizard.model.recommended_section": "推荐",
  "wizard.model.other_section": "其他",
  "wizard.model.custom_section": "自选 Model ID",
  "wizard.model.custom_option": "自选 model ID...",
  "wizard.model.custom_hint": "支持任何 OpenAI 兼容模型",
  "wizard.model.label": "Model ID",
  "wizard.model.placeholder": "例如 gpt-4-turbo / deepseek-r1 / gemini-2.5-pro",
  "wizard.api_url.title": "API Base URL",
  "wizard.api_url.section": "输入",
  "wizard.api_url.label": "URL",
  "wizard.api_key.title": "LLM API Key",
  "wizard.api_key.section": "输入",
  "wizard.api_key.label": "API Key",
  "wizard.kubeconfig.title": "Kubeconfig 路径",
  "wizard.kubeconfig.section": "输入",
  "wizard.kubeconfig.label": "路径",
  "wizard.kube_context.title": "K8s Context",
  "wizard.kube_context.section": "已发现",
  "wizard.permission.title": "权限模式",
  "wizard.permission.section": "模式",
  "wizard.permission.confirm_label": "confirm（推荐 prod）",
  "wizard.permission.confirm_hint": "每次注入前弹卡片确认",
  "wizard.permission.auto_label": "auto",
  "wizard.permission.auto_hint": "跳过确认，自动执行（仅测试集群）",
  "wizard.summary.title": "确认并保存",
  "wizard.summary.section_config": "配置",
  "wizard.summary.section_result": "保存结果",
  "wizard.summary.model": "模型",
  "wizard.summary.api_url": "API URL",
  "wizard.summary.api_key": "API Key",
  "wizard.summary.kubeconfig": "Kubeconfig",
  "wizard.summary.kube_context": "K8s Context",
  "wizard.summary.kube_context_default": "(使用 kubeconfig 当前 context)",
  "wizard.summary.permission": "权限模式",
  "wizard.summary.custom_tag": "(自选)",
  "wizard.summary.saved_to": "已保存到",
  "wizard.summary.saved_keys": "写入字段",
  "wizard.summary.save_error": "保存失败",
  "wizard.validation.in_progress": "校验中…",
  "wizard.returned_hint": "已返回此步骤，可重新校验或编辑",
  "wizard.hint.welcome": "Enter 开始  ·  Esc 取消",
  "wizard.hint.radio_with_back": "A-Z 选择  ·  ↑↓ 移动  ·  Enter 确认  ·  ← 上一步  ·  Esc 取消",
  "wizard.hint.text_with_back": "Enter 确认  ·  ← 上一步  ·  数字 1-8 跳转  ·  Esc 取消",
  "wizard.hint.model_custom": "Enter 确认  ·  Esc 返回预设列表  ·  ← 上一步",
  "wizard.hint.summary": "Enter 保存  ·  数字 1-7 跳回修改  ·  ← 上一步  ·  Esc 取消",
  "wizard.hint.saved": "已保存，Enter 进入主界面",
  "wizard.hint.save_failed": "保存失败，← 返回检查或 Esc 退出",
  "wizard.cancel_message": "配置向导已取消，blade-ai 退出。下次启动会再次提示。",
  "wizard.model.empty_error": "Model ID 不能为空",
};
