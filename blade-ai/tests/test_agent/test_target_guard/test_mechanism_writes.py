"""Tests for the case-file ``mechanism_writes`` manifest (write-set contract).

Covers tasks §1.4 / §2.5 of the write-set-approval-contract change:

  - three entry shapes parse (static / name_prefix / cluster-scoped)
  - malformed / missing frontmatter → empty manifest (fail closed later,
    at the guard — never at load)
  - same-coverage case → empty manifest
  - golden: no-manifest freeze output has no ``mechanism_entries`` key
    (byte-identical to today)
  - drift policy: name-subset pass / foreign-name reject with manifest
    attribution / prefix hit + prefix miss / cluster-scoped node entry
    (other node stays drift)
  - carry-forward: entries_from_list round-trip
  - no-manifest cross-domain miss keeps today's rejection + gains the
    "manifest missing" guidance
"""

from __future__ import annotations


from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.target_guard import approved_from_dict, freeze_approved_target_from_spec
from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy
from chaos_agent.agent.target_guard.mechanism_writes import (
    RECOVERY_CHANNEL_APISERVER_WRITE,
    MechanismWriteEntry,
    entries_beyond_victim,
    entries_from_list,
    entries_to_list,
    format_entries_for_payload,
    load_case_mechanism_writes,
    load_case_recovery_channel,
    match_mechanism_entries,
    names_within_entry,
    parse_mechanism_writes,
    parse_recovery_channel,
)
from chaos_agent.agent.target_guard.types import EffectiveTarget


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NXDOMAIN_FRONTMATTER = """---
name: Pod_网络故障_域名不存在NXDOMAIN
mechanism_writes:
  - scope: configmap
    namespace: kube-system
    names: [coredns-custom]
  - scope: ConfigMap
    namespace: kube-system
    name_prefix: drill-nxdomain-
---
# Case body — prose the Agent reads, never an authorization input.
"""


def _victim_pod_spec() -> FaultSpec:
    return FaultSpec(
        scope="pod", namespace="default", names=["victim-pod"],
        fault_target="cpu", fault_action="fullload",
    )


def _frozen_with_entries(entries):
    return approved_from_dict(
        freeze_approved_target_from_spec(
            _victim_pod_spec(), mechanism_entries=entries,
        )
    )


def _eff(scope: str, ns: str, names=()):
    return EffectiveTarget(scope=scope, namespace=ns, names=tuple(names))


# ---------------------------------------------------------------------------
# §1 Manifest parsing
# ---------------------------------------------------------------------------

