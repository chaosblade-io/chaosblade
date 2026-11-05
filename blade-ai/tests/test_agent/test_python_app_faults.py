"""Tests for the Python-application fault domain (chaosblade_python carrier).

Covers the four seams a new fault domain must land in:
  - vocabulary / family registration (scope, targets, actions, profile)
  - the provider contract (detection isolation from the ChaosBlade OS carrier)
  - the injection tool's command construction and precondition surfacing
  - target-guard classification (the path that would otherwise reject every
    in-process injection as cross-profile drift)
"""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.chaosblade.python_provider import ChaosbladePythonProvider
from chaos_agent.agent.spec.fault_registry import (
    carrier_actions,
    carrier_targets,
    family_for_scope,
    is_host_scope,
    is_python_scope,
    profile_of_scope,
    python_scopes,
    required_intent_params,
)
from chaos_agent.models.command_result import CommandResult


PYTHON_SCOPE = "python"


class TestVocabularyAndFamily:
    def test_scope_registered(self):
        from chaos_agent.agent.spec.fault_spec import INTENT_SCOPES

        assert PYTHON_SCOPE in INTENT_SCOPES

    def test_targets_and_actions_derive_from_carrier(self):
        from chaos_agent.agent.spec.fault_spec import INTENT_ACTIONS, INTENT_TARGETS

        assert carrier_targets("chaosblade_python") == (
            "redis",
            "mysql",
            "http",
            "httpx",
            "grpc",
            "kafka",
            "sqlalchemy",
        )
        assert carrier_actions("chaosblade_python") == (
            "delay",
            "throwCustomException",
            "returnValue",
        )
        for target in carrier_targets("chaosblade_python"):
            assert target in INTENT_TARGETS
        for action in carrier_actions("chaosblade_python"):
            assert action in INTENT_ACTIONS

    def test_family_profile_is_host(self):
        """The injection command runs on the machine hosting the process."""
        family = family_for_scope(PYTHON_SCOPE)
        assert family is not None
        assert family.carrier_types == ("chaosblade_python",)
        assert profile_of_scope(PYTHON_SCOPE) == "host"

    def test_namespace_not_required(self):
        assert "namespace" not in required_intent_params(PYTHON_SCOPE)

    def test_python_scope_predicate_is_distinct_from_host(self):
        """Same capability profile, different fault semantics — the predicates
        must not alias each other, or host-resource logic would run against an
        in-process fault."""
        assert is_python_scope(PYTHON_SCOPE) is True
        assert is_host_scope(PYTHON_SCOPE) is False
        assert is_python_scope("host") is False
        assert python_scopes() == {PYTHON_SCOPE}


def _python_inject_msgs(uid: str = "a" * 16, *, destroyed: bool = False):
    """AIMessage + ToolMessage pair attesting a blade_python_create injection."""
    msgs = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "blade_python_create",
                    "id": "c0",
                    "type": "tool_call",
                    "args": {"target": "redis", "action": "delay"},
                }
            ],
        ),
        ToolMessage(
            content='{"code":200,"success":true,"result":"%s"}' % uid,
            name="blade_python_create",
            tool_call_id="c0",
        ),
    ]
    if destroyed:
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "blade_destroy",
                        "id": "d0",
                        "type": "tool_call",
                        "args": {"uid": uid},
                    }
                ],
            )
        )
    return msgs


class TestProviderContract:
    @pytest.fixture(autouse=True)
    def _register(self):
        FaultProviderRegistry.register_builtins()

    def test_registered_and_resolvable(self):
        assert FaultProviderRegistry.resolve_by_method("python_agent").carrier == (
            "chaosblade_python"
        )
        assert (
            FaultProviderRegistry.resolve_primary_by_scope(PYTHON_SCOPE).carrier
            == "chaosblade_python"
        )

    def test_only_host_channel(self):
        p = ChaosbladePythonProvider()
        assert p.matches_channel("host") is True
        assert p.matches_channel("k8s") is False

    def test_detects_own_injection(self):
        p = ChaosbladePythonProvider()
        msgs = _python_inject_msgs()
        assert p.detect(msgs, is_host=True) == "python_agent"
        assert p.injection_recency(msgs, is_host=True) >= 0

    def test_destroyed_uid_not_reclaimed(self):
        p = ChaosbladePythonProvider()
        msgs = _python_inject_msgs(destroyed=True)
        assert p.detect(msgs, is_host=True) is None

    def test_chaosblade_os_carrier_does_not_claim_python_injection(self):
        """The isolation that makes a separate tool necessary: the OS carrier
        scans only blade_create / kubectl ToolMessages, so it must NOT attribute
        a Python-agent experiment to itself (which would route recovery to the
        wrong backend)."""
        from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider

        msgs = _python_inject_msgs()
        assert ChaosbladeProvider().detect(msgs, is_host=True) is None
        assert (
            FaultProviderRegistry.detect_method(
                msgs,
                is_host=True,
            )
            == "python_agent"
        )

    def test_execute_phase_binds_injection_surface(self):
        names = [t.name for t in ChaosbladePythonProvider().tools("execute")]
        assert "blade_python_create" in names
        assert "blade_destroy" in names  # ReAct cleanup
        assert "blade_create" not in names  # not this domain's tool

    def test_plan_phase_has_no_injection_tool(self):
        names = [t.name for t in ChaosbladePythonProvider().tools("plan")]
        assert "blade_python_create" not in names

    def test_verify_note_directs_to_application_layer(self):
        note = ChaosbladePythonProvider().verify_prompt_note("python_agent")
        assert note
        # The core misreading this note must prevent.
        assert "application layer" in note.lower()
        assert "NOT evidence" in note
        assert ChaosbladePythonProvider().verify_prompt_note("host_blade") == ""


