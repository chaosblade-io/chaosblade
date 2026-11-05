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

import hashlib
import shlex

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


class TestInjectedStateTolerated:
    """Regression guard for the seam migration (A/A run inject-cda2987d).

    ``host_read``/``host_inject`` resolve their transport target through
    ``resolve_transport_target(state)``, the graph state arriving via
    ``InjectedState``. But these two tools ALSO carry an explicit
    ``StrictToolArgs`` schema (``extra="forbid"``) — the task-46317228 guard
    that refuses a per-call ``node``/``host``. An UNDECLARED injected ``state``
    key hit that before-validator, so EVERY real ToolNode call died with
    "does not accept these parameters: state" — which the planning LLM misread
    as "host_read is not available in Phase 1". Direct
    ``.ainvoke({"command": ...})`` unit calls never inject state, so the whole
    pre-existing suite stayed green while the tool was broken end-to-end.

    These tests drive the injected-state path the runtime actually takes.
    """

    @pytest.mark.asyncio
    async def test_injected_state_passes_validation_and_executes(self, monkeypatch):
        from chaos_agent.tools.guard import CommandResult

        calls = []

        async def _spy(cmd, target, **kwargs):
            calls.append((cmd, target))
            return CommandResult(
                exit_code=0, stdout="load average: 1.0", stderr="", duration_ms=1
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", _spy
        )

        # The runtime injects the whole graph state; a plain dict stands in.
        out = await host_read.ainvoke({
            "command": "uptime",
            "state": {"progress_ledger": {}, "fault_spec": None},
        })
        assert "load average: 1.0" in out
        assert len(calls) == 1, "injected state must reach execution, not be refused"

    def test_injected_state_hidden_from_llm_schema(self):
        # Declared with the InjectedState marker => excluded from the LLM-facing
        # schema, so the model still cannot pass it (nor be invited to).
        assert "state" not in host_read.tool_call_schema.model_fields
        assert "state" not in host_inject.tool_call_schema.model_fields

    @pytest.mark.asyncio
    async def test_unknown_key_still_refused_alongside_state(self, monkeypatch):
        # The guard's original intent survives: an LLM-supplied ``node`` is
        # refused even while the framework-injected ``state`` is present, and
        # ``state`` is NOT named as one of the unknown keys.
        async def _spy(*args, **kwargs):
            raise AssertionError("must not execute")

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", _spy
        )

        with pytest.raises(ValidationError) as exc:
            await host_read.ainvoke({
                "command": "uptime",
                "node": "n-1",
                "state": {"progress_ledger": {}},
            })
        # Sorted unknown list is exactly "node" (period-terminated before the
        # advice); a leaked ``state`` would render "node, state".
        assert "does not accept these parameters: node." in str(exc.value)


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
        # Changed: reconcile-first advice points at the BOUND tool
        # (host_inject's read-only superset role), NOT the unbound host_read —
        # host_read is not bound in EXECUTE/RECOVER_VERIFY (the only phases
        # host_inject runs in), so "use host_read" would be a dead-end pointer
        # of the same class the pipe-readonly guard eliminates.
        assert "Outcome UNKNOWN" in result
        assert "STILL be running" in result
        assert "double-execute" in result
        assert "host_inject itself" in result
        assert "host_read is NOT bound" in result
        assert "use host_read" not in result

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
        # ...and teaches reconcile-first like the R58 exception branch —
        # pointing at host_inject's read-only role, not the unbound host_read.
        assert "Outcome UNKNOWN" in result
        assert "STILL be running" in result
        assert "host_inject itself" in result
        assert "host_read is NOT bound" in result
        assert "use host_read" not in result

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


