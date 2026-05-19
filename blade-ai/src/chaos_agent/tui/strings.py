"""User-visible strings — single source for all TUI text.

Centralizes all display strings for consistency and future i18n readiness.
"""

# Phase Timeline
PHASE_NAMES = {
    "intent": "意图识别",
    "safety": "安全检查",
    "inject": "故障注入",
    "verify": "注入验证",
    "recovery": "恢复就绪",
}

# Header / Brand
BRAND_NAME = "blade-ai"
BRAND_TAGLINE = "Kubernetes 混沌工程智能体 — 自然语言驱动故障注入与恢复"

# Permission modes
MODE_CONFIG = {
    "confirm": ("\u2717", "确认", "mode-confirm"),    # ✗
    "auto": ("\u273b", "自动", "mode-auto"),           # ✻
}

# Display-density modes (PR-D1 §17.1) — labels surfaced in /mode messages,
# the slash-menu hint, and the (forthcoming) bottom-toolbar footer. Keep
# pure text (no emoji) so they survive PR-B4 ASCII fallback unchanged.
DISPLAY_MODE_LABELS = {
    "calm": "极简",
    "working": "工作",
    "dense": "全开",
}
DISPLAY_MODE_DESCRIPTIONS = {
    "calm": "新手 / 演示：仅决策摘要 + 工具内联 + 最终结果",
    "working": "默认：+ 实验卡片头、failure_reason、confirm 风险标尺",
    "dense": "老手 / 复盘：+ 全部 sparkline、locator、replan timeline、side_effects",
}

# Welcome card
TIPS = [
    "描述故障场景，例如：\"对 default 命名空间的 nginx Pod 注入 CPU 满载\"",
    "使用 /help 查看所有可用命令",
    "使用 /doctor 检测运行环境",
    "按 Shift+Tab 切换权限模式（确认 ↔ 自动）",
    "按 Ctrl+G 或输入 /mode 切换信息密度（极简 / 工作 / 全开）",
]
WELCOME_CARD_HINT = "输入自然语言描述故障场景，或 /help 查看命令"

# Role labels
ROLE_LABELS = {
    "user": "你",
    "agent": "Agent",
    "system": "系统",
    "error": "错误",
    "thinking": "思考",
}

# System messages
WELCOME_MESSAGE = "欢迎使用 Blade AI！输入自然语言描述故障场景，或使用 /help 查看命令。"
INITIALIZING = "正在初始化..."
NO_ACTIVE_TASK = "无活跃任务"
SESSION_NOT_INITIALIZED = "AgentRunner 未初始化"
INJECTION_CANCELLED = "注入已取消，已触发恢复流程。"

# Onboarding
ONBOARDING_TITLE = "欢迎使用 Blade AI"
ONBOARDING_DESC = "完成以下配置即可开始使用。"
CONFIG_SAVED = "配置已保存，正在启动 Blade AI..."
SETUP_SKIPPED = "已跳过配置。后续可通过 /config wizard 重新打开向导。"

# Wizard (single-panel onboarding flow)
WIZARD_TITLE_FIRST = "Blade AI · 初次配置"
WIZARD_TITLE_EDIT = "Blade AI · 重新配置"
WIZARD_SKIPPED_BANNER = "首次配置已跳过；运行 /config wizard 完成配置"

# Confirm / Question
CONFIRM_TITLE = "确认门禁"
QUESTION_TITLE = "Agent 提问"

# Interrupted tasks
INTERRUPTED_TASKS_TITLE = "未完成任务"
INTERRUPTED_TASKS_COUNT = "项待恢复"
INTERRUPTED_TASKS_HINT = "输入 /recover <task_id> 恢复指定任务"
INTERRUPTED_TASKS_NONE = "没有未执行完的任务"
INTERRUPTED_CONFIRMATION = "等待确认"
INTERRUPTED_QUESTION = "等待回答"
INTERRUPTED_GENERIC = "已中断"

# Error
STREAM_INTERRUPTED = "连接中断，按 Enter 重试。"

# Thinking — verb pool sampled while the model is generating reasoning
# tokens. We do NOT show the raw chain-of-thought; only one of these
# verbs animates next to a breathing spinner. The pool is deliberately
# domain-flavored (chaos-eng) so it teaches users what the agent is
# weighing while still feeling alive.
THINKING_VERBS = (
    "思考中",
    "拆解中",
    "推敲中",
    "比对工具",
    "回忆上下文",
    "权衡爆炸半径",
    "校对意图",
    "复核安全策略",
    "对照技能目录",
    "组织回答",
)

# Input
INPUT_PLACEHOLDER = "输入消息或 /命令...（Shift+Enter 换行）"

