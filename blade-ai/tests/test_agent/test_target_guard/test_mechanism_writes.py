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
    NAME_FROM_VICTIM_NODE,
    NAMESPACE_FROM_VICTIM,
    NETWORK_MECHANISM_NETFILTER,
    NETWORK_MECHANISM_RESOLVER,
    NETWORK_MECHANISM_USERSPACE_BIND,
    NON_NETFILTER_NETWORK_MECHANISMS,
    RECOVERY_CHANNEL_APISERVER_WRITE,
    MechanismWriteEntry,
    entries_beyond_victim,
    entries_from_list,
    entries_to_list,
    format_entries_for_payload,
    load_case_mechanism_writes,
    load_case_network_mechanism,
    load_case_recovery_channel,
    load_case_deterministic_verdict,
    match_mechanism_entries,
    materialize_derived_entries,
    names_within_entries,
    names_within_entry,
    parse_mechanism_writes,
    parse_network_mechanism,
    parse_recovery_channel,
    parse_deterministic_verdict,
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
        # A′: resolve_skill_dir keys on the frontmatter ``name`` (mirroring
        # the registry, which only loads dirs carrying a valid SKILL.md), so
        # a realistic skill dir must declare its name in SKILL.md.
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\nbody",
            encoding="utf-8",
        )
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
        # A′: resolve_skill_dir keys on the frontmatter ``name`` (mirroring
        # the registry, which only loads dirs carrying a valid SKILL.md), so
        # a realistic skill dir must declare its name in SKILL.md.
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\nbody",
            encoding="utf-8",
        )
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
# network_mechanism — the case file's injection-mechanism legislation for
# ``fault_target == "network"`` faults (single source for the feasibility
# checker's netfilter-only-probe gate). Same one-directional chain and same
# escape-proof re-read discipline as recovery_channel.
# ---------------------------------------------------------------------------

class TestParseNetworkMechanism:
    def test_userspace_bind_declaration_parses(self):
        content = "---\nname: x\nnetwork_mechanism: userspace-bind\n---\nbody"
        assert parse_network_mechanism(content) == NETWORK_MECHANISM_USERSPACE_BIND

    def test_netfilter_and_resolver_declarations_parse(self):
        assert parse_network_mechanism(
            "---\nnetwork_mechanism: netfilter\n---\nbody",
        ) == NETWORK_MECHANISM_NETFILTER
        assert parse_network_mechanism(
            "---\nnetwork_mechanism: resolver\n---\nbody",
        ) == NETWORK_MECHANISM_RESOLVER

    def test_value_is_normalised(self):
        # Case + surrounding whitespace are stripped, mirroring
        # parse_recovery_channel: the frontmatter value is author-facing.
        content = "---\nnetwork_mechanism: ' UserSpace-Bind '\n---\nbody"
        assert parse_network_mechanism(content) == NETWORK_MECHANISM_USERSPACE_BIND

    def test_unknown_value_is_ignored(self):
        # A typo must NOT silently widen the gate into skipping netfilter
        # probes — unknown → "" → the checker FAILS SAFE to netfilter (runs
        # every probe, byte-identical to pre-declaration behaviour).
        content = "---\nnetwork_mechanism: userspace-bind-ish\n---\nbody"
        assert parse_network_mechanism(content) == ""

    def test_missing_key_or_frontmatter_yields_empty(self):
        assert parse_network_mechanism("# plain markdown") == ""
        assert parse_network_mechanism("---\nname: x\n---\nbody") == ""
        assert parse_network_mechanism("") == ""

    def test_malformed_yaml_yields_empty(self):
        assert parse_network_mechanism("---\n: : :\n  - [\n---\nbody") == ""

    def test_non_netfilter_set_is_single_source(self):
        # The gate predicate set must contain exactly the two mechanisms that
        # never touch netfilter; netfilter itself must NOT be a member (else
        # the gate would skip its own preconditions).
        assert NON_NETFILTER_NETWORK_MECHANISMS == frozenset({
            NETWORK_MECHANISM_USERSPACE_BIND, NETWORK_MECHANISM_RESOLVER,
        })
        assert NETWORK_MECHANISM_NETFILTER not in NON_NETFILTER_NETWORK_MECHANISMS