class TestInjectionToolCommand:
    async def _run(self, **kwargs) -> tuple[str, list[str], bool]:
        """Invoke the tool with the transport mocked; return (out, argv, bypass)."""
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_create

        captured: dict = {}

        async def _fake(cmd, target, **kw):
            captured["cmd"] = cmd
            captured["bypass"] = kw.get("bypass_channel")
            return CommandResult(
                stdout='{"code":200,"success":true,"result":"%s"}' % ("b" * 16),
                stderr="",
                exit_code=0,
            )

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_create.ainvoke(kwargs)
        return out, captured["cmd"], captured["bypass"]

    @pytest.mark.asyncio
    async def test_redis_delay_command_shape(self):
        out, argv, bypass = await self._run(
            target="redis",
            action="delay",
            cmd="GET",
            flags="--time 500",
        )
        assert argv[1:5] == ["create", "python", "redis", "delay"]
        assert ["--cmd", "GET"] == argv[argv.index("--cmd") : argv.index("--cmd") + 2]
        assert ["--time", "500"] == argv[
            argv.index("--time") : argv.index("--time") + 2
        ]
        # Runs on the host that hosts the application (never wiz-bypassed).
        assert bypass is False
        # Duration guarantee applies to this path too.
        assert "--timeout" in argv
        assert "success" in out

    @pytest.mark.asyncio
    async def test_matchers_invalid_for_target_are_dropped(self):
        """A Redis matcher passed to a MySQL fault must not reach the CLI as an
        unknown flag."""
        _out, argv, _bypass = await self._run(
            target="mysql",
            action="delay",
            cmd="GET",
            sqltype="select",
            flags="--time 100",
        )
        assert "--cmd" not in argv
        assert ["--sqltype", "select"] == argv[
            argv.index("--sqltype") : argv.index("--sqltype") + 2
        ]

    @pytest.mark.asyncio
    async def test_missing_prepare_record_returns_prepare_guidance(self):
        """The precondition is surfaced here — the only point that knows the
        fault is a Python-application fault.

        The stderr fixture is the CLI's VERBATIM output, captured by running
        ``blade create python redis delay --time 500 --cmd GET`` against
        chaosblade 1.9.0-alpha with no running preparation. An earlier version of
        this test used an invented message ("port not found, please execute
        prepare command firstly"), which made the test pass while the production
        match never fired against the real CLI.
        """
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_create

        async def _fake(cmd, target, **kw):
            return CommandResult(
                stdout='{"code":47000,"success":false,"error":"invalid `port` '
                "parameter value: ``. no running python preparation "
                'record found"}',
                stderr="",
                exit_code=1,
            )

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_create.ainvoke(
                {"target": "redis", "action": "delay", "flags": "--time 500"}
            )
        assert out.startswith("Error:")
        # Remedy for THIS failure mode is a prepare call, which is possible
        # mid-drill.
        assert "blade_python_prepare" in out
        assert "retry this injection" in out

    @pytest.mark.asyncio
    async def test_agent_not_listening_reports_prerequisite_failure(self):
        """A prepare record without a live agent needs an application RESTART.

        Verbatim CLI output captured from chaosblade 1.9.0-alpha when a prepare
        record exists but the application was never restarted with the hook on
        PYTHONPATH. This is a different remedy from a missing record, so the two
        must not collapse into one message.
        """
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_create

        async def _fake(cmd, target, **kw):
            return CommandResult(
                stdout='{"code":63064,"success":false,"error":"`http://127.0.0.1'
                ":9535/create?target=redis`: http cmd failed, err: Get "
                '\\"http://127.0.0.1:9535/create\\": dial tcp '
                '127.0.0.1:9535: connect: connection refused"}',
                stderr="",
                exit_code=1,
            )

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_create.ainvoke(
                {"target": "redis", "action": "delay", "flags": "--time 500"}
            )
        assert out.startswith("Error:")
        assert "RESTARTED" in out  # cannot be fixed mid-drill
        assert "PYTHONPATH" in out
        # Reaching this tool means the capability gate already accepted a
        # host-profile channel, so the guidance must point at the agent
        # precondition rather than at channel selection.
        assert "capability gate" in out

    @pytest.mark.asyncio
    async def test_multiple_running_records_surface_shadow_diagnosis(self):
        """When ≥2 prepare records are RUNNING, blade picks the OLDEST and a
        stale record SHADOWS the target's live agent. The connection-refused
        path must enumerate the records read-only and present the shadowing, so
        the model self-corrects in one step instead of guessing which record
        blade bound to (Case #3 run1 burned ~300 log lines on the generic
        single-record message before finding the two records on its own).
        """
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_create

        # Verbatim shape of `blade status --type prepare` with the Case #3 run1
        # residue: a live redis record (9527) shadowed by a stale HTTP-app
        # record (9526) whose app was killed but whose record stayed Running.
        prepare_status = (
            '{"code":200,"success":true,"result":['
            '{"Uid":"c12169b0f4502368","ProgramType":"python","Port":"9527",'
            '"Pid":"/opt/redis_app/redis_app.py","Status":"Running",'
            '"CreateTime":"2026-10-28T10:02:57Z"},'
            '{"Uid":"746279a5b215d86a","ProgramType":"python","Port":"9526",'
            '"Pid":"/opt/app/app.py","Status":"Running",'
            '"CreateTime":"2026-10-23T06:50:39Z"}]}'
        )
        calls: list[list[str]] = []

        async def _fake(cmd, target, **kw):
            calls.append(list(cmd))
            if "status" in cmd:
                return CommandResult(stdout=prepare_status, stderr="", exit_code=0)
            return CommandResult(
                stdout='{"code":63064,"success":false,"error":"`http://127.0.0.1'
                ":9526/create?target=redis`: http cmd failed, err: Get "
                '\\"http://127.0.0.1:9526/create\\": dial tcp '
                '127.0.0.1:9526: connect: connection refused"}',
                stderr="",
                exit_code=1,
            )

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_create.ainvoke(
                {"target": "redis", "action": "delay", "flags": "--time 500"}
            )

        # A read-only enumeration was issued on the failure path.
        assert any(
            "status" in c and "--type" in c and "prepare" in c for c in calls
        )
        assert out.startswith("Error:")
        # Both records named, with their script paths so the model can match
        # against its target, and the bound (refused) port pinned.
        assert "9527" in out and "9526" in out
        assert "c12169b0f4502368" in out and "746279a5b215d86a" in out
        assert "/opt/redis_app/redis_app.py" in out and "/opt/app/app.py" in out
        assert "blade bound here" in out
        # The documented selection rule + the revoke remedy for a stale shadow.
        assert "OLDEST" in out
        assert "SHADOW" in out
        assert "blade_python_revoke" in out
        # The other branch (target's own agent genuinely dead) stays available
        # so the model is not pushed to revoke when a restart is the real fix.
        assert "prerequisite failure" in out

    @pytest.mark.asyncio
    async def test_single_running_record_falls_back_to_restart_message(self):
        """With only the target's own record RUNNING there is nothing to
        disambiguate — the connection-refused cause IS a dead agent, so the
        original app-restart prerequisite message must be preserved verbatim
        (no shadow claim, no regression on the single-record path)."""
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_create

        prepare_status = (
            '{"code":200,"success":true,"result":['
            '{"Uid":"c12169b0f4502368","ProgramType":"python","Port":"9527",'
            '"Pid":"/opt/redis_app/redis_app.py","Status":"Running",'
            '"CreateTime":"2026-10-28T10:02:57Z"}]}'
        )

        async def _fake(cmd, target, **kw):
            if "status" in cmd:
                return CommandResult(stdout=prepare_status, stderr="", exit_code=0)
            return CommandResult(
                stdout='{"code":63064,"success":false,"error":"dial tcp '
                '127.0.0.1:9527: connect: connection refused"}',
                stderr="",
                exit_code=1,
            )

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_create.ainvoke(
                {"target": "redis", "action": "delay", "flags": "--time 500"}
            )

        assert out.startswith("Error:")
        assert "RESTARTED" in out
        assert "PYTHONPATH" in out
        assert "capability gate" in out
        # No shadow diagnosis on the single-record path.
        assert "SHADOW" not in out
        assert "OLDEST" not in out

    @pytest.mark.asyncio
    async def test_shadow_enumeration_failure_degrades_to_restart_message(self):
        """The diagnostic is best-effort: if the read-only enumeration raises or
        returns unparseable output, the error path must still return the generic
        precondition message rather than breaking — the enrichment may never
        become a new failure mode on the failure path."""
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_create

        async def _fake(cmd, target, **kw):
            if "status" in cmd:
                raise RuntimeError("transport blew up")
            return CommandResult(
                stdout='{"code":63064,"success":false,"error":"dial tcp '
                '127.0.0.1:9527: connect: connection refused"}',
                stderr="",
                exit_code=1,
            )

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_create.ainvoke(
                {"target": "redis", "action": "delay", "flags": "--time 500"}
            )

        assert out.startswith("Error:")
        assert "RESTARTED" in out
        assert "capability gate" in out

    def test_render_shadow_diagnosis_none_below_two_records(self):
        """The helper's None contract is what keeps the single-record fallback
        intact: fewer than two RUNNING records means nothing to disambiguate."""
        from chaos_agent.agent.providers.chaosblade.cli_python import (
            _render_shadow_diagnosis,
        )

        assert _render_shadow_diagnosis([], "9526", "raw") is None
        assert (
            _render_shadow_diagnosis(
                [{"uid": "u1", "port": "9527", "script": "/a.py", "createtime": ""}],
                "9527",
                "raw",
            )
            is None
        )
        # Two records → a diagnosis that pins the bound port and names both.
        out = _render_shadow_diagnosis(
            [
                {"uid": "u1", "port": "9527", "script": "/a.py", "createtime": ""},
                {"uid": "u2", "port": "9526", "script": "/b.py", "createtime": ""},
            ],
            "9526",
            "raw-output",
        )
        assert out is not None
        assert "blade bound here" in out
        assert "u1" in out and "u2" in out
        assert "raw-output" in out
        # No bound port extractable → still enumerates, with a generic clause.
        out2 = _render_shadow_diagnosis(
            [
                {"uid": "u1", "port": "9527", "script": "/a.py", "createtime": ""},
                {"uid": "u2", "port": "9526", "script": "/b.py", "createtime": ""},
            ],
            "",
            "raw",
        )
        assert out2 is not None and "blade bound here" not in out2

    def test_capability_gate_restricts_delivery_to_host_channels(self):
        """The framework already fails closed on a scope/transport mismatch.

        ``resolve_profile_for_state`` compares the fault scope's profile against
        the resolved transport's; a disagreement yields ``PROFILE_UNKNOWN`` and
        NO tools at all. This is what actually keeps a python-scope drill off a
        k8s-profile channel — asserted here because the tool docstring and the
        skill case both state it as the reason, and an earlier draft of both
        wrongly claimed nothing cross-validated the two.
        """
        from chaos_agent.agent.capabilities import (
            build_capability_context,
            filter_tools_for_context,
        )
        from chaos_agent.agent.factory import _append_provider_tools
        from chaos_agent.agent.providers import EXECUTE

        tools = _append_provider_tools([], EXECUTE)

        def _visible(mode: str, **extra) -> list[str]:
            state = {
                "fault_spec": {"scope": PYTHON_SCOPE},
                "kube_connection_mode": mode,
                **extra,
            }
            ctx = build_capability_context(state, "execute", tools)
            return [t.name for t in filter_tools_for_context(tools, ctx)]

        # Host-addressed channels: the injection tool is visible.
        assert "blade_python_create" in _visible("ssh", ssh_host="h")
        assert "blade_python_create" in _visible("kubewiz_host", host_name="h")
        # k8s-profile channels: fail closed, no tools whatsoever.
        assert _visible("kubeconfig") == []
        assert _visible("kubewiz_k8s", kubewiz_cluster_uuid="c1") == []

    def test_python_tools_hidden_from_k8s_drills(self):
        """Conversely, a k8s-scope drill must never see this backend's tools —
        the static ToolNode binds every provider's tools, so the per-invocation
        filter is what keeps the surfaces apart."""
        from chaos_agent.agent.capabilities import (
            build_capability_context,
            filter_tools_for_context,
            is_tool_name_allowed_for_context,
        )
        from chaos_agent.agent.factory import _append_provider_tools
        from chaos_agent.agent.providers import EXECUTE

        tools = _append_provider_tools([], EXECUTE)
        # The static binding really does contain both surfaces.
        assert {"blade_create", "blade_python_create"} <= {t.name for t in tools}

        state = {
            "fault_spec": {"scope": "pod", "namespace": "prod"},
            "kube_connection_mode": "kubeconfig",
        }
        ctx = build_capability_context(state, "execute", tools)
        visible = {t.name for t in filter_tools_for_context(tools, ctx)}
        assert "kubectl" in visible
        assert (
            not {"blade_python_create", "blade_python_prepare", "blade_python_revoke"}
            & visible
        )
        # And the screener-side fail-closed check agrees (restored checkpoints).
        assert not is_tool_name_allowed_for_context(
            "blade_python_create",
            state,
            "execute",
        )

    @pytest.mark.asyncio
    async def test_registered_but_failed_experiment_surfaces_uid_for_cleanup(self):
        """A failed create whose experiment WAS registered must name its uid.

        Project policy (``providers.chaosblade.verify``) deliberately refuses to extract a
        ``code=54000 success=false`` uid, so such an experiment never becomes
        ``state['blade_uid']`` — attributing a failed injection would mislead the
        verifier. The k8s path takes the same stance, so this backend must not
        diverge; what it MUST do is put the uid in the returned text, which is
        persisted as a ToolMessage, so the operator / recover LLM can still clean
        it up. Locking that here keeps the only cleanup handle from silently
        disappearing.
        """
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_create
        from chaos_agent.agent.providers.chaosblade.verify import extract_experiment_uid

        uid = "b" * 16
        raw = '{"code":54000,"success":false,"result":"%s"}' % uid

        async def _fake(cmd, target, **kw):
            return CommandResult(stdout=raw, stderr="", exit_code=1)

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_create.ainvoke(
                {"target": "redis", "action": "delay", "flags": "--time 500"}
            )

        assert uid in out  # cleanup handle preserved
        assert "blade_destroy" in out  # and the exact remedy named
        assert extract_experiment_uid(out) is None  # but NOT promoted to state


