"""Vehicle kind-map sentinel (B76-R13 / O-4, P4; P5 landed, P3 landed).

The teardown exemption's DATA SOURCE, not its consumers: the predicate
``execution_artifacts.is_vehicle_teardown_delete`` reads each registered
vehicle's main-asset kind from the RECORDED ``kind`` field — P5 retired
the old suffix convention (``endswith("_deployment")`` → deployment,
else → pod) to a HYDRATION FALLBACK for artifacts persisted before the
field existed, and every registration constructor writes the field now.
The screener carries a SECOND, independent derivation (hardcoded
``"occupant_deployment"`` string membership), the type set
``VEHICLE_ARTIFACT_TYPES`` is a third evolution point, and the
constructor's kind→type whitelist a fourth.

FOUR places must stay in sync when a new vehicle type lands. Nothing guarded
that (the R11 vocabulary sentinel watches CONSUMERS of the write-verb
vocabulary — not the artifact schema feeding the exemption), so a future
``occupant_statefulset`` added to the set + constructor would previously
fall through the suffix derivation to ``pod``: a ``delete statefulset``
teardown would NOT be exempted (ghost attribution — the six-door disease
family recurring wholesale for the new kind), while a same-named ``delete
pod`` WOULD be (wrong-direction exemption). The kind-map below turns the
agreement into structure: an unregistered new member fails here BEFORE
the exemption silently narrows.

P3/P5 LANDED (retirement fulfilled): kind is recorded at every
registration constructor and the predicate reads the field first (suffix
= legacy fallback only — guarded below), and matcher threading (P3)
moved the teardown≠mutation exemption into the vocabulary layer itself
(``make_teardown_matcher`` threading, see
tests/test_agent/test_teardown_vocab_sentinel.py). This map now guards
the RECORDED field's expected value per type and the residue of
independent derivations (the screener's hardcoded strings).
"""

import ast
from pathlib import Path

from chaos_agent.agent.execution_artifacts import (
    VEHICLE_ARTIFACT_TYPES,
    is_vehicle_teardown_delete,
)

#: Explicit {artifact type: expected main-asset kind} registry. Every member
#: of ``VEHICLE_ARTIFACT_TYPES`` MUST be registered here.
#:
#: FOUR-PLACE SYNC DUTY when adding a vehicle type — each place below must
#: be updated together, and this sentinel goes red if any drifts:
#:   1. ``VEHICLE_ARTIFACT_TYPES`` (execution_artifacts.py) — the set;
#:   2. the ``kind`` field written by EVERY registration constructor
#:      (``_debug_pod_artifact`` / ``_drill_vehicle_artifact`` / the
#:      screener's recovery-carrier and drill-target registrations) — the
#:      recorded field is authoritative since P5; the suffix derivation
#:      inside ``is_vehicle_teardown_delete`` survives ONLY as the
#:      hydration fallback for pre-P5 artifacts;
#:   3. the screener's hardcoded type strings (tool_screener.py — e.g. the
#:      ``"occupant_deployment" in vehicle_artifact_types(...)`` checks);
#:   4. the constructor ``_drill_vehicle_artifact``'s kind→type whitelist
#:      (and the recovery-carrier / debug-pod constructors if applicable).
VEHICLE_KIND_MAP: dict[str, str] = {
    "debug_pod": "pod",
    "occupant_pod": "pod",
    "occupant_deployment": "deployment",
    "recovery_carrier": "pod",
}

#: The src tree the constructor-completeness tooth scans.
_AGENT_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "chaos_agent" / "agent"


class _Effective:
    """Duck-typed classifier view of a ``kubectl delete`` (scope/namespace/names)."""

    def __init__(self, scope: str, namespace: str, names: set[str]):
        self.scope = scope
        self.namespace = namespace
        self.names = names


def _minimal_artifact(artifact_type: str, *, kind: str | None = None) -> dict:
    """Smallest registration that exercises the predicate's main-asset path.

    ``kind`` simulates the P5 recorded field: ``None`` leaves the field
    ABSENT (the pre-P5 legacy shape — exercises the suffix fallback); a
    value records it (the shape every constructor writes since P5).
    ``status="cleaned"`` keeps the status-irrelevance semantics visible (a
    cleaned registration still names deletable machinery); an empty
    ``rbac_family`` isolates the main-asset derivation under test.
    """
    artifact = {
        "artifact_id": f"{artifact_type}:ns/drill-v-x",
        "type": artifact_type,
        "status": "cleaned",
        "name": "drill-v-x",
        "namespace": "ns",
        "rbac_family": [],
    }
    if kind is not None:
        artifact["kind"] = kind
    return artifact