class TestReadOnlyProbeExitSemantics:
    """First-principles #31: a non-zero exit is channel signal, not a verdict.

    host_inject is the SUPERSET tool that both injects AND observes in
    EXECUTE/RECOVER_VERIFY. The ``read_only`` flag (computed to skip the
    guard) must ALSO decide exit-code semantics: a read-only absence/health
    probe's non-zero exit IS the observation (``cat <deleted>`` → ENOENT
    proves the file is gone), so it must NOT be framed "Error: ... failed" —
    that mislabels fault evidence as a tool malfunction AND, because the
    single-source verdict gate (tool_verdicts.GENERIC_ERROR_PREFIXES) keys on
    the "Error" prefix, would route a successful observation into
    classify_error → introspection / auto-replan. The injection path
    (read_only=False) keeps the "failed" verdict unchanged.
    """

    @pytest.mark.asyncio
    async def test_readonly_probe_nonzero_exit_is_observation_not_failure(
        self, monkeypatch
    ):
        """``cat <deleted-file>`` exit 1 + ENOENT is the fault evidence."""
        from chaos_agent.agent.tool_verdicts import (
            tool_result_error_text,
            tool_result_failed,
        )
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr=(
                    "cat: /opt/drill-filedel/app-config.yaml: "
                    "No such file or directory"
                ),
                duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_inject.ainvoke({
            "command": "cat /opt/drill-filedel/app-config.yaml",
        })

        # NOT framed as a tool failure — no "Error" prefix, no "failed" verdict.
        assert not result.startswith("Error")
        assert "failed" not in result.split("\n")[0]
        # Carries the exit code + the verbatim observation (the evidence).
        assert "read-only observation" in result
        assert "exit 1" in result
        assert "No such file or directory" in result
        # The load-bearing protection is the single-source verdict gate: with no
        # "Error" prefix it returns None, so classify_error is NEVER invoked on
        # this text — react_helpers.detect_tool_error_hint (L972-976) and
        # detect_transient_retry_exhaustion (L1105-1113) both short-circuit on
        # ``message_result_error_text is None`` BEFORE calling classify_error,
        # and execute_loop's auto-replan reads the graph-state ``result["error"]``
        # (None here), not the raw tool string. We therefore assert the GATE
        # invariant, NOT ``classify_error(result)`` directly: the verbatim ENOENT
        # evidence legitimately contains an absence phrase, so classifying the
        # raw text out of the consumer pipeline would misroute to REPLAN — which
        # is exactly why every consumer goes through the gate rather than the
        # tool string. The gate is the protection; sanitizing evidence is not.
        assert tool_result_error_text("host_inject", result) is None
        assert tool_result_failed("host_inject", result) is False

    @pytest.mark.asyncio
    async def test_readonly_probe_empty_output_is_absence_observation(
        self, monkeypatch
    ):
        """EMPTY output + non-zero exit is the absence-probe's POSITIVE
        observation, NOT "the probe never ran".

        ROOT FIX (design-level, 2026-11-02): the former code framed an empty
        result as an honest "Error: the probe never ran: host unreachable /
        lack the binary", splitting read-only exits on output-text presence.
        That axis was BACKWARDS (measured): ``pgrep``/``test -e``/``grep -q``
        are SILENT by design on no-match — exit!=0 with BOTH streams empty IS
        the finding ("not running" / "not present"), exactly as this tool's own
        ``pgrep <killed-proc>`` design note and the sibling host_read guidance
        ("`(no output)` means NOT installed — the probe succeeded") state;
        host_read returns "(no output)" for this shape with no Error at all. A
        genuine transport drop or missing binary WRITES stderr (non-empty → the
        observation branch), so the empty branch never caught the "host
        unreachable" case it claimed to. Empty output must therefore be
        observation data (no "Error" prefix), consistent with host_read and with
        the non-empty branch above.
        """
        from chaos_agent.agent.tool_verdicts import (
            tool_result_error_text,
            tool_result_failed,
        )
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=1, stdout="", stderr="", duration_ms=5
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        # ``pgrep <absent-proc>`` no-match: exit 1, silent — the absence
        # observation ("process not running"), not a tool failure.
        result = await host_inject.ainvoke({
            "command": "pgrep -a nonexistent_proc",
        })

        # NOT framed as a tool failure — no "Error" prefix, no "failed" verdict.
        assert not result.startswith("Error")
        assert "failed" not in result.split("\n")[0]
        # Carries the exit code + the empty-result absence guidance, and no
        # false "host unreachable" assertion.
        assert "read-only observation" in result
        assert "empty result" in result
        assert "unreachable" not in result
        # The load-bearing protection is the single-source verdict gate: with no
        # "Error" prefix it returns None, so classify_error is NEVER invoked on
        # this text and a successful absence observation is never routed into
        # introspection / auto-replan.
        assert tool_result_error_text("host_inject", result) is None
        assert tool_result_failed("host_inject", result) is False

    @pytest.mark.asyncio
    async def test_mutation_nonzero_exit_keeps_failed_verdict_contrast(
        self, monkeypatch
    ):
        """Contrast: a FAULT command's non-zero exit really IS a failure.

        ``iptables -F`` is a mutation (read_only=False), so it keeps the
        "Error: ... failed" framing + verdict — the read-only probe-mode
        discriminator must NOT leak onto the injection path.
        """
        from chaos_agent.agent.tool_verdicts import tool_result_failed
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr="iptables: command not found",
                duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )

        result = await host_inject.ainvoke({"command": "iptables -F"})

        assert result.startswith("Error: host_inject failed (exit 1)")
        assert "read-only observation" not in result
        assert tool_result_failed("host_inject", result) is True


