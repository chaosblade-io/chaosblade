"""Tests for the Layer 1 verification domain.

Parsing lives in ``providers/chaosblade/verify.py`` (moved from
``nodes/verify/_verifier_layer1.py`` in phase-4 T4); state orchestration
(``run_layer1_for_state`` channel selection) stays in ``_verifier_layer1.py``.
"""

import json

import pytest
from langchain_core.messages import ToolMessage

# Phase-4 T4 canonical address (the Layer-1 execution domain moved from
# nodes/verify/_verifier_layer1.py to the provider layer).
from chaos_agent.agent.providers.chaosblade.verify import (
    _parse_blade_status_output,
    _parse_blade_query_k8s_output,
    _find_blade_query_in_messages,
    _map_query_k8s_to_layer1,
    _QueryK8sResult,
)
from chaos_agent.agent.result.verdict import Layer1Result, Layer1Status


class TestParseBladeStatusOutput:
    def test_running(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Running"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"
        assert not expired

    def test_success_status(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Success"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"

    def test_destroyed_expired(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Destroyed"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert expired is True
        assert "expired" in details.lower()

    def test_destroyed_early_cleanup_is_warning(self):
        """Record destroyed well before its --timeout ⇒ external cleanup, not
        timeout expiry. Must NOT be failed (task inject-e47de3e8: executor
        destroyed the record +20.4s after creation with --timeout=600)."""
        raw = json.dumps({"code": 200, "success": True, "result": {
            "Status": "Destroyed",
            "CreateTime": "2026-08-16T16:22:07.49559281Z",
            "UpdateTime": "2026-08-16T16:22:27.880948049Z",
            "Flag": " --timeout=600 --signal=15 --pid=3456823",
        }})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "warning"
        assert expired is True
        assert "timeout" not in details.lower() or "not by timeout" in details.lower()
        # Must not carry the misleading "increase --duration" advice.
        assert "increasing" not in details.lower()

    def test_destroyed_after_timeout_is_failed(self):
        """Record lived out its full --timeout window ⇒ genuine expiry stays failed."""
        raw = json.dumps({"code": 200, "success": True, "result": {
            "Status": "Destroyed",
            "CreateTime": "2026-08-16T16:22:07.000000Z",
            "UpdateTime": "2026-08-16T16:32:08.000000Z",
            "Flag": " --timeout=600 --signal=15",
        }})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert expired is True
        assert "--timeout elapsed" in details

    def test_revoked_expired(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Revoked"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert expired is True

    def test_api_failure(self):
        raw = json.dumps({"code": 500, "success": False, "result": {}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert not expired

    def test_initialized_is_a_setup_phase_not_a_verdict(self):
        """``Initialized`` means the Operator has not reconciled the CRD yet.

        The exact payload from task-fc64c982: the CRD exists, ``success`` is
        true, and ``statuses`` is empty because reconciliation has not started.
        Reading that as ``failed`` reported a drill that had already stopped
        containerd (node went Ready→NotReady, confirmed in the same run) as a
        failure — and because ``failed`` is terminal, Layer 2 never ran to say
        otherwise.
        """
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"error": "", "phase": "Initialized", "statuses": [],
                       "success": True, "uid": "dea3008a9cc9f817"},
        })
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "warning"
        assert not expired
        assert "Layer 2" in details

    def test_creating_is_also_a_setup_phase(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"phase": "Creating"}})
        status, _, expired = _parse_blade_status_output(raw)
        assert status == "warning"
        assert not expired

    def test_a_setup_phase_keeps_layer2_in_play(self):
        """The point of ``warning`` over ``failed``: it is not terminal."""
        raw = json.dumps({"code": 200, "success": True, "result": {"phase": "Initialized"}})
        status, _, _ = _parse_blade_status_output(raw)
        assert Layer1Result(status=Layer1Status(status)).is_terminal() is False

    def test_an_unknown_phase_still_fails_closed(self):
        """Only the enumerated setup phases are exempt."""
        raw = json.dumps({"code": 200, "success": True, "result": {"phase": "WhatIsThis"}})
        status, _, _ = _parse_blade_status_output(raw)
        assert status == "failed"

    def test_non_dict_result_means_success(self):
        raw = json.dumps({"code": 200, "success": True, "result": "abc123uid"})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"

    def test_non_json_fallback_running(self):
        raw = "Status: Running, everything is fine"
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"

    def test_non_json_fallback_no_match(self):
        raw = "Error: something went wrong"
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"

    def test_transient_please_wait(self):
        """Both transient signals present — either branch must reach ``warning``.

        This fixture carries ``Status: Initialized`` AND ``Error: please wait``,
        so the setup-phase check now answers first. The verdict is what matters;
        asserting the exact wording tied the test to whichever branch happened to
        run, which is why adding the setup-phase check broke it.
        """
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"Status": "Initialized", "Error": "please wait, preparing"},
        })
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "warning"
        assert not expired
        assert "Layer 2" in details

    def test_please_wait_alone_is_transient(self):
        """``please wait`` without a setup phase still defers to Layer 2."""
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"Status": "Whatever", "Error": "please wait, preparing"},
        })
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "warning"
        assert "transient" in details.lower()

    def test_unknown_status(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Unknown"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert not expired

    def test_wrapped_record_not_found_is_failed(self):
        # Regression: a destroyed/absent experiment returns a `success:false`
        # JSON body wrapped by a shell "command terminated" trailer, which makes
        # a naive json.loads fail. It must be FAILED — never misread as passed
        # via the "success" substring inside `"success":false`.
        raw = (
            '{"code":67002,"success":false,'
            '"error":"2ef5 record not found, please add --target k8s flag"}\n'
            "command terminated with exit code 1\n"
        )
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert not expired

    def test_non_json_success_false_not_running(self):
        # Non-JSON fallback must not treat the "success" substring (inside
        # `"success":false`) or a "record not found" tail as a Running signal.
        raw = 'garbage "success":false record not found'
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"


class TestParseBladeQueryK8sOutput:
    def test_all_success(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"statuses": [
                {"name": "pod-1", "success": True, "state": "Running"},
                {"name": "pod-2", "success": True, "state": "Running"},
            ]},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "passed"
        assert r.affected_count == 2

    def test_some_failed(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"statuses": [
                {"name": "pod-1", "success": True},
                {"name": "pod-2", "success": False},
            ]},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "failed"

    def test_expired_state(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"statuses": [
                {"name": "exp-1", "state": "Destroyed", "success": True},
            ]},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "failed"
        assert r.expired is True

    def test_empty_input(self):
        r = _parse_blade_query_k8s_output("")
        assert r.status == "unknown"

    def test_error_not_found(self):
        r = _parse_blade_query_k8s_output("Error: not found")
        assert r.status == "unknown"
        assert "CRD" in r.details

    def test_non_json(self):
        r = _parse_blade_query_k8s_output("this is not json")
        assert r.status == "unknown"

    def test_api_error_not_found(self):
        raw = json.dumps({"code": 63061, "success": False, "error": "resource not found"})
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "unknown"
        assert "not found" in r.details.lower()

    def test_no_statuses_but_success(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"success": True},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "passed"


class TestFindBladeQueryInMessages:
    def test_finds_matching_message(self):
        uid = "abc-123-xyz"
        content = json.dumps({"success": True, "result": {"uid": uid, "status": "Running"}})
        messages = [
            ToolMessage(content="unrelated", name="kubectl", tool_call_id="tc1"),
            ToolMessage(content=content, name="kubectl", tool_call_id="tc2"),
        ]
        assert _find_blade_query_in_messages(messages, uid) == content

    def test_no_match(self):
        messages = [
            ToolMessage(content="no blade data", name="kubectl", tool_call_id="tc1"),
        ]
        assert _find_blade_query_in_messages(messages, "uid-999") == ""

    def test_wrong_uid(self):
        content = json.dumps({"success": True, "result": {"uid": "other-uid"}})
        messages = [
            ToolMessage(content=content, name="kubectl", tool_call_id="tc1"),
        ]
        assert _find_blade_query_in_messages(messages, "wanted-uid") == ""

    def test_empty_messages(self):
        assert _find_blade_query_in_messages([], "uid") == ""


class TestMapQueryK8sToLayer1:
    def test_passed(self):
        q = _QueryK8sResult("passed", "all ok", [], 2, False)
        r = _map_query_k8s_to_layer1(q, "{}", "pod-1", "original")
        assert r.status == "passed"

    def test_expired(self):
        q = _QueryK8sResult("failed", "expired", [], 1, True)
        r = _map_query_k8s_to_layer1(q, "{}", "pod-1", "discovery")
        assert r.status == "failed"
        assert r.expired is True

    def test_failed_not_expired(self):
        q = _QueryK8sResult("failed", "some failure", [], 1, False)
        r = _map_query_k8s_to_layer1(q, "{}", "pod-1", "original")
        assert r.status == "failed"
        assert r.expired is False


class TestHostNativeLayer1Skip:
    """P1.4: host_native injection has no blade experiment, so Layer 1 must be
    skipped explicitly rather than polling blade_status (which false-reports).
    The skip is now owned by ``HostShellProvider.layer1_verify`` and reached via
    the ``run_layer1_for_state`` seam keyed on ``injection_method``."""

    @pytest.mark.asyncio
    async def test_host_native_skips_layer1(self):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import run_layer1_for_state
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        state = {"injection_method": "host_native", "messages": []}
        r = await run_layer1_for_state(state, "", "/tmp/kubeconfig", task_id="t")
        assert r.status == "skipped"
        assert "host-native" in r.details

    @pytest.mark.asyncio
    async def test_kubectl_exec_empty_uid_skips_without_polling(self):
        # Q2#3 (task-76c59364): the kubectl_exec Layer-1 path must NOT issue
        # `blade status ''` when there is no UID — that returns ChaosBlade code
        # 45000 which reads as a genuine FAILURE. An absent UID is skipped
        # (not applicable), letting Layer 2 verify the actual cluster state.
        from chaos_agent.agent.providers.chaosblade.verify import (
            _run_layer1_via_kubectl_exec,
        )

        r = await _run_layer1_via_kubectl_exec("", "/tmp/kubeconfig", task_id="t")
        assert r.status == "skipped"
        assert "no experiment_uid" in r.details


class TestPluralLayer1AnchorSelection:
    """Round-28 K3 — composite-born tasks poll EVERY live experiment.

    The dispatch anchor is last-write-wins: a composite double-create
    whose first birth died leaves the slot naming the corpse while the
    sibling keeps running, and polling the dead anchor returned
    blade_status's "not found" FAILED as the TASK verdict. The plural
    poll keys on the liability oracle (the same set the sweep and the
    destroy whitelist consume): a live anchor keeps the single-poll
    mainline byte-identical; a dead anchor hands the role to the first
    survivor."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"

    @staticmethod
    def _install_fake_layer1(monkeypatch, polled: list):
        from chaos_agent.agent.providers.chaosblade.provider import (
            ChaosbladeProvider,
        )

        async def fake_layer1_verify(
            self, state, *, experiment_uid, kubeconfig, task_id="",
        ):
            polled.append(experiment_uid)
            return Layer1Result(
                status="passed",
                details=f"status of {experiment_uid}",
                raw_output=f"raw of {experiment_uid}",
            )

        monkeypatch.setattr(
            ChaosbladeProvider, "layer1_verify", fake_layer1_verify,
        )

    @pytest.mark.asyncio
    async def test_dead_anchor_polls_the_live_sibling(self, monkeypatch):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            run_layer1_for_state,
        )
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        polled: list[str] = []
        self._install_fake_layer1(monkeypatch, polled)

        state = {
            "experiment_uid": self.UID_A,  # dead slot, never cleared
            "retired_experiment_uids": [self.UID_A],
            "owned_experiment_uids": [self.UID_A, self.UID_B],
            "messages": [],
        }
        result = await run_layer1_for_state(
            state, self.UID_A, "/tmp/kubeconfig", task_id="t",
        )
        assert polled == [self.UID_B]  # the corpse is never polled
        assert result.details == "status of " + self.UID_B

    @pytest.mark.asyncio
    async def test_live_anchor_keeps_mainline_single_poll(self, monkeypatch):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            run_layer1_for_state,
        )
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        polled: list[str] = []
        self._install_fake_layer1(monkeypatch, polled)

        state = {
            "experiment_uid": self.UID_A,
            "retired_experiment_uids": [],
            "owned_experiment_uids": [self.UID_A],
            "messages": [],
        }
        result = await run_layer1_for_state(
            state, self.UID_A, "/tmp/kubeconfig", task_id="t",
        )
        # Single-experiment mainline: one poll, no sibling decoration.
        assert polled == [self.UID_A]
        assert result.details == "status of " + self.UID_A
        assert result.raw_output == "raw of " + self.UID_A

    @pytest.mark.asyncio
    async def test_sibling_evidence_structured_for_multiple_live(self, monkeypatch):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            run_layer1_for_state,
        )
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        polled: list[str] = []
        self._install_fake_layer1(monkeypatch, polled)

        state = {
            "experiment_uid": self.UID_A,
            "retired_experiment_uids": [],
            "owned_experiment_uids": [self.UID_A, self.UID_B],
            "messages": [],
        }
        result = await run_layer1_for_state(
            state, self.UID_A, "/tmp/kubeconfig", task_id="t",
        )
        # Round-29: anchor fields carry the anchor's machine verdict
        # ALONE (the r28 string-append is retired); the plural face is
        # structured — one ExperimentEvidence per polled experiment,
        # anchor first, sibling after.
        assert polled == [self.UID_A, self.UID_B]
        assert result.details == "status of " + self.UID_A
        assert result.raw_output == "raw of " + self.UID_A
        assert [e.uid for e in result.experiments] == [self.UID_A, self.UID_B]
        assert result.experiments[0].is_anchor is True
        assert result.experiments[1].is_anchor is False
        assert result.experiments[1].status == "passed"
        assert result.experiments[1].details == "status of " + self.UID_B

    @pytest.mark.asyncio
    async def test_empty_ledger_keeps_pre_round28_path(self, monkeypatch):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            run_layer1_for_state,
        )
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        polled: list[str] = []
        self._install_fake_layer1(monkeypatch, polled)

        # Legacy checkpoint / pre-registry state: no ownership ledger,
        # the dispatch uid is the only knowledge — the exact pre-round-28
        # single-poll path.
        state = {"experiment_uid": self.UID_A, "messages": []}
        result = await run_layer1_for_state(
            state, self.UID_A, "/tmp/kubeconfig", task_id="t",
        )
        assert polled == [self.UID_A]
        assert result.details == "status of " + self.UID_A


class TestLiveAnchorSeam:
    """Round-29 K2 — the anchor-selection seam shared by every verdict-side
    renderer (the seventh private copy closed): a dead dispatch slot hands
    the anchor role to the first surviving liability; a live dispatch uid,
    an empty live set and a ledger failure all keep the dispatch uid."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"

    def test_dead_anchor_hands_role_to_first_survivor(self):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            live_anchor_uid,
        )

        state = {
            "experiment_uid": self.UID_A,
            "retired_experiment_uids": [self.UID_A],
            "owned_experiment_uids": [self.UID_A, self.UID_B],
            "messages": [],
        }
        assert live_anchor_uid(state, self.UID_A) == self.UID_B

    def test_live_anchor_keeps_dispatch_uid(self):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            live_anchor_uid,
        )

        state = {
            "experiment_uid": self.UID_A,
            "retired_experiment_uids": [],
            "owned_experiment_uids": [self.UID_A, self.UID_B],
            "messages": [],
        }
        assert live_anchor_uid(state, self.UID_A) == self.UID_A

    def test_empty_live_set_keeps_dispatch_uid(self):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            live_anchor_uid,
        )

        # Ledger-less legacy checkpoint: the dispatch uid is the only
        # knowledge — the pre-plural path verbatim.
        state = {"experiment_uid": self.UID_A, "messages": []}
        assert live_anchor_uid(state, self.UID_A) == self.UID_A

    def test_empty_dispatch_uid_returns_empty(self):
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            live_anchor_uid,
        )

        # UID-less dispatch (native carrier): no anchor to choose.
        assert live_anchor_uid({"messages": []}, "") == ""

    @pytest.mark.asyncio
    async def test_sibling_poll_failure_becomes_error_entry(self, monkeypatch):
        # Round-29 K3: a failed sibling poll is an honest error ENTRY —
        # the same honesty standard the anchor always had — never a
        # swallowed exception.
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            run_layer1_for_state,
        )
        from chaos_agent.agent.providers import FaultProviderRegistry
        from chaos_agent.agent.providers.chaosblade.provider import (
            ChaosbladeProvider,
        )

        FaultProviderRegistry.register_builtins()
        uid_b = self.UID_B

        async def fake_layer1_verify(self, st, *, experiment_uid, kubeconfig, task_id=""):
            if experiment_uid == uid_b:
                raise RuntimeError("blade_status transport broke")
            return Layer1Result(
                status="passed",
                details=f"status of {experiment_uid}",
                raw_output=f"raw of {experiment_uid}",
            )

        monkeypatch.setattr(ChaosbladeProvider, "layer1_verify", fake_layer1_verify)
        state = {
            "experiment_uid": self.UID_A,
            "retired_experiment_uids": [],
            "owned_experiment_uids": [self.UID_A, self.UID_B],
            "messages": [],
        }
        result = await run_layer1_for_state(
            state, self.UID_A, "/tmp/kubeconfig", task_id="t",
        )
        # Anchor verdict UNDECORATED by the failure; the sibling's error
        # is a structured entry Layer 2 can see and weigh.
        assert result.details == "status of " + self.UID_A
        assert len(result.experiments) == 2
        err_entry = result.experiments[1]
        assert err_entry.uid == self.UID_B
        assert err_entry.status == "error"
        assert "transport broke" in err_entry.details
        assert not err_entry.is_anchor