class TestLoadCaseNetworkMechanism:
    def test_load_reads_case_file_directly(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\nbody",
            encoding="utf-8",
        )
        (skill_dir / "cases" / "case.md").write_text(
            "---\nnetwork_mechanism: userspace-bind\n---\nbody",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_network_mechanism(
            "demo-skill", "cases/case.md",
        ) == NETWORK_MECHANISM_USERSPACE_BIND

    def test_load_escapes_and_missing_inputs_are_empty(self, tmp_path, monkeypatch):
        # Same escape-proof resolver discipline as recovery_channel: any
        # failure → "" → the checker fails safe to netfilter.
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_network_mechanism("demo-skill", "../../etc/passwd") == ""
        assert load_case_network_mechanism("", "cases/case.md") == ""
        assert load_case_network_mechanism("demo-skill", "") == ""
        assert load_case_network_mechanism("no-such-skill", "cases/case.md") == ""


# ---------------------------------------------------------------------------
# §1c deterministic_verdict — the case file's frozen verdict-binding
# declaration (openspec mechanism-invariant-trust-boundary, decision C)
# ---------------------------------------------------------------------------

_DV_FRONTMATTER = """---
name: StatefulSet_滚动异常_分区更新卡住
deterministic_verdict:
  target: statefulset
  action: partition-stall
  expected:
    Partition: 2
---
# Case body — prose the Agent reads, never an authorization input.
"""


class TestParseDeterministicVerdict:
    def test_full_declaration_parses(self):
        assert parse_deterministic_verdict(_DV_FRONTMATTER) == {
            "target": "statefulset",
            "action": "partition-stall",
            "expected": {"Partition": "2"},
        }

    def test_expected_values_are_stringified_and_blanks_dropped(self):
        content = (
            "---\ndeterministic_verdict:\n  target: statefulset\n"
            "  action: partition-stall\n  expected:\n    Partition: 2\n"
            "    Blank: ''\n    Zero: 0\n---\nbody"
        )
        parsed = parse_deterministic_verdict(content)
        # Zero is a legal value (kept as "0"); only blank/None are dropped.
        assert parsed["expected"] == {"Partition": "2", "Zero": "0"}

    def test_whitespace_target_action_normalised(self):
        content = (
            "---\ndeterministic_verdict:\n  target: ' statefulset '\n"
            "  action: ' partition-stall '\n---\nbody"
        )
        parsed = parse_deterministic_verdict(content)
        assert (parsed["target"], parsed["action"]) == (
            "statefulset", "partition-stall",
        )

    def test_missing_target_or_action_yields_empty(self):
        # Shape-only validation: a declaration without BOTH keys is inert —
        # the verify layer falls back to the LLM-triple path.
        assert parse_deterministic_verdict(
            "---\ndeterministic_verdict:\n  target: statefulset\n---\nbody",
        ) == {}
        assert parse_deterministic_verdict(
            "---\ndeterministic_verdict:\n  action: partition-stall\n---\nbody",
        ) == {}

    def test_non_mapping_value_yields_empty(self):
        assert parse_deterministic_verdict(
            "---\ndeterministic_verdict: statefulset\n---\nbody",
        ) == {}

    def test_missing_key_or_frontmatter_yields_empty(self):
        assert parse_deterministic_verdict("# plain markdown") == {}
        assert parse_deterministic_verdict("---\nname: x\n---\nbody") == {}
        assert parse_deterministic_verdict("") == {}

    def test_malformed_yaml_yields_empty(self):
        assert parse_deterministic_verdict("---\n: : :\n  - [\n---\nbody") == {}

    def test_unknown_rule_key_is_not_validated_here(self):
        # parse validates SHAPE only (no registry lookup — avoids an import
        # cycle); an unknown key simply resolves to no rule downstream.
        content = (
            "---\ndeterministic_verdict:\n  target: bogus-kind\n"
            "  action: bogus-action\n  expected:\n    Foo: 1\n---\nbody"
        )
        assert parse_deterministic_verdict(content) == {
            "target": "bogus-kind",
            "action": "bogus-action",
            "expected": {"Foo": "1"},
        }


class TestLoadCaseDeterministicVerdict:
    def test_load_reads_case_file_directly(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        # A′: resolve_skill_dir keys on the frontmatter ``name`` (mirroring
        # the registry, which only loads dirs carrying a valid SKILL.md), so
        # a realistic skill dir must declare its name in SKILL.md.
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\nbody",
            encoding="utf-8",
        )
        (skill_dir / "cases" / "case.md").write_text(
            _DV_FRONTMATTER, encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_deterministic_verdict(
            "demo-skill", "cases/case.md",
        ) == {
            "target": "statefulset",
            "action": "partition-stall",
            "expected": {"Partition": "2"},
        }

    def test_load_escapes_and_missing_inputs_are_empty(self, tmp_path, monkeypatch):
        # Same escape-proof resolver discipline as recovery_channel:
        # traversal / missing inputs degrade to {} → LLM-triple fallback.
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        assert load_case_deterministic_verdict("demo-skill", "../../etc/passwd") == {}
        assert load_case_deterministic_verdict("", "cases/case.md") == {}
        assert load_case_deterministic_verdict("demo-skill", "") == {}
        assert load_case_deterministic_verdict("no-such-skill", "cases/case.md") == {}


class TestFreezeDeterministicVerdict:
    def test_no_declaration_freeze_has_no_key(self):
        # Byte-identical contract: every legacy snapshot stays untouched —
        # the key is absent, not empty, so the verify fallback reads off
        # key absence.
        snap = freeze_approved_target_from_spec(_victim_pod_spec())
        assert "deterministic_verdict" not in snap

    def test_declared_verdict_round_trips(self):
        dv = {"target": "statefulset", "action": "partition-stall",
              "expected": {"Partition": "2"}}
        snap = freeze_approved_target_from_spec(
            _victim_pod_spec(), deterministic_verdict=dv,
        )
        assert snap["deterministic_verdict"] == dv
        assert approved_from_dict(snap).deterministic_verdict == dv

    def test_malformed_declaration_is_not_frozen(self):
        # Fail-safe: a partial declaration (missing target/action) never
        # lands in the snapshot — key stays absent.
        snap = freeze_approved_target_from_spec(
            _victim_pod_spec(),
            deterministic_verdict={"target": "", "action": "x"},
        )
        assert "deterministic_verdict" not in snap

    def test_hydrated_malformed_verdict_is_dropped(self):
        # A hand-edited state must not smuggle a partial declaration into
        # the verify layer — hydration re-validates the shape.
        snap = freeze_approved_target_from_spec(_victim_pod_spec())
        snap["deterministic_verdict"] = {"target": "statefulset"}
        assert approved_from_dict(snap).deterministic_verdict == {}
        snap["deterministic_verdict"] = "garbage"
        assert approved_from_dict(snap).deterministic_verdict == {}


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


class TestReadOnlyCarveOutW704:
    """W-70-4 (Case #70 Phase B): the secondary-namespace-drift rejection
    must state it covers the exec/write ONLY and that read-only observation
    of the drifted namespace is still cleared — so the model does not
    over-generalize one write refusal into "the whole namespace is off
    limits" and abandon collectable evidence (Phase B skipped the read-only
    ``kubectl logs -n ingress-nginx`` controller-log probe after an ``exec``
    into the controller pod was refused here; 70-R2 proves the logs passes).

    The carve-out lives in this branch's OWN reason, never in the shared
    ``_build_suggestion`` — nine rejection paths call it, so leaking there
    would spread a secondary-ns-only statement across every verb/rejection
    (b7cd803d: reason and suggestion must point at the same thing, per verb
    class).
    """

    def _drift(self, approved, eff_ns="kube-system"):
        return K8sDriftPolicy().check_identity_drift(
            approved, _eff("configmap", eff_ns, ["coredns-custom"]),
        )

    def test_no_manifest_reason_names_readonly_carve_out(self):
        approved = approved_from_dict(
            freeze_approved_target_from_spec(_victim_pod_spec()),
        )
        eff_ns = "kube-system"
        d = self._drift(approved, eff_ns)
        assert d is not None
        # Phrased as the SCOPE of this rejection, not a new allow rule.
        assert "this rejection covers the exec/write" in d.reason
        # ① consistency invariant: the carve-out names the SAME namespace as
        #    the drift (single-source interpolation of effective_ns), so it
        #    tracks the runtime value instead of a dead literal.
        assert f"read-only observation of {eff_ns}" in d.reason
        # No-manifest branch keeps the manifest-missing suffix alongside.
        assert "manifest missing" in d.reason

    def test_manifest_present_reason_also_carries_carve_out(self):
        # The carve-out is unconditional: read-only calls never reach this
        # drift check regardless of manifest, so both branches state it.
        entries = (MechanismWriteEntry(
            scope="secret", namespace="kube-system", names=("s1",),
        ),)
        d = self._drift(_frozen_with_entries(entries))
        assert d is not None
        assert "read-only observation of kube-system" in d.reason
        assert "manifest missing" not in d.reason

    def test_carve_out_never_leaks_into_shared_suggestion(self):
        # ② anti-leak: the shared _build_suggestion describes only the
        #    approved target; a secondary-ns-only carve-out placed there
        #    would leak cross-verb / cross-rejection.
        approved = approved_from_dict(
            freeze_approved_target_from_spec(_victim_pod_spec()),
        )
        d = self._drift(approved)
        assert d is not None
        assert "read-only observation" not in d.suggestion
        assert "this rejection covers" not in d.suggestion


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
        # A′: resolve_skill_dir keys on the frontmatter ``name`` (mirroring
        # the registry, which only loads dirs carrying a valid SKILL.md), so
        # a realistic skill dir must declare its name in SKILL.md.
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: demo\n---\nbody",
            encoding="utf-8",
        )
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


# ---------------------------------------------------------------------------
# W-56-1: the DERIVED node entry — ``name_from: victim_node``
#
# A node-host mechanism under a namespaced (pod) victim writes the node the
# victim runs on. That node name is runtime-derived, so ``names`` /
# ``name_prefix`` cannot express it (the structural blind spot behind #53
# verified / #56 drift_terminated on the SAME case). The case legislates the
# SEMANTIC; ``materialize_derived_entries`` + ``freeze.discover_victim_nodes``
# inject the concrete name at freeze time.
# ---------------------------------------------------------------------------

_DERIVED_NODE_FRONTMATTER = """---
name: Pod_Terminating_节点宕机kubelet失联
mechanism_writes:
  - scope: node
    name_from: victim_node
---
# Case body — the mechanism writes the victim pod's host node.
"""


class TestDerivedNodeParse:
    def test_name_from_entry_parses(self):
        entries = parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER)
        assert len(entries) == 1
        e = entries[0]
        assert e.scope == "node"
        assert e.namespace == ""  # cluster-scoped normalised away
        assert e.name_from == NAME_FROM_VICTIM_NODE
        assert e.names == ()  # not yet materialized
        assert e.name_prefix == ""

    def test_name_from_is_normalised_lowercase(self):
        content = "---\nmechanism_writes:\n  - scope: node\n    name_from: ' Victim_Node '\n---\nbody\n"
        entries = parse_mechanism_writes(content)
        assert len(entries) == 1
        assert entries[0].name_from == NAME_FROM_VICTIM_NODE

    def test_name_from_with_names_rejects_entry(self):
        # Three-way XOR: two selectors = ambiguous authority → dropped.
        content = "---\nmechanism_writes:\n  - scope: node\n    names: [n1]\n    name_from: victim_node\n---\nbody\n"
        assert parse_mechanism_writes(content) == ()

    def test_unknown_name_from_rejects_entry(self):
        # A typo must not silently widen (or vacuously satisfy) the write set.
        content = "---\nmechanism_writes:\n  - scope: node\n    name_from: victim_node_ish\n---\nbody\n"
        assert parse_mechanism_writes(content) == ()