class TestPreconditionTools:
    """Execute the prepare / revoke bodies, which the rest of the suite only
    referenced by NAME. They are the operator's only handle on the agent
    precondition, so their command shape and failure surfacing are asserted."""

    async def _run(self, tool, args, *, exit_code=0):
        captured: dict = {}

        async def _fake(cmd, target, **kw):
            captured["argv"] = cmd
            captured["bypass"] = kw.get("bypass_channel")
            if exit_code:
                # Verbatim prepare failure from chaosblade 1.9.0-alpha: the port
                # must be FREE, because prepare's hook starts an agent there.
                return CommandResult(
                    stdout='{"code":47000,"success":false,"error":"invalid `port` '
                    "parameter value: `9526`. the port has been used by "
                    'other program"}',
                    stderr="",
                    exit_code=exit_code,
                )
            return CommandResult(
                stdout='{"code":200,"success":true,"result":"p1"}',
                stderr="",
                exit_code=0,
            )

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await tool.ainvoke(args)
        return out, captured

    @pytest.mark.asyncio
    async def test_prepare_always_sends_mandatory_target_script(self):
        """``--target-script`` is REQUIRED by the CLI, not optional.

        Verified: ``blade prepare python --port 9527 --python-path ...`` without
        it fails with ``required flag(s) "target-script" not set``. An earlier
        version of this test asserted the opposite (that the flag be omitted).
        """
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_prepare

        out, cap = await self._run(
            blade_python_prepare,
            {
                "port": 9527,
                "python_path": "/usr/bin/python3",
                "target_script": "/srv/app/main.py",
            },
        )
        assert cap["argv"][1:3] == ["prepare", "python"]
        assert cap["argv"][cap["argv"].index("--port") + 1] == "9527"
        assert "--python-path" in cap["argv"]
        assert (
            cap["argv"][cap["argv"].index("--target-script") + 1] == "/srv/app/main.py"
        )
        assert cap["bypass"] is False
        assert "p1" in out

    @pytest.mark.asyncio
    async def test_prepare_rejects_empty_target_script_before_executing(self):
        """Fail with an actionable message instead of letting the CLI reject it."""
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_prepare

        called = False

        async def _fake(cmd, target, **kw):  # pragma: no cover - must not run
            nonlocal called
            called = True
            return CommandResult(stdout="", stderr="", exit_code=0)

        with patch(
            "chaos_agent.agent.providers.chaosblade.cli_python.execute_via_transport",
            new=AsyncMock(side_effect=_fake),
        ):
            out = await blade_python_prepare.ainvoke({"target_script": "  "})
        assert out.startswith("Error:") and "target_script is required" in out
        assert called is False

    @pytest.mark.asyncio
    async def test_revoke_command_shape(self):
        from chaos_agent.agent.providers.chaosblade.cli_python import blade_python_revoke

        _out, cap = await self._run(blade_python_revoke, {"uid": "p1"})
        assert cap["argv"][1:] == ["revoke", "p1"]

    @pytest.mark.asyncio
    async def test_failures_are_surfaced_as_errors(self):
        from chaos_agent.agent.providers.chaosblade.cli_python import (
            blade_python_prepare,
            blade_python_revoke,
        )

        out, _ = await self._run(
            blade_python_prepare,
            {"port": 9526, "target_script": "/srv/app/main.py"},
            exit_code=1,
        )
        assert out.startswith("Error:") and "has been used by other program" in out
        out, _ = await self._run(blade_python_revoke, {"uid": "x"}, exit_code=1)
        assert out.startswith("Error:")