class TestVehicleKindMap:
    """The structural invariant: every vehicle type has a correct kind mapping."""

    def test_every_vehicle_type_is_registered(self):
        """完备性牙：``VEHICLE_ARTIFACT_TYPES`` 的每个成员都必须在
        VEHICLE_KIND_MAP 登记 expected kind。新 vehicle type 加入集合而未
        登记映射 ⇒ 本牙红——先于 teardown 豁免对该 kind 静默收窄（六扇门
        病家族对新 kind 整体复发）。"""
        unregistered = sorted(set(VEHICLE_ARTIFACT_TYPES) - set(VEHICLE_KIND_MAP))
        assert not unregistered, (
            "vehicle artifact types without a VEHICLE_KIND_MAP entry: "
            f"{unregistered}. Register each new type's expected main-asset "
            "kind in VEHICLE_KIND_MAP (tests/test_agent/"
            "test_vehicle_kind_sentinel.py) and check the FOUR-PLACE SYNC "
            "DUTY in the map's comment — the ``kind`` field at every "
            "registration constructor, the screener's hardcoded type "
            "strings, and the constructor's kind→type whitelist must all "
            "agree, or the teardown exemption silently narrows for the new "
            "kind."
        )

    def test_registered_kind_matches_recorded_field(self):
        """记录一致性牙（主咬合面，P5 形态）：登记的 expected kind 必须与
        RECORDED kind 字段的判定一致——对每个 (type, kind)：匹配 kind 的
        delete 被豁免（正向），错配 kind 的 delete 不被豁免（负向）。防
        「构造点写了 kind 却与 VEHICLE_KIND_MAP 登记漂移」——如
        occupant_statefulset 构造点写 kind=pod 而登记为 statefulset（正向
        红：delete statefulset 不豁免=幽灵归因；负向红：delete pod 反而被
        豁免=错误方向豁免）。"""
        for artifact_type, expected_kind in sorted(VEHICLE_KIND_MAP.items()):
            artifact = _minimal_artifact(artifact_type, kind=expected_kind)
            # Positive: a delete of the REGISTERED kind hits the main asset.
            assert is_vehicle_teardown_delete(
                _Effective(expected_kind, "ns", {"drill-v-x"}), [artifact]
            ) is True, (
                f"{artifact_type}: a delete of the registered kind "
                f"'{expected_kind}' is NOT exempted despite the RECORDED "
                "kind field matching — the predicate ignores the recorded "
                "field (P5 regression: see the FOUR-PLACE SYNC DUTY — the "
                "constructor writes 'kind', this map registers the "
                "expected value; they must agree)."
            )
            # Negative: a delete of any OTHER kind must not be exempted.
            other = "pod" if expected_kind != "pod" else "deployment"
            assert is_vehicle_teardown_delete(
                _Effective(other, "ns", {"drill-v-x"}), [artifact]
            ) is False, (
                f"{artifact_type}: a delete of kind '{other}' IS exempted "
                f"while the recorded kind is '{expected_kind}' — the "
                "predicate is wrong-direction (exempting a kind the "
                "registration never named)."
            )

    def test_recorded_kind_overrides_suffix_fallback(self):
        """记录权威牙（P5 退役兑现）：kind 字段一旦记录即权威——即使与
        后缀推导相悖。occupant_pod 的后缀推导说 pod，若记录
        kind=deployment，则 delete deployment 被豁免、delete pod 不被豁免。
        防「谓词回退为纯后缀推导而忽略记录字段」——那会让构造点的 kind
        写入变成死数据，P5 退役承诺被悄悄撕毁。"""
        artifact = _minimal_artifact("occupant_pod", kind="deployment")
        assert is_vehicle_teardown_delete(
            _Effective("deployment", "ns", {"drill-v-x"}), [artifact]
        ) is True, (
            "the RECORDED kind ('deployment') must override the suffix "
            "derivation ('occupant_pod' → pod) — the predicate fell back "
            "to the suffix despite a recorded kind field (P5 regression)."
        )
        assert is_vehicle_teardown_delete(
            _Effective("pod", "ns", {"drill-v-x"}), [artifact]
        ) is False, (
            "the suffix-derived kind ('pod') must NOT exempt when the "
            "recorded kind says 'deployment' — the fallback outranked the "
            "authoritative field (wrong-direction exemption)."
        )

    def test_suffix_fallback_hydrates_legacy_artifacts(self):
        """后备牙（P5 水合路径）：无 kind 字段的 pre-P5 历史 artifact 走
        后缀推导——occupant_deployment → deployment，其余 → pod。后备必须
        保持与 VEHICLE_KIND_MAP 逐条一致（历史 artifact 的豁免不因字段
        缺失而丢失，也不因后备漂移而错误方向）。"""
        for artifact_type, expected_kind in sorted(VEHICLE_KIND_MAP.items()):
            artifact = _minimal_artifact(artifact_type)  # no kind field
            assert is_vehicle_teardown_delete(
                _Effective(expected_kind, "ns", {"drill-v-x"}), [artifact]
            ) is True, (
                f"{artifact_type}: a kind-less (pre-P5) artifact's suffix "
                f"fallback fails to exempt a delete of the expected kind "
                f"'{expected_kind}' — the fallback drifted from "
                "VEHICLE_KIND_MAP (legacy artifacts lose their exemption)."
            )
            other = "pod" if expected_kind != "pod" else "deployment"
            assert is_vehicle_teardown_delete(
                _Effective(other, "ns", {"drill-v-x"}), [artifact]
            ) is False, (
                f"{artifact_type}: a kind-less (pre-P5) artifact's suffix "
                f"fallback exempts kind '{other}' while the map expects "
                f"'{expected_kind}' — the fallback is wrong-direction."
            )

    def test_every_vehicle_type_literal_records_kind(self):
        """构造点完备性牙（P5）：``src/chaos_agent/agent`` 树内任何含
        ``"type": <vehicle type>`` 常量的 dict 字面量必须同时含 ``"kind"``
        key——新构造点忘写 kind 字段 ⇒ 本牙红（构造退化为隐性依赖后缀
        推导，P5 的记录化承诺被悄悄违反）。变量形式的 type（如
        ``_drill_vehicle_artifact`` 的 ``artifact_type``）无法静态判定，
        由记录一致性牙的行为断言覆盖。"""
        violations: list[str] = []
        for py in sorted(_AGENT_SRC_ROOT.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                type_value = None
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "type"
                        and isinstance(value, ast.Constant)
                        and value.value in VEHICLE_ARTIFACT_TYPES
                    ):
                        type_value = value.value
                        break
                if type_value is None:
                    continue
                if not any(
                    isinstance(k, ast.Constant) and k.value == "kind"
                    for k in node.keys
                ):
                    violations.append(
                        f"{py.relative_to(_AGENT_SRC_ROOT.parent.parent.parent)}: "
                        f"{type_value}"
                    )
        assert not violations, (
            "vehicle-artifact dict literals missing the 'kind' key (P5: "
            "every constructor records kind; the suffix derivation is a "
            "legacy-artifact fallback ONLY): "
            f"{violations}. Add the 'kind' field at each construction site "
            "— a new constructor that skips it silently re-couples the "
            "exemption to the suffix convention."
        )

    def test_map_has_no_stale_entries(self):
        """活性牙：映射表不得有条目对应不存在的 vehicle type——type 被删
        /改名后残留映射会掩盖新成员的登记缺口（同 R11 白名单活性纪律）。"""
        stale = sorted(set(VEHICLE_KIND_MAP) - set(VEHICLE_ARTIFACT_TYPES))
        assert not stale, (
            "stale VEHICLE_KIND_MAP entries (artifact type gone — remove "
            f"the entry or update the type name): {stale}"
        )