class TestMaterializeDerivedEntries:
    def _derived(self):
        return parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER)

    def test_fills_names_from_discovered_nodes(self):
        out = materialize_derived_entries(
            self._derived(), victim_nodes=("cn-shanghai.25.209.71.148",),
        )
        assert len(out) == 1
        assert out[0].names == ("cn-shanghai.25.209.71.148",)
        assert out[0].scope == "node"
        # Provenance survives so the card can say WHY these names are here.
        assert out[0].name_from == NAME_FROM_VICTIM_NODE

    def test_multiple_victim_nodes_all_materialize(self):
        # discover_victim_nodes returns a sorted tuple; materialize preserves
        # the order it is given (the union of every node the victim spans).
        out = materialize_derived_entries(
            self._derived(), victim_nodes=("node-a", "node-b"),
        )
        assert out[0].names == ("node-a", "node-b")

    def test_empty_discovery_drops_entry_fail_closed(self):
        # Victim pod unscheduled / absent / query failed → NO node derived.
        # The entry is DROPPED, never frozen with empty names (an empty-names
        # entry would match startswith("") = ANY node — a fail-OPEN hole).
        assert materialize_derived_entries(self._derived(), victim_nodes=()) == ()

    def test_no_derived_entries_is_passthrough(self):
        # Byte-identical no-op for every manifest without a derived entry —
        # the same tuple object comes back, so freeze output is unchanged.
        entries = parse_mechanism_writes(_NXDOMAIN_FRONTMATTER)
        assert materialize_derived_entries(entries, victim_nodes=("n1",)) is entries

    def test_mixed_entries_materialize_only_derived(self):
        entries = parse_mechanism_writes(_NXDOMAIN_FRONTMATTER) + self._derived()
        out = materialize_derived_entries(entries, victim_nodes=("node-x",))
        # Two configmap entries pass through verbatim + one materialized node.
        assert [e.scope for e in out] == ["configmap", "configmap", "node"]
        assert out[0].names == ("coredns-custom",)
        assert out[2].names == ("node-x",)

    def test_rematerialize_keys_on_name_from(self):
        # A materialized entry still carries name_from, so a second pass
        # re-derives from the nodes it is given. Production calls this ONCE at
        # the safety_check freeze (confirmation_gate / tool_screener rehydrate
        # the frozen names via entries_from_list and never re-materialize), so
        # this documents that name_from — not empty names — is the derivation
        # key, and a re-run stays deterministic off the same discovery.
        once = materialize_derived_entries(self._derived(), victim_nodes=("node-x",))
        twice = materialize_derived_entries(once, victim_nodes=("node-x",))
        assert once == twice
        assert twice[0].names == ("node-x",)


