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
    StepActionScan,
)
from chaos_agent.agent.providers.chaosblade.declaration import (
    CARRIER_ID as _CHAOSBLADE_CARRIER_ID,
    PYTHON_CARRIER_ID as _PYTHON_CARRIER_ID,
    PYTHON_SUPPORTED_ACTIONS as _PYTHON_SUPPORTED_ACTIONS,
    PYTHON_SUPPORTED_TARGETS as _PYTHON_SUPPORTED_TARGETS,
    SUPPORTED_ACTIONS as _CHAOSBLADE_SUPPORTED_ACTIONS,
    SUPPORTED_TARGETS as _CHAOSBLADE_SUPPORTED_TARGETS,
    build_command_preview as _build_blade_command_preview,
)
from chaos_agent.agent.providers.faultdrill.declaration import (
    CARRIER_ID as _FAULTDRILL_CARRIER_ID,
    SUPPORTED_ACTIONS as _FAULTDRILL_SUPPORTED_ACTIONS,
    SUPPORTED_TARGETS as _FAULTDRILL_SUPPORTED_TARGETS,
)
from chaos_agent.agent.providers.host_shell.declaration import (
    CARRIER_ID as _HOST_SHELL_CARRIER_ID,
    SUPPORTED_ACTIONS as _HOST_SHELL_SUPPORTED_ACTIONS,
    SUPPORTED_TARGETS as _HOST_SHELL_SUPPORTED_TARGETS,
)
from chaos_agent.agent.providers.k8s_native.declaration import (
    CARRIER_ID as _K8S_NATIVE_CARRIER_ID,
    SUPPORTED_ACTIONS as _K8S_NATIVE_SUPPORTED_ACTIONS,
    SUPPORTED_TARGETS as _K8S_NATIVE_SUPPORTED_TARGETS,
)
from chaos_agent.agent.providers.registry import FaultProviderRegistry
from chaos_agent.agent.spec.fault_registry import (
    declare_carrier_vocabulary,
    declare_command_preview,
)

__all__ = [
    "FaultProvider",
    "ProviderPrompts",
    "RecoverResult",
    "StepActionScan",
    "ProviderPhase",
    "FaultProviderRegistry",
    "PLAN",
    "EXECUTE",
    "VERIFY",
    "RECOVER_VERIFY",
]

# ---------------------------------------------------------------------------
# Phase-12 carrier-vocabulary assembly (spec-import-retirement).
#
# This package import is the SINGLE assembly point where every carrier's
# lightweight ``declaration`` is registered with ``fault_registry``:
# importing ``chaos_agent.agent.spec.fault_spec`` (or anything that derives
# INTENT_* vocabulary) triggers this import first, so the aggregate is
# complete before derivation. Declarations are registration DATA only —
# importing them here pulls no provider implementation (their dependency
# discipline is pinned by the phase-12 guard in test_phase9_rename_guards.py);
# the heavy provider classes still register lazily through
# ``register_builtins`` below. ``fault_registry`` imports nothing from
# providers at module level — this direction (assembly point → registry
# declare API) is what keeps the dependency graph acyclic.
# ---------------------------------------------------------------------------
declare_carrier_vocabulary(
    _CHAOSBLADE_CARRIER_ID,
    _CHAOSBLADE_SUPPORTED_TARGETS,
    _CHAOSBLADE_SUPPORTED_ACTIONS,
)
declare_carrier_vocabulary(
    _K8S_NATIVE_CARRIER_ID,
    _K8S_NATIVE_SUPPORTED_TARGETS,
    _K8S_NATIVE_SUPPORTED_ACTIONS,
)
declare_carrier_vocabulary(
    _HOST_SHELL_CARRIER_ID,
    _HOST_SHELL_SUPPORTED_TARGETS,
    _HOST_SHELL_SUPPORTED_ACTIONS,
)
declare_carrier_vocabulary(
    _PYTHON_CARRIER_ID,
    _PYTHON_SUPPORTED_TARGETS,
    _PYTHON_SUPPORTED_ACTIONS,
)
declare_carrier_vocabulary(
    _FAULTDRILL_CARRIER_ID,
    _FAULTDRILL_SUPPORTED_TARGETS,
    _FAULTDRILL_SUPPORTED_ACTIONS,
)
declare_command_preview(
    _CHAOSBLADE_CARRIER_ID, _build_blade_command_preview
)

# Self-register the built-in backends when the package is imported, so no
# caller has to remember to bootstrap. ``register_builtins`` is the single
# ordered source of the built-in set (precedence matters — see its docstring)
# and is idempotent, so it stays valid as the post-``clear()`` re-registration
# entry and the lazy self-bootstrap in ``detect_method``. Placed at the bottom
# (after ``FaultProviderRegistry`` is bound) and using lazy imports inside
# ``register_builtins`` keeps the ``registry ← concrete provider ← base`` import
# order acyclic.
FaultProviderRegistry.register_builtins()