class TestHostSuccessMergesBothStreams:
    """Success-path (exit 0) mirror of kubectl's TestSuccessOutputMergesBothStreams.

    Case #12 R1 (inject-2969899c): arming the recovery carrier
    (``systemd-run --on-active``) and latching the kill (``systemctl mask``)
    BOTH exited 0 with their confirmation on **stderr** — "Running timer as
    unit: …" and "Created symlink … → /dev/null." — while stdout was empty
    (verified live on host-drill:2222). The stdout-only receipt said
    "(command completed, no output)", so the model could not see its own
    plan's success criterion ("output contains 'Running timer as unit …'")
    and burned two extra fail-closed read-only ``systemctl show`` probes
    (~31s of LLM turn) to re-confirm state the transport had already
    reported but dropped. host_inject's success path was the lone holdout
    against the invariant its own failure branch, host_read, kubectl
    (Case #63) and execute_skill_script all uphold: non-empty stderr must
    ride along under the "--- STDERR ---" separator.
    """

    @pytest.mark.asyncio
    async def test_stderr_only_carrier_arm_confirmation_survives(self, monkeypatch):
        """systemd-run's "Running … as unit" (stderr, stdout empty) reaches
        the model — the exact confirmation Case #12's plan keyed on."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0,
                stdout="",
                stderr=(
                    "Running timer as unit: blade-restore-x.timer\n"
                    "Will run service as unit: blade-restore-x.service"
                ),
                duration_ms=141,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_inject.ainvoke({
            "command": "systemd-run --on-active=240s --unit=blade-restore-x true",
            "timeout": 5,
        })
        assert "--- STDERR ---" in out
        assert "Running timer as unit" in out      # the plan's success criterion
        assert "no output" not in out               # dropped-evidence receipt gone
        assert not out.startswith("\n")             # no leading blank from empty stdout

    @pytest.mark.asyncio
    async def test_stderr_only_mask_confirmation_survives(self, monkeypatch):
        """systemctl mask's "Created symlink → /dev/null" (stderr) survives."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0,
                stdout="",
                stderr="Created symlink /etc/systemd/system/x.service → /dev/null.",
                duration_ms=162,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_inject.ainvoke({"command": "systemctl mask x", "timeout": 5})
        assert "--- STDERR ---" in out
        assert "Created symlink" in out

    @pytest.mark.asyncio
    async def test_stdout_and_stderr_both_present_host_read(self, monkeypatch):
        """Both streams non-empty → stdout first, stderr appended. host_read
        previously dropped stderr whenever stdout was present (``stdout or
        stderr``) — the milder half of the same invariant violation."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0, stdout="payload-line", stderr="warning: deprecated",
                duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_read.ainvoke({"command": "systemctl status x", "timeout": 5})
        assert "payload-line" in out
        assert "--- STDERR ---" in out
        assert "warning: deprecated" in out
        assert out.index("payload-line") < out.index("--- STDERR ---")

    @pytest.mark.asyncio
    async def test_clean_stdout_byte_identical(self, monkeypatch):
        """Routine reads (empty stderr) are byte-identical to before — no
        separator, no marker, stdout verbatim. This is the no-regression
        guarantee for the overwhelming majority of host observations."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0, stdout="Restart=always\nMainPID=1279186", stderr="",
                duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_read.ainvoke({
            "command": "systemctl show x -p Restart -p MainPID", "timeout": 5,
        })
        assert out == "Restart=always\nMainPID=1279186"
        assert "--- STDERR ---" not in out

    @pytest.mark.asyncio
    async def test_both_empty_keeps_per_tool_marker(self, monkeypatch):
        """A truly silent success keeps the honest per-tool empty marker —
        the merge must not invent evidence that was never produced."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(exit_code=0, stdout="", stderr="", duration_ms=5)

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        inject_out = await host_inject.ainvoke({
            "command": "systemctl reset-failed x", "timeout": 5,
        })
        read_out = await host_read.ainvoke({"command": "uptime", "timeout": 5})
        assert inject_out == "(command completed, no output)"
        assert read_out == "(no output)"


class TestHostSuccessOutputSafetyValve:
    """The host success paths must BOUND runaway output like every sibling.

    host_cmd kept the merge half of the Case #63 fix but omitted its PAIRED
    output safety valve, so it was the ONE tool success path (against
    ``kubectl_cli`` / ``registry`` / ``host_http_probe_cli``) that returned an
    unbounded byte stream — violating truncation.py's contract that success and
    error share the SAME 64KB ceiling ("a one-sided valve would just move the
    crash to the other side"). host_inject/host_read run arbitrary shell whose
    stdout can be runaway (``dd if=/dev/zero`` with no count, ``cat
    /dev/urandom``, an unbounded ``journalctl``, a loop), and those raw bytes
    entered state.messages + the compactor's FULL on-disk cache + the off-graph
    consumers (metric extractor, verifier) that read a tool return before
    compaction. These lock the tool-layer bound for the whole command class —
    no case-specific branch, the invariant is "any host success output is
    bounded at the shared ceiling".

    They deliberately do NOT assert a tail-borne summary reaches the LLM: the
    compactor governs the LLM-visible form head-only (16KB recent / 1KB old,
    ``truncate_text`` keeps ``encoded[:max_bytes]``) and re-cuts the valved
    body, so a merged-tail line (dd's stderr timing) still falls out of the
    compacted context downstream. Surviving THAT is the metric extractor's
    ``[Auto-extracted]`` HEAD-prefix job — a separate coverage question the
    valve does not and cannot answer.
    """

    _CEILING = 64 * 1024

    @pytest.mark.asyncio
    async def test_runaway_stdout_bounded_at_ceiling_host_read(self, monkeypatch):
        """A read-only diagnostic flooding stdout is pinned to the ceiling."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0, stdout="A" * (200 * 1024), stderr="", duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_read.ainvoke({"command": "systemctl status x", "timeout": 5})
        assert len(out.encode("utf-8")) <= self._CEILING
        assert "OUTPUT_TRUNCATED" in out

    @pytest.mark.asyncio
    async def test_runaway_stdout_bounded_at_ceiling_host_inject(self, monkeypatch):
        """Same bound on the injection face — the two share merge + valve."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0, stdout="A" * (200 * 1024), stderr="", duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_inject.ainvoke({"command": "systemctl mask x", "timeout": 5})
        assert len(out.encode("utf-8")) <= self._CEILING
        assert "OUTPUT_TRUNCATED" in out

    @pytest.mark.asyncio
    async def test_valve_budget_sees_merged_total_not_one_stream(self, monkeypatch):
        """The valve judges the MERGED body, not a pre-merge component.

        Regression discipline (4524e903): stdout ALONE stays under the ceiling
        while stdout + a NON-EMPTY stderr crosses it, so a valve that judged the
        original component length would pass through un-truncated. An
        empty-stderr variant is a false-green side channel — merged and stdout
        lengths would coincide and hide the de-coupling, so stderr is load-bearing
        here by construction.
        """
        from chaos_agent.transports.protocol import CommandResult

        stdout = "A" * (self._CEILING - 64)   # under the ceiling on its own
        stderr = "B" * 512                     # pushes the MERGED body over

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0, stdout=stdout, stderr=stderr, duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_read.ainvoke({"command": "systemctl status x", "timeout": 5})
        assert len(out.encode("utf-8")) <= self._CEILING
        assert "OUTPUT_TRUNCATED" in out       # fired on the merged total
        assert "--- STDERR ---" in out          # merge happened BEFORE valving

    @pytest.mark.asyncio
    async def test_small_merged_output_passthrough_byte_identical(self, monkeypatch):
        """Under the ceiling: byte-identical passthrough, zero valve noise.

        The overwhelming majority of host reads are small; the valve must be a
        no-op within budget so the fix changes nothing for them.
        """
        from chaos_agent.transports.protocol import CommandResult
        from chaos_agent.tools.host_cmd import _merge_success_streams

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(
                exit_code=0, stdout="load average: 1.0", stderr="note", duration_ms=5,
            )

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_read.ainvoke({"command": "uptime", "timeout": 5})
        # timeout=5 sits inside the clamp window → clamp_note is empty, so the
        # success path is exactly the merged body (valve passthrough).
        assert out == _merge_success_streams(
            "load average: 1.0", "note", empty="(no output)"
        )
        assert "OUTPUT_TRUNCATED" not in out


# -- payload fidelity receipt (by-construction evidence) ----------------------


class TestPayloadFidelityReceipt:
    """The transport states the payload boundary as a FACT it already holds.

    Every host channel rebuilds the cleared argv with
    ``" ".join(shlex.quote(p) for p in cmd)`` (``SSHChannel.wrap_command``,
    ``_wiz_preserve_stderr``, ``embed_stdin_in_command``), so a ``sh -c
    '<script>'`` payload arrives as ONE intact element and its internal syntax
    is parsed remotely. The readback a model reaches for instead,
    ``systemctl show <unit> -p ExecStart``, renders ``argv[]`` SPACE-JOINED
    WITHOUT QUOTES — byte-identical whether the script arrived intact or was
    split. Host Case #13 recognised the ambiguity and took the only exit the
    old feedback left open: it ``systemctl stop``'d an already-armed carrier and
    re-armed as three bare-argv units (~331s, 14.9% of the run), which cannot
    carry ``|| true`` — so the ``chronyc makestep`` unit exited 1 and landed
    ``failed``, a state systemd does not GC.

    The receipt is the load-bearing half of the fix: it puts the machine fact in
    the message where the doubt would form, so it does not depend on any
    earlier context surviving.
    """

    @pytest.mark.parametrize("cmd", [
        # Case #13's real composite restore armament — the shape that was torn
        # apart and rebuilt.
        "systemd-run --on-active=240s --unit=blade-restore-ntp sh -c "
        "'systemctl start chrony; systemctl start lima-guestagent; "
        "chronyc makestep 2>/dev/null || true'",
        # A single-word chain is still a payload: split into bare argv,
        # ``sh -c systemctl`` would run only ``systemctl`` and pass
        # ``restart``/``nginx`` as $0/$1. Whitespace, not shell syntax, is what
        # makes the rendering ambiguous — a "contains a shell operator"
        # proxy silently missed this one when first written.
        "systemd-run --on-active=60s --unit=restore sh -c 'systemctl restart nginx'",
        "systemd-run --on-active=60s --unit=r /bin/bash -c 'systemctl restart nginx'",
        # The detector's scope is the guard's scope, so it also describes a form
        # the guard would refuse — the receipt never changes a verdict.
        "sh -c 'echo PROBE_A PROBE_B'",
    ])
    def test_fires_on_an_interpreter_payload(self, cmd):
        from chaos_agent.tools.host_cmd import payload_fidelity_receipt

        assert payload_fidelity_receipt(shlex.split(cmd)) is not None, cmd

    @pytest.mark.parametrize("cmd", [
        # bare-argv armaments: the overwhelming majority of host calls, and the
        # form Case #13 was pushed INTO. No boundary ambiguity, no receipt.
        "systemd-run --on-active=240s --unit=blade-restore-ntp systemctl start chrony",
        "systemd-run --on-active=240s --unit=blade-restore-makestep chronyc makestep",
        # a whitespace-free payload is a lossless space-join
        "systemd-run --on-active=60s --unit=r sh -c true",
        # interpreter forms the guard refuses are not admitted payloads:
        # a script FILE is parsed by the remote shell, not carried as a token
        "systemd-run --on-active=60s --unit=r sh /tmp/restore.sh",
        # plain injections
        "date -s +2hours",
        'date -s "+2 hours"',
        "iptables -A INPUT -p tcp --dport 80 -j DROP",
        "stress-ng --cpu 4 --timeout 60",
        "dd if=/dev/zero of=/data/fill bs=1M count=1024",
        'kubectl patch pvc foo -p {"spec":{"storageClassName":"invalid sc"}}',
        # reads, including the ambiguous readback itself
        "systemctl show blade-restore-ntp.service -p ExecStart",
        "chronyc tracking",
        "df -h /var/lib",
        "top -bn1",
    ])
    def test_silent_when_there_is_no_boundary_ambiguity(self, cmd):
        from chaos_agent.tools.host_cmd import payload_fidelity_receipt

        assert payload_fidelity_receipt(shlex.split(cmd)) is None, cmd

    def test_indices_point_at_the_payload_element(self):
        """The receipt must describe the element it is vouching for, so the
        index has to be the payload's, not the interpreter's or the flag's."""
        from chaos_agent.tools.host_cmd import _interpreter_payload_indices

        argv = shlex.split(
            "systemd-run --on-active=60s --unit=r sh -c 'systemctl start chrony; true'"
        )
        assert argv == [
            "systemd-run", "--on-active=60s", "--unit=r", "sh", "-c",
            "systemctl start chrony; true",
        ]
        assert _interpreter_payload_indices(argv) == [5]
        assert argv[5].startswith("systemctl start chrony")
        # A truncated argv has no payload element to vouch for — the window
        # needs the interpreter, ``-c`` and the script, so a ragged tail is
        # silent rather than IndexError.
        assert _interpreter_payload_indices(argv[:5]) == []
        assert _interpreter_payload_indices(["sh", "-c", "a b"]) == [2]

    def test_receipt_describes_the_element_it_vouches_for(self):
        from chaos_agent.tools.host_cmd import payload_fidelity_receipt

        argv = shlex.split(
            "systemd-run --on-active=60s --unit=r sh -c 'systemctl start chrony; true'"
        )
        text = payload_fidelity_receipt(argv)
        # element index is 1-based and names the whole argv
        assert "element 6 of 6" in text
        assert f"{len(argv[5].encode('utf-8'))} bytes" in text
        assert (
            "sha256:" + hashlib.sha256(argv[5].encode("utf-8")).hexdigest()[:12]
        ) in text
        # The fact itself, and the reason no probe can exist.
        assert "arrived as ONE intact token" in text
        assert "parsed by the REMOTE shell" in text
        assert "admits none" in text
        # It must also disarm the readback the model would otherwise trust or
        # distrust — naming WHY that channel cannot settle it.
        assert "SPACE-JOINED WITHOUT QUOTES" in text
        assert "a property of the rendering, not evidence of a split" in text
        # FACTS ONLY, pinned by what the receipt does NOT contain: it names no
        # alternative carrier form. Case #13's re-arm was motivated by
        # unverifiability, not by taste, so removing the doubt is sufficient —
        # a receipt that also legislated form would be steering on a question
        # nobody asked, and form preference belongs to the skill's carrier
        # standard.
        for legislating in ("systemd-run", "wiz", "bare-argv", "bare argv"):
            assert legislating not in text, legislating

    @pytest.mark.asyncio
    async def test_rides_the_success_receipt_only(self, monkeypatch):
        """Wiring: the fact must actually reach the model. A detector that is
        correct but not attached is the state this test prevents regressing to."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(exit_code=0, stdout="Running as unit: r.service",
                                 stderr="", duration_ms=5)

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        armed = await host_inject.ainvoke({
            "command": "systemd-run --on-active=60s --unit=r sh -c "
                       "'systemctl start chrony; chronyc makestep || true'",
            "timeout": 5,
        })
        assert "Running as unit: r.service" in armed
        assert "[payload fidelity — by construction]" in armed

        plain = await host_inject.ainvoke({
            "command": "systemd-run --on-active=60s --unit=r chronyc makestep",
            "timeout": 5,
        })
        assert "Running as unit: r.service" in plain
        assert "payload fidelity" not in plain

    @pytest.mark.asyncio
    async def test_read_path_never_carries_a_receipt(self, monkeypatch):
        """``host_read`` refuses shell composition outright, so no opaque
        element can reach a success receipt there — the observation path stays
        silent by construction, not by a special case."""
        from chaos_agent.transports.protocol import CommandResult

        async def fake_run(cmd, target, **kwargs):
            return CommandResult(exit_code=0, stdout="ok", stderr="", duration_ms=5)

        monkeypatch.setattr(
            "chaos_agent.tools.host_cmd.execute_via_transport", fake_run
        )
        out = await host_read.ainvoke({"command": "uptime", "timeout": 5})
        assert "payload fidelity" not in out


# -- host_read refusal dispatch ----------------------------------------------


class TestHostReadRefusalDispatch:
    """Two refusals with OPPOSITE fix paths must not share one text.

    A shape refusal (pipe / redirect / write flag) is fixed by reshaping. The
    unregistered-binary terminal says no shape of that binary can ever be
    admitted — so the generic text's "check whether a binary exists" advice
    sends the model at a probe that cannot change the verdict. Case #13 ran
    ``command -v chronyc``, got ``/usr/bin/chronyc``, and learned nothing
    (installed ≠ registered).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", [
        "losetup -a",                       # unregistered, read-shaped
        "rm -rf /var/lib/chaos_fill.img",   # unregistered, genuinely mutating
    ])
    async def test_unregistered_branch_disarms_the_existence_probe(self, cmd):
        out = await host_read.ainvoke({"command": cmd})
        assert out.startswith("Error:")
        assert "REGISTERED diagnostic" in out
        # The probe is named only to forbid it, and the reason it cannot help
        # is stated — a bare prohibition invites a retry.
        assert "Do NOT probe whether it is installed" in out
        assert "cannot change this verdict" in out
        # A mutating intent still gets told where it belongs, without a pointer
        # at a tool that is not bound in this phase.
        assert "execution phase" in out
        assert "host_inject" not in out

    @pytest.mark.asyncio
    async def test_shape_branch_keeps_the_existence_probe(self):
        """The other half must survive the split: for a shape refusal the
        binary is often genuinely missing, so ``command -v`` guidance is the
        fix. Removing it from both branches would trade one dead end for
        another."""
        out = await host_read.ainvoke({"command": "df -h | grep vda"})
        assert out.startswith("Error:")
        assert "command -v" in out
        assert "admitted by SHAPE, not by binary" in out

    @pytest.mark.parametrize("cmd", [
        # Every tool the unregistered refusal NAMES as a way through must
        # actually be admitted — naming one that is refused would put a fresh
        # false assertion in the very message written to remove one.
        "df -h", "ps -ef", "ss -tlnp", "cat /proc/mounts", "ls /var/log",
        "stat /etc/chrony.conf", "find /var/log -name '*.log'", "date",
        "uname -a", "cat /proc/uptime",
        "systemctl status chrony", "timedatectl status", "iptables -L -n",
        "blade status",
    ])
    def test_every_named_way_through_is_really_admitted(self, cmd):
        from chaos_agent.tools.readonly import host_command_rejection_reason

        assert host_command_rejection_reason(cmd) is None, (
            f"the refusal text advertises {cmd!r} but the classifier refuses it: "
            f"{host_command_rejection_reason(cmd)}"
        )