class TestDerivedEntryFailClosedMatching:
    def test_unmaterialized_entry_authorises_nothing(self):
        # The guard-plug: a name_from entry with NEITHER names NOR prefix must
        # NOT fall through to ``startswith("")`` (vacuously True = fail open).
        unmaterialized = MechanismWriteEntry(
            scope="node", namespace="", name_from=NAME_FROM_VICTIM_NODE,
        )
        assert not names_within_entry(
            unmaterialized, EffectiveTarget(scope="node", namespace="", names=("any-node",)),
        )
        assert not names_within_entries(
            (unmaterialized,), EffectiveTarget(scope="node", namespace="", names=("any-node",)),
        )

    def test_materialized_entry_matches_its_node_only(self):
        materialized = MechanismWriteEntry(
            scope="node", namespace="", names=("node-x",),
            name_from=NAME_FROM_VICTIM_NODE,
        )
        assert names_within_entry(
            materialized, EffectiveTarget(scope="node", namespace="", names=("node-x",)),
        )
        assert not names_within_entry(
            materialized, EffectiveTarget(scope="node", namespace="", names=("node-evil",)),
        )


class TestDerivedNodeDriftPolicy:
    """The #56 regression: a blade node fault under a POD victim is rejected
    at drift_policy's cluster-scoped+fault_target branch UNLESS a materialized
    derived node entry authorises it (branch 3.6, which fires first)."""

    def setup_method(self):
        self.policy = K8sDriftPolicy()
        self.node_write = EffectiveTarget(
            scope="node", namespace="", names=("cn-shanghai.25.209.71.148",),
            fault_target="network",
        )

    def test_blade_node_write_without_entry_is_drift(self):
        approved = _frozen_with_entries(())
        d = self.policy.check_identity_drift(approved, self.node_write)
        assert d is not None
        assert d.verdict.value == "reject_drift"

    def test_blade_node_write_with_materialized_entry_passes(self):
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER),
            victim_nodes=("cn-shanghai.25.209.71.148",),
        )
        approved = _frozen_with_entries(materialized)
        assert self.policy.check_identity_drift(approved, self.node_write) is None

    def test_other_node_stays_drift_even_with_entry(self):
        # Name-level precision: the derived entry pins the VICTIM's node; a
        # different node is still drift (in-zone passes, another node doesn't).
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER),
            victim_nodes=("cn-shanghai.25.209.71.148",),
        )
        approved = _frozen_with_entries(materialized)
        d = self.policy.check_identity_drift(
            approved,
            EffectiveTarget(scope="node", namespace="", names=("some-other-node",),
                            fault_target="network"),
        )
        assert d is not None

    def test_derived_node_entry_is_beyond_victim(self):
        # A DERIVED (name_from) node entry IS beyond the victim even though
        # node sits in the pod victim's secondary net: the net does NOT pass
        # a blade fault_target write on a cluster-scoped kind (drift_policy
        # rejects it — see test_blade_node_write_without_entry_is_drift), so
        # the entry is the SOLE authority that unlocks the node fault. That
        # is GRANTED authority, not tightening, so it must surface on the
        # interactive card and in the unattended auto_approved audit event
        # like any widened contract. (Surfacing does NOT gate/pause unattended
        # runs — AUTO delegation still approves; see _write_set_boundary.)
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER),
            victim_nodes=("node-x",),
        )
        approved = _frozen_with_entries(materialized)
        beyond = entries_beyond_victim(approved)
        assert any(e.scope == "node" and e.name_from for e in beyond)


