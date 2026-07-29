"""Fault execution backend providers.

The behaviour seam for adding new fault injection backends. See
:mod:`chaos_agent.agent.providers.base` for the protocol and
:mod:`chaos_agent.agent.providers.registry` for the dispatch registry.
"""

from chaos_agent.agent.providers.base import (
    EXECUTE,
    PLAN,
    RECOVER_VERIFY,
    VERIFY,
    FaultProvider,
    ProviderPhase,
    ProviderPrompts,
    RecoverResult,
)
from chaos_agent.agent.providers.registry import FaultProviderRegistry

__all__ = [
    "FaultProvider",
    "ProviderPrompts",
    "RecoverResult",
    "ProviderPhase",
    "FaultProviderRegistry",
    "PLAN",
    "EXECUTE",
    "VERIFY",
    "RECOVER_VERIFY",
]

# Self-register the built-in backends when the package is imported, so no
# caller has to remember to bootstrap. ``register_builtins`` is the single
# ordered source of the built-in set (precedence matters — see its docstring)
# and is idempotent, so it stays valid as the post-``clear()`` re-registration
# entry and the lazy self-bootstrap in ``detect_method``. Placed at the bottom
# (after ``FaultProviderRegistry`` is bound) and using lazy imports inside
# ``register_builtins`` keeps the ``registry ← concrete provider ← base`` import
# order acyclic.
FaultProviderRegistry.register_builtins()
