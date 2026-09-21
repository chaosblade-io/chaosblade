"""Budget guard for LLM-visible tool schemas (anti-backflow).

Context: the kubectl docstring was once slimmed and its long-tail
examples moved to ``knowledge/kubectl-recipes.md`` ("examples that the
kubectl tool docstring no longer carries inline"). Without a guard, the
docstring grew back from 5179 → 5546 → 6318 chars. This test pins the
budget so any future growth must be a conscious decision, not drift.

Measurement caliber (calibrated 2026-07, reproduces the 12696-tok
baseline of the slimming plan):
  ``json.dumps(convert_to_openai_tool(tool), ensure_ascii=False)`` — the
  FULL schema the LLM sees (description + args). Counted with
  o200k_base, which tracks Qwen2.5 within ±1.4% on these schemas
  (K=qwen/o200k measured 0.979-1.037 across all 22 tools), so the MCP
  warn threshold (500 tok per tool) applies directly. ``transformers``
  + the Qwen2.5 tokenizer stays the arbiter when in doubt.

Rules when a cap trips:
  1. New content is a CLASS-A constraint distilled from a real incident
     (MUST/NOT/refused/auto-stripped) → raise that ONE tool's cap with a
     comment citing the incident, keep the total cap untouched.
  2. New content is a long-tail EXAMPLE / recipe → it belongs in
     ``knowledge/kubectl-recipes.md`` (or the matching knowledge doc),
     with a one-line pointer in the docstring. Do NOT raise the cap.
  3. Anything else → compress or drop; the schema budget is load-bearing
     for prompt-cache and context-window costs.
"""

import json

import pytest
import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_tool

from chaos_agent.agent.nodes.planning.intent_clarification import (
    query_active_experiments,
    recover_task,
    submit_batch_intent,
    submit_fault_intent,
)
from chaos_agent.agent.nodes.verify._verifier_submit import (
    submit_recover_verification,
    submit_verification,
)
from chaos_agent.agent.replan import request_replan
from chaos_agent.agent.providers.chaosblade.cli import (
    blade_create,
    blade_destroy,
    blade_help,
    blade_query_k8s,
    blade_status,
)
from chaos_agent.agent.providers.chaosblade.cli_python import (
    blade_python_create,
    blade_python_prepare,
    blade_python_revoke,
)
from chaos_agent.agent.providers.faultdrill.assembler import (
    faultdrill_assemble_carrier,
)
from chaos_agent.tools.host_cmd import host_inject, host_read
from chaos_agent.tools.knowledge_reader import read_knowledge_resource
from chaos_agent.tools.kubectl_cli import kubectl, kubectl_read
from chaos_agent.tools.progress import update_progress
from chaos_agent.tools.wait import time_wait

_ENCODING = tiktoken.get_encoding("o200k_base")


def _schema_tokens(tool) -> int:
    """Tokens of the FULL tool schema as serialized to the LLM."""
    schema = json.dumps(convert_to_openai_tool(tool), ensure_ascii=False)
    return len(_ENCODING.encode(schema))


