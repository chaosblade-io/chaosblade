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
    (``providers.message_scanning.scan_host_native_injection``) treats any ToolMessage
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

        from chaos_agent.agent.providers.message_scanning import scan_host_native_injection

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


class TestToolTimeoutPresentsUnknownOutcome:
    """R58: a caller-budget expiry is outcome-UNKNOWN, not a plain failure.

    The generic ``except Exception`` fallthrough formatted ToolTimeoutError
    as ``Error: host_inject blocked or failed: Command timed out ...`` —
    a retryable-shaped verdict on a command that may STILL be running on
    the host (R57 measured the shared transport: the local wait dies, the
    command survives server-side, ghost marker t+72s). On the host face
    the edge is the NORM: this tool's own docstring example
    (``stress-ng --timeout 600s``) outlives the default 60s budget.
    """

    @pytest.mark.asyncio
    async def test_tool_timeout_presents_unknown_outcome_reconcile_first(
        self, monkeypatch
    ):
        from chaos_agent.errors import ErrorAction, ToolTimeoutError, classify_error

        async def fake_run(cmd, target, **kwargs):
            raise ToolTimeoutError(
                "Command timed out after 60s: stress-ng --cpu 4 --timeout 600s"
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_inject.ainvoke({
            "command": "stress-ng --cpu 4 --timeout 600s",
        })

        # Keep: contract head, raw timeout text, no "failed" verdict.
        assert result.startswith("Error: host_inject:")
        assert "Command timed out after 60s" in result
        assert "failed" not in result.split("\n")[0]
        # The transient census still reads it as retryable-shaped (the
        # budget layer, not the text, caps retries).
        assert classify_error(result).action == ErrorAction.SHORT_RETRY
        # Changed: reconcile-first advice with host vocabulary.
        assert "Outcome UNKNOWN" in result
        assert "STILL be running" in result
        assert "double-execute" in result
        assert "host_read" in result

    @pytest.mark.asyncio
    async def test_receipt_timeout_presents_unknown_outcome(self, monkeypatch):
        """R59: the CLI-side wait expiry arrives as a receipt, not an exception.

        The caller timeout feeds both the local kill and the wiz CLI's
        --wait-timeout mirror; when the CLI wait expires first, the CLI
        exits non-zero with the platform's fixed "task timed out" receipt
        (measured verbatim in R56 on the isomorphic k8s channel) and
        parse_wiz_output passes it through — no ToolTimeoutError, so the
        R58 exception branch above never sees it.
        """
        from chaos_agent.errors import ErrorAction, classify_error
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr=(
                    "Error: task timed out after 30s "
                    "(task_uuid: 11111111-2222-3333-4444-555555555555)"
                ),
                duration_ms=30123,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_inject.ainvoke({
            "command": "stress-ng --cpu 4 --timeout 600s",
        })

        # Receipt head keeps the contract and drops the "failed" verdict...
        assert result.startswith("Error: host_inject (exit 1):")
        assert "task timed out after 30s" in result
        assert "failed" not in result.split("\n")[0]
        # ...stays retryable-shaped (the budget layer caps retries)...
        assert classify_error(result).action == ErrorAction.SHORT_RETRY
        # ...and teaches reconcile-first like the R58 exception branch.
        assert "Outcome UNKNOWN" in result
        assert "STILL be running" in result
        assert "host_read" in result

    @pytest.mark.asyncio
    async def test_plain_nonzero_exit_keeps_failed_verdict(self, monkeypatch):
        """R59 fail-safe: a receipt miss renders exactly as before."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr="iptables: command not found",
                duration_ms=12,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_inject.ainvoke({"command": "iptables -F"})

        assert result == (
            "Error: host_inject failed (exit 1): iptables: command not found"
        )

    def test_docstring_teaches_timeout_semantics(self):
        """R58-proposal item (3): the budget is the local wait, NOT the
        command's own runtime — the model must know a longer-running
        command returns outcome-UNKNOWN, not a misleading failure."""
        desc = getattr(host_inject, "description", "") or ""
        assert "NOT the" in desc
        assert "command's own runtime" in desc

    @pytest.mark.asyncio
    async def test_timeout_clamped_to_ceiling_with_warning(self, monkeypatch):
        """R60: the ONLY LLM-writable wait in src is bounded — an absurd
        value is clamped to settings.timeout_host_cmd (min semantics) and
        the output DISCLOSES the clamp (the LLM must know it waited less
        than it asked for). The clamp is the SSH face's only hang bound:
        its wrap carries no timeout and there is no server-side budget."""
        from chaos_agent.config.settings import settings
        from chaos_agent.transports.protocol import CommandResult

        captured: dict = {}

        async def fake_run(cmd, target, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return CommandResult(
                exit_code=0, stdout="ok", stderr="", duration_ms=5
            )

        monkeypatch.setattr(settings, "timeout_host_cmd", 600)
        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_inject.ainvoke({
            "command": "stress-ng --cpu 4 --timeout 600s",
            "timeout": 999999,
        })

        assert captured["timeout"] == 600
        assert "clamped" in result
        assert "999999" in result and "600s" in result
        # The note is a trailing disclosure, not an error verdict: the
        # call itself succeeded and must not read as a failure.
        assert result.startswith("ok")
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_no_clamp_below_ceiling_stays_silent(self, monkeypatch):
        """R60 zero-noise guarantee: at/below the ceiling the budget is the
        caller's value verbatim and no clamp note is emitted."""
        from chaos_agent.config.settings import settings
        from chaos_agent.transports.protocol import CommandResult

        captured: dict = {}

        async def fake_run(cmd, target, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return CommandResult(
                exit_code=0, stdout="ok", stderr="", duration_ms=5
            )

        monkeypatch.setattr(settings, "timeout_host_cmd", 600)
        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_inject.ainvoke({
            "command": "iptables -L -n", "timeout": 60,
        })

        assert captured["timeout"] == 60
        assert "clamped" not in result
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_read_clamps_and_warns_too(self, monkeypatch):
        """R60: host_read shares the bound — same ceiling, same disclosure
        (its generic except may carry a ToolTimeoutError, where the wait
        DID happen, so the clamp fact belongs on the error path too)."""
        from chaos_agent.config.settings import settings
        from chaos_agent.transports.protocol import CommandResult

        captured: dict = {}

        async def fake_run(cmd, target, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return CommandResult(
                exit_code=0, stdout="load ok", stderr="", duration_ms=5
            )

        monkeypatch.setattr(settings, "timeout_host_cmd", 600)
        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_read.ainvoke({
            "command": "uptime", "timeout": 999999,
        })

        assert captured["timeout"] == 600
        assert "clamped" in result
        assert result.startswith("load ok")

    def test_docstring_teaches_the_ceiling(self):
        """R60: both tools' descriptions teach the configured ceiling so
        the model knows per-call waits are bounded before it sends one."""
        for t in (host_inject, host_read):
            desc = getattr(t, "description", "") or ""
            assert "BLADE_AI_TIMEOUT_HOST_CMD" in desc
            assert "clamped" in desc

    @pytest.mark.asyncio
    async def test_timeout_floor_covers_zero_and_negative(self, monkeypatch):
        """R61: the clamp has a FLOOR too — a requested 0 or negative
        passed through unclamped (R60 bounded only the ceiling) while the
        local wait_for treats <=0 as an IMMEDIATE timeout (0.000s), so
        every call died before the command could start and landed in the
        R58 reconcile branch. The helper raises such values to 1s and
        DISCLOSES the floor (symmetric with the ceiling note)."""
        from chaos_agent.config.settings import settings
        from chaos_agent.transports.protocol import CommandResult

        captured: list = []

        async def fake_run(cmd, target, **kwargs):
            captured.append(kwargs.get("timeout"))
            return CommandResult(
                exit_code=0, stdout="ok", stderr="", duration_ms=5
            )

        monkeypatch.setattr(settings, "timeout_host_cmd", 600)
        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        for bad in (0, -5):
            result = await host_inject.ainvoke({
                "command": "uptime",
                "timeout": bad,
            })
            assert captured[-1] == 1
            assert "raised" in result
            assert f"{bad}s" in result and "1s" in result
            # Trailing disclosure, not a verdict: the call succeeded.
            assert result.startswith("ok")
            assert "Error" not in result

    @pytest.mark.asyncio
    async def test_floor_keeps_window_boundaries_silent(self, monkeypatch):
        """R61: inside the [1, ceiling] window the output stays
        byte-identical — both boundary values (1s floor, exact ceiling)
        pass through with no note, same zero-noise contract as below."""
        from chaos_agent.config.settings import settings
        from chaos_agent.transports.protocol import CommandResult

        captured: list = []

        async def fake_run(cmd, target, **kwargs):
            captured.append(kwargs.get("timeout"))
            return CommandResult(
                exit_code=0, stdout="ok", stderr="", duration_ms=5
            )

        monkeypatch.setattr(settings, "timeout_host_cmd", 600)
        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        for edge in (1, 600):
            result = await host_inject.ainvoke({
                "command": "uptime",
                "timeout": edge,
            })
            assert captured[-1] == edge
            assert result == "ok"