class TestTargetGuardClassification:
    def test_create_classified_as_python_scope(self):
        from chaos_agent.agent.target_guard.classifier import infer_effective_target
        from chaos_agent.agent.target_guard.types import ConfidenceLevel

        et = infer_effective_target(
            "blade_python_create",
            {"target": "redis", "action": "delay", "cmd": "GET"},
        )
        assert et.scope == PYTHON_SCOPE
        assert et.fault_target == "redis"
        assert et.confidence == ConfidenceLevel.HIGH

    def test_not_misresolved_to_pod_scope(self):
        """``BLADE_TARGET_TO_SCOPE`` maps redis→pod; going through
        ``_classify_blade_create`` would therefore make every in-process
        injection look like a cross-profile drift."""
        from chaos_agent.agent.providers.chaosblade.provider import BLADE_TARGET_TO_SCOPE
        from chaos_agent.agent.target_guard.classifier import infer_effective_target

        assert BLADE_TARGET_TO_SCOPE["redis"] == "pod"  # the trap being avoided
        et = infer_effective_target(
            "blade_python_create",
            {"target": "redis", "action": "delay"},
        )
        assert et.scope != "pod"

    def test_guard_allows_matching_target(self):
        from chaos_agent.agent.target_guard.classifier import infer_effective_target
        from chaos_agent.agent.target_guard.guard import target_drift_guard
        from chaos_agent.agent.target_guard.types import ApprovedTarget, GuardVerdict

        approved = ApprovedTarget(
            scope=PYTHON_SCOPE,
            namespace="",
            fault_target="redis",
            fault_action="delay",
            lock_fault_type=True,
        )
        et = infer_effective_target(
            "blade_python_create",
            {"target": "redis", "action": "delay"},
        )
        assert target_drift_guard(et, approved).verdict == GuardVerdict.ALLOW

    def test_guard_rejects_fault_type_drift(self):
        from chaos_agent.agent.target_guard.classifier import infer_effective_target
        from chaos_agent.agent.target_guard.guard import target_drift_guard
        from chaos_agent.agent.target_guard.types import ApprovedTarget, GuardVerdict

        approved = ApprovedTarget(
            scope=PYTHON_SCOPE,
            namespace="",
            fault_target="redis",
            fault_action="delay",
            lock_fault_type=True,
        )
        et = infer_effective_target(
            "blade_python_create",
            {"target": "mysql", "action": "delay"},
        )
        assert target_drift_guard(et, approved).verdict == GuardVerdict.REJECT_DRIFT

    @pytest.mark.parametrize("tool", ["blade_python_prepare", "blade_python_revoke"])
    def test_precondition_tools_are_readonly(self, tool):
        """Neither touches a fault target; an unclassified tool name would be
        default-denied as REJECT_UNKNOWN."""
        from chaos_agent.agent.target_guard.classifier import (
            SCOPE_READONLY,
            infer_effective_target,
        )

        assert infer_effective_target(tool, {"port": 9526}).scope == SCOPE_READONLY