# Per-tool full-schema caps (tokens, o200k_base ≈ Qwen2.5). Target is the
# MCP warn threshold 500; the tools above it sit at their CLASS-A floor —
# their remaining content is incident-distilled constraints plus the only
# enumeration of valid values, none of which may be dropped (slimming-plan
# hard constraint: "A 类一条不删"). The count is deliberately NOT spelled
# out here (a hardcoded "six" went stale as rows were added); a cap above
# 500 must instead carry a per-row reason, and
# ``faultdrill_assemble_carrier`` is the one row whose overage is only
# PARTLY class-A — see its note before treating any over-500 cap as settled.
_CAPS: dict[str, tuple[object, int]] = {
    "kubectl": (kubectl, 935),                      # class-A floor (~593 qwen); +shell-quoting MUST (task-190c94e8); +one-shot debug 120s cap / systemd-run carrier (inject-59b289a6: 300s loop hosted in a one-shot debug pod was killed at the 120s cap, fragmenting the fault window + ~150s re-arm); +two-layer v_args semantics (inject-17617837: old flat "No shell features" line contradicted skill recipes' sh -c heredocs and cost 90s model hesitation; the selector-strip regex it advertised silently amputated `ls -l /proc/$(cat ...)` inside quoted payloads); +run admitted for the recovery-carrier shape only, flag whitelist (openspec recovery-carrier-standard: new refused-verb-class security contract; clean tree was already 772 over the old 766 cap, this raise covers both); +create admitted for the carrier RBAC stack, imperative-create routing (inject-cc2d5080: the subcommand list lacked `create` and the "Non-workload via apply" line steered the model into a manifest apply that the kind whitelist refuses — the carrier never stacked and the fault landed with no timer armed; measured 881, 890 keeps the same serialization-drift headroom rationale as kubectl_read); +run armed-before-inject cognition (run6 inject-3d5de7fa: the bare `sleep N` skeleton passed the old `active`-tolerant gate and the object-write landed permanently unrecovered — the `run` line now states the skeleton does NOT self-restore and the gate REFUSES until an `exec` arms the carrier `recovery_armed`, with a pointer to knowledge `recovery-carrier-arming.md` where the arming SOP/recipe lives (rule 2: recipe out, class-A constraint in); measured 926, 935 keeps the same serialization-drift headroom)
    "kubectl_read": (kubectl_read, 630),            # class-A floor (~552 qwen); +shell-quoting MUST (task-190c94e8); +exec single-command constraint (c157857 real-rejection-rework drift); +jsonpath whole-template quoting (inject-774ecd39: unquoted template word-split remotely, error invisible); +kubectl-layer selector rejection clarified (inject-17617837). Cap 616→630 is NOT content growth: the schema measures 616 on py3.14-system and 623 on .venv with identical source — the ±7 tok spread is json.dumps/convert_to_openai_tool serialization drift across library versions (within the o200k/Qwen ±1.4% caliber noted in the header). Zero-headroom at 616 made this pin flip red per-environment; 630 absorbs the drift while staying ~1000 tok below the 6318-char bloat era.
    "blade_python_create": (blade_python_create, 605),  # class-A floor (~592)
    "blade_python_prepare": (blade_python_prepare, 500),
    "blade_python_revoke": (blade_python_revoke, 375),
    "submit_fault_intent": (submit_fault_intent, 775),  # class-A floor (~586) + dynamic INTENT_* enums; +case hint param (case_resource_path); +intent-accuracy provenance contract (probe trail / template-not-data); +one-line duration contract (duration_seconds channel); +names-vs-labels disambiguation (names = scope-kind instances, workload/owner targets → labels — confusion caused real wrong-target submissions)
    "submit_batch_intent": (submit_batch_intent, 395),  # +one-line duration contract (duration_seconds channel)
    "submit_verification": (submit_verification, 495),
    "submit_recover_verification": (submit_recover_verification, 455),  # +unverified verdict vocabulary (openspec unverified-verdict-semantics): overall 4-value domain + "unverified ≠ unrecovered" anti-conflation note + layer2 "unknown" — contract-mandated, not long-tail example bloat; +B76 round-14 root-cause fix: vocabulary lines now DERIVED from the verdict enums (full 6-word layer2 + 7-word item sets — class-A closed-set contract, cited incident: three contradictory hand-copied vocabularies found live)
    "blade_create": (blade_create, 610),            # class-A floor (~597)
    "blade_destroy": (blade_destroy, 365),
    "blade_status": (blade_status, 325),
    "blade_help": (blade_help, 305),
    "blade_query_k8s": (blade_query_k8s, 360),
    "request_replan": (request_replan, 560),        # class-A floor (~550)
    "host_read": (host_read, 510),
    "host_inject": (host_inject, 495),
    "read_knowledge_resource": (read_knowledge_resource, 440),
    "time_wait": (time_wait, 225),
    "query_active_experiments": (query_active_experiments, 220),
    "recover_task": (recover_task, 320),  # +task_id multi-format example (461398b); +intent-exclusion clause (2026-09-20 pass-1~4 cleanup review): the REMEMBER mirror carrying "For ANY other intent, do NOT call recover_task" was deleted with the recency mirror, and the node's recover_handler has NO program-side intent check (a recover_task ToolMessage confirms the intent outright — verified in intent_clarification.py), so the schema's When-to-use is the only remaining carrier at tool-selection time; measured 308, 320 keeps the ±2% serialization-drift headroom rationale (231→308, class-A refused-constraint per rule 1)
    "update_progress": (update_progress, 395),  # +reserved terminal-marker note (inject-ac45b369 #55 it12: model wrote phase=execution-complete via update_progress at finish; validator rejected the WHOLE call incl. log_append, one 15.1s retry round. Class-A refused-constraint taught at the only call-site-adjacent surface; measured 386 + drift headroom)
    # Backfill of a BLANK registration, not a class-A raise: the M1
    # assembler shipped without a _CAPS row, so this guard — whose whole
    # reason to exist is that unregistered growth is invisible growth —
    # never saw it. Measured 641 (.venv py3.11 / lc-core 1.3.0) and 630
    # (system py3.14 / lc-core 1.4.0); 650 pins the higher side + the same
    # ~1.4% cross-environment serialization-drift headroom the kubectl_read
    # cap documents. Over the 500 warn line, and the overage is NOT all
    # class-A: the three-state receipt semantics ARE (``partial`` = carrier
    # still ARMED, do NOT rebuild, converge via blade-ai recover — misreading
    # it leaves a live liability) and so is the NEVER-call-twice constraint,
    # but the Inputs enumeration and the recipe-flavoured duration/buffer
    # arithmetic are rule-2 externalisation candidates (knowledge/ pointer).
    # Slimming was considered and NOT taken (option D-2 declined in favour of
    # D-1 register-at-current-size); revisit if this cap is ever raised again.
    # 650→730 (#55 M3 retest inject-593c7826 live-fire): +carrier_image — the
    # planning-phase image decision channel. The LLM had §9-decided terway
    # (probe candidates ∩ evidence archive) while the standalone selector
    # picked an unpullable curlimages image and the tool contract had NO
    # parameter to carry the decision — every fallback heuristic stays a
    # guess because the one agent that saw the evidence cannot speak. The
    # three teaching elements (when to pass / must-be-inside-the-allowlist
    # fail-closed / omitted→tool-picks) are class-A safety semantics, not
    # examples; measured 717, 730 keeps the same drift headroom.
    "faultdrill_assemble_carrier": (faultdrill_assemble_carrier, 730),
}

