"""Acceptance fixtures for the write-set contract (§4 of the change).

Drives the REAL backfilled case files through load → freeze → guard (and
the boundary-exit chain), pinning the empirically-known cross-domain
shapes against the code that must accept them:

  - #7 (NXDOMAIN): pod victim in ``default``, kube-system mechanism
    writes — the backfilled frontmatter loads, the extended snapshot
    freezes, every mechanism write passes the guard (zero drift
    rejections), and smuggled writes stay rejected
  - node-shape (#4 first-occurrence family): a cluster-scoped
    ``{node, "", [name]}`` entry — node write passes by name subset,
    a write to ANOTHER node stays drift
  - unattended early exit driven by the REAL manifest: the card carries
    the widening marker, the runner's unattended decision resolves to
    the boundary resume, and the gate terminates before execution
  - cascade: a same-kind drift correction rebuilds the snapshot with
    mechanism entries carried forward — mechanism writes still pass
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.gates.confirmation_gate import confirmation_gate
from chaos_agent.agent.nodes.planning.tool_screener import tool_screener
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.target_guard import approved_from_dict
from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy
from chaos_agent.agent.target_guard.freeze import freeze_approved_target_from_spec
from chaos_agent.agent.target_guard.mechanism_writes import (
    load_case_mechanism_writes,
)
from chaos_agent.agent.target_guard.types import EffectiveTarget

_NXDOMAIN_SKILL = "k8s-chaos-skills"
_NXDOMAIN_CASE = (
    "references/catalogue/Pod_网络故障/Pod_网络故障_域名不存在NXDOMAIN.md"
)


def _real_nxdomain_entries():
    entries = load_case_mechanism_writes(_NXDOMAIN_SKILL, _NXDOMAIN_CASE)
    assert entries, (
        f"the backfilled NXDOMAIN case ({_NXDOMAIN_CASE}) must carry a "
        "mechanism_writes manifest — the backfill is part of this change"
    )
    return entries


def _victim_pod_spec() -> FaultSpec:
    return FaultSpec(
        scope="pod", namespace="default", names=["victim-pod"],
        fault_target="network", fault_action="loss",
        case_resource_path=_NXDOMAIN_CASE,
    )


def _frozen_real():
    return approved_from_dict(
        freeze_approved_target_from_spec(
            _victim_pod_spec(), mechanism_entries=_real_nxdomain_entries(),
        )
    )


def _ai_with_tool_call(name: str, args: dict, call_id: str = "tc-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


# ---------------------------------------------------------------------------
# §4.2 — NXDOMAIN backfill acceptance (#7-style)
# ---------------------------------------------------------------------------

class TestNxDomainBackfillAcceptance:
    """Victim in default, mechanism in kube-system — the #7 shape, legalized."""

    def test_real_case_loads_the_declared_write_set(self):
        entries = _real_nxdomain_entries()
        described = {e.describe() for e in entries}
        # Path A (shared Corefile), path B (transient ConfigMap prefix),
        # both paths (rollout restart / strategic patch of the CoreDNS
        # Deployment — pod approval's secondary_scopes has no deployment).
        assert "configmap/kube-system: names=['coredns', 'kube-dns']" in described
        assert "configmap/kube-system: name_prefix='drill-nxdomain-'" in described
        assert "deployment/kube-system: names=['coredns', 'kube-dns']" in described

    def test_full_mechanism_write_set_passes_zero_drift(self):
        approved = _frozen_real()
        policy = K8sDriftPolicy()

        # Every write the case teaches, both paths + both distribution
        # names + recovery deletions — zero drift rejections.
        writes = [
            ("configmap", "kube-system", ["coredns"]),          # path A inject
            ("configmap", "kube-system", ["coredns", "kube-dns"]),  # distro alias
            ("configmap", "kube-system", ["drill-nxdomain-tmp"]),  # path B create
            ("deployment", "kube-system", ["coredns"]),         # path B patch
            ("deployment", "kube-system", ["coredns", "kube-dns"]),  # rollout
        ]
        for scope, ns, names in writes:
            effective = EffectiveTarget(
                scope=scope, namespace=ns, names=tuple(names),
            )
            decision = policy.check_identity_drift(approved, effective)
            assert decision is None, (
                f"mechanism write {scope}/{ns}/{names} must be in-contract, "
                f"got: {decision.reason if decision else None}"
            )

    def test_recovery_delete_passes_through_prefix_entry(self):
        approved = _frozen_real()
        policy = K8sDriftPolicy()
        decision = policy.check_identity_drift(
            approved,
            EffectiveTarget(
                scope="configmap", namespace="kube-system",
                names=("drill-nxdomain-tmp",),
            ),
        )
        assert decision is None

    def test_victim_pod_write_unchanged(self):
        # The victim's own domain stays under the ordinary freeze rules —
        # the manifest only adds, never subtracts.
        approved = _frozen_real()
        policy = K8sDriftPolicy()
        assert policy.check_identity_drift(
            approved,
            EffectiveTarget(
                scope="pod", namespace="default", names=("victim-pod",),
            ),
        ) is None
        assert policy.check_identity_drift(
            approved,
            EffectiveTarget(
                scope="pod", namespace="default", names=("other-pod",),
            ),
        ) is not None