class TestReActPathWiring:
    """The ReAct (LLM) path must attribute AND capture the uid for this carrier.

    These two seams are the only thing standing between an LLM-issued
    in-process injection and a recoverable task: without them ``blade_uid``
    stays empty and both verification Layer 1 and recovery have no uid to
    act on.
    """

    def test_issue_time_method_classification(self):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            classify_issue_time_method,
        )

        assert (
            classify_issue_time_method(
                "blade_python_create",
                {"target": "redis", "action": "delay"},
                is_host=True,
            )
            == "python_agent"
        )

    def test_uid_extracted_from_python_tool_message(self):
        # Phase-4 T2 canonical address (kept under the historical call name).
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uid_from_messages as _extract_blade_uid_from_messages,
        )

        uid = "e" * 16
        assert _extract_blade_uid_from_messages(_python_inject_msgs(uid)) == uid

    def test_destroyed_uid_not_returned(self):
        # Phase-4 T2 canonical address (kept under the historical call name).
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uid_from_messages as _extract_blade_uid_from_messages,
        )

        uid = "f" * 16
        assert (
            _extract_blade_uid_from_messages(_python_inject_msgs(uid, destroyed=True))
            != uid
        )


class TestCatalogExampleCommands:
    """The skill catalogue's example commands are LLM-facing guidance.

    ``infer_scope`` defaults unknown catalogue prefixes to ``pod``, so a
    ``Python_*`` category would otherwise be advertised with a namespace and a
    kubeconfig — neither of which an in-process fault has any use for.
    """

    def test_python_category_infers_python_scope(self):
        from chaos_agent.skills.catalog_generator import infer_scope

        assert infer_scope("Python_Redis延迟") == PYTHON_SCOPE
        assert infer_scope("Python_MySQL异常") == PYTHON_SCOPE

    def test_python_example_cmd_omits_namespace_and_kubeconfig(self):
        from chaos_agent.skills.catalog_generator import build_nl_cmd

        cmd = build_nl_cmd("缓存变慢", "Python_Redis延迟", PYTHON_SCOPE)
        assert "namespace" not in cmd
        assert "kubeconfig" not in cmd
        assert "app-host" in cmd

    def test_other_scopes_unchanged(self):
        from chaos_agent.skills.catalog_generator import build_nl_cmd, infer_scope

        assert infer_scope("Pod_网络丢包") == "pod"
        assert infer_scope("Node_磁盘IO异常") == "node"
        pod_cmd = build_nl_cmd("x", "Pod_网络丢包", "pod")
        assert "命名空间为<namespace>" in pod_cmd
        assert "kubeconfig" in pod_cmd
        assert "<node-name>" in build_nl_cmd("x", "Node_磁盘IO异常", "node")