# Slash commands — descriptions render as the meta column in the popup,
# so keep each one a single concise line that hints at usage.

# General
CMD_HELP_DESC = "列出所有可用的 / 命令及其说明"
CMD_CLEAR_DESC = "清空终端 + 结束当前对话（保留任务历史）"
CMD_EXIT_DESC = "退出 TUI（等同于 Ctrl+D）"
CMD_DOCTOR_DESC = "重跑环境自检（kubectl / Operator / API Key），按需引导安装"
CMD_CONFIG_DESC = "查看或修改配置；支持 list / get / set / unset / path"
CMD_MODEL_DESC = "查看或切换 LLM 模型；支持 list / set <name>"
CMD_MODE_DESC = "切换信息密度：calm / working / dense（不带参数则循环）"
CMD_COMPACT_DESC = "立即压缩当前会话上下文以释放 token"
CMD_MEMORY_DESC = "查看或清理会话记忆；支持 show / clear / path"

# Business
CMD_PLAN_DESC = "Dry-Run 多轮规划：/plan <自然语言描述>，不真正执行"
CMD_RUN_DESC = "执行故障注入：/run <描述> 或不带参数落地当前 Dry-Run 计划"
CMD_RECOVER_DESC = "回滚已注入的故障：/recover <task_id> | latest | list"
CMD_REVIEW_DESC = "查看任务详情：/review [task_id]，默认查看最近一条"
CMD_TASKS_DESC = "列出任务：/tasks [active|failed|all]"
CMD_EXPERIMENTS_DESC = "列出已知故障实验场景"

# Locator commands (PR-D4): /show / /copy / /rerun resolve the [E#] /
# [T#] handles allocated by experiment_card and tool_panel renderers.
CMD_SHOW_DESC = "查看一个 locator：/show E1 | T3"
CMD_COPY_DESC = "打印一个 locator 的可复制文本：/copy E1 | T3"
CMD_RERUN_DESC = "回放一个实验 locator：/rerun E1（仅打印原始描述供再发起）"
# /expand pairs with the inline-two-line tool result hint; the inline
# preview drops to one line per tool call, /expand re-prints the full
# cached output. Accepts a bare digit so muscle memory from the hint
# ``/expand 1`` works alongside the locator form ``/expand T1``.
CMD_EXPAND_DESC = "展开一个工具调用的完整输出：/expand T1（也接受 /expand 1）"

# PR-E3 — recording replay
CMD_REPLAY_DESC = "回放某次任务的事件录像：/replay <task_id> [--instant] [--speed N]"
CMD_RECORDINGS_DESC = "列出 / 导出事件录像：/recordings [list | export <task_id> <out>]"

# Skills
CMD_SKILLS_DESC = "技能管理；支持 list / show / reload / install / enable / disable / path"
CMD_SKILLS_LIST_DESC = "列出所有已加载的技能"
CMD_SKILLS_SHOW_DESC = "查看指定技能的元数据与资源"
CMD_SKILLS_RELOAD_DESC = "重新扫描技能目录"
CMD_SKILLS_INSTALL_DESC = "从 git URL 或本地路径安装技能（仅拷贝文件）"
CMD_SKILLS_ENABLE_DESC = "启用一个先前禁用的技能"
CMD_SKILLS_DISABLE_DESC = "禁用一个技能（保留文件）"
CMD_SKILLS_PATH_DESC = "打印技能目录的解析过程"

# Removed (kept for reference; do not register):
#   /inject  → 改为 /run
#   /status  → 改为 /review
#   /export  → 已移除（请使用 `blade-ai | tee session.log`）

# PR-E9 — JIT 学习提示。每条至多触发一次（calm 模式全静默），
# 用于把"下一步可以做什么"一行点亮在用户视线里，避免 /help 全文吞屏。
HINT_CHAT_TRY_EXAMPLE = (
    "提示：试试 \u300c\u7ed9 default \u547d\u540d\u7a7a\u95f4\u6ce8\u5165 CPU "
    "\u6ee1\u8f7d 60 \u79d2\u300d\u8fd9\u6837\u7684\u63cf\u8ff0\uff0c"
    "\u6216 /help \u67e5\u770b\u547d\u4ee4\u3002"
)
HINT_FIRST_ERROR = "提示：输入 /doctor 检测当前运行环境（kubectl / Operator / API Key）。"
HINT_FIRST_LOCATOR = "提示：输入 /show {label} 可重新查看刚才那条记录的详情。"
HINT_DISPLAY_MODE_USAGE = (
    "提示：calm 极简 · working 工作（默认）· dense 全开；按 Ctrl+G 循环切换。"
)