class TestDerivedNodeSerialisation:
    def test_round_trip_preserves_name_from_and_names(self):
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER),
            victim_nodes=("node-x",),
        )
        assert entries_from_list(entries_to_list(materialized)) == materialized
        assert entries_from_list(entries_to_list(materialized))[0].name_from == (
            NAME_FROM_VICTIM_NODE
        )

    def test_freeze_hydrates_materialized_entry(self):
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER),
            victim_nodes=("node-x",),
        )
        approved = _frozen_with_entries(materialized)
        assert approved.mechanism_entries == materialized

    def test_unmaterialized_entry_dropped_on_hydration(self):
        # Defence in depth: even if an unmaterialized name_from entry reached
        # the snapshot, hydration drops it (no names/prefix) — fail closed.
        raw = [{"scope": "node", "namespace": "", "names": [],
                "name_prefix": "", "name_from": NAME_FROM_VICTIM_NODE}]
        assert entries_from_list(raw) == ()

    def test_payload_description_shows_derivation(self):
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER),
            victim_nodes=("node-x",),
        )
        payload = format_entries_for_payload(materialized)
        assert payload[0]["name_from"] == NAME_FROM_VICTIM_NODE
        assert "derived from victim_node" in payload[0]["description"]


# ---------------------------------------------------------------------------
# §5 namespace_from: victim — the DERIVED-NAMESPACE axis (#65 NetworkPolicy误配)
#
# A victim-scoped NetworkPolicy must live in the VICTIM's namespace to select
# it, and networkpolicy is NOT in a pod victim's secondary_scopes — so without
# a mechanism entry the apply is rejected at drift_policy step 4 (scope drift:
# approved=pod effective=networkpolicy) and never reaches the execute-phase
# armed gate. Unlike NXDOMAIN (configmap in a FIXED kube-system) or an unbound
# PVC (default), the netpol's namespace tracks the victim's runtime location,
# which a portable case cannot hardcode. ``namespace_from: victim`` legislates
# the SEMANTIC; materialize_derived_entries injects spec.namespace at freeze —
# NO cluster query (the victim's ns is already known), unlike name_from's
# discover_victim_nodes.
# ---------------------------------------------------------------------------

