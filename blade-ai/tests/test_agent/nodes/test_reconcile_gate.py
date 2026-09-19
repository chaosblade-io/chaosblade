"""Tests for the create-reconcile gate's three-state scan (_reconcile_gate).

blade-create-reconcile-before-retry D6, scan half: the most recent
blade_create ToolMessage is classified as uncertain / gate-blocked /
fabricated-never-executed / plain, driving the create_reconcile flag
(register / keep / clear). The interception half (gate judgment in
_process_response_tool_calls) gets its own suite with task 4.5.

Carrier-vocabulary note (the D6 provider-seam cut): the generic state
machine consults the judgment material through the registry, so these
tests exercise the blade_create scenario END-TO-END through that seam —
the fingerprint helper imports the carrier-side constructor directly
(tests are not under the generic-layer import guard) and the probe tests
monkeypatch the carrier-side conflict query, proving the seam wiring
rather than bypassing it.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute._reconcile_gate import (
    NO_CHANGE,
    RECONCILE_BLOCK_LIMIT,
    apply_reconcile_gate,
    fingerprint_to_state_dict,
    scan_create_reconcile,
)
from chaos_agent.agent.providers.chaosblade.reconcile import (
    fingerprint_from_tool_call_args,
)
from chaos_agent.tools.markers import (
    GATE_RECONCILE_BLOCKED_MARKER,
    UNCERTAIN_OUTCOME_MARKER,
)


# ------------------------------------------------------------------ helpers

_RETRY_ARGS = {
    "scope": "pod",
    "target": "network",
    "action": "delay",
    "namespace": "cms-demo",
    "names": ["accounting-7dc7b44956-krtm6"],
    "labels": {"app": "accounting"},
}


def _issued_create(call_id="call_1", args=None):
    """AIMessage issuing a blade_create with the given tool_call id/args."""
    return AIMessage(content="", tool_calls=[
        {"name": "blade_create", "args": dict(args or _RETRY_ARGS), "id": call_id},
    ])


def _blade_return(call_id="call_1", content="", status=None):
    """ToolMessage answering a blade_create call."""
    kwargs = {}
    if status is not None:
        kwargs["status"] = status
    return ToolMessage(
        content=content, name="blade_create", tool_call_id=call_id, **kwargs
    )


def _uncertain_content():
    return (
        "Error: blade create transport failure: read timeout\n"
        f"{UNCERTAIN_OUTCOME_MARKER} Outcome UNKNOWN — reconcile first "
        "(check for an active experiment before retrying)."
    )


def _registered_flag(args=None, blocked=0, reconciled=False, call_id="call_1"):
    """A create_reconcile flag as it would sit in state."""
    fp = fingerprint_from_tool_call_args(args or _RETRY_ARGS)
    return {
        "fingerprint": fingerprint_to_state_dict(fp),
        "uncertain_call_id": call_id,
        "blocked_count": blocked,
        "gate_reconciled": reconciled,
    }


# ------------------------------------------------- scan: register / keep / clear

class TestScanThreeStates:
    """One test per state of the scan, plus the identity guards."""

    def test_uncertain_registers_flag(self):
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
        ]
        out = scan_create_reconcile(messages, None)
        assert isinstance(out, dict)
        assert out["uncertain_call_id"] == "call_1"
        assert out["blocked_count"] == 0
        assert out["gate_reconciled"] is False
        assert out["fingerprint"] == fingerprint_to_state_dict(
            fingerprint_from_tool_call_args(_RETRY_ARGS)
        )

    def test_gate_marker_keeps_flag(self):
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
            _issued_create("call_2"),
            _blade_return("call_2", (
                f"{GATE_RECONCILE_BLOCKED_MARKER} blocked — reconcile "
                "before retrying this create."
            )),
        ]
        current = _registered_flag(blocked=1)
        assert scan_create_reconcile(messages, current) is NO_CHANGE

    def test_matching_execution_clears(self):
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
            _issued_create("call_2"),
            _blade_return("call_2", '{"code": 200, "success": true, "result": "abc"}'),
        ]
        current = _registered_flag()
        assert scan_create_reconcile(messages, current) is None

    def test_terminal_failure_execution_also_clears(self):
        # A deterministic failure (no marker) for the SAME fingerprint is a
        # resolved outcome — the cycle closes regardless of success.
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
            _issued_create("call_2"),
            _blade_return("call_2", "Error: blade create failed (exit 1): bad flag"),
        ]
        current = _registered_flag()
        assert scan_create_reconcile(messages, current) is None

    def test_mismatched_execution_keeps_flag(self):
        other = dict(_RETRY_ARGS, namespace="other-ns")
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
            _issued_create("call_2", other),
            _blade_return("call_2", '{"code": 200, "success": true}'),
        ]
        current = _registered_flag()
        assert scan_create_reconcile(messages, current) is NO_CHANGE

    def test_re_uncertain_restarts_cycle(self):
        # Flag mid-cycle (blocked twice, proxy query done) meets ANOTHER
        # uncertain outcome for the same request → fresh cycle.
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
            _issued_create("call_2"),
            _blade_return("call_2", _uncertain_content()),
        ]
        current = _registered_flag(blocked=2, reconciled=True)
        out = scan_create_reconcile(messages, current)
        assert isinstance(out, dict)
        assert out["blocked_count"] == 0
        assert out["gate_reconciled"] is False
        assert out["uncertain_call_id"] == "call_2"

    def test_no_blade_create_no_change(self):
        messages = [AIMessage(content="planning prose")]
        assert scan_create_reconcile(messages, _registered_flag()) is NO_CHANGE
        assert scan_create_reconcile([], None) is NO_CHANGE

    def test_uncertain_without_issuing_ai_no_change(self):
        # Compaction severed the issuing AIMessage — neither register
        # (fingerprint unrecoverable) nor clear (protection live) is
        # defensible, so the scan stands still.
        messages = [_blade_return("call_9", _uncertain_content())]
        assert scan_create_reconcile(messages, None) is NO_CHANGE

    def test_plain_execution_without_flag_no_change(self):
        # No flag registered: a plain execution resolves nothing.
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", '{"code": 200, "success": true}'),
        ]
        assert scan_create_reconcile(messages, None) is NO_CHANGE


class TestScanFabricatedAnswers:
    """Never-executed answers from our own guards must not clear the flag."""

    def test_truncated_answer_keeps_flag(self):
        truncated = (
            "Error: tool call `blade_create` was NOT executed — the "
            "response hit the output token limit, so its arguments may "
            "be truncated and incomplete. Nothing ran and no state "
            "changed."
        )
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
            _issued_create("call_2"),
            _blade_return("call_2", truncated),
        ]
        current = _registered_flag()
        assert scan_create_reconcile(messages, current) is NO_CHANGE

    def test_screener_rejection_keeps_flag(self):
        messages = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
            _issued_create("call_2"),
            _blade_return("call_2", "rejected: phase violation", status="error"),
        ]
        current = _registered_flag()
        assert scan_create_reconcile(messages, current) is NO_CHANGE


# ------------------------------------------------------- fingerprint identity
# The fingerprint construction itself (argument-shape normalisation,
# order-insensitive matching, unified key face) is carrier judgment
# material — tested in
# tests/test_agent/test_providers/test_chaosblade_reconcile.py.


def test_markers_wording_distinct():
    # The scan's discrimination key: the two markers (both on neutral
    # ground, tools/markers.py) must never substring-contain each other.
    assert GATE_RECONCILE_BLOCKED_MARKER not in UNCERTAIN_OUTCOME_MARKER
    assert UNCERTAIN_OUTCOME_MARKER not in GATE_RECONCILE_BLOCKED_MARKER


# ------------------------------------------------ execute_loop wiring / ordering

class TestScanWiringInExecuteLoop:
    """The scan runs at iteration top and publishes the flag into the SAME
    iteration's result — the merged view the interception gate reads when
    judging a response that carries the blind retry (spec: 置位先于门禁判定)."""

    class _FakeStore:
        def __init__(self):
            self.appended = []

        def append_messages(self, task_id, messages, node_name=""):
            self.appended.extend(messages)

    class _FakeHook:
        def __init__(self):
            self.session_store = TestScanWiringInExecuteLoop._FakeStore()

        async def __call__(self, state):
            return {}

    class _FakeLLM:
        def __init__(self):
            self.seen = None

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.seen = messages
            # Blind retry of the SAME request (same fingerprint) — the
            # exact scenario the gate exists to intercept.
            return AIMessage(content="", tool_calls=[
                {"name": "blade_create", "args": dict(_RETRY_ARGS), "id": "call_2"},
            ])

    def _node(self):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop
        return make_execute_loop(
            hook=self._FakeHook(), llm=self._FakeLLM(),
            tools=[], env_info={"context": "test"},
        )

    @pytest.mark.asyncio
    async def test_scan_publishes_flag_same_iteration(self, sample_agent_state):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = [
            _issued_create("call_1"),
            _blade_return("call_1", _uncertain_content()),
        ]

        result = await node(state)

        # The uncertain return was classified at iteration top and the
        # flag landed in THIS iteration's result — and since the response
        # carries a blind same-fingerprint retry, the gate (judging AFTER
        # the scan, same iteration) already held it: blocked_count 0 → 1.
        # The increment is itself the timing proof: had the flag arrived
        # one iteration late, this retry would have run instead.
        flag = result.get("create_reconcile")
        assert isinstance(flag, dict)
        assert flag["blocked_count"] == 1  # registered at 0, then held
        assert flag["gate_reconciled"] is False
        assert flag["uncertain_call_id"] == "call_1"
        assert flag["fingerprint"] == fingerprint_to_state_dict(
            fingerprint_from_tool_call_args(_RETRY_ARGS)
        )
        assert result["_reconcile_gate_blocked"] is True

    @pytest.mark.asyncio
    async def test_no_blade_create_scenario_writes_nothing(self, sample_agent_state):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = [
            AIMessage(content="inspect", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": "get",
                                             "v_args": "pods"}, "id": "k1"},
            ]),
            ToolMessage(content="pod list", name="kubectl", tool_call_id="k1"),
        ]

        result = await node(state)
        # Normal path zero-change: no uncertain outcome → no flag in state.
        assert "create_reconcile" not in result


# ---------------------------------------------------------- interception half

class _FakeTracker:
    def __init__(self):
        self.updates = []

    def update(self, msg, meta=None, **kwargs):
        self.updates.append((msg, meta))


def _retry_response(call_id="call_2", args=None):
    """AIMessage freshly issuing the (blind) same-fingerprint retry."""
    return AIMessage(content="", tool_calls=[
        {"name": "blade_create", "args": dict(args or _RETRY_ARGS), "id": call_id},
    ])


def _uncertain_history():
    """Message history whose latest blade_create return is uncertain."""
    return [
        _issued_create("call_1"),
        _blade_return("call_1", _uncertain_content()),
    ]


class TestApplyReconcileGate:
    """Direct tests of the gate judgment: hold / release / cap."""

    @pytest.mark.asyncio
    async def test_blind_retry_held(self):
        out = await apply_reconcile_gate(
            _retry_response(), _uncertain_history(), _registered_flag(),
        )
        assert out is not None
        answers, new_flag = out
        assert len(answers) == 1
        msg = answers[0]
        assert msg.name == "blade_create"
        assert msg.tool_call_id == "call_2"
        assert msg.status == "error"
        assert GATE_RECONCILE_BLOCKED_MARKER in msg.content
        # GuardFeedback semantics: reason + fix + not-a-ban + fingerprint.
        assert "UNKNOWN outcome" in msg.content
        assert "DUPLICATE" in msg.content
        assert "namespace=cms-demo" in msg.content
        assert "is_hard_floor=False" in msg.content
        assert "blade_status" in msg.content
        assert new_flag["blocked_count"] == 1
        assert new_flag["gate_reconciled"] is False

    @pytest.mark.asyncio
    async def test_whole_batch_held_together(self):
        response = AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "args": {"resource": "pods"}, "id": "k1"},
            {"name": "blade_create", "args": dict(_RETRY_ARGS), "id": "call_2"},
        ])
        out = await apply_reconcile_gate(
            response, _uncertain_history(), _registered_flag(),
        )
        answers, _ = out
        assert len(answers) == 2
        by_id = {m.tool_call_id: m for m in answers}
        # The matching create gets the gate feedback...
        assert GATE_RECONCILE_BLOCKED_MARKER in by_id["call_2"].content
        # ...everything else in the batch gets the held-back notice.
        assert "NOT executed" in by_id["k1"].content
        assert GATE_RECONCILE_BLOCKED_MARKER not in by_id["k1"].content

    @pytest.mark.asyncio
    async def test_released_after_whitelist_read(self):
        messages = _uncertain_history() + [
            AIMessage(content="", tool_calls=[
                {"name": "blade_status", "args": {"uid": ""}, "id": "s1"},
            ]),
            ToolMessage(content='{"code":200}', name="blade_status",
                        tool_call_id="s1"),
        ]
        out = await apply_reconcile_gate(
            _retry_response(), messages, _registered_flag(),
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_whitelist_read_before_uncertain_not_counted(self):
        messages = [
            AIMessage(content="", tool_calls=[
                {"name": "blade_status", "args": {}, "id": "s0"},
            ]),
            ToolMessage(content='{"code":200}', name="blade_status",
                        tool_call_id="s0"),
        ] + _uncertain_history()
        out = await apply_reconcile_gate(
            _retry_response(), messages, _registered_flag(),
        )
        assert out is not None  # the read predates the unknown outcome

    @pytest.mark.asyncio
    async def test_released_after_gate_reconciled(self):
        out = await apply_reconcile_gate(
            _retry_response(), _uncertain_history(),
            _registered_flag(reconciled=True),
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_mismatched_fingerprint_released(self):
        other = dict(_RETRY_ARGS, namespace="other-ns")
        out = await apply_reconcile_gate(
            _retry_response(args=other), _uncertain_history(),
            _registered_flag(),
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_no_flag_released(self):
        out = await apply_reconcile_gate(
            _retry_response(), _uncertain_history(), None,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_no_create_call_released(self):
        response = AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {"subcommand": "get",
                                         "v_args": "pods"}, "id": "k1"},
        ])
        out = await apply_reconcile_gate(
            response, _uncertain_history(), _registered_flag(),
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_cap_exceeded_released_with_warning(self):
        tracker = _FakeTracker()
        out = await apply_reconcile_gate(
            _retry_response(), _uncertain_history(),
            _registered_flag(blocked=RECONCILE_BLOCK_LIMIT),
            tracker=tracker,
        )
        assert out is None
        assert tracker.updates, "cap-exceeded release must record a warning"
        assert "block limit" in tracker.updates[0][0]

    @pytest.mark.asyncio
    async def test_below_cap_still_holds(self):
        out = await apply_reconcile_gate(
            _retry_response(), _uncertain_history(),
            _registered_flag(blocked=RECONCILE_BLOCK_LIMIT - 1),
        )
        assert out is not None
        _, new_flag = out
        assert new_flag["blocked_count"] == RECONCILE_BLOCK_LIMIT

    @pytest.mark.asyncio
    async def test_locator_lost_whitelist_scans_whole_history(self):
        # Compaction severed the uncertain return: the whitelist read
        # anywhere in history counts (wide-open, anti-deadlock).
        messages = _uncertain_history() + [
            AIMessage(content="", tool_calls=[
                {"name": "kubectl_read", "args": {"resource": "pods"},
                 "id": "r1"},
            ]),
            ToolMessage(content="pod list", name="kubectl_read",
                        tool_call_id="r1"),
        ]
        flag = _registered_flag(call_id="vanished_id")
        out = await apply_reconcile_gate(_retry_response(), messages, flag)
        assert out is None


class TestGateWiringInExecuteLoop:
    """End-to-end interception and release through make_execute_loop."""

    class _FakeStore:
        def __init__(self):
            self.appended = []

        def append_messages(self, task_id, messages, node_name=""):
            self.appended.extend(messages)

    class _FakeHook:
        def __init__(self):
            self.session_store = TestGateWiringInExecuteLoop._FakeStore()

        async def __call__(self, state):
            return {}

    class _BlindRetryLLM:
        def __init__(self):
            self.seen = None

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.seen = messages
            return _retry_response()

    def _node(self):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop
        return make_execute_loop(
            hook=self._FakeHook(), llm=self._BlindRetryLLM(),
            tools=[], env_info={"context": "test"},
        )

    @pytest.mark.asyncio
    async def test_blind_retry_intercepted_end_to_end(self, sample_agent_state):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = _uncertain_history()

        result = await node(state)

        # The batch was held: routing flag set, blocked_count incremented
        # (scan registered it this same iteration, the gate then held the
        # retry on top), fabricated answer published with the gate marker.
        assert result["_reconcile_gate_blocked"] is True
        assert result["truncated_tool_calls"] is False
        assert result["create_reconcile"]["blocked_count"] == 1
        tail = result["messages"][-1]
        assert tail.name == "blade_create"
        assert GATE_RECONCILE_BLOCKED_MARKER in tail.content
        # Ordering: the issuing AIMessage precedes its fabricated answers
        # (hints + [response] + answers), keeping the conversation
        # well-formed for the next LLM turn.
        assert isinstance(result["messages"][-2], AIMessage)

    @pytest.mark.asyncio
    async def test_released_after_whitelist_end_to_end(self, sample_agent_state):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = _uncertain_history() + [
            AIMessage(content="", tool_calls=[
                {"name": "blade_status", "args": {"uid": ""}, "id": "s1"},
            ]),
            ToolMessage(content='{"code":200}', name="blade_status",
                        tool_call_id="s1"),
        ]

        result = await node(state)

        # Released: no hold flag, the response itself is the last message
        # (normal path — nothing fabricated after it), and the flag stays
        # registered (only a real execution clears it, via the scan).
        assert result.get("_reconcile_gate_blocked") is False
        assert isinstance(result["messages"][-1], AIMessage)
        assert result["create_reconcile"]["blocked_count"] == 0


# ------------------------------------------------- interception-time probing

from chaos_agent.agent.nodes.side_effect._conflict_check import ConflictInfo  # noqa: E402

_HOST_ARGS = {
    "scope": "host", "target": "cpu", "action": "fullload",
    "namespace": "", "names": ["node-1"], "labels": "",
}


class TestInterceptionProbe:
    """The intercept path probes the cluster with the registered
    fingerprint (same query as safety_check) and splices the outcome into
    the fabricated feedback; only a COMPLETED probe counts as
    reconciliation (gate_reconciled)."""

    def _gate(self, flag=None, args=None, kubeconfig="/kc", **kw):
        return apply_reconcile_gate(
            _retry_response(args=args), _uncertain_history(),
            flag or _registered_flag(args=args),
            kubeconfig=kubeconfig, **kw,
        )

    @pytest.mark.asyncio
    async def test_probe_hit_carries_uid_and_reuse_options(self, monkeypatch):
        async def fake_check(kubeconfig, task_id, **kwargs):
            return ["exp-123"], [ConflictInfo(
                uid="exp-123", same_action_as_request=True,
                overlaps_target=True,
            )]

        monkeypatch.setattr(
            "chaos_agent.agent.providers.chaosblade.reconcile"
            ".check_blade_conflicts", fake_check,
        )
        out = await self._gate()
        answers, new_flag = out
        content = answers[0].content
        assert "exp-123" in content
        assert "REUSE" in content          # option a: reuse the UID
        assert "blade_destroy" in content   # option b: cleanup then retry
        # Completed probe counts as reconciliation → next retry released.
        assert new_flag["gate_reconciled"] is True

    @pytest.mark.asyncio
    async def test_probe_miss_confirms_absence(self, monkeypatch):
        async def fake_check(kubeconfig, task_id, **kwargs):
            return [], []

        monkeypatch.setattr(
            "chaos_agent.agent.providers.chaosblade.reconcile"
            ".check_blade_conflicts", fake_check,
        )
        out = await self._gate()
        answers, new_flag = out
        assert "NO active experiment" in answers[0].content
        assert new_flag["gate_reconciled"] is True

    @pytest.mark.asyncio
    async def test_probe_failure_degrades_without_reconciled(self, monkeypatch):
        async def fake_check(kubeconfig, task_id, **kwargs):
            raise TimeoutError("probe timed out")

        monkeypatch.setattr(
            "chaos_agent.agent.providers.chaosblade.reconcile"
            ".check_blade_conflicts", fake_check,
        )
        out = await self._gate()
        answers, new_flag = out
        assert "FAILED" in answers[0].content
        assert "blade_status" in answers[0].content  # manual guidance
        assert new_flag["gate_reconciled"] is False

    @pytest.mark.asyncio
    async def test_host_scope_skips_probe_entirely(self, monkeypatch):
        calls = []

        async def fake_check(kubeconfig, task_id, **kwargs):
            calls.append(kwargs)
            return [], []

        monkeypatch.setattr(
            "chaos_agent.agent.providers.chaosblade.reconcile"
            ".check_blade_conflicts", fake_check,
        )
        out = await self._gate(
            flag=_registered_flag(args=_HOST_ARGS), args=_HOST_ARGS,
        )
        answers, new_flag = out
        # CRD query never runs — its miss would be an invalid conclusion.
        assert calls == []
        assert "HOST" in answers[0].content
        assert "host" in answers[0].content.lower()
        assert new_flag["gate_reconciled"] is False

    @pytest.mark.asyncio
    async def test_unreachable_cluster_skips_probe(self, monkeypatch):
        calls = []

        async def fake_check(kubeconfig, task_id, **kwargs):
            calls.append(kwargs)
            return [], []

        monkeypatch.setattr(
            "chaos_agent.agent.providers.chaosblade.reconcile"
            ".check_blade_conflicts", fake_check,
        )
        monkeypatch.setattr(
            "chaos_agent.transports.is_kubewiz_channel", lambda: False,
        )
        out = await self._gate(kubeconfig="")  # no kubeconfig, no gateway
        answers, new_flag = out
        assert calls == []
        assert "FAILED" in answers[0].content
        assert new_flag["gate_reconciled"] is False

    @pytest.mark.asyncio
    async def test_reuse_path_releases_next_retry(self, monkeypatch):
        """Full reuse chain: intercept-with-hit sets gate_reconciled, and
        the LLM's subsequent same-fingerprint retry is RELEASED without
        any further whitelist read — the shortest legal chain (hold once,
        probe once, release)."""
        async def fake_check(kubeconfig, task_id, **kwargs):
            return ["exp-123"], [ConflictInfo(
                uid="exp-123", same_action_as_request=True,
                overlaps_target=True,
            )]

        monkeypatch.setattr(
            "chaos_agent.agent.providers.chaosblade.reconcile"
            ".check_blade_conflicts", fake_check,
        )
        out = await self._gate()
        _, flag_after_hit = out
        assert flag_after_hit["gate_reconciled"] is True

        # The LLM read the feedback and chose to re-issue the create
        # (reuse choice would carry the UID to verify instead — that path
        # never reaches this gate; the release below is the retry choice).
        second = await self._gate(flag=flag_after_hit)
        assert second is None


class TestProviderInconsistencyFallback:
    """Registry-level degradation (defensive branch): a provider that
    fingerprints a create yet composes NO hold feedback (an implementation
    inconsistency — both hooks claim the same tool set, so this cannot
    happen with the builtins) must not silently release the blind retry:
    the gate falls back to generic, carrier-neutral wording and keeps the
    interception honest (held, marked, retriable via cap/probe rules)."""

    class _FingerprintOnlyProvider:
        # Fingerprint hook WITHOUT its hold-feedback twin — the exact
        # inconsistency the fallback guards against.
        carrier = "my_carrier"
        injection_methods = ("my_method",)
        reconcile_create_tool_names = frozenset({"my_tool"})
        reconcile_read_tool_names = frozenset()

        def matches_channel(self, profile):
            return True

        def build_reconcile_fingerprint(self, tool_name, tool_args):
            if tool_name != "my_tool":
                return None
            from chaos_agent.tools.request_identity import RequestFingerprint

            return RequestFingerprint(namespace="ns-x", target_names="w1")

        async def reconcile_hold_feedback(self, *args, **kwargs):
            return None

        def reconcile_batch_held_feedback(self, tool_name, other_tool_name):
            return None

    @pytest.fixture(autouse=True)
    def _registered_inconsistent_provider(self):
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.clear()
        FaultProviderRegistry.register(self._FingerprintOnlyProvider())
        yield
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()

    async def test_degrades_to_generic_hold_and_batch_wording(self):
        from chaos_agent.tools.request_identity import RequestFingerprint

        response = AIMessage(content="", tool_calls=[
            {"name": "my_tool", "args": {"namespace": "ns-x", "names": "w1"},
             "id": "c1"},
            {"name": "other_tool", "args": {}, "id": "c2"},
        ])
        flag = {
            "fingerprint": fingerprint_to_state_dict(
                RequestFingerprint(namespace="ns-x", target_names="w1")
            ),
            "uncertain_call_id": "call_1",
            "blocked_count": 0,
            "gate_reconciled": False,
        }
        out = await apply_reconcile_gate(response, [], flag)
        assert out is not None
        answers, new_flag = out
        by_id = {m.tool_call_id: m for m in answers}
        # Held create: generic wording — marker-headed, reason/fix pairing,
        # is_hard_floor=False, and ZERO carrier vocabulary (no provider
        # composed it, so none could have leaked in).
        assert GATE_RECONCILE_BLOCKED_MARKER in by_id["c1"].content
        assert "is_hard_floor=False" in by_id["c1"].content
        assert "DUPLICATE" in by_id["c1"].content
        assert "blade" not in by_id["c1"].content
        # Batch-mate: generic never-executed notice naming the held call.
        assert "NOT executed" in by_id["c2"].content
        assert "other_tool" in by_id["c2"].content
        assert "blade" not in by_id["c2"].content
        # The hold still counts and the un-composed probe reconciles
        # nothing (the generic fallback never claims reconciliation).
        assert new_flag["blocked_count"] == 1
        assert new_flag["gate_reconciled"] is False


class TestScreenerConsumesGateHold:
    """The ToolNode must never see a gate-held batch (mirror of the
    truncated diversion tests in test_truncated_tool_calls.py): the flag
    set by execute_loop's interception is consumed by tool_screener,
    which routes the turn back to the loop and clears the flag."""

    HELD = [
        AIMessage(content="", tool_calls=[
            {"name": "blade_create", "args": dict(_RETRY_ARGS), "id": "call_9"},
        ]),
        ToolMessage(
            content=(
                f"{GATE_RECONCILE_BLOCKED_MARKER} [create-reconcile] BLOCKED —"
            ),
            name="blade_create",
            tool_call_id="call_9",
            status="error",
        ),
    ]

    async def test_tool_screener_retries_on_gate_held_batch(self):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            SCREENER_ROUTE_RETRY,
            tool_screener,
        )

        delta = await tool_screener(
            {"_reconcile_gate_blocked": True, "messages": list(self.HELD)}
        )
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert delta["_reconcile_gate_blocked"] is False

    async def test_tool_screener_retries_when_flag_set_without_pending_batch(self):
        # Flag set but the last message is a plain ToolMessage (no pending
        # AIMessage): same retry diversion as the answered truncated batch.
        from chaos_agent.agent.nodes.planning.tool_screener import (
            SCREENER_ROUTE_RETRY,
            tool_screener,
        )

        delta = await tool_screener(
            {"_reconcile_gate_blocked": True, "messages": [
                AIMessage(content="done", tool_calls=[]),
            ]}
        )
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert delta["_reconcile_gate_blocked"] is False

    async def test_stale_flag_with_pending_batch_is_screened_normally(self):
        # Stale flag + a pending AIMessage batch: the flag is ignored (not
        # cleared here — execute_loop's release path clears it at source)
        # and screening proceeds normally; a read-only call must pass.
        from chaos_agent.agent.nodes.planning.tool_screener import (
            SCREENER_ROUTE_RETRY,
            tool_screener,
        )

        delta = await tool_screener(
            {"_reconcile_gate_blocked": True, "messages": [
                AIMessage(content="", tool_calls=[
                    {"name": "kubectl_read", "args": {"command": "get pods"},
                     "id": "call_10"},
                ]),
            ]}
        )
        assert delta.get("screener_route") != SCREENER_ROUTE_RETRY
        assert "_reconcile_gate_blocked" not in delta

    async def test_unset_flag_leaves_normal_screening(self):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            SCREENER_ROUTE_RETRY,
            tool_screener,
        )

        delta = await tool_screener(
            {"messages": list(self.HELD)}
        )
        # No flag → no gate diversion; the fabricated-looking tail is just
        # history, screening judges the pending batch (none here) — the
        # defensive no-tool_calls branch passes it through.
        assert delta.get("screener_route") != SCREENER_ROUTE_RETRY
        assert "_reconcile_gate_blocked" not in delta