class TestLayer1ResultPluralSerialization:
    """Round-29 — the plural face survives the canonical serialization
    round-trip (layer1_to_dict → model_validate) and stays absent on
    legacy caches without breaking validation."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"

    def test_plural_roundtrip_through_layer1_to_dict(self):
        from chaos_agent.agent.result.verdict import (
            ExperimentEvidence, layer1_to_dict,
        )

        result = Layer1Result(
            status="passed",
            details="anchor details",
            raw_output="anchor raw",
            experiments=[
                ExperimentEvidence(
                    uid=self.UID_A, status="passed", is_anchor=True,
                    details="anchor details", raw_output="anchor raw",
                ),
                ExperimentEvidence(
                    uid=self.UID_B, status="warning", is_anchor=False,
                    details="sibling details",
                ),
            ],
        )
        dumped = layer1_to_dict(result)
        # Plain-string statuses (mode="json" guarantee) on every entry.
        assert dumped["experiments"][0]["status"] == "passed"
        assert type(dumped["experiments"][0]["status"]) is str
        restored = Layer1Result.model_validate(dumped)
        assert [e.uid for e in restored.experiments] == [self.UID_A, self.UID_B]
        assert restored.experiments[0].is_anchor is True
        assert restored.experiments[1].status == "warning"

    def test_legacy_cache_without_experiments_key_validates(self):
        # Pre-round-29 checkpoints carry no ``experiments`` key — the
        # restore path (inject_layer1_cache) must keep working verbatim.
        legacy = {
            "status": "passed",
            "details": "d",
            "raw_output": "r",
            "resource_statuses": [],
            "affected_count": 0,
            "expired": False,
        }
        restored = Layer1Result.model_validate(legacy)
        assert restored.experiments == []
        assert restored.status == "passed"

    def test_single_experiment_mainline_keeps_empty_list(self):
        # The mainline (no siblings) renders an empty plural face — the
        # Layer-2 context and every legacy consumer see no change.
        from chaos_agent.agent.result.verdict import layer1_to_dict

        result = Layer1Result(status="passed", details="d", raw_output="r")
        assert layer1_to_dict(result).get("experiments") == []
