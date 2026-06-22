"""Backward-compat shim — canonical location is utils.knowledge_registry."""
from chaos_agent.utils.knowledge_registry import (  # noqa: F401
    KNOWLEDGE_DIR,
    get_knowledge_registry,
    rebuild_registry,
)
