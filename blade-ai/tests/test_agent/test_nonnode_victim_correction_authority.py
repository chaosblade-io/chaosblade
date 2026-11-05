"""openspec generalize-nonnode-victim-identity-correction — Fix1 路 A + Fix4.

Covers the non-node victim identity-correction authority:

- ``intent_anchor.user_text_corroborates`` — verify-not-parse against the
  fixed entry text (triple narrowing, fail-closed).
- The ``approved_target`` freeze plumbing that carries ``user_description``
  through the single spec funnel (and keeps the low-level path byte-identical).
- ``_alignment_violation``'s kind-agnostic dispatch — the node/host anchored
  path preserved verbatim, the non-node path gated by corroboration, and the
  fabricated-proposal-description twin of the node forgery test.
- Fix4's drift-side identity-degradation attribution — corroborated cross-kind
  writes are attributed as identity degradation (never a mechanism switch),
  uncorroborated ones keep the generic scope-drift reason, and case-manifest
  mechanism writes are structurally unaffected (the 3.6 gate returns first).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute.execute_loop import _collect_guard_rejections
from chaos_agent.agent.nodes.planning.plan_change_confirm import (
    _alignment_violation,
    plan_change_confirm,
)
from chaos_agent.agent.prompts.sections.workflow import get_replan_section
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.spec.intent_anchor import user_text_corroborates
from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy
from chaos_agent.agent.target_guard.freeze import (
    approved_from_dict,
    freeze_approved_target,
    freeze_approved_target_from_spec,
)
from chaos_agent.agent.target_guard.mechanism_writes import parse_mechanism_writes
from chaos_agent.agent.target_guard.types import EffectiveTarget

_INGRESS_INTENT = "对 default 命名空间下的 ingress entry 注入后端服务不存在故障"


# ---------------------------------------------------------------------------
# D1 · user_text_corroborates — verify, not parse; fail-closed
# ---------------------------------------------------------------------------

class TestUserTextCorroborates:
    def test_name_and_kind_both_present_corroborates(self):
        assert user_text_corroborates(_INGRESS_INTENT, "ingress", ("entry",))

    def test_kind_absent_does_not_corroborate(self):
        # The user named "ingress", not "service": a service-scope proposal is
        # NOT backed by the user's own words (this is the Case #70 degradation).
        assert not user_text_corroborates(_INGRESS_INTENT, "service", ("entry",))

    def test_name_absent_does_not_corroborate(self):
        assert not user_text_corroborates(_INGRESS_INTENT, "ingress", ("other",))

    def test_whole_token_boundary_rejects_substring(self):
        # "entry" must not match inside "entrypoint" — the same lookbehind /
        # lookahead discipline the namespace anchor uses.
        assert not user_text_corroborates(
            "打 entrypoint 的 ingress 故障", "ingress", ("entry",),
        )

    def test_registered_alias_kind_hits(self):
        # KIND_ALIASES is the single source: the plural / short alias counts.
        assert user_text_corroborates(
            "patch the svc drill-svc selector", "service", ("drill-svc",),
        )

    def test_pure_chinese_kind_fails_closed(self):
        # No English alias for "入口": honest refusal rather than a guess.
        assert not user_text_corroborates(
            "对入口 entry 注入故障", "ingress", ("entry",),
        )

    def test_node_and_host_are_excluded(self):
        # Anchored kinds belong to extract_explicit_node_anchor, never here.
        assert not user_text_corroborates("在节点 n1 上注入", "node", ("n1",))
        assert not user_text_corroborates("在主机 h1 上注入", "host", ("h1",))

    def test_empty_inputs_fail_closed(self):
        assert not user_text_corroborates("", "ingress", ("entry",))
        assert not user_text_corroborates(_INGRESS_INTENT, "ingress", ())
        assert not user_text_corroborates(_INGRESS_INTENT, "", ("entry",))


# ---------------------------------------------------------------------------
# Fix4 plumbing · the frozen snapshot carries user_description
# ---------------------------------------------------------------------------

def _spec(scope: str, name: str, *, user_description: str = "") -> FaultSpec:
    return FaultSpec(
        scope=scope, namespace="default", names=[name],
        fault_target="config", fault_action="patch",
        duration_seconds=360, source="cli_nl",
        user_description=user_description,
    )


class TestFreezeCarriesUserDescription:
    def test_from_spec_freezes_user_description(self):
        d = freeze_approved_target_from_spec(
            _spec("ingress", "entry", user_description=_INGRESS_INTENT),
        )
        assert d["user_description"] == _INGRESS_INTENT
        assert approved_from_dict(d).user_description == _INGRESS_INTENT

    def test_from_dict_input_carries_user_description(self):
        d = freeze_approved_target_from_spec(
            _spec("ingress", "entry", user_description=_INGRESS_INTENT).to_dict(),
        )
        assert approved_from_dict(d).user_description == _INGRESS_INTENT

    def test_no_text_keeps_legacy_snapshot_byte_identical(self):
        # A spec with no user_description must NOT grow the key — the golden
        # freeze shape stays byte-identical (conditional-key discipline).
        d = freeze_approved_target_from_spec(_spec("ingress", "entry"))
        assert "user_description" not in d
        assert approved_from_dict(d).user_description == ""

    def test_low_level_freeze_never_emits_key(self):
        d = freeze_approved_target(
            target={"namespace": "prod", "names": ["pod-a"], "resource_type": "pod"},
            params={"scope": "pod", "target": "cpu", "action": "fullload"},
            fault_scope="pod", fault_target="cpu", fault_action="fullload",
        )
        assert "user_description" not in d


# ---------------------------------------------------------------------------
# Fix1 · _alignment_violation kind-agnostic dispatch
# ---------------------------------------------------------------------------

def _current(scope: str, name: str, text: str, *, fault_target: str = "config") -> FaultSpec:
    return _spec(scope, name, user_description=text).replace(fault_target=fault_target)


class TestAlignmentDispatch:
    def test_nonnode_corroborated_is_aligned(self):
        current = _current("service", "drill-svc", _INGRESS_INTENT)
        # The correct fix: re-point at the ingress the user actually named.
        candidate = _spec("ingress", "entry").replace(fault_target="config")
        assert _alignment_violation(candidate, current) is None

    def test_nonnode_uncorroborated_is_identity_violation(self):
        current = _current("service", "drill-svc", _INGRESS_INTENT)
        # A configmap the user never named: fail closed to identity.
        candidate = _spec("configmap", "drill-cm")
        assert _alignment_violation(candidate, current) == "identity"

    def test_nonnode_domain_swap_is_mechanism_violation(self):
        current = _current("ingress", "entry", _INGRESS_INTENT, fault_target="config")
        candidate = _spec("ingress", "entry").replace(fault_target="network")
        assert _alignment_violation(candidate, current) == "mechanism"

    def test_fabricated_proposal_description_does_not_corroborate(self):
        """Twin of test_cli_rejects_fabricated_anchor_in_proposal_description:
        authority is the CURRENT contract's entry text, never the candidate's
        own echoed description — a forged description naming the resource must
        NOT unlock the non-node auto-approval."""
        current = _current("service", "drill-svc", "验证一下丢包效果")  # names nothing
        candidate = _spec("ingress", "entry").replace(
            user_description="对 ingress entry 注入故障（LLM 回显伪造）",
        )
        assert _alignment_violation(candidate, current) == "identity"

    def test_node_anchored_path_preserved(self):
        node_text = "模拟节点宕机：在节点 cn-node-1 上切断网络"
        current = _current("node", "", node_text, fault_target="network").replace(
            names=("cn-node-1",),
        )
        candidate = _spec("node", "cn-node-1").replace(fault_target="network")
        assert _alignment_violation(candidate, current) is None

    def test_node_anchorless_still_rejected(self):
        current = _current("node", "", "验证丢包", fault_target="network")
        candidate = _spec("node", "cn-node-1").replace(fault_target="network")
        assert _alignment_violation(candidate, current) == "anchorless"

    def test_node_different_name_still_identity(self):
        node_text = "在节点 cn-node-1 上切断网络"
        current = _current("node", "", node_text, fault_target="network")
        candidate = _spec("node", "cn-node-2").replace(fault_target="network")
        assert _alignment_violation(candidate, current) == "identity"


# ---------------------------------------------------------------------------
# Fix4 · drift-side identity-degradation attribution
# ---------------------------------------------------------------------------

def _approved(scope: str, name: str, *, user_description: str = "", entries=()):
    return approved_from_dict(
        freeze_approved_target_from_spec(
            _spec(scope, name, user_description=user_description),
            mechanism_entries=entries,
        )
    )


def _eff(scope: str, name: str, ns: str = "default") -> EffectiveTarget:
    return EffectiveTarget(scope=scope, namespace=ns, names=(name,))


class TestFix4DriftAttribution:
    def test_corroborated_cross_kind_attributed_as_identity_degradation(self):
        # Frozen victim degraded to service, but the user's own text named the
        # ingress the executor is now trying to write → identity degradation,
        # NOT a mechanism failure. This is the Case #70 attribution Fix4 adds.
        approved = _approved("service", "drill-svc", user_description=_INGRESS_INTENT)
        decision = K8sDriftPolicy().check_identity_drift(
            approved, _eff("ingress", "entry"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"
        assert "target identity degradation" in decision.reason
        assert "do NOT substitute a different mechanism" in decision.reason

    def test_uncorroborated_cross_kind_keeps_scope_drift(self):
        # The user named an ingress; a configmap write is NOT what they asked
        # for → genuine unauthorized drift, generic reason preserved.
        approved = _approved("ingress", "entry", user_description=_INGRESS_INTENT)
        decision = K8sDriftPolicy().check_identity_drift(
            approved, _eff("configmap", "drill-cm"),
        )
        assert decision is not None
        assert "scope drift" in decision.reason
        assert "target identity degradation" not in decision.reason

    def test_empty_user_description_keeps_scope_drift(self):
        # Fail-closed: a legacy snapshot with no frozen text never enters the
        # corroboration branch — today's reason stays byte-identical.
        approved = _approved("service", "drill-svc")
        decision = K8sDriftPolicy().check_identity_drift(
            approved, _eff("ingress", "entry"),
        )
        assert decision is not None
        assert "scope drift" in decision.reason

    def test_same_kind_victim_own_body_allows_with_text_present(self):
        # No false-block on the happy path: an in-contract write never reaches
        # the cross-kind branch, even when user_description is frozen.
        approved = _approved("ingress", "entry", user_description=_INGRESS_INTENT)
        assert K8sDriftPolicy().check_identity_drift(
            approved, _eff("ingress", "entry"),
        ) is None

    def test_mechanism_authorized_cross_kind_unaffected(self):
        """Zero false-block for mechanism_writes cases: the 3.6 gate returns
        None for a case-manifest-authorized cross-kind write BEFORE the Fix4
        branch is ever reached, even with user_description frozen."""
        entries = parse_mechanism_writes(
            "---\nname: X\nmechanism_writes:\n"
            "  - scope: configmap\n    namespace: kube-system\n"
            "    names: [coredns-custom]\n---\n# body\n"
        )
        approved = _approved(
            "pod", "victim-pod",
            user_description="对 victim-pod 注入 NXDOMAIN，改 kube-system 的 coredns-custom",
            entries=entries,
        )
        decision = K8sDriftPolicy().check_identity_drift(
            approved, _eff("configmap", "coredns-custom", ns="kube-system"),
        )
        assert decision is None


# ---------------------------------------------------------------------------
# L2 · the CONNECTED chain: Fix4 attribution → replan hard constraint → Fix1
#      auto-approval. One degraded fixture threaded end to end, deterministic
#      (no live LLM). Fix4's attribution (TestFix4DriftAttribution) and Fix1's
#      auto-approval (test_plan_change_confirm graph tests) are each covered in
#      isolation; THIS proves the SEAM between them that task 3.5 only checked
#      statically — that the Fix4 reason physically survives the screener
#      format + content[:500] cap, lands in get_replan_section as a HARD
#      CONSTRAINT naming propose_plan_change, and the SAME degraded contract
#      then closes through Fix1.
# ---------------------------------------------------------------------------

def _degraded_current() -> FaultSpec:
    """The Case #70 degradation shape threaded through the whole chain: the
    user named an ingress (``_INGRESS_INTENT``), the frozen contract locked a
    service victim. revision 0 so the correction is not stale-flagged."""
    return _spec("service", "drill-svc", user_description=_INGRESS_INTENT).replace(
        revision=0,
    )


class TestFix4ToFix1ConnectedChain:
    def test_fix4_reason_flows_through_replan_as_hard_constraint(self):
        """Steps A–C: Fix4 fires on the degraded freeze, and its reason travels
        intact through the real replan plumbing into the hard-constraint block
        that steers the LLM to propose_plan_change (NOT a mechanism switch)."""
        # ---- Step A: Fix4 fires on the degraded freeze --------------------
        approved = _approved("service", "drill-svc", user_description=_INGRESS_INTENT)
        decision = K8sDriftPolicy().check_identity_drift(
            approved, _eff("ingress", "entry"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"
        assert "target identity degradation" in decision.reason
        assert "do NOT substitute a different mechanism" in decision.reason

        # ---- Step B: the reason survives the screener render + [:500] cap --
        # Rendered exactly as tool_screener._format_rejection_for_llm does
        # ("[target_guard] <VERDICT> — <reason>", em dash), then collected by
        # the REAL execute_loop scanner.
        receipt = f"[target_guard] {decision.verdict.value.upper()} \u2014 {decision.reason}"
        rejections = _collect_guard_rejections([
            ToolMessage(content=receipt, name="kubectl", tool_call_id="t1"),
        ])
        assert len(rejections) == 1
        assert rejections[0]["verdict"] == "REJECT_DRIFT"
        # The steering clause MUST survive content[:500] — this is the exact
        # fact task 3.5 verified only by character-count reasoning.
        assert "identity degradation" in rejections[0]["message"]
        assert "do NOT substitute" in rejections[0]["message"]

        # ---- Step C: replan renders it as a HARD CONSTRAINT that names the
        #              correction channel Fix1 handles ----------------------
        section = get_replan_section({
            "error_summary": "write blocked by target_guard",
            "guard_rejections": rejections,
        })
        assert "HARD CONSTRAINTS" in section
        assert "identity degradation" in section
        assert "do NOT substitute" in section
        assert "propose_plan_change" in section

    @pytest.mark.asyncio
    async def test_fix1_closes_the_same_degraded_contract(self):
        """Step D: the loop closes — the SAME degraded contract, corrected via
        propose_plan_change back to the ingress the user named, is auto-approved
        by Fix1 (no human at the CLI interrupt, no mechanism switch)."""
        current = _degraded_current()
        proposed = current.to_intent_dict() | {
            "scope": "ingress", "names": ["entry"], "labels": {},
        }
        call = {
            "name": "propose_plan_change", "id": "chain-1",
            "args": {
                "reason": "frozen service victim is wrong; the user named the ingress",
                "fault_revision": 0,
                "proposed_fault": proposed,
            },
        }
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[call]),
                ToolMessage(content="ok", name=call["name"], tool_call_id=call["id"]),
            ],
            "interaction_mode": "cli",
            "replan_context": {"error_summary": "target identity degradation"},
            "plan_change_reject_count": 0,
            "fault_spec": current.to_dict(),
            "batch_submit_args": None,
        }
        with patch(
            "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
            side_effect=AssertionError("a corroborated non-node fix must not interrupt"),
        ):
            result = await plan_change_confirm(state)

        spec = FaultSpec.from_dict(result["fault_spec"])
        assert spec is not None
        assert spec.scope == "ingress"
        assert spec.names == ("entry",)
        assert spec.revision == 1
        assert "CLI non-interactive" in result["messages"][0].content
