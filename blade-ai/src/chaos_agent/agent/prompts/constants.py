"""Prompt constants and budgets."""

REPLAN_MARKER = "[REPLAN]"
CACHE_BOUNDARY = "\n<!-- BLADE_AI_CACHE_BOUNDARY -->\n"

# --- Single resource budgets ---
MAX_AGENT_MD_BYTES = 25_000          # 单个 AGENT.md 上限 (与 Claude Code MEMORY.md 一致)
MAX_KNOWLEDGE_SUMMARY_BYTES = 5_000  # 知识摘要上限

# --- Global prompt budget ---
MAX_SYSTEM_PROMPT_CHARS = 80_000     # 系统提示词总字符数上限 (~20K tokens)