class TestVerifyAndRecoverBodies:
    """Execute the provider's Layer-1 / recovery bodies, not just their presence.

    These two methods are only reached during a real drill's verification and
    recovery phases, and they delegate to shared helpers by keyword. A wrong
    argument name here would surface for the first time AFTER a fault is live —
    exactly when it must not. So the delegation and the RecoverResult assembly
    are asserted with the helpers stubbed.
    """

    @pytest.mark.asyncio
    async def test_layer1_delegates_to_host_blade_status(self):
        from chaos_agent.agent.result.verdict import Layer1Result

        with patch(
            "chaos_agent.agent.providers.chaosblade.verify._run_host_blade_layer1",
            new=AsyncMock(return_value=Layer1Result(status="passed", details="ok")),
        ) as helper:
            result = await ChaosbladePythonProvider().layer1_verify(
                {"messages": ["m"]},
                experiment_uid="uid-1",
                kubeconfig="kc",
                task_id="t",
            )

        assert result.status.value == "passed"
        # uid + kubeconfig positional, task_id/messages by keyword.
        assert helper.await_args.args == ("uid-1", "kc")
        assert helper.await_args.kwargs["task_id"] == "t"
        assert helper.await_args.kwargs["messages"] == ["m"]

    @staticmethod
    def _layer1_stub(passed: bool):
        class _R:
            status = "passed" if passed else "failed"
            details = "destroyed" if passed else "destroy failed"
            raw_output = ""

            def is_passed(self):
                return passed

            def model_dump(self, mode="python"):
                # Signature aligns with the real pydantic Layer1Result: the
                # storage-shape helper calls model_dump(mode="json") since
                # phase-5 (str-Enum status rendered as plain string).
                return {"status": self.status}

        return _R()

    @pytest.mark.asyncio
    async def test_recover_success_maps_to_recovered(self):
        from chaos_agent.agent.providers.chaosblade import recover as cr

        with patch.object(
            cr,
            "run_layer1_destroy",
            new=AsyncMock(return_value=self._layer1_stub(True)),
        ) as helper:
            res = await ChaosbladePythonProvider().recover(
                {},
                {"kind": "blade_uid", "value": "uid-1"},
                kubeconfig="",
                messages=[],
            )

        assert res.recovered is True
        assert res.level == "recovered"
        assert res.experiment_uid == "uid-1"
        assert res.failure is None
        # Layer 2 is skipped without an LLM, and that must be stated.
        assert res.layer2["status"] == "skipped"
        assert res.warnings
        assert helper.await_args.args == ("uid-1", "")

    @pytest.mark.asyncio
    async def test_recover_failure_maps_to_failure_category(self):
        from chaos_agent.agent.providers.chaosblade import recover as cr
        from chaos_agent.agent.result.verdict import FailureCategory

        with patch.object(
            cr,
            "run_layer1_destroy",
            new=AsyncMock(return_value=self._layer1_stub(False)),
        ):
            res = await ChaosbladePythonProvider().recover(
                {},
                {"kind": "blade_uid", "value": "uid-1"},
                kubeconfig="",
                messages=[],
            )

        assert res.recovered is False
        assert res.level == "unrecovered"
        assert res.failure[0] == FailureCategory.RECOVERY_FAILED
        assert not res.warnings  # no "recovered" reassurance on a failure

    def test_recover_layer2_context_is_application_oriented(self):
        layer1 = self._layer1_stub(True)
        ctx, instruction = ChaosbladePythonProvider().recover_layer2_context(
            {},
            layer1,
            is_deterministic=True,
            experiment_uid="uid-1",
            is_host_scope=True,
        )
        assert "uid-1" in ctx
        # Layer 2 must verify the application recovered, not poke the cluster.
        assert "kubectl" not in instruction
        assert "RECOVERY_VERIFICATION_RESULT" in instruction