_DERIVED_NETPOL_FRONTMATTER = """---
name: Pod_网络故障_NetworkPolicy误配
mechanism_writes:
  - scope: networkpolicy
    namespace_from: victim
    name_prefix: drill-netpol-
---
# Case body — the mechanism applies a deny-all NetworkPolicy in the victim's
# own namespace (it must live there to select the victim pod).
"""


def _victim_pod_spec_ns(namespace: str) -> FaultSpec:
    return FaultSpec(
        scope="pod", namespace=namespace, names=["victim-pod"],
        fault_target="network", fault_action="loss",
    )


def _frozen_ns(entries, namespace="default"):
    return approved_from_dict(
        freeze_approved_target_from_spec(
            _victim_pod_spec_ns(namespace), mechanism_entries=entries,
        )
    )


def _netpol_eff(ns="default", name="drill-netpol-deny-x"):
    return EffectiveTarget(scope="networkpolicy", namespace=ns, names=(name,))


class TestParseNamespaceFrom:
    def test_namespace_from_victim_parses(self):
        entries = parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER)
        assert len(entries) == 1
        e = entries[0]
        assert e.scope == "networkpolicy"
        assert e.namespace == ""          # materialized at freeze, not parse
        assert e.namespace_from == NAMESPACE_FROM_VICTIM
        assert e.name_prefix == "drill-netpol-"

    def test_namespace_and_namespace_from_are_mutually_exclusive(self):
        content = ("---\nmechanism_writes:\n  - scope: networkpolicy\n"
                   "    namespace: kube-system\n    namespace_from: victim\n"
                   "    name_prefix: drill-\n---\nbody\n")
        assert parse_mechanism_writes(content) == ()

    def test_unknown_namespace_from_rejects_entry(self):
        content = ("---\nmechanism_writes:\n  - scope: networkpolicy\n"
                   "    namespace_from: victim_ish\n    name_prefix: drill-\n"
                   "---\nbody\n")
        assert parse_mechanism_writes(content) == ()

    def test_namespace_from_on_cluster_scoped_rejects_entry(self):
        # A cluster-scoped kind has no namespace to derive.
        content = ("---\nmechanism_writes:\n  - scope: node\n"
                   "    namespace_from: victim\n    names: [n1]\n---\nbody\n")
        assert parse_mechanism_writes(content) == ()

    def test_namespace_from_still_requires_a_name_selector(self):
        # namespace_from is orthogonal to the name-selector XOR: an entry with
        # a derived ns but NO names/name_prefix/name_from authorises nothing.
        content = ("---\nmechanism_writes:\n  - scope: networkpolicy\n"
                   "    namespace_from: victim\n---\nbody\n")
        assert parse_mechanism_writes(content) == ()


