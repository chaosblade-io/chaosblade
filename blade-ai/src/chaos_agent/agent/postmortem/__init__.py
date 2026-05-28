"""Postmortem auto-generation subsystem.

Triggered at the end of save_memory when an experiment completes
(success or qualifying failure). LLM produces a structured markdown
report saved to ``~/.blade-ai/postmortems/<task_id>.md`` and surfaced
in the TUI ResultCard via the result envelope's ``postmortem`` field.

Public surface:
    build_postmortem_context(state) → dict
    generate_postmortem(context, llm, *, timeout) → str
    save_postmortem(task_id, markdown, root) → Path
    read_postmortem(task_id, root) → str
    should_generate_postmortem(state, settings) → bool
"""

from chaos_agent.agent.postmortem.builder import (
    build_postmortem_context,
    should_generate_postmortem,
)
from chaos_agent.agent.postmortem.generator import generate_postmortem
from chaos_agent.agent.postmortem.store import (
    POSTMORTEM_DIR,
    read_postmortem,
    save_postmortem,
)

__all__ = [
    "build_postmortem_context",
    "should_generate_postmortem",
    "generate_postmortem",
    "save_postmortem",
    "read_postmortem",
    "POSTMORTEM_DIR",
]
