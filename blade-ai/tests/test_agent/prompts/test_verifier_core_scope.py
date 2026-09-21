"""Verifier core-scope contract anchors (openspec: verifier-core-only).

verify 的唯一职责是"注入动作在目标上生效了吗"。传播效应（OOM/驱逐、
延迟、业务影响）不是判定对象——prompt 不得引导观察它们，checklist
不得携带 category 分类，schema/工具 docstring 不得提及分类字段。
"""

import inspect
from pathlib import Path

from langchain_core.messages import HumanMessage

from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
    dict_to_verification_result,
)
from chaos_agent.agent.nodes.verify import _verifier_messages as _verifier_messages_module
from chaos_agent.agent.nodes.verify._verifier_messages import _build_layer2_messages
from chaos_agent.agent.nodes.verify.verifier import verifier as _verifier_module
from chaos_agent.agent.nodes.verify import _verifier_submit
from chaos_agent.agent.prompts.sections.verification import (
    get_verifier_layer2_section,
)
from chaos_agent.agent.result.verdict import ChecklistItem, Layer1Result

# ---------------------------------------------------------------------------
# Section-level anchors: no impact-side guidance in the shared prompt
# ---------------------------------------------------------------------------


def test_verifier_layer2_section_scope():
    text = get_verifier_layer2_section()
    # Coverage Awareness sub-section removed in the 2026-09-20 verifier
    # cleanup (pass-4): target-set completeness is carried by Core
    # Principles #4 ("coverage of the target set") and the Output
    # contract's Overall 'verified' definition. Impact-side observations
    # stay out of verify's scope.
    assert "Coverage Awareness" not in text
    assert "Were ALL target resources" not in text
    assert "Anomalies" not in text
    assert "Application Impact" not in text
    assert "downstream impact" not in text


# ---------------------------------------------------------------------------
# Mode contexts: single-tier checklist, fallback clause present
# ---------------------------------------------------------------------------

_MODE1_CASE = (
    "## 注入验证\n"
    "1. 执行 kubectl top pod 确认 CPU 达到注入百分比\n"
    "2. 检查 Pod 是否出现 OOMKilled 事件\n"
)
_MODE2_CASE = "## 注入验证\n以文字说明如何确认 CPU 满载，观察指标变化即可。\n"
_MODE3_CASE = "## 故障背景\ncpu fullload 场景说明。\n"
_MODE0_CASE = (
    "--- Candidate 1 ---\n## 注入验证\n1. kubectl top pod 观察 CPU\n"
    "--- Candidate 2 ---\n## 注入验证\n1. 观察节点 CPU 使用率\n"
)


def _layer2_context(skill_case: str) -> str:
    state = {
        "task_id": "t-core-scope",
        "fault_scope": "pod",
        "fault_target": "cpu",
        "fault_action": "fullload",
        "experiment_uid": "uid-core-scope",
        "skill_case_content": skill_case,
        "injection_parsed_params": {},
        "params": {},
        "target": {"namespace": "default", "names": ["myapp-pod"], "labels": {}},
        "kubeconfig": "/path/to/kubeconfig",
        "kubectl_exec_pod_name": "otel-c-tool-abc",
    }
    layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
    msgs = _build_layer2_messages(
        state, layer1, "uid-core-scope", "cpu-fullload",
        "/path/to/kubeconfig", count=1,
    )
    return "\n".join(
        m.content for m in msgs if isinstance(m, HumanMessage)
    )


def test_mode_contexts_have_no_tier_lexicon():
    for skill_case in (_MODE0_CASE, _MODE1_CASE, _MODE2_CASE, _MODE3_CASE):
        ctx = _layer2_context(skill_case)
        for token in ("[CORE]", "[IMPACT]", "Two-Tier", "one cheap observation",
                      "[category]", "[CORE|IMPACT]"):
            assert token not in ctx, f"{token!r} leaked into checklist context"


def test_mode1_carries_fallback_clause():
    ctx = _layer2_context(_MODE1_CASE)
    # Template line format is single-tier.
    assert "Step 1: [status]" in ctx
    # D1 fallback clause: propagated-effect steps are not verdict criteria
    # and never justify waiting/retrying/sampling.
    assert "NOT verdict criteria" in ctx
    assert "NEVER add waiting, retries or extra sampling" in ctx


