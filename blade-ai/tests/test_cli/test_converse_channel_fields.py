"""Channel fields must reach the intent graph on the CLI/TUI path.

Regression cover for the second occurrence of the same omission. The first was
in ``l4/interaction.py`` (three ``graph_input`` branches carried only the four
Kubernetes fields); this one is ``AgentRunner.converse_stream``, which builds
``intent_input`` for the TUI.

Why it matters beyond command dispatch: the intent prompt renders its
`Capability Profile` section from ``state["kube_connection_mode"]``, and the
Inject Flow rule tells the model to check that section before submitting. With
the field unset the profile resolved to ``unknown``, the section was skipped, and
the rule pointed at something absent — so a host fault submitted against a k8s
channel produced no warning at all (observed in a real TUI session: the tool ran,
no intent card appeared, the turn ended silently).

Command dispatch never surfaced the gap because ``TransportTarget.from_state``
applies the settings fallback itself; the prompt does not.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from chaos_agent.cli.runner import AgentRunner


_CHANNEL_FIELDS = (
    "kube_connection_mode",
    "host_name",
    "ssh_host",
    "ssh_user",
    "ssh_key_path",
    "ssh_port",
)


def _converse_stream_src() -> str:
    return textwrap.dedent(inspect.getsource(AgentRunner.converse_stream))


def test_channel_fields_are_forwarded_when_caller_supplies_them():
    """The session-key whitelist must not stop at the Kubernetes fields."""
    src = _converse_stream_src()
    start = src.index("_session_keys")
    block = src[start:start + 600]
    for field in _CHANNEL_FIELDS:
        assert field in block, (
            f"{field} missing from _session_keys — a caller that passes it "
            "would have it silently dropped"
        )


def test_channel_fields_fall_back_to_settings():
    """Whitelisting alone is not enough: the TUI passes no transport kwargs.

    ``conversation.py`` calls ``converse_stream(session_id, user_message,
    interrupt_callback)`` and nothing else, while the channel lives in
    ``~/.blade-ai/config.json``. Without an explicit settings fallback the field
    stays unset no matter how wide the whitelist is.
    """
    src = _converse_stream_src()
    assert "getattr(settings," in src, (
        "no settings fallback — TUI sessions would still have no channel"
    )
    # the fallback must cover the same field set
    start = src.index("getattr(settings,")
    window = src[max(0, start - 400):start + 400]
    for field in ("kube_connection_mode", "host_name", "ssh_host"):
        assert field in window, f"{field} not covered by the settings fallback"


def test_fallback_does_not_override_an_explicit_caller_value():
    """An explicitly passed channel must win over the process-wide default."""
    src = _converse_stream_src()
    start = src.index("getattr(settings,")
    block = src[max(0, start - 300):start + 300]
    # guarded by a falsy check on the already-merged value
    assert "if not intent_input.get(" in block, (
        "fallback must only fill absent values, never clobber caller intent"
    )


def test_intent_input_is_the_target_dict():
    """The fields must land in the dict actually handed to the intent graph."""
    src = _converse_stream_src()
    tree = ast.parse(src)

    # ``intent_graph.astream_events(intent_input, ...)`` — the same name that the
    # fallback writes to, so the two cannot drift apart silently.
    streamed_names = {
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "astream_events"
        and node.args
        and isinstance(node.args[0], ast.Name)
    }
    assert "intent_input" in streamed_names, (
        "intent_input is no longer what gets streamed — the channel fallback "
        "may be writing to a dict nobody reads"
    )
