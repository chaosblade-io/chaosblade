"""Channel fields must reach the intent graph on the server /turn route.

Regression cover for the third occurrence of the same omission. The first
was ``l4/interaction.py`` (three ``graph_input`` branches carried only the
four Kubernetes fields); the second was ``AgentRunner.converse_stream``
(the local TUI twin, retired 2026-09-01 — this guard's predecessor lived in
tests/test_cli/test_converse_channel_fields.py and was retired with it);
this one is the first-turn ``initial_state`` in ``server/routes/turn.py``,
the single TUI conversation entry point that remains.

Why it matters beyond command dispatch: the intent prompt renders its
`Capability Profile` section from ``state["kube_connection_mode"]``, and
the Inject Flow rule tells the model to check that section before
submitting. With the field unset the profile resolved to ``unknown``, the
section was skipped, and the rule pointed at something absent — so a host
fault submitted against a k8s channel produced no warning at all
(observed in a real TUI session: the tool ran, no intent card appeared,
the turn ended silently).

Command dispatch never surfaced the gap because ``TransportTarget.from_state``
applies the settings fallback itself; the prompt does not.
``TurnRequest`` carries no transport fields, so the route must resolve
them from settings — first turn only; later turns inherit via checkpoint
merge.
"""

from __future__ import annotations

import ast
import inspect

from chaos_agent.agent.state import IntentState
from chaos_agent.server.routes import turn


_CHANNEL_FIELDS = (
    "kube_connection_mode",
    "host_name",
    "ssh_host",
    "ssh_user",
    "ssh_key_path",
    "ssh_port",
)


def _turn_route_src() -> str:
    return inspect.getsource(turn)


def _first_turn_block(src: str) -> str:
    start = src.index("if is_first_turn:")
    end = src.index("else:", start)
    return src[start:end]


def _later_turn_block(src: str) -> str:
    start = src.index("else:")
    return src[start:]


def test_first_turn_state_carries_all_channel_fields():
    """Every channel field must land in the first-turn initial_state."""
    block = _first_turn_block(_turn_route_src())
    for field in _CHANNEL_FIELDS:
        assert field in block, (
            f"{field} missing from the first-turn initial_state — the intent "
            "prompt's Capability Profile section would be skipped and the "
            "Inject Flow rule would point at something absent"
        )


def test_channel_fields_resolve_from_settings():
    """TurnRequest carries no transport fields — settings is the only source."""
    block = _first_turn_block(_turn_route_src())
    assert "settings.kube_connection_mode" in block
    for field in ("host_name", "ssh_host", "ssh_user", "ssh_key_path"):
        assert f'settings, "{field}"' in block, (
            f"{field} must resolve via getattr(settings, ...) — no request "
            "field carries it"
        )


def test_later_turns_inherit_via_checkpoint_not_rebuild():
    """The later-turn branch must not touch channel fields.

    They arrive via checkpoint merge from the first turn. A channel field
    appearing in the else-branch would mean someone misunderstood the
    merge contract — or started rebuilding state per turn.
    """
    later = _later_turn_block(_turn_route_src())
    for field in _CHANNEL_FIELDS:
        assert field not in later, (
            f"{field} rebuilt on later turns — should inherit via checkpoint "
            "merge from the first turn"
        )


def _initial_state_keys() -> set[str]:
    """Every key of every ``initial_state = {...}`` dict in the route source.

    AST walk (not regex) so nested dicts / multi-line entries stay correct.
    """
    tree = ast.parse(_turn_route_src())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "initial_state"
                for t in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ):
            keys.update(
                k.value
                for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
    assert keys, "no initial_state dict found — the parser drifted from the route"
    return keys


def test_every_route_key_is_a_declared_intent_channel():
    """Structural guard: an undeclared key is silently DROPPED by langgraph.

    Inputs are filtered by the schema's channel set exactly like node
    updates (state.py's IntentState docstring: "unknown-channel updates
    vanish without error"). ``planning_mode`` shipped in the route from its
    first commit yet was the one key of eighteen with no IntentState
    channel — so the TUI's planning-mode request never reached
    plan_builder until the field was declared (found in review
    2026-09-01). This test pins the whole class, not just that one key:
    add a key to the route's initial_state without declaring the channel
    and it fails here, not silently in production.
    """
    channels = IntentState.__required_keys__ | IntentState.__optional_keys__
    missing = _initial_state_keys() - channels
    assert not missing, (
        f"initial_state keys with no IntentState channel: {sorted(missing)} — "
        "langgraph drops unknown keys from both inputs and updates, so "
        "these values never reach the intent graph (declare them on "
        "IntentState in agent/state.py)"
    )