def test_negative_evidence_enumeration_kept_without_tier_lexicon():
    ctx = _layer2_context(_MODE1_CASE)
    # The enumeration duty itself is retained (anti selective reporting)...
    assert "NEGATIVE EVIDENCE ENUMERATION" in ctx
    assert "MUST include a 'Negative Evidence' section" in ctx
    # ...only the IMPACT classification phrasing is gone.
    assert "propagated IMPACT effects" not in ctx


def test_checklist_status_choice_expected_semantics_split():
    ctx = _layer2_context(_MODE1_CASE)
    # 'expected' WITHOUT observation is valid for propagated-effect steps;
    # injection-effect steps still require the observation.
    assert "valid WITHOUT observation" in ctx
    assert "requires the observation" in ctx


# ---------------------------------------------------------------------------
# Schema / tool-surface anchors: no category field anywhere
# ---------------------------------------------------------------------------


def test_json_reminder_schema_has_no_category():
    assert '"category"' not in inspect.getsource(_verifier_module)


def test_submit_tool_docstring_and_schema_have_no_category():
    assert "category" not in inspect.getsource(_verifier_submit)
    from chaos_agent.agent.nodes.verify._verifier_submit import submit_verification

    props = submit_verification.args_schema.model_json_schema().get("properties", {})
    assert "category" not in props


def test_checklist_item_model_has_no_category():
    props = ChecklistItem.model_json_schema()["properties"]
    assert "category" not in props


def test_historical_residual_category_key_ignored():
    result = dict_to_verification_result({
        "level": "verified",
        "layer1": {"status": "passed"},
        "layer2": {"status": "passed"},
        "checklist": {"items": [
            # Residual key from a historical checkpoint dict — silently
            # ignored at the model boundary (design D6).
            {"step": 1, "status": "passed", "evidence": "mem 81%",
             "category": "impact"},
        ]},
    })
    item = result.checklist.items[0]
    assert item.step == 1
    assert "category" not in item.model_dump()


# ---------------------------------------------------------------------------
# Knowledge-doc anchors: the fifth chain (verify-phase data assets) must
# teach the same core-only semantics — no impact-as-verdict, no rollback
# delegated to the verifier LLM, no stale tier vocabulary.
# ---------------------------------------------------------------------------

_KNOWLEDGE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "chaos_agent" / "knowledge"
)


def _knowledge_doc(name: str) -> str:
    return (_KNOWLEDGE_DIR / name).read_text(encoding="utf-8")


def test_verification_strategies_doc_teaches_core_only():
    text = _knowledge_doc("fault-verification-strategies.md")
    # Propagated effects are observational, never verdict criteria...
    assert "Propagated-effect observation" in text
    assert "ultimate goal" not in text
    assert "must NOT be skipped" not in text
    assert "Layer 3 (impact verification)" not in text
    assert "three-layer verification model" not in text
    assert "never wait, retry or sample" in text
    # ...and recovery is the framework's job, not the verifier LLM's
    # (the verifier toolset is read-only and has no blade_destroy).
    assert "call `blade destroy`" not in text
    assert "the framework rolls back automatically" in text


def test_k8s_knowledge_doc_verdict_question_is_single():
    text = _knowledge_doc("k8s-knowledge.md")
    assert "not only verify" not in text
    assert "verdict answers exactly one question" in text
    assert "never wait, retry or sample" in text


def test_principles_doc_layer3_is_observational():
    text = _knowledge_doc("chaos-engineering-principles.md")
    assert "Propagated-effect observation" in text
    assert "three-layer verification model" not in text
    assert "Impact verification" not in text
    assert "never gates the verdict" in text


def test_verifier_messages_have_no_stale_lexicon():
    source = inspect.getsource(_verifier_messages_module)
    assert "evidence tiers" not in source
    assert "flagged as INCOMPLETE" not in source
    # The baseline format requirement aligns with D1: propagated-effect
    # steps may omit the comparison; no phantom per-step program threat.
    assert "weaken their own evidence" in source
    assert "Propagated-effect" in source


def test_planner_workflow_section_has_no_stale_lexicon():
    # The planner overlay contract (workflow.py) shares the verifier's
    # vocabulary: the case defines which steps to verify — no Two-Tier
    # "evidence tiers" era wording may leak into the overlay preamble.
    from chaos_agent.agent.prompts.sections import workflow

    source = inspect.getsource(workflow)
    assert "evidence tiers" not in source
