"""Terminal reports node: postmortem generation + issue-report publishing.

Extracted from ``save_memory`` (task-349ccf5d) so every experiment
terminal path — ``se_detect`` and ``reject`` — funnels through report
generation BEFORE persistence:

    se_detect ───────────┐
                         ├→ terminal_reports → save_memory → batch_next/END
    reject ──────────────┘

The node only PRODUCES the report artifacts (``postmortem`` /
``issue_report`` state fields, R11 always-write). Persistence stays a
single concern of ``save_memory``'s ``sync_to_store``, so the task JSON
format is unchanged.

Observability wiring preserved verbatim from the original save_memory
implementation: StatusTracker events (CLI printer / SSE), TUI
``node_message`` lines, session JSONL ``record_aux_llm_call`` audit, and
the R10 tracing/OTel callbacks on the postmortem LLM call.
"""

import asyncio
import logging
import time

from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.nodes.store._store_sync import sync_node_status_to_session
from chaos_agent.agent.result.operation_outcome import (
    read_inject_verification,
    read_operation_outcome,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import (
    StatusCategory,
    get_tracker,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


def _format_duration_ms(ms) -> str:
    """Render a duration in ms as ``Ns`` / ``Nm Ns``; empty when unknown."""
    try:
        ms_int = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if ms_int <= 0:
        return ""
    seconds = ms_int // 1000
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


async def _generate_postmortem(
    state: AgentState, task_id: str, tracker,
) -> dict | None:
    """Generate postmortem report via LLM when conditions are met.

    All exceptions are swallowed so the result envelope ships unimpeded.
    """
    postmortem_payload: dict | None = None
    try:
        from chaos_agent.agent.postmortem import (
            build_postmortem_context,
            generate_postmortem,
            render_postmortem_markdown,
            save_postmortem,
            should_generate_postmortem,
        )
        from chaos_agent.agent.postmortem.generator import make_summary

        if should_generate_postmortem(dict(state), settings):
            tracker.start(
                StatusCategory.NODE, "postmortem",
                "Generating postmortem (LLM)...",
            )
            await dispatch_node_message("postmortem", "Generating postmortem (LLM)...")
            # R10 — wire the SAME tracing / OTel callbacks as the main
            # graph LLM so postmortem's token usage flows into
            # ``TaskTrace.total_token_input/output`` + OTel GenAI export.
            # enable_thinking=False: single-shot report generation where
            # reasoning tokens only add 6x+ latency (bench_thinking.py) —
            # but ONLY for models strong enough to absorb the disable
            # (>= 1M window; weak models keep thinking ON for quality,
            # see factory.aux_calls_can_skip_thinking).
            from chaos_agent.agent.factory import (
                aux_calls_can_skip_thinking,
                make_llm,
            )
            from chaos_agent.observability import status_tracker as _st_mod
            _pm_callbacks: list = []
            _trace_cb = getattr(_st_mod, "_tracing_callback", None)
            if _trace_cb is not None:
                _pm_callbacks.append(_trace_cb)
            _otel_cb = getattr(_st_mod, "_otel_callback", None)
            if _otel_cb is not None:
                _pm_callbacks.append(_otel_cb)
            pm_llm = make_llm(
                callbacks=_pm_callbacks or None,
                # None falls back to settings.llm_enable_thinking (ON by
                # default) when the capability gate withholds the disable.
                enable_thinking=False if aux_calls_can_skip_thinking() else None,
            )
            context = build_postmortem_context(
                dict(state),
                max_messages=settings.postmortem_max_messages,
            )
            try:
                _pm_t0 = time.monotonic()
                markdown_body = await generate_postmortem(
                    context, pm_llm,
                    timeout=settings.postmortem_timeout_seconds,
                )
                _pm_dur_ms = int((time.monotonic() - _pm_t0) * 1000)
                # Audit trail: the postmortem LLM call is off the main graph and
                # its request never enters ``messages`` — only the resulting
                # markdown is kept. Record the call so an audit can see what the
                # model was given, not just what it produced.
                try:
                    import json as _json

                    from chaos_agent.memory.session_store import (
                        get_global_session_store,
                    )
                    _store = get_global_session_store()
                    if _store is not None:
                        _store.record_aux_llm_call(
                            task_id, purpose="postmortem",
                            request=_json.dumps(context, ensure_ascii=False, default=str),
                            response=markdown_body or "",
                            duration_ms=_pm_dur_ms,
                        )
                except Exception as _e:
                    logger.debug("postmortem aux record skipped: %s", _e)
            except asyncio.TimeoutError:
                logger.warning(
                    "Postmortem LLM call timed out after %ds for task %s",
                    settings.postmortem_timeout_seconds, task_id,
                )
                tracker.update("Postmortem skipped (timeout)")
                markdown_body = ""
            except Exception as e:
                logger.warning(
                    "Postmortem LLM call failed for task %s: %s", task_id, e,
                )
                tracker.update(f"Postmortem skipped ({type(e).__name__})")
                markdown_body = ""

            if markdown_body:
                from chaos_agent.agent.result.operation_summary import (
                    format_summary_target,
                )
                from chaos_agent.agent.spec.fault_spec import (
                    fault_type_from_state,
                    legacy_params_dict,
                    legacy_target_dict,
                    read_fault_spec,
                )
                _spec = read_fault_spec(state)
                verification = read_inject_verification(state) or {}
                outcome = read_operation_outcome(state)
                # Deterministic facts the framework ALREADY knows. The
                # program renders these into the report header (single
                # source: render_postmortem_markdown) so they reach BOTH
                # the disk file and the platform-consumed payload verbatim,
                # with zero transcription variance. The generator prompt
                # tells the LLM these facts are already written above its
                # ``## Summary`` and to NOT restate them — the model owns
                # only the judgment sections plus the injection / recovery
                # ACTIONS and the resolved target that only the execution
                # trace reveals. Target/params reuse the canonical
                # legacy_*_dict projectors + format_summary_target so there
                # is no second, drifting rendering of the selector.
                _params = legacy_params_dict(dict(state))
                header_meta = {
                    "fault_type": fault_type_from_state(state) or "unknown",
                    "namespace": (_spec.namespace if _spec else "") or "unknown",
                    "status": verification.get("level", "unknown"),
                    "duration": _format_duration_ms(
                        outcome.result.get("duration_ms", 0)
                    ) if isinstance(outcome.result, dict) else "",
                    "generated_at": now_iso(),
                    "user_request": (
                        state.get("input")
                        or (_spec.user_description if _spec else "")
                        or ""
                    ).strip(),
                    "target": format_summary_target(
                        legacy_target_dict(dict(state))
                    ),
                    "params": ", ".join(
                        f"{k}={v}" for k, v in _params.items()
                    ) if _params else "",
                }
                # Render ONCE here for the payload; save_postmortem re-
                # renders the SAME string from the SAME inputs (pure
                # function) for disk. That is what makes the on-disk file
                # and payload["markdown"] byte-identical — replacing the
                # old divergence where the disk carried a header the
                # payload never saw and the platform read a body-only
                # report missing every fact above.
                full_markdown = render_postmortem_markdown(
                    task_id, markdown_body, header_meta=header_meta,
                )
                try:
                    pm_path = save_postmortem(
                        task_id, markdown_body, header_meta=header_meta,
                    )
                    postmortem_payload = {
                        "path": str(pm_path),
                        "markdown": full_markdown,
                        "summary": make_summary(full_markdown),
                    }
                    tracker.update(
                        f"Postmortem saved ({len(full_markdown)} chars)",
                    )
                except Exception as e:
                    logger.warning(
                        "Postmortem write failed for task %s: %s", task_id, e,
                    )
                    tracker.update("Postmortem skipped (write error)")
    except Exception:
        logger.exception("Postmortem subsystem unexpected error for task %s", task_id)
    # Emit a completion event so the platform UI can show postmortem finished.
    # The tracker.start("postmortem", ...) is emitted inside the try block;
    # this complete pairs with it regardless of success/skip/error.
    try:
        tracker.complete(
            f"Postmortem {'generated' if postmortem_payload else 'skipped'}"
        )
    except Exception:
        pass
    return postmortem_payload


async def _publish_issue_report(
    state: AgentState, task_id: str, postmortem_payload: dict | None,
) -> dict | None:
    """Publish the failure report as a GitHub issue (info collection).

    Fire-and-forget with postmortem-identical semantics: gated by the
    EXISTING failure criterion (no new failure semantics), all
    exceptions swallowed so the result envelope ships unimpeded.
    Returns the TUI-facing payload, or None when the mechanism is
    disabled / this is not a failed inject (no hint rendered).
    """
    try:
        from chaos_agent.agent.issue_report import (
            publish_issue_report,
            should_publish_issue,
        )

        if not should_publish_issue(dict(state), settings):
            return None
        await dispatch_node_message(
            "issue_report", "Publishing drill failure report to GitHub...",
        )
        payload = await publish_issue_report(
            dict(state), task_id, postmortem_payload,
        )
        status = str(payload.get("status", ""))
        archive = str(payload.get("archive_path") or "")
        if status == "success":
            summary = f"failure report published: {payload.get('issue_url', '')}"
        elif status == "skipped_cap":
            summary = "failure report skipped (daily cap reached)"
            if archive:
                summary += f"; report archived: {archive}"
        else:
            summary = f"failure report publish failed: {payload.get('error', '')}"
            if archive:
                summary += f"; report archived: {archive}"
        await dispatch_node_message("issue_report", summary)
        return payload
    except Exception:
        logger.exception("Issue report subsystem unexpected error for task %s", task_id)
        return None


async def terminal_reports_node(state: AgentState) -> dict:
    """Generate terminal report artifacts before persistence.

    Runs on every experiment terminal path (se_detect / reject) ahead of
    ``save_memory``. Produces the
    ``postmortem`` and ``issue_report`` state fields; R11 — BOTH keys
    are ALWAYS written (even when None) to overwrite any leftover value
    from a prior experiment that shares this LangGraph thread.

    Non-injection intents (chat / recover-bridge) short-circuit the
    same way ``save_memory`` does: no fault experiment to report on.
    """
    task_id = state.get("task_id", "") or ""
    confirmed_intent = state.get("confirmed_intent")

    if confirmed_intent in ("chat", "recover"):
        return {"postmortem": None, "issue_report": None}

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "terminal_reports",
        "Generating terminal reports (postmortem / issue)",
    )

    # The tracker is a flat single-source model: _generate_postmortem
    # runs its own start/complete lifecycle under source "postmortem"
    # when the gate fires, which overwrites _current_source. Save and
    # restore so this node's own COMPLETED event below is attributed to
    # "terminal_reports", not mis-labelled "postmortem".
    _tracker_saved = tracker.save_state()
    postmortem_payload = await _generate_postmortem(state, task_id, tracker)
    tracker.restore_state(_tracker_saved)
    issue_report_payload = await _publish_issue_report(
        dict(state), task_id, postmortem_payload,
    )

    sync_node_status_to_session(
        state, "terminal_reports",
        f"Terminal reports: postmortem="
        f"{'generated' if postmortem_payload else 'skipped'}, "
        f"issue_report={'published' if issue_report_payload else 'skipped'}",
    )
    tracker.complete("Terminal reports generated")

    return {
        "postmortem": postmortem_payload,
        "issue_report": issue_report_payload,
    }