class TestParseMechanismWrites:
    def test_three_entry_shapes_parse(self):
        entries = parse_mechanism_writes(_NXDOMAIN_FRONTMATTER)
        assert len(entries) == 2
        static, dynamic = entries
        assert static.scope == "configmap"
        assert static.namespace == "kube-system"
        assert static.names == ("coredns-custom",)
        assert static.name_prefix == ""
        assert dynamic.scope == "configmap"  # kind canonicalised
        assert dynamic.namespace == "kube-system"
        assert dynamic.names == ()
        assert dynamic.name_prefix == "drill-nxdomain-"

    def test_cluster_scoped_entry_normalises_namespace_away(self):
        content = """---
mechanism_writes:
  - scope: node
    namespace: ignored-stray-value
    names: [worker-1]
---
body
"""
        entries = parse_mechanism_writes(content)
        assert len(entries) == 1
        assert entries[0].scope == "node"
        assert entries[0].namespace == ""
        assert entries[0].names == ("worker-1",)

    def test_missing_frontmatter_yields_empty(self):
        assert parse_mechanism_writes("# plain markdown, no frontmatter") == ()

    def test_no_manifest_block_yields_empty(self):
        # Same-coverage case: frontmatter exists but declares no writes.
        assert parse_mechanism_writes("---\nname: x\nauthor: human\n---\nbody") == ()

    def test_malformed_yaml_yields_empty(self):
        assert parse_mechanism_writes("---\n: : :\n  - [\n---\nbody") == ()

    def test_non_list_block_yields_empty(self):
        assert parse_mechanism_writes(
            "---\nmechanism_writes: {bad: shape}\n---\nbody",
        ) == ()

    def test_names_and_prefix_together_rejects_entry(self):
        # Ambiguous authority (both selectors) — entry dropped, logged.
        content = """---
mechanism_writes:
  - scope: configmap
    namespace: kube-system
    names: [a]
    name_prefix: p-
---
body
"""
        assert parse_mechanism_writes(content) == ()

    def test_neither_names_nor_prefix_rejects_entry(self):
        content = """---
mechanism_writes:
  - scope: configmap
    namespace: kube-system
---
body
"""
        assert parse_mechanism_writes(content) == ()

    def test_unknown_key_rejects_entry(self):
        # A typo (names_prefix) must surface as "entry ignored", never as a
        # silently-widened or silently-narrowed write set.
        content = """---
mechanism_writes:
  - scope: configmap
    namespace: kube-system
    names_prefix: drill-
---
body
"""
        assert parse_mechanism_writes(content) == ()

    def test_entry_isolation_one_bad_entry_does_not_poison_siblings(self):
        content = """---
mechanism_writes:
  - scope: configmap
    namespace: kube-system
    names: [coredns-custom]
  - scope: configmap
    namespace: kube-system
    bad_key: true
  - scope: secret
    namespace: kube-system
    name_prefix: drill-
---
body
"""
        entries = parse_mechanism_writes(content)
        assert [e.scope for e in entries] == ["configmap", "secret"]

    def test_csv_names_accepted(self):
        content = """---
mechanism_writes:
  - scope: configmap
    namespace: kube-system
    names: a, b ,c
---
body
"""
        entries = parse_mechanism_writes(content)
        assert entries[0].names == ("a", "b", "c")

    def test_namespace_defaults_to_default_for_namespaced_kinds(self):
        content = """---
mechanism_writes:
  - scope: configmap
    names: [x]
---
body
"""
        entries = parse_mechanism_writes(content)
        assert entries[0].namespace == "default"


