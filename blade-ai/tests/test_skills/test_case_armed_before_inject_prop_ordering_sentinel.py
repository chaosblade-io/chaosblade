"""Sentinel: a ``mechanism_writes`` **create-domain prop object** (a prop PVC
referencing a nonexistent StorageClass) is an *armed-before-inject* object-write
injection — NOT setup — so the case docs must order its ``apply -f`` AFTER the
carrier arms, and must register it into the assembler's ``extra_delete`` so the
non-patch-domain residue is closed by carrier TTL self-recovery + ``blade-ai
recover`` replay.

Root cause this sentinel pins (#12-R-live deep-dive, statically confirmed for
#46 whose live target was already NotFound):

The armed-before-inject trigger ``_is_object_write_injection``
(``tool_screener.py``) is DOUBLE-coverage — mutation verbs
(``KUBECTL_WRITE_SUBCOMMANDS``) OR ``is_apply_native_fault_injection``
(``classifier.py``: kubectl + apply/create + inline ``stdin_data`` + every kind
in ``PERSISTENT_FAULT_MANIFEST_KINDS``). A prop PVC created via ``apply -f`` with
an inline manifest therefore trips the gate. The #12/#46 docs historically placed
the prop-PVC ``apply`` BEFORE the assembler call and mis-labelled it "setup"
(#46: "武装点定案：步骤 1-2 均为 setup"), so the live run got
``REJECT_BANNED armed-before-inject`` and the agent burned an execute_loop ReAct
self-correction round inverting to "assembler-first → prop-after → register
extra_delete" and overwriting its own approved plan. The fix is the doc ordering
+ the standard-component legislation, NOT the guard (the guard is correct).

Per 4012d277 the high-value teeth here are the ORDERING structural fact, the
NEGATIVE mutation tooth (misclassification wording must not return), and
CROSS-SOURCE consistency (both POINTER-face cases must point at the standard
legislation); positive wording assertions are kept minimal.
"""

from __future__ import annotations

import pathlib

_REFS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "skills"
    / "k8s-chaos-skills"
    / "references"
)
_CARRIER = _REFS / "carrier" / "recovery-carrier.md"
_CASE12 = _REFS / "catalogue" / "Pod_Pending" / "Pod_Pending_PVC未绑定.md"
_CASE46 = _REFS / "catalogue" / "Pod_Pending" / "Pod_Pending_STS_PVC未绑定.md"

# The single legislation anchor every POINTER-face case must resolve to.
_LEGISLATION = "create 域道具对象是故障生效动作、绝非 setup"


def _read(path: pathlib.Path) -> str:
    assert path.exists(), f"doc moved or renamed: {path}"
    return path.read_text(encoding="utf-8", errors="replace")


def test_standard_component_legislates_prop_create_is_injection() -> None:
    """Minimal wiring: the standard component (POINTER-face authoritative
    source) must carry the hard legislation so every referencing case inherits
    it. Also pins the two hard orders (arm-first + extra_delete) and the
    command-form carve-out (no stdin_data → not gated)."""
    text = _read(_CARRIER)
    assert _LEGISLATION in text, (
        "标准件缺失『create 域道具对象是故障生效动作、绝非 setup』硬立法："
        "POINTER 面 case（#12/#46）失去权威源，armed-before-inject 顺序约束无从传导"
    )
    assert "is_apply_native_fault_injection" in text, (
        "标准件未点名判据 is_apply_native_fault_injection：读者无法核对双覆盖门禁的真实触发面"
    )
    assert "create quota --hard" in text, (
        "标准件缺失命令式 create（无 stdin_data）不被 gate 的边界注："
        "#13 ResourceQuota/#16 ConfigMap 命令式道具会被误判为同缺陷而错误重排"
    )


def test_case12_prop_pvc_ordered_after_assembler() -> None:
    """ORDERING structural fact: in #12's main path the numbered assembler step
    (arm + patch-domain inject) must come BEFORE the numbered prop-PVC create
    step. This is the exact inversion the live run was forced into; if a future
    edit restores create-before-arm ordering this fails."""
    text = _read(_CASE12)
    assembler = text.find("2. 调用 `faultdrill_assemble_carrier`")
    create = text.find("3. **创建道具 PVC（必须排在步骤 2 装配器武装之后")
    assert assembler != -1, "#12 主路径步骤 2（装配器调用）锚点丢失——步骤编号被改写"
    assert create != -1, "#12 主路径步骤 3（道具 PVC 创建，armed-before-inject 硬序）锚点丢失"
    assert assembler < create, (
        "#12 顺序回归：道具 PVC 创建（步骤 3）必须排在装配器武装（步骤 2）之后；"
        "create-before-arm 会被 target_guard 判 REJECT_BANNED armed-before-inject，"
        "逼 agent 执行期自纠 inverted 并覆写 approved plan"
    )


def test_case46_setup_misclassification_not_reintroduced() -> None:
    """NEGATIVE mutation tooth: #46's armed-point 定案 once mis-labelled the
    prop-PVC create step as setup ("步骤 1-2 均为 setup"). That wording is the
    defect; it must NOT come back."""
    text = _read(_CASE46)
    assert "步骤 1-2 均为 setup" not in text, (
        "#46 误分类回归：『步骤 1-2 均为 setup』把道具 PVC 的 apply-native "
        "object-write 注入当成 setup 排在武装前——正是 armed-before-inject 顺序缺陷"
    )


def test_both_cases_register_prop_pvc_into_extra_delete() -> None:
    """Recovery-closure half of the fix: both cases must register the prop PVC
    into the assembler's ``extra_delete`` so carrier TTL self-recovery +
    ``blade-ai recover`` replay delete it (otherwise the non-patch-domain prop
    residue has no automatic deleter and degrades to a manual sweep)."""
    for path, claim in ((_CASE12, "app-data-claim"), (_CASE46, "sts-unbound-claim")):
        text = _read(path)
        assert "extra_delete" in text and claim in text, (
            f"{path.name} 未把道具 PVC（{claim}）登记进 extra_delete："
            "非 patch 域 residue 无自动删除执行者，退化为人工 sweep"
        )


def test_both_cases_point_to_standard_legislation() -> None:
    """CROSS-SOURCE consistency: both POINTER-face cases must resolve to the
    standard-component legislation anchor rather than restating a divergent
    local rule (which would let the two drift and force planning-time
    arbitration)."""
    for path in (_CASE12, _CASE46):
        text = _read(path)
        assert _LEGISLATION in text, (
            f"{path.name} 未引用标准件第四节『{_LEGISLATION}』立法锚："
            "POINTER 面脱离权威源，与标准件产生本地矛盾"
        )


def test_docs_not_vacuous() -> None:
    """Anti-vacuous guard: the sentinel must scan real, non-trivial docs."""
    for path in (_CARRIER, _CASE12, _CASE46):
        text = _read(path)
        assert len(text) > 3000, (
            f"{path.name} suspiciously small ({len(text)} bytes) — sentinel may be vacuous"
        )
    assert "PersistentVolumeClaim" in _read(_CASE12), (
        "#12 no longer describes the prop PVC — sentinel anchor drifted"
    )