# ---------------------------------------------------------------------------
# Namespace-topology single-source teeth (R21/G-5): the third match
# dimension — WHICH KINDS LIVE OUTSIDE ANY NAMESPACE — was an inline
# set copied FOUR times across the agent tree (drift_policy's
# CLUSTER_SCOPED_KINDS, the k8s-native classifier's inline tuple, the
# carrier family's two-kind subset, and the rbac-create kinds map).
# Each copy evolved alone; the carrier-family copy drifted from the
# predicate's ns comparison into G-5's self-referential contradiction.
# The topology rule now lives ONCE in the target-guard classifier
# (``is_cluster_scoped_kind``); these teeth pin the single source and
# the DERIVED (never hand-listed) carrier-family subset.
# ---------------------------------------------------------------------------


class TestNamespaceTopologySingleSource:
    def test_classifier_exposes_topology_primitive(self):
        """单源存在性牙：``is_cluster_scoped_kind`` 从 classifier 导出且
        判定正确——agent 域内任何「此 kind 是否 cluster-scoped」判定都
        必须引用它，不再各自维护内联集合。"""
        from chaos_agent.agent.target_guard.classifier import (
            is_cluster_scoped_kind,
        )

        assert is_cluster_scoped_kind("clusterrole") is True
        assert is_cluster_scoped_kind("clusterrolebinding") is True
        assert is_cluster_scoped_kind("node") is True
        assert is_cluster_scoped_kind("role") is False
        assert is_cluster_scoped_kind("serviceaccount") is False
        assert is_cluster_scoped_kind("pod") is False
        assert is_cluster_scoped_kind("") is False

    def test_carrier_family_cluster_set_is_derived(self):
        """派生一致性牙：载体家族的 cluster-scoped 子集必须与
        ``_RECOVERY_CARRIER_CREATE_KINDS`` × 拓扑单源的派生结果逐 kind
        一致——未来向 create kinds 加入 cluster-scoped kind（如某个
        cluster-scoped 变体）时，手写集合会漏而派生自动覆盖。"""
        from chaos_agent.agent.execution_artifacts import (
            _RECOVERY_CARRIER_CLUSTER_SCOPED_KINDS,
            _RECOVERY_CARRIER_CREATE_KINDS,
        )
        from chaos_agent.agent.target_guard.classifier import (
            is_cluster_scoped_kind,
        )

        derived = frozenset(
            kind for kind in _RECOVERY_CARRIER_CREATE_KINDS.values()
            if is_cluster_scoped_kind(kind)
        )
        assert _RECOVERY_CARRIER_CLUSTER_SCOPED_KINDS == derived, (
            "the carrier family's cluster-scoped subset drifted from the "
            f"topology single source: has {_RECOVERY_CARRIER_CLUSTER_SCOPED_KINDS}, "
            f"derived {derived} — re-derive it, never hand-list it"
        )