class TestMaterializeNamespaceFrom:
    def _derived(self):
        return parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER)

    def test_fills_namespace_from_victim(self):
        out = materialize_derived_entries(
            self._derived(), victim_namespace="prod-team-a",
        )
        assert len(out) == 1
        assert out[0].namespace == "prod-team-a"
        assert out[0].name_prefix == "drill-netpol-"   # name axis untouched
        # Provenance survives so the card can say WHY this ns is here.
        assert out[0].namespace_from == NAMESPACE_FROM_VICTIM

    def test_empty_victim_namespace_drops_entry_fail_closed(self):
        # A cluster-scoped victim (no ns) or a hand-built entry → nothing to
        # derive. DROP, never freeze an empty-ns entry.
        assert materialize_derived_entries(
            self._derived(), victim_namespace="",
        ) == ()

    def test_no_derived_entries_is_passthrough(self):
        entries = parse_mechanism_writes(_NXDOMAIN_FRONTMATTER)
        assert materialize_derived_entries(
            entries, victim_namespace="prod-team-a",
        ) is entries

    def test_both_axes_materialize_independently(self):
        # A manifest may derive a name (node) AND a namespace (netpol); each
        # entry materializes on its own axis, input order preserved.
        entries = (
            parse_mechanism_writes(_DERIVED_NODE_FRONTMATTER) + self._derived()
        )
        out = materialize_derived_entries(
            entries, victim_nodes=("node-x",), victim_namespace="prod-team-a",
        )
        assert [e.scope for e in out] == ["node", "networkpolicy"]
        assert out[0].names == ("node-x",)
        assert out[1].namespace == "prod-team-a"

    def test_rematerialize_is_deterministic(self):
        once = materialize_derived_entries(
            self._derived(), victim_namespace="prod-team-a",
        )
        twice = materialize_derived_entries(
            once, victim_namespace="prod-team-a",
        )
        assert once == twice


class TestNamespaceFromBeyondVictim:
    """A DERIVED (``namespace_from``) entry never takes the secondary-net
    exemption, even when its kind sits in the victim's ``secondary_scopes``
    AND its materialized ns equals ``secondary_namespace``. This pins the
    generalized ``not _declared_axes(entry)`` guard in ``entries_beyond_victim``:
    the pre-III-b form checked only ``not entry.name_from``, which would have
    silently EXEMPTED a ``namespace_from`` entry here (empirically confirmed:
    OLD -> ``[]``, NEW -> surfaced). The generalization is fail-safe — it only
    surfaces MORE on the visibility/audit surface, never blocks a run or grants
    new write authority — and treats every derived axis uniformly, so a future
    axis inherits the same visibility by construction.
    """

    _CM_DERIVED = ("---\nmechanism_writes:\n  - scope: configmap\n"
                   "    namespace_from: victim\n    name_prefix: drill-probe-\n"
                   "---\nbody\n")

    def test_derived_ns_entry_in_secondary_scope_is_beyond_victim(self):
        # ``configmap`` IS in a pod victim's secondary_scopes, and the derived
        # ns materializes to the victim's ns == secondary_namespace — so a
        # STATIC entry in this exact domain would be exempt. A DERIVED one must
        # stay visible.
        materialized = materialize_derived_entries(
            parse_mechanism_writes(self._CM_DERIVED), victim_namespace="default",
        )
        approved = _frozen_ns(materialized, namespace="default")
        frozen = approved.mechanism_entries[0]
        # Preconditions: the entry really sits in the secondary net with a
        # matching ns (otherwise the exemption branch is never reached and the
        # test would prove nothing).
        assert frozen.scope in set(approved.secondary_scopes or ())
        assert frozen.namespace == (approved.secondary_namespace or "default")
        assert frozen.namespace_from == NAMESPACE_FROM_VICTIM
        beyond = entries_beyond_victim(approved)
        assert any(e.namespace_from == NAMESPACE_FROM_VICTIM for e in beyond), (
            "a namespace_from entry in a secondary scope must stay beyond-victim"
        )

    def test_static_entry_in_same_secondary_scope_is_exempt(self):
        # Contrast: the SAME configmap/default domain as a STATIC entry (no
        # derivation) IS covered by the secondary net -> NOT beyond victim.
        static = (MechanismWriteEntry(
            scope="configmap", namespace="default", name_prefix="drill-probe-",
        ),)
        approved = _frozen_ns(static, namespace="default")
        beyond = entries_beyond_victim(approved)
        assert not any(
            e.scope == "configmap" and e.namespace == "default" for e in beyond
        ), "a static secondary-scope entry must take the secondary-net exemption"


class TestNamespaceFromFailClosedMatching:
    def test_unmaterialized_namespace_entry_is_inert(self):
        # The fail-open pitfall guard: an entry whose namespace_from never
        # materialized carries namespace="" — it must NOT match any write
        # (match requires entry.namespace == effective ns, and "" never equals
        # a real ns), so it authorises NOTHING rather than falling open.
        unmaterialized = MechanismWriteEntry(
            scope="networkpolicy", namespace="", name_prefix="drill-netpol-",
            namespace_from=NAMESPACE_FROM_VICTIM,
        )
        approved = _frozen_ns((unmaterialized,), namespace="prod-team-a")
        assert match_mechanism_entries(approved, _netpol_eff("prod-team-a")) == ()
        d = K8sDriftPolicy().check_identity_drift(
            approved, _netpol_eff("prod-team-a"),
        )
        assert d is not None and d.verdict.value == "reject_drift"