class TestStateAndSkills:
    def test_no_dead_prepare_uid_state_field(self):
        """``blade prepare python`` is host-level setup with NO in-agent consumer.

        We deliberately do not persist its uid: recovery destroys the EXPERIMENT
        uid and never revokes the shared host registration, so a ``prepare_uid``
        state field would be written nowhere and only pollute task JSON /
        checkpoints. If a future feature needs it, add the field together with
        its writer and reader.
        """
        from chaos_agent.agent.state import AgentState
        from chaos_agent.agent.state_mgmt.state_lifecycle import STATE_FIELD_POLICIES

        assert "python_prepare_uid" not in AgentState.__annotations__
        assert "python_prepare_uid" not in STATE_FIELD_POLICIES

    def test_no_scope_blind_python_preflight_check(self):
        """No agent-port preflight check may be registered globally.

        Preflight carries no fault scope, so such a check would warn every k8s /
        host drill; and a local probe would inspect the wrong machine (the agent
        lives on the target application's host). The precondition is surfaced by
        ``blade_python_create`` instead.
        """
        from chaos_agent import preflight

        assert not hasattr(preflight, "check_python_agent")
        names = {c.__name__ for c in preflight.INJECT_CHECKS}
        assert not any("python" in n for n in names)

    def test_action_description_covers_python_verbs(self):
        """The SDK/LLM-facing action description must enumerate this carrier's
        verbs, otherwise ``throwCustomException`` / ``returnValue`` are
        undiscoverable even though the scope enum accepts ``python``."""
        from chaos_agent.agent.spec.fault_spec import INTENT_ACTION_DESCRIPTION
        from chaos_agent.l4.schemas import _FAULT_INTENT_SCHEMA

        assert "throwCustomException" in INTENT_ACTION_DESCRIPTION
        assert "returnValue" in INTENT_ACTION_DESCRIPTION
        action_desc = _FAULT_INTENT_SCHEMA["properties"]["action"]["description"]
        assert "throwCustomException" in action_desc
        assert PYTHON_SCOPE in _FAULT_INTENT_SCHEMA["properties"]["scope"]["enum"]

    # (target, action) -> catalogue directory that MUST be the top hit.
    # Every entry was confirmed against the real CLI's flag surface before the
    # case was written, so this table doubles as the coverage inventory.
    _EXPECTED_TOP_HIT = {
        ("redis", "delay"): "Python_Redis延迟",
        ("redis", "throwCustomException"): "Python_Redis异常",
        ("redis", "returnValue"): "Python_Redis返回值篡改",
        ("mysql", "delay"): "Python_MySQL延迟",
        ("mysql", "throwCustomException"): "Python_MySQL异常",
        ("http", "delay"): "Python_HTTP延迟",
        ("http", "throwCustomException"): "Python_HTTP异常",
        ("grpc", "delay"): "Python_gRPC延迟",
        ("kafka", "throwCustomException"): "Python_Kafka异常",
    }

    def _registry(self):
        from pathlib import Path

        from chaos_agent.skills.registry import SkillRegistry

        skills_dir = Path(__file__).resolve().parents[2] / "skills"
        if not (skills_dir / "python-app-chaos-skills").exists():
            pytest.skip("python skill pack not present in this checkout")
        registry = SkillRegistry()
        registry.load_from_directory(skills_dir)
        return registry

    def test_catalogue_lookup_hits_python_use_cases(self):
        """Each covered (target, action) must resolve to ITS OWN case.

        Retrieval narrows by target keyword and then by action keyword, so a
        directory named for one action must not out-rank the directory named for
        the requested action. Asserting the top hit (not merely "non-empty")
        catches a naming collision such as ``Python_Redis返回值篡改`` shadowing
        ``Python_Redis延迟``.
        """
        from pathlib import Path

        registry = self._registry()
        for (target, action), expected_dir in self._EXPECTED_TOP_HIT.items():
            matches = registry.match_use_cases(PYTHON_SCOPE, target, action)
            assert matches, f"no case found for {target}/{action}"
            top = Path(matches[0]).parent.name
            assert top == expected_dir, (
                f"{target}/{action} resolved to {top}, expected {expected_dir}"
            )

    def test_every_catalogue_case_is_reachable_by_retrieval(self):
        """No case may be dead weight: every directory must appear in the
        expected-top-hit table, which the test above proves is reachable. A case
        retrieval never surfaces first is invisible to the LLM no matter how
        correct its content is."""
        from pathlib import Path

        catalogue = (
            Path(__file__).resolve().parents[2]
            / "skills"
            / "python-app-chaos-skills"
            / "references"
            / "catalogue"
        )
        if not catalogue.exists():
            pytest.skip("python skill pack not present in this checkout")
        on_disk = {d.name for d in catalogue.iterdir() if d.is_dir()}
        assert on_disk == set(self._EXPECTED_TOP_HIT.values()), (
            "catalogue directories and the expected-top-hit table disagree; "
            "update the table when adding or renaming a case"
        )

    def test_every_documented_command_is_executable(self):
        """Every shell command in this skill pack must pass the ToolGuard.

        The skill case is the verifier's PRIMARY AUTHORITY, so a command the
        guard will reject is worse than no guidance: the agent burns loop budget
        on rejections. The original draft told the agent to probe the in-process
        agent with ``curl http://127.0.0.1:9526/health`` — ``curl`` is in NO
        provider's ``injection_binaries``, so Gate ① blocks it every time. Agent
        liveness is therefore framed as an operator-side precondition, and
        experiment state is read with ``blade status`` instead.
        """
        import re
        from pathlib import Path

        from chaos_agent.tools.guard import ToolGuard

        skill_dir = (
            Path(__file__).resolve().parents[2] / "skills" / "python-app-chaos-skills"
        )
        if not skill_dir.exists():
            pytest.skip("python skill pack not present in this checkout")

        guard = ToolGuard()
        # Lines starting with these are documentation of operator-side setup or
        # of the agent's own CLI, not commands the drill agent executes.
        _OPERATOR_SIDE = {
            "blade-ai",
            "python",
            "pip",
            "export",
            "chaosblade-exec-python",
        }
        offenders: list[tuple[str, str]] = []
        for md in skill_dir.rglob("*.md"):
            for line in md.read_text(encoding="utf-8").splitlines():
                stripped = line.strip().strip("`")
                match = re.match(r"^([a-z][a-z0-9_.-]*)\s", stripped)
                if not match or match.group(1) in _OPERATOR_SIDE:
                    continue
                allowed, _reason = guard.check(stripped.split())
                if not allowed:
                    offenders.append((md.name, stripped[:80]))

        assert not offenders, (
            f"skill pack documents commands the ToolGuard will reject: {offenders}"
        )
