"""Single-source failure verdict (``agent/tool_verdicts.py``).

The seam exists because "did this tool call fail?" was answered locally at
every consumption site by matching the result TEXT against ``startswith(
"Error")``. That is sound while every tool renders failures the same way and
unsound the moment one reports them STRUCTURALLY — the prefix never matches,
so each consumer independently concluded "not a failure".

These tests pin the three properties that make it a single source rather than
one more ``startswith`` term:

1. the generic renderings are unchanged (no regression on the text dialect);
2. a carrier's declared result shape is read by the carrier, and the framework
   routes to it without naming the tool;
3. the verdict is THREE-valued — abstention is not a success verdict. Property
   3 is the one that was violated before: ``detect_transient_retry_exhaustion``
   inverted a pattern miss into "the blip healed", so a structurally-reporting
   tool cleared its own retry budget on every failure.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers.registry import FaultProviderRegistry
from chaos_agent.agent.tool_verdicts import (
    GENERIC_ERROR_PREFIXES,
    loads_dict,
    message_result_error_text,
    message_result_failed,
    tool_result_error_text,
    tool_result_failed,
)

ASSEMBLER = "faultdrill_assemble_carrier"


@pytest.fixture(autouse=True)
def _builtins_registered():
    """The verdict routes through the registry, so the builtins must be live.

    ``register_builtins`` is idempotent (register overwrites), so this is safe
    alongside any suite-level registry isolation.
    """
    FaultProviderRegistry.register_builtins()
    yield


def _assembler_receipt(
    status: str = "failed",
    error: str = "",
    steps: list | None = None,
    **extra,
) -> str:
    return json.dumps(
        {
            "status": status,
            "error": error,
            "carrier": {"armed": status != "failed"},
            "steps": steps if steps is not None else [],
            **extra,
        }
    )


# ---------------------------------------------------------------------------
# 1. Generic renderings — unchanged for every text-dialect tool
# ---------------------------------------------------------------------------


class TestGenericVerdict:
    def test_error_prefix_is_a_failure(self):
        assert tool_result_failed("kubectl", "Error: exit status 1")

    def test_error_prefix_is_matched_without_the_colon(self):
        # The wider form is deliberate: execute_loop and react_helpers always
        # matched "Error", and a rendering like "Error(1234):" must not slip.
        assert tool_result_failed("kubectl", "Error(1234): boom")

    @pytest.mark.parametrize("prefix", GENERIC_ERROR_PREFIXES)
    def test_every_generic_prefix_is_a_failure(self, prefix):
        assert tool_result_failed("kubectl", f"{prefix} something went wrong")

    def test_target_guard_rejection_is_a_failure(self):
        # The route gate renders BEFORE dispatch, so it never carries the
        # tool-layer prefix — the term exists because it was once missing
        # (provider.py's ``[target_guard] REJECT_BANNED`` attribution bug).
        assert tool_result_failed("kubectl", "[target_guard] REJECT_BANNED: ...")

    def test_message_status_error_is_a_failure_even_with_empty_content(self):
        assert tool_result_failed("kubectl", "", status="error")
        # The evidence text must still be non-empty so a classifier has
        # something to read.
        assert tool_result_error_text("kubectl", "", status="error")

    def test_plain_successful_output_is_not_a_failure(self):
        assert not tool_result_failed("kubectl", "pod/nginx created")
        assert not tool_result_failed("kubectl", "NAME  READY  STATUS\nx  1/1  Running")

    def test_leading_whitespace_does_not_hide_a_failure(self):
        assert tool_result_failed("kubectl", "\n  Error: exit status 1")

    def test_error_word_midway_is_not_a_failure(self):
        # A substring match would false-fire on embedded mentions (a trace
        # containing "ToolGuardError:") — prefix-only, as before.
        assert not tool_result_failed("kubectl", "stderr: ToolGuardError: blocked")

    def test_unknown_tool_name_stays_on_the_generic_path(self):
        assert tool_result_failed("some_unregistered_tool", "Error: nope")
        assert not tool_result_failed("some_unregistered_tool", "all good")

    def test_non_string_content_does_not_crash(self):
        assert not tool_result_failed("kubectl", None)
        assert not tool_result_failed("kubectl", ["list", "content"])


# ---------------------------------------------------------------------------
# 2. Declared result shapes — read by the owning carrier
# ---------------------------------------------------------------------------


class TestAssemblerReceiptShape:
    """The case that exposed the seam: the assembler's honest receipt is JSON
    by design and its exceptions are caught in-tool (``# noqa: BLE001 — honest
    receipt, never a crash``), so a failure carries neither the ``Error:``
    prefix nor ``status="error"``."""

    def test_structured_failure_is_a_failure(self):
        content = _assembler_receipt(
            error="target read for baseline precheck failed: rbac denied",
        )
        assert tool_result_failed(ASSEMBLER, content)

    def test_evidence_is_the_receipts_own_reason(self):
        content = _assembler_receipt(error="rbac denied: cannot patch configmap")
        assert tool_result_error_text(ASSEMBLER, content) == (
            "rbac denied: cannot patch configmap"
        )

    def test_evidence_includes_the_first_failed_step_detail(self):
        # The step detail is what carries the underlying kubectl stderr — the
        # text ``errors.classify_error`` can actually match on. Without it the
        # verdict would say "failed" but the classifier could not say why.
        content = _assembler_receipt(
            error="injection failed",
            steps=[
                {"step": "rbac", "ok": True, "detail": "created"},
                {"step": "inject", "ok": False,
                 "detail": "dial tcp 10.0.0.1:6443: i/o timeout"},
                {"step": "cleanup", "ok": False, "detail": "second failure"},
            ],
        )
        text = tool_result_error_text(ASSEMBLER, content)
        assert "injection failed" in text
        assert "i/o timeout" in text
        assert "step inject" in text
        # Only the FIRST failed step — the rest is noise for classification.
        assert "second failure" not in text

    def test_cleanup_residue_is_named(self):
        content = _assembler_receipt(
            error="boom", cleanup_failures=["rolebinding left behind"],
        )
        assert "rolebinding left behind" in tool_result_error_text(ASSEMBLER, content)

    def test_missing_reason_still_yields_evidence(self):
        text = tool_result_error_text(ASSEMBLER, _assembler_receipt(error=""))
        assert text and ASSEMBLER in text

    def test_success_receipt_is_not_a_failure(self):
        assert not tool_result_failed(ASSEMBLER, _assembler_receipt(status="success"))

    def test_partial_receipt_is_not_a_plain_failure(self):
        # ``partial`` = carrier ARMED, injection unconfirmed. It is a live
        # liability to recover, not a call that never happened, and
        # ``parse_receipt`` registers it — reporting it as a plain failure
        # would mislead the replan context.
        assert not tool_result_failed(ASSEMBLER, _assembler_receipt(status="partial"))

    def test_success_receipts_error_key_is_not_read_as_a_failure(self):
        # Every success/partial receipt ALSO carries an ``error`` key (empty on
        # success), so the status field is the only sound discriminator.
        content = _assembler_receipt(status="success", error="")
        assert not tool_result_failed(ASSEMBLER, content)

    def test_compacted_receipt_abstains(self):
        # ``memory.tool_compactor`` truncates a JSON body by bytes once
        # ``smart_strip_k8s_json`` declines it, so a compacted receipt is
        # JSON-SHAPED but not valid JSON. Unknown is not success — and not
        # failure either.
        truncated = '{"status": "failed", "error": "rbac den'
        assert loads_dict(truncated) is None
        assert tool_result_error_text(ASSEMBLER, truncated) is None
        assert not tool_result_failed(ASSEMBLER, truncated)

    def test_unrelated_json_body_abstains(self):
        assert tool_result_error_text(ASSEMBLER, '{"unrelated": true}') is None

    def test_the_declaration_does_not_leak_to_other_tools(self):
        # Same bytes, different tool name: no provider owns that shape, so the
        # verdict stays generic (and generic reads no JSON).
        content = _assembler_receipt(error="boom")
        assert not tool_result_failed("kubectl", content)


class TestBladeJsonShape:
    """``blade_create`` returns the CLI's raw stdout on exit 0, and the blade
    CLI can report a failure INSIDE that JSON with a zero exit code. This is
    the shape ``execute_loop`` used to hardcode a per-tool branch for."""

    def test_in_json_failure_with_zero_exit_is_a_failure(self):
        content = json.dumps(
            {"code": 54000, "success": False, "error": "pod not found"}
        )
        assert tool_result_failed("blade_create", content)
        assert tool_result_error_text("blade_create", content) == "pod not found"

    def test_keyless_error_json_is_a_failure(self):
        # verify.py pins this shape as a FAILED destroy; the create face agrees.
        content = json.dumps({"code": 500, "error": "internal"})
        assert tool_result_failed("blade_create", content)

    def test_error_text_falls_back_to_a_string_result(self):
        content = json.dumps({"code": 500, "success": False, "result": "boom"})
        assert tool_result_error_text("blade_create", content) == "boom"

    def test_error_text_falls_back_to_the_code(self):
        content = json.dumps({"code": 500, "success": False})
        assert "500" in tool_result_error_text("blade_create", content)

    def test_successful_create_is_not_a_failure(self):
        content = json.dumps(
            {"code": 200, "success": True, "result": {"uid": "5aaa51dbcb78a25d"}}
        )
        assert not tool_result_failed("blade_create", content)

    def test_the_text_failure_paths_still_work(self):
        # cli.py's exit!=0 branch renders "Error: blade create failed (exit 1)".
        assert tool_result_failed("blade_create", "Error: blade create failed (exit 1)")

    def test_uncertain_outcome_is_not_a_failure(self):
        # cli.py's transport-exception path: "the request MAY have been
        # accepted — the outcome is UNKNOWN, not failed". Its own text tells
        # the agent to POLL, not replan, so it must not read as a failure
        # (``_build_replan_context``'s old ``"error" in content.lower()``
        # fallback swept it in; that was the bug, not the contract).
        content = (
            "Warning: blade create returned error (exit 1) but experiment CRD "
            "was created (UID: 5aaa51dbcb78a25d). The error appears TRANSIENT "
            "(timeout); POLL the cluster state to check if the fault takes effect"
        )
        assert not tool_result_failed("blade_create", content)

    def test_non_dialect_json_abstains(self):
        assert tool_result_error_text("blade_create", '{"result": "x"}') is None

    def test_read_tools_are_deliberately_not_declared(self):
        # For blade_status a ``success:false`` is often the ANSWER: code 406
        # and the "not found" wording both mean "already destroyed", which
        # ``classify_destroy_output`` scores as NOT_FOUND and recover.py's
        # ``parse_blade_status_destroyed`` scores as PASSED. Folding that into
        # a generic failure verdict would invert a recovery judgement.
        not_found = json.dumps({"code": 406, "success": False, "error": "not found"})
        assert not tool_result_failed("blade_status", not_found)
        assert not tool_result_failed("blade_query_k8s", not_found)

    def test_the_python_carrier_reads_the_same_dialect(self):
        # One shared predicate for both blade carriers, so they cannot drift
        # on the same bytes.
        content = json.dumps({"code": 500, "success": False, "error": "agent down"})
        assert tool_result_failed("blade_python_create", content)
        assert tool_result_error_text("blade_python_create", content) == "agent down"

    def test_destroy_is_deliberately_not_declared(self):
        # Same reason as the read tools: a destroy's "not found" is the
        # convergence valve, and only the destroy authority may read it.
        content = json.dumps({"code": 406, "success": False, "error": "not found"})
        assert not tool_result_failed("blade_destroy", content)


# ---------------------------------------------------------------------------
# 3. Three-valued: abstention is not a success verdict
# ---------------------------------------------------------------------------


class TestThreeValued:
    def test_abstention_is_neither_failed_nor_proven_success(self):
        from chaos_agent.agent.nodes.execute.react_helpers import (
            _result_proves_success,
        )

        truncated = '{"status": "success", "artifact": {"recovery_han'
        assert not tool_result_failed(ASSEMBLER, truncated)
        assert not _result_proves_success(truncated)

    def test_message_helpers_agree_with_the_content_helpers(self):
        content = _assembler_receipt(error="boom")
        msg = ToolMessage(content=content, name=ASSEMBLER, tool_call_id="t1")
        assert message_result_failed(msg)
        assert message_result_error_text(msg) == tool_result_error_text(ASSEMBLER, content)

    def test_message_status_is_read_from_the_message(self):
        msg = ToolMessage(content="weird", name="kubectl", tool_call_id="t1",
                          status="error")
        assert message_result_failed(msg)

    def test_a_success_message_is_not_a_failure(self):
        msg = ToolMessage(content="pod/nginx created", name="kubectl",
                          tool_call_id="t1")
        assert not message_result_failed(msg)


# ---------------------------------------------------------------------------
# 4. The consumers actually route through it
# ---------------------------------------------------------------------------


class TestConsumersAreWired:
    """The seam is only a single source if the consumers use it. These pin the
    two behaviours that were broken, end to end."""

    @staticmethod
    def _request():
        from chaos_agent.agent.replan import ReplanRequest

        return ReplanRequest(
            kind="feasibility",
            decision="plan_invalid",
            invalidated_assumption="the carrier assembly did not land",
            affected_step="inject",
        )

    @staticmethod
    def _state_with_one_result(tool_name: str, content: str, call_id: str) -> dict:
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "id": call_id, "name": tool_name, "args": {},
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(content=content, name=tool_name, tool_call_id=call_id),
            ],
        }

    def test_transient_guard_counts_a_structured_failure(self):
        from chaos_agent.agent.nodes.execute.react_helpers import (
            detect_transient_retry_exhaustion,
        )
        from chaos_agent.config.settings import settings

        budget = int(settings.max_transient_retry or 0)
        if budget <= 0:
            pytest.skip("transient retry guard disabled by settings")

        receipt = _assembler_receipt(
            error="precheck failed: dial tcp 10.0.0.1:6443: i/o timeout",
            steps=[{"step": "precheck", "ok": False, "detail": "i/o timeout"}],
        )
        messages: list = []
        for i in range(budget + 2):
            messages.append(
                AIMessage(
                    content="",
                    tool_calls=[{
                        "id": f"c{i}", "name": ASSEMBLER, "args": {"i": i},
                        "type": "tool_call",
                    }],
                )
            )
            messages.append(
                ToolMessage(content=receipt, name=ASSEMBLER, tool_call_id=f"c{i}")
            )

        hint = detect_transient_retry_exhaustion(messages)
        assert hint is not None, (
            "the assembler's structured failures must reach the retry-budget "
            "guard — before the seam they carried no Error prefix, so every "
            "one of them reset its own count and the guard stayed silent"
        )
        assert ASSEMBLER in hint

    def test_replan_context_records_a_structured_failure(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _build_replan_context

        receipt = _assembler_receipt(error="rbac denied: cannot patch configmap")
        ctx = _build_replan_context(
            self._state_with_one_result(ASSEMBLER, receipt, "c1"), self._request()
        )
        assert ASSEMBLER in set(ctx.get("failed_tool_names") or ())
        assert "c1" in (ctx.get("evidence_refs") or [])

    def test_replan_context_records_an_in_json_blade_failure(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _build_replan_context

        content = json.dumps(
            {"code": 54000, "success": False, "error": "pod not found"}
        )
        ctx = _build_replan_context(
            self._state_with_one_result("blade_create", content, "c2"),
            self._request(),
        )
        # Behaviour-preserving: the retired ``if name == "blade_create"``
        # branch already caught this one. It must survive the move to the
        # shared verdict.
        assert "blade_create" in set(ctx.get("failed_tool_names") or ())

    def test_replan_context_ignores_a_successful_blade_create(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _build_replan_context

        content = json.dumps({"code": 200, "success": True, "result": {"uid": "u1"}})
        ctx = _build_replan_context(
            self._state_with_one_result("blade_create", content, "c3"),
            self._request(),
        )
        assert "blade_create" not in set(ctx.get("failed_tool_names") or ())

    def test_replan_context_ignores_an_uncertain_blade_create(self):
        # The one deliberate behaviour change: the old branch's non-JSON
        # fallback matched ``"error" in content.lower()``, which swept in the
        # UNKNOWN-outcome rendering. cli.py is explicit that this shape is
        # "UNKNOWN, not failed", and its own text tells the agent to POLL
        # rather than replan.
        from chaos_agent.agent.nodes.execute.execute_loop import _build_replan_context

        content = (
            "Warning: blade create returned error (exit 1) but experiment CRD "
            "was created (UID: 5aaa51dbcb78a25d). The error appears TRANSIENT"
        )
        ctx = _build_replan_context(
            self._state_with_one_result("blade_create", content, "c4"),
            self._request(),
        )
        assert "blade_create" not in set(ctx.get("failed_tool_names") or ())

    # -- the planning-exit route ------------------------------------------
    #
    # ``route_after_phase1_tools`` reads a SIGNAL that ENDS Phase 1, so a
    # misread is worse than the replan case above: a refused
    # ``propose_plan_change`` would be taken as a submitted one.

    @staticmethod
    def _planning_route(tool_name: str, content: str, **msg_kwargs) -> str:
        from chaos_agent.agent.router import route_after_phase1_tools

        return route_after_phase1_tools({
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "id": "p1", "name": tool_name, "args": {},
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    content=content, name=tool_name, tool_call_id="p1",
                    **msg_kwargs,
                ),
            ],
        })

    def test_planning_route_skips_a_refused_plan_change(self):
        from chaos_agent.agent.node_names import AGENT_LOOP

        # factory.py's exact refusal rendering for a partial FaultSpec.
        content = (
            "Error: proposed_fault is a partial contract; a plan change "
            "replaces the reviewed FaultSpec wholesale and must carry every "
            "field. Missing or empty: boundaries, assumptions."
        )
        assert self._planning_route("propose_plan_change", content) == AGENT_LOOP

    def test_planning_route_skips_a_framework_error_result(self):
        from chaos_agent.agent.node_names import AGENT_LOOP

        assert self._planning_route(
            "finish_planning", "", status="error",
        ) == AGENT_LOOP

    def test_planning_route_skips_a_target_guard_rejection(self):
        from chaos_agent.agent.node_names import AGENT_LOOP

        # Wider than the local ``startswith("Error:")`` this replaced: a
        # route-gate rejection carries no tool-layer prefix, so the old
        # predicate read it as a genuine exit signal and ended Phase 1 on a
        # call that never ran.
        assert self._planning_route(
            "finish_planning", "[target_guard] REJECT_BANNED: ...",
        ) == AGENT_LOOP

    def test_planning_route_still_reads_a_genuine_exit_signal(self):
        from chaos_agent.agent.node_names import EXTRACT_PLANNING_METADATA

        # Behaviour-preserving: the real signal must still exit Phase 1.
        assert self._planning_route(
            "finish_planning", "Planning finalized. Summary: patch the cm",
        ) == EXTRACT_PLANNING_METADATA


# ---------------------------------------------------------------------------
# 5. The registry seam itself
# ---------------------------------------------------------------------------


class TestRegistryRouting:
    def test_unowned_tool_never_reaches_a_provider_hook(self):
        assert FaultProviderRegistry.tool_result_error_text("", "Error: x") is None
        assert (
            FaultProviderRegistry.tool_result_error_text("kubectl", '{"a": 1}') is None
        )

    def test_every_declared_tool_resolves_to_exactly_one_owner(self):
        # Overlapping claims are resolved by registration order, which is
        # silent and arbitrary — two carriers declaring the same tool name is
        # a programming error worth surfacing here rather than at runtime.
        owners: dict[str, list[str]] = {}
        for provider in FaultProviderRegistry.all_providers():
            for name in getattr(provider, "result_shape_tool_names", ()) or ():
                owners.setdefault(name, []).append(provider.carrier)
        duplicated = {k: v for k, v in owners.items() if len(v) > 1}
        assert not duplicated, f"tool result shape claimed twice: {duplicated}"