class TestNetpolDriftPolicy:
    """The #65 regression: a victim-scoped NetworkPolicy apply under a POD
    victim is rejected at drift_policy step 4 (networkpolicy is not in the pod
    secondary net) UNLESS a materialized ``namespace_from: victim`` entry
    authorises it (branch 3.6, which fires first)."""

    def setup_method(self):
        self.policy = K8sDriftPolicy()

    def test_netpol_apply_without_entry_is_drift(self):
        # [i6-0 scenario 1] — the injection never reaches the armed gate.
        approved = _frozen_ns((), namespace="default")
        d = self.policy.check_identity_drift(approved, _netpol_eff("default"))
        assert d is not None
        assert d.verdict.value == "reject_drift"
        assert "approved=pod effective=networkpolicy" in d.reason

    def test_netpol_apply_with_materialized_entry_passes(self):
        # [i6-0 scenario 2] — the entry (ns matched) authorises the write.
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER),
            victim_namespace="default",
        )
        approved = _frozen_ns(materialized, namespace="default")
        assert self.policy.check_identity_drift(
            approved, _netpol_eff("default"),
        ) is None

    def test_entry_tracks_a_non_default_victim_namespace(self):
        # THE i6-A WIN over i6-B: victim in prod-team-a → the derived entry
        # materializes to prod-team-a and authorises the netpol there.
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER),
            victim_namespace="prod-team-a",
        )
        approved = _frozen_ns(materialized, namespace="prod-team-a")
        assert materialized[0].namespace == "prod-team-a"
        assert self.policy.check_identity_drift(
            approved, _netpol_eff("prod-team-a"),
        ) is None

    def test_static_default_entry_fails_a_non_default_victim(self):
        # [i6-0 scenario 4] — the i6-B dead end locked as a regression: a
        # STATIC ``namespace: default`` entry cannot track a prod-team-a
        # victim, so the netpol apply in prod-team-a is rejected.
        static = (MechanismWriteEntry(
            scope="networkpolicy", namespace="default",
            name_prefix="drill-netpol-",
        ),)
        approved = _frozen_ns(static, namespace="prod-team-a")
        d = self.policy.check_identity_drift(
            approved, _netpol_eff("prod-team-a"),
        )
        assert d is not None and d.verdict.value == "reject_drift"

    def test_foreign_prefix_stays_drift_even_with_entry(self):
        # Name-level precision: the prefix still governs — a netpol NOT under
        # drill-netpol- is drift even in the right namespace.
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER),
            victim_namespace="default",
        )
        approved = _frozen_ns(materialized, namespace="default")
        d = self.policy.check_identity_drift(
            approved, _netpol_eff("default", name="evil-policy"),
        )
        assert d is not None

    def test_netpol_entry_is_beyond_victim(self):
        # networkpolicy is NOT in a pod victim's secondary net, so the entry
        # grants new authority and must surface on the interactive card +
        # the unattended auto_approved audit event (visibility, not gating).
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER),
            victim_namespace="default",
        )
        approved = _frozen_ns(materialized, namespace="default")
        beyond = entries_beyond_victim(approved)
        assert any(e.scope == "networkpolicy" for e in beyond)


class TestNamespaceFromSerialisation:
    def test_round_trip_preserves_namespace_from_and_namespace(self):
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER),
            victim_namespace="prod-team-a",
        )
        assert entries_from_list(entries_to_list(materialized)) == materialized
        rt = entries_from_list(entries_to_list(materialized))[0]
        assert rt.namespace == "prod-team-a"
        assert rt.namespace_from == NAMESPACE_FROM_VICTIM

    def test_freeze_hydrates_materialized_entry(self):
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER),
            victim_namespace="prod-team-a",
        )
        approved = _frozen_ns(materialized, namespace="prod-team-a")
        assert approved.mechanism_entries == materialized

    def test_payload_description_shows_derived_namespace(self):
        materialized = materialize_derived_entries(
            parse_mechanism_writes(_DERIVED_NETPOL_FRONTMATTER),
            victim_namespace="prod-team-a",
        )
        payload = format_entries_for_payload(materialized)
        assert payload[0]["namespace"] == "prod-team-a"
        assert payload[0]["namespace_from"] == NAMESPACE_FROM_VICTIM
        assert "derived from victim" in payload[0]["description"]