class TestLoadCaseMechanismWrites:
    def test_load_reads_case_file_directly(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        (skill_dir / "cases" / "case.md").write_text(
            _NXDOMAIN_FRONTMATTER, encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        entries = load_case_mechanism_writes("demo-skill", "cases/case.md")
        assert len(entries) == 2
        assert entries[0].names == ("coredns-custom",)

    def test_load_escapes_are_empty_manifest(self, tmp_path, monkeypatch):
        # case_resource_path is LLM-influenced state: traversal and
        # absolute paths land in the loader's escape-proof rejection,
        # which here degrades to an empty manifest (fail closed at guard).
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_mechanism_writes("demo-skill", "../../etc/passwd") == ()

    def test_load_missing_skill_or_path_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_mechanism_writes("", "cases/case.md") == ()
        assert load_case_mechanism_writes("demo-skill", "") == ()
        assert load_case_mechanism_writes("no-such-skill", "cases/case.md") == ()


# ---------------------------------------------------------------------------
# §1b recovery_channel — the case file's recovery-route legislation (D3
# source 1, faultdrill-cr-channel task 3.6)
# ---------------------------------------------------------------------------

class TestParseRecoveryChannel:
    def test_apiserver_write_declaration_parses(self):
        content = "---\nname: x\nrecovery_channel: apiserver-write\n---\nbody"
        assert parse_recovery_channel(content) == RECOVERY_CHANNEL_APISERVER_WRITE

    def test_value_is_normalised(self):
        content = "---\nrecovery_channel: ' ApiServer-Write '\n---\nbody"
        assert parse_recovery_channel(content) == RECOVERY_CHANNEL_APISERVER_WRITE

    def test_unknown_value_is_ignored(self):
        # A typo must not widen the CR channel's admission surface — the
        # gate falls back to the verb proxy exactly as if no declaration
        # existed (fail closed to the pre-declaration behaviour).
        content = "---\nrecovery_channel: apiserver-write-ish\n---\nbody"
        assert parse_recovery_channel(content) == ""

    def test_missing_key_or_frontmatter_yields_empty(self):
        assert parse_recovery_channel("# plain markdown") == ""
        assert parse_recovery_channel("---\nname: x\n---\nbody") == ""
        assert parse_recovery_channel("") == ""

    def test_malformed_yaml_yields_empty(self):
        assert parse_recovery_channel("---\n: : :\n  - [\n---\nbody") == ""


class TestLoadCaseRecoveryChannel:
    def test_load_reads_case_file_directly(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        (skill_dir / "cases" / "case.md").write_text(
            "---\nrecovery_channel: apiserver-write\n---\nbody",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_recovery_channel(
            "demo-skill", "cases/case.md",
        ) == RECOVERY_CHANNEL_APISERVER_WRITE

    def test_load_escapes_and_missing_inputs_are_empty(self, tmp_path, monkeypatch):
        # Same escape-proof resolver discipline as the manifest loader:
        # traversal lands in the loader rejection → "" → verb-proxy
        # fallback (never a routing input from LLM-influenced paths).
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_recovery_channel("demo-skill", "../../etc/passwd") == ""
        assert load_case_recovery_channel("", "cases/case.md") == ""
        assert load_case_recovery_channel("demo-skill", "") == ""
        assert load_case_recovery_channel("no-such-skill", "cases/case.md") == ""


# ---------------------------------------------------------------------------
# §2.1 Freeze golden + serialisation
# ---------------------------------------------------------------------------

class TestFreezeGolden:
    def test_no_manifest_freeze_has_no_key(self):
        snap = freeze_approved_target_from_spec(_victim_pod_spec())
        # Byte-identical contract: the key is absent, not empty.
        assert "mechanism_entries" not in snap
        # ...and the rest of the shape is untouched.
        assert snap["scope"] == "pod"
        assert snap["namespace"] == "default"
        assert snap["names"] == ["victim-pod"]

    def test_manifest_freeze_carries_entries(self):
        entries = parse_mechanism_writes(_NXDOMAIN_FRONTMATTER)
        snap = freeze_approved_target_from_spec(
            _victim_pod_spec(), mechanism_entries=entries,
        )
        assert "mechanism_entries" in snap
        hydrated = approved_from_dict(snap)
        assert hydrated.mechanism_entries == entries

    def test_serialisation_round_trip(self):
        entries = parse_mechanism_writes(_NXDOMAIN_FRONTMATTER)
        as_list = entries_to_list(entries)
        assert entries_from_list(as_list) == entries
        # Lenient hydration: junk rows are skipped, not fatal.
        assert entries_from_list([{"nope": 1}, *as_list]) == entries
        assert entries_from_list(None) == ()
        assert entries_from_list("junk") == ()

    def test_no_declaration_freeze_has_no_recovery_channel_key(self):
        # Byte-identical contract (task 3.6): every legacy snapshot stays
        # untouched — the key is absent, not empty, so the route gate's
        # fallback reads off key absence.
        snap = freeze_approved_target_from_spec(_victim_pod_spec())
        assert "recovery_channel" not in snap

    def test_declared_recovery_channel_round_trips(self):
        snap = freeze_approved_target_from_spec(
            _victim_pod_spec(),
            recovery_channel=RECOVERY_CHANNEL_APISERVER_WRITE,
        )
        assert snap["recovery_channel"] == RECOVERY_CHANNEL_APISERVER_WRITE
        hydrated = approved_from_dict(snap)
        assert hydrated.recovery_channel == RECOVERY_CHANNEL_APISERVER_WRITE

    def test_unknown_hydrated_recovery_channel_is_dropped(self):
        # A hand-edited state or a future renamed value must not smuggle
        # an unknown channel into the gate's declaration-first branch —
        # hydration re-validates against the known vocabulary (fail
        # closed to the verb proxy).
        snap = freeze_approved_target_from_spec(_victim_pod_spec())
        snap["recovery_channel"] = "totally-unknown-channel"
        assert approved_from_dict(snap).recovery_channel == ""


# ---------------------------------------------------------------------------
# §2.2 / §2.3 Guard branch
# ---------------------------------------------------------------------------

class TestDriftPolicyMechanismBranch:
    def setup_method(self):
        self.policy = K8sDriftPolicy()
        self.approved = _frozen_with_entries(
            parse_mechanism_writes(_NXDOMAIN_FRONTMATTER) + (
                MechanismWriteEntry(scope="node", namespace="", names=("worker-1",)),
            ),
        )

    def test_name_subset_passes(self):
        assert self.policy.check_identity_drift(
            self.approved, _eff("configmap", "kube-system", ["coredns-custom"]),
        ) is None

    def test_foreign_name_rejected_with_manifest_attribution(self):
        d = self.policy.check_identity_drift(
            self.approved, _eff("configmap", "kube-system", ["other-cm"]),
        )
        assert d is not None
        assert d.verdict.value == "reject_drift"
        # Attribution names both directions: case under-declaration vs
        # planner over-reach.
        assert "under-declares" in d.reason
        assert "over-reached" in d.reason
        assert "coredns-custom" in d.reason

    def test_prefix_hit_passes_and_prefix_miss_rejects(self):
        assert self.policy.check_identity_drift(
            self.approved, _eff("configmap", "kube-system", ["drill-nxdomain-01"]),
        ) is None
        d = self.policy.check_identity_drift(
            self.approved, _eff("configmap", "kube-system", ["evil-"]),
        )
        assert d is not None and "manifest" in d.reason

    def test_same_domain_entries_are_alternatives(self):
        # The NXDOMAIN shape: static + prefix entries SHARE the
        # configmap/kube-system domain. The authorization unit is the
        # OBJECT WRITE: a batch touching one name per entry writes only
        # legislated objects and passes.
        assert self.policy.check_identity_drift(
            self.approved,
            _eff("configmap", "kube-system", ["drill-nxdomain-01", "coredns-custom"]),
        ) is None
        # Mixed bag: one in-contract name + one foreign name → drift.
        d = self.policy.check_identity_drift(
            self.approved,
            _eff("configmap", "kube-system", ["drill-nxdomain-01", "other-cm"]),
        )
        assert d is not None and "manifest" in d.reason

    def test_cluster_scoped_node_entry(self):
        assert self.policy.check_identity_drift(
            self.approved, _eff("node", "", ["worker-1"]),
        ) is None
        d = self.policy.check_identity_drift(
            self.approved, _eff("node", "", ["worker-2"]),
        )
        assert d is not None and "manifest" in d.reason

    def test_victim_domain_untouched_by_entries(self):
        # Entry in the victim's own domain is inert: the victim comparison
        # still rules (a foreign pod name is drift even though a manifest
        # exists, and the victim pod itself passes).
        assert self.policy.check_identity_drift(
            self.approved, _eff("pod", "default", ["victim-pod"]),
        ) is None
        d = self.policy.check_identity_drift(
            self.approved, _eff("pod", "default", ["other-pod"]),
        )
        assert d is not None
        # Victim-domain rejection keeps the existing wording family —
        # it is resource-selection drift, not manifest attribution.
        assert "selection drift" in d.reason

    def test_empty_effective_names_fail_closed(self):
        # A call that pinned no name proves nothing — rejected.
        d = self.policy.check_identity_drift(
            self.approved, _eff("configmap", "kube-system", []),
        )
        assert d is not None

    def test_match_mechanism_entries_collects_domain(self):
        matched = match_mechanism_entries(
            self.approved, _eff("configmap", "kube-system", ["x"]),
        )
        assert len(matched) == 2  # static + prefix share the domain
        assert match_mechanism_entries(
            self.approved, _eff("secret", "kube-system", ["x"]),
        ) == ()

    def test_no_manifest_run_is_byte_identical(self):
        # With no entries the additive branch is a no-op: the call lands
        # on today's secondary-namespace path (workload secondary scopes
        # cover configmap) and keeps its wording, plus the manifest-missing
        # attribution suffix.
        approved = approved_from_dict(
            freeze_approved_target_from_spec(_victim_pod_spec()),
        )
        d = self.policy.check_identity_drift(
            approved, _eff("configmap", "kube-system", ["coredns-custom"]),
        )
        assert d is not None
        assert d.reason.startswith(
            "secondary namespace drift: approved=default effective=kube-system",
        )


class TestManifestMissingGuidance:
    def test_cross_domain_miss_without_manifest_gains_guidance(self):
        # No manifest + cross-domain write: rejected exactly as today,
        # plus the "manifest missing" attribution for drill-loop backfill.
        approved = approved_from_dict(
            freeze_approved_target_from_spec(_victim_pod_spec()),
        )
        policy = K8sDriftPolicy()
        d = policy.check_identity_drift(
            approved, _eff("configmap", "kube-system", ["coredns-custom"]),
        )
        assert d is not None
        assert "manifest missing" in d.reason

    def test_manifest_present_cross_domain_miss_keeps_plain_wording(self):
        # With a manifest, the secondary-namespace path keeps today's
        # plain wording (the additive branch owns the domain; the miss
        # here is a domain the manifest does not cover at all).
        entries = (MechanismWriteEntry(
            scope="secret", namespace="kube-system", names=("s1",),
        ),)
        approved = _frozen_with_entries(entries)
        policy = K8sDriftPolicy()
        d = policy.check_identity_drift(
            approved, _eff("configmap", "kube-system", ["coredns-custom"]),
        )
        assert d is not None
        assert "manifest missing" not in d.reason


# ---------------------------------------------------------------------------
# §3.2 Boundary predicate
# ---------------------------------------------------------------------------

class TestEntriesBeyondVictim:
    def setup_method(self):
        self.approved = _frozen_with_entries(
            parse_mechanism_writes(_NXDOMAIN_FRONTMATTER) + (
                MechanismWriteEntry(scope="node", namespace="", names=("worker-1",)),
            ),
        )

    def test_kube_system_entries_are_beyond(self):
        beyond = entries_beyond_victim(self.approved)
        assert [e.describe() for e in beyond] == [
            "configmap/kube-system: names=['coredns-custom']",
            "configmap/kube-system: name_prefix='drill-nxdomain-'",
        ]

    def test_cluster_scoped_secondary_entry_is_not_beyond(self):
        # node under a pod victim: the secondary net already passes node
        # writes today — the entry only tightens the guard, it grants no
        # NEW authority, so unattended runs keep established semantics.
        assert all(e.scope != "node" for e in entries_beyond_victim(self.approved))

    def test_no_manifest_is_never_beyond(self):
        approved = approved_from_dict(
            freeze_approved_target_from_spec(_victim_pod_spec()),
        )
        assert entries_beyond_victim(approved) == ()

    def test_payload_renders_entries_verbatim(self):
        beyond = entries_beyond_victim(self.approved)
        payload = format_entries_for_payload(beyond)
        assert payload[0]["scope"] == "configmap"
        assert payload[0]["namespace"] == "kube-system"
        assert payload[0]["names"] == ["coredns-custom"]
        assert payload[1]["name_prefix"] == "drill-nxdomain-"


class TestNamesWithinEntry:
    def test_static_subset(self):
        e = MechanismWriteEntry(scope="configmap", namespace="kube-system",
                                names=("a", "b"))
        assert names_within_entry(e, _eff("configmap", "kube-system", ["a"]))
        assert names_within_entry(e, _eff("configmap", "kube-system", ["a", "b"]))
        assert not names_within_entry(e, _eff("configmap", "kube-system", ["c"]))
        assert not names_within_entry(e, _eff("configmap", "kube-system", []))

    def test_prefix(self):
        e = MechanismWriteEntry(scope="configmap", namespace="kube-system",
                                name_prefix="drill-")
        assert names_within_entry(e, _eff("configmap", "kube-system", ["drill-x"]))
        assert not names_within_entry(e, _eff("configmap", "kube-system", ["x-drill"]))
        assert not names_within_entry(e, _eff("configmap", "kube-system", []))


# ---------------------------------------------------------------------------
# Line-4: the claim anchor's in-band half, DERIVED from the write-set
# ---------------------------------------------------------------------------

_PVC_CASE_FRONTMATTER = """---
name: Pod_Pending_PVC未绑定
mechanism_writes:
  - scope: persistentvolumeclaim
    namespace: default
    names: [app-data-claim]
---
# Case body — the #38 shape: the drill applies this PVC in-band.
"""


class TestDerivePvcClaimsFromWrites:
    """The in-band claim anchor comes from the WRITE-set, not a second
    hand-copied list.

    The #39 time-dimension gap closed by derivation: the PVC a
    #38-shaped case applies in-band must be in ``mechanism_writes``
    (the drift guard rejects the write otherwise), so the write entries
    ARE the time-proof half of the claims anchor — no ``pvc_claims``
    frontmatter block that can drift from the write it shadows.
    """

    def test_pvc_writes_contribute_names_non_pvc_do_not(self):
        from chaos_agent.agent.target_guard.mechanism_writes import (
            MechanismWriteEntry,
            derive_pvc_claims_from_writes,
        )
        entries = (
            MechanismWriteEntry(
                scope="pvc", namespace="default",
                names=("app-data-claim",), name_prefix="",
            ),
            MechanismWriteEntry(
                scope="configmap", namespace="kube-system",
                names=("coredns-custom",), name_prefix="",
            ),
        )
        assert derive_pvc_claims_from_writes(entries) == ("app-data-claim",)

    def test_names_dedup_across_pvc_entries(self):
        from chaos_agent.agent.target_guard.mechanism_writes import (
            MechanismWriteEntry,
            derive_pvc_claims_from_writes,
        )
        entries = (
            MechanismWriteEntry(
                scope="pvc", namespace="default",
                names=("data-pvc", "logs-pvc"), name_prefix="",
            ),
            MechanismWriteEntry(
                scope="pvc", namespace="default",
                names=("logs-pvc",), name_prefix="",
            ),
        )
        assert derive_pvc_claims_from_writes(entries) == ("data-pvc", "logs-pvc")

    def test_prefix_entry_contributes_nothing(self):
        # A prefix authorises writes by pattern; the anchor is
        # name-exact — the prefix shape contributes nothing (fail
        # closed).
        from chaos_agent.agent.target_guard.mechanism_writes import (
            MechanismWriteEntry,
            derive_pvc_claims_from_writes,
        )
        entries = (
            MechanismWriteEntry(
                scope="pvc", namespace="default",
                names=(), name_prefix="drill-pvc-",
            ),
        )
        assert derive_pvc_claims_from_writes(entries) == ()

    def test_no_pvc_writes_yield_empty(self):
        from chaos_agent.agent.target_guard.mechanism_writes import (
            derive_pvc_claims_from_writes,
        )
        entries = parse_mechanism_writes(_NXDOMAIN_FRONTMATTER)
        assert entries  # fixture sanity: it legislates two ConfigMaps
        assert derive_pvc_claims_from_writes(entries) == ()
        assert derive_pvc_claims_from_writes(()) == ()

    def test_end_to_end_case_pvc_write_freezes_claim(self, tmp_path, monkeypatch):
        """The #39 shape end-to-end: a case legislating the PVC it will
        apply in-band → load → derive → the anchor carries the claim
        BEFORE the PVC exists (the time-dimension gap, closed by the
        same manifest the human already approved)."""
        from chaos_agent.agent.target_guard.mechanism_writes import (
            derive_pvc_claims_from_writes,
            load_case_mechanism_writes,
        )
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        (skill_dir / "cases" / "case.md").write_text(
            _PVC_CASE_FRONTMATTER, encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        entries = load_case_mechanism_writes("demo-skill", "cases/case.md")
        # scope spelled persistentvolumeclaim in the case — the derived
        # set sees the canonical kind.
        assert derive_pvc_claims_from_writes(entries) == ("app-data-claim",)
