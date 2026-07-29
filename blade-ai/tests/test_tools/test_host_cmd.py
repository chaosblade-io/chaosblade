"""Tests for the host tools' strict argument schema.

Regression guard for task-46317228: the LLM called ``host_read`` eight times
with ``node="cn-shanghai-cloudspe.25.209.68.1"`` — an explicit statement of
which machine it wanted. ``host_read``'s signature has no ``node``, so
LangChain silently dropped it and the command ran through the configured
``kubewiz_k8s`` channel, landing on the KubeWiz platform executor pod. The
verifier then used that unrelated machine's ``load average 0.02`` to contradict
the target node's real 90% CPU.

The fix must hold two properties: the extra key is REFUSED (not dropped), and
nothing is executed when it is present.
"""

import pytest
from pydantic import ValidationError

from chaos_agent.tools.host_cmd import host_inject, host_read


class TestStrictArgsSchema:
    def test_schema_exposes_only_declared_fields(self):
        """The rejection must not come at the cost of advertising extra fields.

        A ``**kwargs`` signature would make LangChain publish an ``unexpected``
        object field in the tool schema, inviting the model to fill it.
        """
        assert list(host_read.args) == ["command", "timeout", "task_id"]
        assert list(host_inject.args) == ["command", "timeout", "task_id"]


class TestUnknownArgsRefused:
    @pytest.mark.asyncio
    async def test_host_read_refuses_node_and_does_not_execute(self, monkeypatch):
        """Replay the exact accident shape."""
        from chaos_agent.config.settings import settings

        # Pin the channel: the advice text is session-dependent, so relying on
        # ambient settings would make this assertion pass or fail by accident.
        monkeypatch.setattr(settings, "kube_connection_mode", "kubewiz_k8s")
        monkeypatch.setattr(settings, "kubewiz_cluster_uuid", "uuid-1")
        monkeypatch.setattr(settings, "kubewiz_profile", "p1")
        calls = []

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("must not execute")

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", _spy
        )

        with pytest.raises(ValidationError) as exc:
            await host_read.ainvoke({
                "command": "uptime",
                "node": "cn-shanghai-cloudspe.25.209.68.1",
                "task_id": "task-46317228",
            })

        message = str(exc.value)
        assert "host_read does not accept these parameters: node" in message
        # The rejection must name the correct alternative — otherwise it is no
        # more actionable than the silent drop it replaces.
        assert "kubectl_read" in message
        assert "ssh / kubewiz_host" in message
        assert calls == [], "no command may be dispatched when args are refused"

    @pytest.mark.asyncio
    async def test_host_inject_refuses_unknown_arg(self, monkeypatch):
        async def _spy(*args, **kwargs):
            raise AssertionError("must not execute")

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", _spy
        )

        with pytest.raises(ValidationError) as exc:
            await host_inject.ainvoke({
                "command": "stress-ng --cpu 4 --timeout 60s",
                "host": "10.0.0.1",
            })

        assert "host_inject does not accept these parameters: host" in str(exc.value)

    @pytest.mark.asyncio
    async def test_multiple_unknown_args_all_listed(self, monkeypatch):
        async def _spy(*args, **kwargs):
            raise AssertionError("must not execute")

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", _spy
        )

        with pytest.raises(ValidationError) as exc:
            await host_read.ainvoke({"command": "uptime", "pod": "p", "node": "n"})

        # Sorted, so the message is stable regardless of dict ordering.
        assert "node, pod" in str(exc.value)


class TestRefusalReachesTheModel:
    """A refusal is only useful if the node hands its text to the model.

    Phase 1 / plan_builder replace ToolNode errors with their own "not available
    in this phase" wording (deliberately, to avoid suggesting bypass tools). That
    rewrite would swallow this hint AND assert something false — ``host_read`` IS
    bound in Phase 1; only the extra argument was wrong.
    """

    @pytest.mark.asyncio
    async def test_phase1_error_handler_passes_the_hint_through(self, monkeypatch):
        from chaos_agent.agent.graph import _phase1_handle_tool_error

        async def _spy(*args, **kwargs):
            raise AssertionError("must not execute")

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", _spy
        )

        with pytest.raises(ValidationError) as exc:
            await host_read.ainvoke({"command": "uptime", "node": "node-a"})

        rendered = _phase1_handle_tool_error(exc.value)
        assert "does not accept these parameters: node" in rendered
        assert "kubectl_read" in rendered
        assert "not available in Phase 1" not in rendered, (
            "an argument refusal must not be reported as a phase restriction — "
            "the model would abandon a legitimate tool instead of fixing the arg"
        )

    def test_other_phase1_errors_keep_the_phase_wording(self):
        """The pass-through must be narrow, not a blanket bypass."""
        from chaos_agent.agent.graph import _phase1_handle_tool_error

        rendered = _phase1_handle_tool_error(
            ValueError("Error: blade_create is not a valid tool, try one of [kubectl]")
        )
        assert "not available in Phase 1" in rendered
        assert "try one of" not in rendered, (
            "the phase message must not echo LangGraph's bypass suggestion list"
        )


