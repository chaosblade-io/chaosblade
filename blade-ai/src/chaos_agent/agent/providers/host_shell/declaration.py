"""host_shell carrier declaration — vocabulary surface.

Phase-12 (spec-import-retirement): lightweight knowledge surface for the
spec layer. The provider class reads these tuples as its
``supported_targets`` / ``supported_actions`` class attributes, so the
runtime provider surface and the ``fault_registry`` aggregation can never
drift apart.

Dependency discipline (pinned by the declaration guard in
``tests/test_agent/test_phase9_rename_guards.py``): stdlib, typing,
``chaos_agent.transports`` and ``chaos_agent.config.settings`` ONLY — no
imports of the providers assembly layer, ``agent.spec``, or sibling
provider implementation modules.
"""
from __future__ import annotations

#: Carrier id of the host raw-shell backend (native-command faults).
CARRIER_ID = "host_shell"

#: Host raw-shell faults cover the same OS subsystems as the ChaosBlade OS
#: executor — these overlap the chaosblade set and dedup away in the
#: ``fault_registry`` aggregate (the ``host`` family declares both carriers).
SUPPORTED_TARGETS = ("cpu", "mem", "network", "disk", "process")

#: Action verbs available through raw shell commands (subset of the
#: ChaosBlade verbs — no ``stop``).
SUPPORTED_ACTIONS = (
    "fullload",
    "load",
    "delay",
    "loss",
    "drop",
    "fill",
    "kill",
    "burn",
)

#: Raw host command tools that, when run successfully, mark a host-native
#: injection — the host-native carrier vocabulary. SINGLE SOURCE for two
#: consumers that must never drift (R23/G-7): the provider's
#: ``inject_tool_names`` class attribute (detect/recency scans) and the
#: machinery≠mutation HOST face's tool-name domain
#: (``execution_artifacts.host_call_is_registered_recovery`` — the
#: exemption is earned by the provider's own tool names, never by a
#: hand-copied set). Declared HERE so the generic layer consumes pure
#: data through the declaration seam instead of importing the provider
#: class (phase-11 carrier-import boundary).
HOST_INJECT_TOOL_NAMES = frozenset(
    {"host_inject", "exec_host_command", "shell"}
)
