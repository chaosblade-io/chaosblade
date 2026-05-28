"""Memory system: three-layer progressive memory architecture."""

from chaos_agent.memory.compactor import compact_memory
from chaos_agent.memory.context_manager import ContextManager
from chaos_agent.memory.hook import PreReasoningHook
from chaos_agent.memory.operational_memory import OperationalMemory
from chaos_agent.memory.session_store import SessionStore
from chaos_agent.memory.tokens import (
    TokenCount,
    TokenCountQuality,
    count_tokens,
    count_tokens_messages,
)
from chaos_agent.memory.tool_compactor import ToolResultCompactor
from chaos_agent.memory.tui_session_store import TuiSessionStore

__all__ = [
    "ContextManager",
    "ToolResultCompactor",
    "compact_memory",
    "SessionStore",
    "TuiSessionStore",
    "OperationalMemory",
    "PreReasoningHook",
    # E1 — model-aware token counting (replaces estimate_tokens /
    # count_tokens_approx / TOKEN_ESTIMATE_SAFETY_MARGIN).
    "TokenCount",
    "TokenCountQuality",
    "count_tokens",
    "count_tokens_messages",
]
