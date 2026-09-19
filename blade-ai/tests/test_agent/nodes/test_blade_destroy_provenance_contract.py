"""Contract tests for blade_destroy provenance across compaction (SC1 fix).

``_screen_blade_destroy`` only allows cleaning up an experiment whose UID
is proven by this task's own ``blade_create`` results. The proof source
used to be message history ONLY — but compaction removes old ToolMessages
BY DESIGN, so mid-task the whitelist went empty and the agent could no
longer destroy its OWN injection: the very recovery step failed the
provenance gate.

The fix unions the message scan with ``state["experiment_uid"]`` — the
framework's durable record of the live experiment, maintained by the
execution loop and preserved by the compressed-history restore path. The
gate's semantics are unchanged: every UID admitted is still proven
created by this task, now via either the visible result or the durable
record. These tests pin:

1. The message scan remains the primary record (including failed-create
   CRD UIDs that still need cleanup).
2. The durable record restores provenance once the ToolMessage is
   compacted away.
3. Foreign UIDs stay REJECT_UNKNOWN even with a durable record set —
   the union must not weaken the provenance gate.
4. Empty/whitespace durable records contribute nothing.
5. The two-argument legacy call (no state) keeps its original behaviour.
"""

from langchain_core.messages import ToolMessage

from chaos_agent.agent.nodes.planning.tool_screener import (
    _experiment_uids_created_by_current_task,
    _screen_blade_destroy,
    _screen_destroy_uid_provenance,
)
from chaos_agent.agent.target_guard import GuardVerdict
from chaos_agent.agent.target_guard.classifier import (
    SCOPE_UNKNOWN,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.types import EffectiveTarget

OWN_UID = "a1b2c3d4-e5f6-0718-9abc-def012345678"
# Round-19 N3 flip: the failed-create ``UID:`` wording anchor composes the
# single-source hex16 shape now (dashed = K8s-object vocabulary, refused);
# the SUCCESS create path above still accepts the dashed legacy spelling
# (_UID_SHAPE_RE's destroy-face compatibility branch), so the two fixtures
# deliberately differ in shape domain.
FAILED_CREATE_UID = "b2c3d4e5f6071829"
FOREIGN_UID = "ffffffff-0000-0000-0000-000000000000"


def _create_result(uid: str) -> ToolMessage:
    return ToolMessage(
        content=f'{{"code": 200, "success": true, "result": "{uid}"}}',
        name="blade_create",
        tool_call_id=f"call-{uid[:8]}",
    )


class TestMessageScanRemainsPrimary:
    def test_success_create_proves_uid(self):
        uids = _experiment_uids_created_by_current_task([_create_result(OWN_UID)])
        assert uids == {OWN_UID}

    def test_failed_create_crd_uid_still_counted(self):
        # Terminal create failures don't yield an "active" UID via
        # extract_blade_uid, but their CRDs still need cleanup — the
        # screener keeps admitting them.
        msg = ToolMessage(
            content=f'Error: experiment rejected (UID: {FAILED_CREATE_UID})',
            name="blade_create",
            tool_call_id="call-fail",
        )
        uids = _experiment_uids_created_by_current_task([msg])
        assert FAILED_CREATE_UID in uids

    def test_destroy_allowed_from_message_history(self):
        _, decision = _screen_blade_destroy(
            {"uid": OWN_UID}, [_create_result(OWN_UID)],
        )
        assert decision.verdict == GuardVerdict.ALLOW


class TestDurableRecordRestoresProvenance:
    def test_compacted_history_with_durable_uid_allows(self):
        # The blade_create ToolMessage is gone (compacted by design); the
        # framework still records the live experiment in state.
        _, decision = _screen_blade_destroy(
            {"uid": OWN_UID}, [], {"experiment_uid": OWN_UID},
        )
        assert decision.verdict == GuardVerdict.ALLOW

    def test_union_covers_both_sources(self):
        messages = [_create_result(FAILED_CREATE_UID)]
        state = {"experiment_uid": OWN_UID}
        uids = _experiment_uids_created_by_current_task(messages, state)
        assert uids == {OWN_UID, FAILED_CREATE_UID}

    def test_durable_uid_survives_partial_compaction(self):
        # Some history survives, but not the create result — the durable
        # record keeps proving provenance.
        _, decision = _screen_blade_destroy(
            {"uid": OWN_UID},
            [ToolMessage(content="ok", name="kubectl_get", tool_call_id="c")],
            {"experiment_uid": OWN_UID},
        )
        assert decision.verdict == GuardVerdict.ALLOW


class TestGateNotWeakened:
    def test_foreign_uid_rejected_with_durable_record(self):
        _, decision = _screen_blade_destroy(
            {"uid": FOREIGN_UID}, [], {"experiment_uid": OWN_UID},
        )
        assert decision.verdict == GuardVerdict.REJECT_UNKNOWN

    def test_foreign_uid_rejected_without_durable_record(self):
        _, decision = _screen_blade_destroy({"uid": FOREIGN_UID}, [])
        assert decision.verdict == GuardVerdict.REJECT_UNKNOWN

    def test_empty_uid_rejected(self):
        _, decision = _screen_blade_destroy({"uid": "  "}, [], {"experiment_uid": OWN_UID})
        assert decision.verdict == GuardVerdict.REJECT_UNKNOWN


class TestDurableRecordHygiene:
    def test_blank_durable_uid_contributes_nothing(self):
        assert _experiment_uids_created_by_current_task([], {"experiment_uid": ""}) == set()
        assert _experiment_uids_created_by_current_task([], {"experiment_uid": "   "}) == set()

    def test_non_string_durable_uid_is_coerced_safely(self):
        assert _experiment_uids_created_by_current_task([], {"experiment_uid": None}) == set()

    def test_durable_uid_is_stripped(self):
        uids = _experiment_uids_created_by_current_task([], {"experiment_uid": f" {OWN_UID} "})
        assert uids == {OWN_UID}


class TestLegacyCallShape:
    def test_no_state_keeps_original_behaviour(self):
        # Callers that pass no state see exactly the pre-fix semantics.
        _, allowed = _screen_blade_destroy({"uid": OWN_UID}, [_create_result(OWN_UID)])
        assert allowed.verdict == GuardVerdict.ALLOW
        _, rejected = _screen_blade_destroy({"uid": OWN_UID}, [])
        assert rejected.verdict == GuardVerdict.REJECT_UNKNOWN


class TestInlineDestroyFace:
    """The SAME provenance gate covers the kubectl-exec destroy channel.

    Twelfth-round finding E3: ``kubectl exec POD -- blade destroy <uid>``
    classified READONLY ("drift comparison not applicable") and passed
    with zero provenance — the same mutating action the blade_destroy
    tool face gates. The classifier now routes it to SCOPE_UNKNOWN with
    the UID extracted, and the screener runs the shared gate before
    execution. These tests pin the shared-gate behaviour for the inline
    face and the one-lesson wording (the rejection reason is the tool
    face's original sentence, so the model reads one consistent lesson
    whichever channel its cleanup attempt rode).
    """

    def _inline_effective(self, uid: str) -> EffectiveTarget:
        return infer_effective_target(
            "kubectl",
            {
                "command": [
                    "exec", "chaosblade-tool-x", "-n", "chaosblade",
                    "--", "blade", "destroy", uid,
                ],
            },
        )

    def test_classifier_routes_inline_destroy_to_unknown_with_uid(self):
        eff = self._inline_effective(FOREIGN_UID)
        assert eff.scope == SCOPE_UNKNOWN
        assert eff.blade_destroy_uid == FOREIGN_UID

    def test_inline_own_uid_allows_via_shared_gate(self):
        # in-cluster delivery: the registry itself instructs the model to
        # destroy via kubectl exec; the create receipts (or the durable
        # birth registry they feed) keep that path usable.
        eff = self._inline_effective(OWN_UID)
        decision = _screen_destroy_uid_provenance(
            eff.blade_destroy_uid, eff, [_create_result(OWN_UID)],
        )
        assert decision.verdict == GuardVerdict.ALLOW
        assert decision.effective is eff

    def test_inline_foreign_uid_rejects_via_shared_gate(self):
        eff = self._inline_effective(FOREIGN_UID)
        decision = _screen_destroy_uid_provenance(
            eff.blade_destroy_uid, eff, [_create_result(OWN_UID)],
        )
        assert decision.verdict == GuardVerdict.REJECT_UNKNOWN
        # One lesson, one wording: the tool face's original sentence.
        assert decision.reason == (
            "blade_destroy UID was not produced by this task's blade_create"
        )

    def test_inline_durable_record_restores_provenance(self):
        # Compaction away of the create receipt must not kill the
        # in-cluster recovery path either — the durable record is the
        # same union the tool face rides.
        eff = self._inline_effective(OWN_UID)
        decision = _screen_destroy_uid_provenance(
            eff.blade_destroy_uid, eff, [], {"experiment_uid": OWN_UID},
        )
        assert decision.verdict == GuardVerdict.ALLOW

    def test_inline_empty_uid_rejects(self):
        decision = _screen_destroy_uid_provenance(
            "", EffectiveTarget(scope=SCOPE_UNKNOWN, namespace=""),
            [_create_result(OWN_UID)],
        )
        assert decision.verdict == GuardVerdict.REJECT_UNKNOWN


class TestDeathLedgerChannelParity:
    """Round-14 F1: the death ledger must see BOTH destroy delivery faces.

    A proven inline kill used to register the literal token after the verb
    (``--uid``) instead of the experiment UID — the real experiment stayed
    live in every liability read while its paired receipt proved the death.
    Revoke joins the destroy vocabulary; ``scan_destroyed_uids`` (issued =
    terminal, the attribution half) must see the inline face too.
    """

    @staticmethod
    def _inline_destroy_call(v_args: str):
        from langchain_core.messages import AIMessage

        return AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": "exec", "v_args": v_args},
            "id": "tc-destroy",
            "type": "tool_call",
        }])

    @staticmethod
    def _death_receipt() -> ToolMessage:
        return ToolMessage(
            content='{"code": 200, "success": true}',
            name="kubectl",
            tool_call_id="tc-destroy",
        )

    @staticmethod
    def _run_scans(v_args: str):
        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_destroyed_proven_uids,
            scan_destroyed_uids,
        )

        msgs = [
            TestDeathLedgerChannelParity._inline_destroy_call(v_args),
            TestDeathLedgerChannelParity._death_receipt(),
        ]
        return scan_destroyed_uids(msgs), scan_destroyed_proven_uids(msgs)

    def test_flag_spelling_registers_real_uid(self):
        issued, proven = self._run_scans(
            f"toolpod -n chaosblade -- blade destroy --uid {OWN_UID}"
        )
        assert OWN_UID in issued
        assert OWN_UID in proven

    def test_glued_flag_spelling_registers_real_uid(self):
        _, proven = self._run_scans(
            f"toolpod -n chaosblade -- blade destroy --uid={OWN_UID}"
        )
        assert OWN_UID in proven

    def test_revoke_vocabulary_is_not_a_death_carrier(self):
        # r14 pinned revoke as a death verb on both channels; round-25
        # K1 — aligning the inline face with the round-24 K3 host-face
        # ruling — corrected it: revoke tears down a PREPARE uid (a
        # precondition), never an experiment. Neither the issued=terminal
        # channel nor the proven channel may register it.
        issued, proven = self._run_scans(
            f"toolpod -n chaosblade -- blade revoke {OWN_UID}"
        )
        assert OWN_UID not in issued
        assert OWN_UID not in proven

    def test_leading_value_flag_does_not_shadow_uid(self):
        _, proven = self._run_scans(
            f"toolpod -- blade destroy --kubeconfig /root/kc {OWN_UID}"
        )
        assert OWN_UID in proven

    def test_failed_destroy_proves_nothing(self):
        # Doubt is not death: a failed receipt keeps the UID out of the
        # proven ledger (fail-closed), while issued=terminal still applies.
        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_destroyed_proven_uids,
            scan_destroyed_uids,
        )

        msgs = [
            self._inline_destroy_call(
                f"toolpod -n chaosblade -- blade destroy {OWN_UID}"
            ),
            ToolMessage(
                content='{"code": 500, "success": false, "error": "boom"}',
                name="kubectl",
                tool_call_id="tc-destroy",
            ),
        ]
        assert OWN_UID in scan_destroyed_uids(msgs)
        assert OWN_UID not in scan_destroyed_proven_uids(msgs)

    def test_composite_payload_still_captures_all(self):
        # Round-26 alignment: a composite's REAL receipt is one JSON object
        # per command (the J3-era single-JSON anchor was an impossible
        # form — blade prints once per invocation). The shlex-quoted script
        # face splits inside the quotes, and the line-per-segment
        # alignment registers both kills, each proven by its OWN line.
        _, proven = self._run_scans(
            f"sh -c 'blade destroy {OWN_UID} && blade destroy {FAILED_CREATE_UID}'"
        )
        # _run_scans pairs the single _death_receipt (success JSON); a
        # 2-segment composite needs TWO lines to align — this call's
        # receipt is misaligned, so NOTHING is proven (fail-closed; the
        # convergence valve owns the doubt). The ISSUED channel still
        # captures both (issued = terminal).
        assert proven == set()
        issued, _ = self._run_scans(
            f"sh -c 'blade destroy {OWN_UID} && blade destroy {FAILED_CREATE_UID}'"
        )
        assert OWN_UID in issued and FAILED_CREATE_UID in issued

    def test_composite_payload_aligned_receipt_captures_all(self):
        # The real composite receipt shape: one JSON line per destroy.
        # The aligned verdict registers BOTH kills — each destroy's own
        # line proves its own kill.
        from langchain_core.messages import AIMessage
        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_destroyed_proven_uids,
        )

        receipt = (
            '{"code": 200, "success": true}\n{"code": 200, "success": true}'
        )
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "exec",
                    "v_args": (
                        f"toolpod -n chaosblade -- sh -c 'blade destroy "
                        f"{OWN_UID} && blade destroy {FAILED_CREATE_UID}'"
                    ),
                },
                "id": "tc-c-aligned",
                "type": "tool_call",
            }]),
            ToolMessage(
                content=receipt,
                name="kubectl",
                tool_call_id="tc-c-aligned",
            ),
        ]
        assert scan_destroyed_proven_uids(msgs) == {OWN_UID, FAILED_CREATE_UID}

    def test_create_command_is_not_a_destroy(self):
        issued, proven = self._run_scans(
            "toolpod -n chaosblade -- blade create k8s pod-cpu fullload "
            f"--names nginx-1 --uid {OWN_UID}"
        )
        assert issued == set() and proven == set()