# The two flagship tools carry the full five-section structure; long-tail
# examples live in knowledge/kubectl-recipes.md, not inline.
_STRUCTURED_TOOLS = (kubectl, kubectl_read)
_SECTIONS = (
    "When to use:",
    "Inputs:",
    "Output:",
    "Side effects:",
    "Constraints",
)

# Total budget across all tracked schemas (current ~9897). If this trips,
# the growth is aggregate drift — find the culprit via the per-tool caps.
# 9795 = 9750 + 45: single class-A raise for the kubectl one-shot debug
# 120s cap constraint (inject-59b289a6), approved by budget owner.
# 9867 = 9795 + 72: class-A raise for kubectl two-layer v_args semantics
# + kubectl_read selector-rejection clarification (inject-17617837),
# approved by budget owner.
# 9897 = 9867 + 30: class-A raise for update_progress reserved
# terminal-marker note (inject-ac45b369: #55 it12 phase=execution-complete
# write rejected, one 15.1s retry round), approved by budget owner.
# 9942 = 9897 + 45: class-A raise for the kubectl `run` armed-before-inject
# cognition (run6 inject-3d5de7fa: a bare-`sleep N` carrier passed the old
# `active`-tolerant gate and the object-write landed permanently
# unrecovered), approved by budget owner. Restores total headroom above the
# ±7-tok cross-environment serialization drift noted in the kubectl_read cap.
# 9942→10010 (2026-09-20 pass-1~4 cleanup review): the recover_task
# intent-exclusion clause is a user-ruled class-A addition (the deleted
# REMEMBER mirror was the only negative-form carrier and recover_handler
# confirms intent on the tool message with no program-side check), same
# raise-on-ruling path as a99774cc above.
# 10010→10670 (2026-09-20 assembler registration backfill): this is a
# COVERAGE change, not description growth — the M1 assembler's 641 tok have
# been riding every EXECUTE-phase request since it shipped, untracked. The
# tracked total was 10001 against a 10010 line, i.e. 9 tok of headroom, so
# the row could not be added without raising it. 10642 measured + ~28 tok
# for the cross-environment serialization drift documented on the
# kubectl_read cap (this tool alone measures 641 on .venv py3.11 vs 630 on
# system py3.14 — 11 tok, wider than kubectl_read's ±7). Approved by the
# budget owner as D-1 (register at current size); D-2 (slim the recipe-
# flavoured Inputs/duration prose into knowledge/ first, then register under
# 500) was declined, so the aggregate line absorbs the full 641.
# 10670→10740: the carrier_image class-A raise on
# faultdrill_assemble_carrier (650→730, #55 M3 retest inject-593c7826 —
# see the row note) carries the total with it; 10740 keeps the same
# drift headroom over the measured 10718.
_TOTAL_CAP = 10740


class TestToolDescriptionBudget:
    @pytest.mark.parametrize(
        "name", sorted(_CAPS), ids=lambda n: n
    )
    def test_schema_within_cap(self, name: str) -> None:
        tool, cap = _CAPS[name]
        count = _schema_tokens(tool)
        assert count <= cap, (
            f"{name} full schema grew to {count} tok (cap {cap}; MCP warn "
            "threshold 500). If this is a class-A incident constraint, "
            "raise THIS cap with a comment citing the incident. If it is a "
            "long-tail example, move it to knowledge/ (see "
            "kubectl-recipes.md) and leave a pointer. See "
            "tests/test_tools/test_tool_description_budget.py header."
        )

    @pytest.mark.parametrize(
        "tool", _STRUCTURED_TOOLS, ids=lambda t: t.name
    )
    def test_flagship_tools_keep_five_sections(self, tool) -> None:
        desc = tool.description
        missing = [s for s in _SECTIONS if s not in desc]
        assert not missing, (
            f"{tool.name} lost structural section(s) {missing}. The unified "
            "structure (When to use / Inputs / Output / Side effects / "
            "Constraints) is a hard constraint of the slimming plan — "
            "compress WITHIN sections, never drop the headers."
        )

    def test_total_budget(self) -> None:
        total = sum(_schema_tokens(t) for t, _ in _CAPS.values())
        assert total <= _TOTAL_CAP, (
            f"Total tool-schema budget is {total} tok (cap {_TOTAL_CAP}). "
            "See tests/test_tools/test_tool_description_budget.py header "
            "for the class-A / long-tail / drift triage rules."
        )