# ---------------------------------------------------------------------------
# §4.7 — prompt-injection smuggling under a REAL manifest
# ---------------------------------------------------------------------------

class TestPromptInjectionSmuggling:
    """A smuggled kube-system write finds no path that legalizes it."""

    def test_smuggled_secret_write_rejected(self):
        approved = _frozen_real()
        policy = K8sDriftPolicy()
        decision = policy.check_identity_drift(
            approved,
            EffectiveTarget(
                scope="secret", namespace="kube-system",
                names=("cluster-admin-token",),
            ),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"

    def test_foreign_configmap_name_rejected_with_manifest_attribution(self):
        approved = _frozen_real()
        policy = K8sDriftPolicy()
        decision = policy.check_identity_drift(
            approved,
            EffectiveTarget(
                scope="configmap", namespace="kube-system",
                names=("kube-root-ca.crt",),
            ),
        )
        assert decision is not None
        # The manifest attribution routes the fix: under-declared case vs
        # over-reaching plan.
        assert "manifest" in decision.reason
        assert "under-declares" in decision.reason or "over-reached" in decision.reason

    def test_prefix_miss_rejected(self):
        approved = _frozen_real()
        policy = K8sDriftPolicy()
        decision = policy.check_identity_drift(
            approved,
            EffectiveTarget(
                scope="configmap", namespace="kube-system",
                names=("evil-drill-not-ours",),
            ),
        )
        assert decision is not None
        assert "manifest" in decision.reason


# ---------------------------------------------------------------------------
# §4.3 — node-shape backfill acceptance (cluster-scoped entry)
# ---------------------------------------------------------------------------

_NODE_SHAPE_CASE = """---
mechanism_writes:
  - scope: node
    namespace: ""
    names: [drill-worker-1]
---
**用例名称** node-shape fixture

The mechanism taints the node the victim Pod runs on; the case author
legislates the node by name.
"""


class TestNodeShapeBackfillAcceptance:
    """Cluster-scoped entry: name subset passes, ANOTHER node stays drift."""

    def test_node_write_end_to_end(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        (skill_dir / "cases" / "node-shape.md").write_text(
            _NODE_SHAPE_CASE, encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )

        entries = load_case_mechanism_writes("demo-skill", "cases/node-shape.md")
        assert len(entries) == 1
        assert entries[0].scope == "node"
        assert entries[0].namespace == ""
        assert entries[0].names == ("drill-worker-1",)

        # Freeze → hydrate round-trip keeps the cluster-scoped entry.
        approved = approved_from_dict(
            freeze_approved_target_from_spec(
                _victim_pod_spec(), mechanism_entries=entries,
            )
        )
        assert len(approved.mechanism_entries) == 1

        policy = K8sDriftPolicy()

        # Same node: passes by name subset.
        assert policy.check_identity_drift(
            approved,
            EffectiveTarget(scope="node", namespace="", names=("drill-worker-1",)),
        ) is None

        # ANOTHER node: stays drift, with the manifest attribution.
        decision = policy.check_identity_drift(
            approved,
            EffectiveTarget(scope="node", namespace="", names=("drill-worker-2",)),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"
        assert "manifest" in decision.reason


# ---------------------------------------------------------------------------
# §4.4 — unattended early exit driven by the REAL manifest
# ---------------------------------------------------------------------------

class TestUnattendedEarlyExitFromRealManifest:
    """Card marker → runner decision → gate outcome, one chain, no
    28-minute drift loop. AUTO delegation (flipped 2026-09-01): the
    runner resumes "approved", the gate's approved branch clears the
    pending marker, and the widened snapshot is executable — the guard
    enforces the manifest boundary at dispatch time."""

    @pytest.mark.asyncio
    async def test_real_manifest_early_exit_chain(self, sample_agent_state):
        from chaos_agent.cli.runner import _unattended_resume_value

        state = sample_agent_state
        state["skill_name"] = _NXDOMAIN_SKILL
        state["safety_status"] = "safe"
        state["interaction_mode"] = "cli"
        state["plan"] = "Patch the coredns ConfigMap in kube-system."
        spec = _victim_pod_spec()
        state["fault_spec"] = spec.to_dict()
        state["approved_target"] = freeze_approved_target_from_spec(
            spec, mechanism_entries=_real_nxdomain_entries(),
        )

        seen_payloads = []

        def unattended_interrupt(payload):
            # Replay the unattended runner: it never asks a human, it
            # resolves the resume value from the card payload alone.
            seen_payloads.append(payload)
            return _unattended_resume_value(payload)

        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            side_effect=unattended_interrupt,
        ):
            result = await confirmation_gate(state)

        # The card carried the widening marker over the real manifest.
        # Six entries: the three original mechanism writes (coredns cm,
        # drill-nxdomain- cm prefix, deployment) plus the cross-ns
        # carrier's role/rolebinding @ kube-system (drill-rc- prefix),
        # plus the CR-channel body itself (faultdrill/kube-system,
        # fd- prefix — the T3.4 pre-fix: the entry the route gate's
        # write-set admission requires; without it the first CR apply
        # is scope-drift-rejected before the gate is ever consulted).
        assert seen_payloads[0]["write_set_widened"]
        assert len(seen_payloads[0]["write_set_widened"]["mechanism_writes"]) == 6
        widened = {
            e["description"]
            for e in seen_payloads[0]["write_set_widened"]["mechanism_writes"]
        }
        assert "faultdrill/kube-system: name_prefix='fd-'" in widened

        # The runner's decision was the AUTO delegation ("approved"),
        # and the gate flowed through its approved branch: the pending
        # marker is cleared and the widened snapshot is executable (the
        # runtime guard — not a human — is the enforcement boundary).
        assert result["needs_confirmation"] is False
        frozen = result["approved_target"]
        assert frozen["mechanism_entries"]
        assert "widening_pending_approval" not in frozen


# ---------------------------------------------------------------------------
# §4.5 — cascade: drift correction keeps mechanism writes in-contract
# ---------------------------------------------------------------------------

class TestCascadeMechanismWritesStillPass:
    @pytest.mark.asyncio
    async def test_rebuilt_snapshot_passes_mechanism_writes(self):
        from chaos_agent.config.settings import settings

        # Snapshot-restore: hard-coding False here poisoned later suites
        # that rely on the session default (enforcing=True).
        orig_enforcing = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            spec = _victim_pod_spec()
            state = {
                "messages": [
                    _ai_with_tool_call("blade_create", {
                        "scope": "pod", "target": "network",
                        "namespace": "default", "names": ["pod-OTHER"],
                    }),
                ],
                "approved_target": freeze_approved_target_from_spec(
                    spec, mechanism_entries=_real_nxdomain_entries(),
                ),
                "fault_spec": spec.to_dict(),
            }
            # Same-kind drift correction approved by a human: the victim
            # identity moves, the mechanism entries ride along.
            with patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="approved",
            ):
                delta = await tool_screener(state)
            rebuilt = delta["approved_target"]
            assert rebuilt["names"] == ["pod-OTHER"]

            # The rebuilt snapshot still passes mechanism writes at the
            # guard — carry-forward is not just data presence.
            rebuilt_approved = approved_from_dict(rebuilt)
            policy = K8sDriftPolicy()
            assert policy.check_identity_drift(
                rebuilt_approved,
                EffectiveTarget(
                    scope="configmap", namespace="kube-system",
                    names=("coredns",),
                ),
            ) is None
        finally:
            settings.target_guard_enforcing = orig_enforcing