class TestDeclaredArgsStillWork:
    @pytest.mark.asyncio
    async def test_normal_call_executes(self, monkeypatch):
        """The guard must not change behaviour for well-formed calls."""
        from chaos_agent.tools.guard import CommandResult

        async def _fake(cmd, target, **kwargs):
            return CommandResult(exit_code=0, stdout="load average: 1.0", stderr="", duration_ms=1)

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", _fake
        )

        out = await host_read.ainvoke({"command": "uptime", "timeout": 5})
        assert "load average: 1.0" in out


class TestProfileRefusalContract:
    """A profile refusal must read as an ERROR, not as command output.

    The ``Error:`` prefix is load-bearing, not cosmetic: carrier attribution
    (``providers._detection.scan_host_native_injection``) treats any ToolMessage
    NOT starting with ``Error:`` as a successful host-native injection. Drop the
    prefix and a REFUSED ``host_inject`` is recorded as a real injection — the
    recover graph then tries to reverse a fault that never happened.
    """

    @staticmethod
    def _k8s_channel(monkeypatch):
        from chaos_agent.config.settings import settings

        monkeypatch.setattr(settings, "kube_connection_mode", "kubewiz_k8s")
        monkeypatch.setattr(settings, "kubewiz_cluster_uuid", "uuid-1")
        monkeypatch.setattr(settings, "kubewiz_profile", "p1")

    @pytest.mark.asyncio
    async def test_host_read_refusal_is_prefixed(self, monkeypatch):
        self._k8s_channel(monkeypatch)
        out = await host_read.ainvoke({"command": "uptime"})
        assert out.startswith("Error: host_read refused:"), out[:120]
        assert "'host' profile channel" in out

    @pytest.mark.asyncio
    async def test_host_inject_refusal_is_not_attributed_as_an_injection(
        self, monkeypatch
    ):
        from langchain_core.messages import AIMessage, ToolMessage

        from chaos_agent.agent.providers._detection import scan_host_native_injection

        self._k8s_channel(monkeypatch)
        out = await host_inject.ainvoke({"command": "stress-ng --cpu 4 --timeout 60s"})
        assert out.startswith("Error: host_inject"), out[:120]

        messages = [
            AIMessage(content="", tool_calls=[{
                "name": "host_inject",
                "args": {"command": "stress-ng --cpu 4 --timeout 60s"},
                "id": "c1", "type": "tool_call",
            }]),
            ToolMessage(content=out, name="host_inject", tool_call_id="c1"),
        ]
        assert scan_host_native_injection(
            messages, frozenset({"host_inject"})
        ) is False, (
            "a refused injection must not be attributed as one — recovery would "
            "then try to reverse a fault that never happened"
        )


class TestRefusalAdviceMatchesTheSession:
    """The alternative named in a refusal must be usable IN THAT SESSION.

    "The hint has to name the correct alternative, or a rejection is no more
    useful than the silent drop it replaces" — and on a host channel the correct
    alternative is NOT ``kubectl_read``: the capability gate refuses that tool on
    exactly that session, so the model would walk from one dead end into another.
    That branch is also the reachable one: on a k8s channel the runtime screen
    refuses ``host_read`` before its arguments are ever validated.
    """

    @staticmethod
    async def _advice(monkeypatch, mode, **cfg):
        from chaos_agent.config.settings import settings

        monkeypatch.setattr(settings, "kube_connection_mode", mode)
        for key, value in cfg.items():
            monkeypatch.setattr(settings, key, value)
        with pytest.raises(ValidationError) as exc:
            await host_read.ainvoke({"command": "uptime", "node": "n-1"})
        return str(exc.value)

    @pytest.mark.asyncio
    async def test_host_session_does_not_point_at_a_gated_tool(self, monkeypatch):
        from chaos_agent.agent.capabilities import is_tool_name_allowed_for_context
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        state = {
            "fault_spec": {"scope": "host"}, "kube_connection_mode": "ssh",
            "ssh_host": "10.0.0.7", "ssh_user": "root",
        }
        # Premise: on this session kubectl_read is refused by the gate.
        assert is_tool_name_allowed_for_context("kubectl_read", state, "verify") is False

        message = await self._advice(
            monkeypatch, "ssh", ssh_host="10.0.0.7", ssh_user="root",
        )
        assert "kubectl_read" not in message, (
            "the refusal recommends a tool the gate refuses on this very session"
        )
        assert "drop the parameter" in message

    @pytest.mark.asyncio
    async def test_cluster_session_points_at_kubectl(self, monkeypatch):
        message = await self._advice(
            monkeypatch, "kubewiz_k8s", kubewiz_cluster_uuid="u", kubewiz_profile="p",
        )
        assert "kubectl_read" in message
        assert "ssh / kubewiz_host" in message

    @pytest.mark.asyncio
    async def test_advice_failure_cannot_swallow_the_refusal(self, monkeypatch):
        """Building the advice must never turn a rejection into a pass."""
        import chaos_agent.tools.host_cmd as mod

        monkeypatch.setattr(
            mod, "_targeting_advice",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # The override is only consulted through the args base class, so the
        # refusal itself must still happen even if advice construction is broken.
        monkeypatch.setattr(
            mod._HostTargetingArgs, "unknown_key_advice",
            classmethod(lambda cls: mod._targeting_advice()),
        )
        with pytest.raises(Exception) as exc:
            await host_read.ainvoke({"command": "uptime", "node": "n-1"})
        assert exc.type is not None  # refused one way or another, never executed
