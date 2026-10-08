"""Human-readable rendering of active fault experiments for disambiguation.

Both the ``query_active_experiments`` LLM tool and the ``recover_handler``
fallback list active experiments so the user (or the LLM) can pick which one
to recover. They must show the *same* discriminating fields — injection time,
target resource, real fault type, plan summary — so this single formatter is
their shared source of truth.

Presentation only: its only imports are ``utils.time`` plus deferred,
exception-guarded reads of ``execution_artifacts.RECOVERY_CARRIER_TYPES`` and
the stdlib clock, and it degrades gracefully on any missing field (never
raises).
"""

from __future__ import annotations

from chaos_agent.utils.time import format_relative_time


def _target_descriptor(experiment: dict) -> str:
    """Compact ``namespace/name`` (or ``namespace (labels)``) target string."""
    target = experiment.get("target") or {}
    namespace = target.get("namespace") or "?"
    names = target.get("names") or []
    labels = target.get("labels") or {}
    if names:
        return f"{namespace}/{','.join(str(n) for n in names)}"
    if labels:
        rendered = ",".join(f"{k}={v}" for k, v in labels.items())
        return f"{namespace} ({rendered})"
    # target_name is the indexed first-name column; use it as a last resort.
    tname = experiment.get("target_name") or ""
    if tname:
        return f"{namespace}/{tname}"
    return namespace


def _carrier_fact_line(experiment: dict) -> str:
    """One FACTUAL line about the recovery-bearing carriers on this row.

    States only what the framework OBSERVED — carrier form, last recorded
    status, whether the reversal was voided, whether a detached timer's
    deadline has passed, or that no reversal carrier was ever registered. It
    renders NO verdict (never "live" / "cleared" / "recover this"): combining
    these facts with a live cluster probe is the LLM's call, not the
    presenter's. Returns "" when there is nothing factual to add.

    Degrades gracefully on any missing/malformed field (never raises), per
    this module's presentation-only contract.
    """
    arts = experiment.get("execution_artifacts")
    if not isinstance(arts, list):
        return ""
    try:
        from chaos_agent.agent.execution_artifacts import RECOVERY_CARRIER_TYPES
    except Exception:  # pragma: no cover — import must never break rendering
        return ""

    carriers = [
        a for a in arts
        if isinstance(a, dict) and a.get("type") in RECOVERY_CARRIER_TYPES
    ]
    if not carriers:
        # No reversal carrier registered. Either a bare native mutation (no
        # vehicle at all) or only a probe channel rode along — either way the
        # framework recorded NO teardown of the fault itself.
        has_probe = any(
            isinstance(a, dict) and a.get("type") == "debug_pod" for a in arts
        )
        if has_probe:
            return (
                "carrier: only a probe channel (debug_pod) registered — not a "
                "reversal carrier; no teardown of the fault itself observed"
            )
        return "carrier: none registered — no teardown of the fault observed"

    import time as _time
    now = _time.time()
    segs = []
    for a in carriers:
        seg = f"{a.get('type') or '?'}={a.get('status') or '?'}"
        if a.get("recovery_void"):
            seg += ",recovery_void(reversal died with carrier)"
        if a.get("recovery_form") == "host_timer":
            deadline = a.get("recovery_deadline_epoch")
            try:
                state = "passed" if float(deadline) <= now else "pending"
            except (TypeError, ValueError):
                state = "unknown"
            seg += f",host_timer deadline {state}"
        segs.append(seg)
    return "carrier: " + "; ".join(segs)


def format_experiment_line(idx: int, experiment: dict) -> str:
    """Render one active experiment as an indented, discriminating list item.

    Example::

        1. [yesterday 15:02] task_id=task-787124d0  fault: pod-image-error  target: reg-center/registry-sts
            description: point StatefulSet registry-sts at the invalid image nginx:doesnotexist

    ``fault_type`` (the derived ``{scope}-{target}-{action}`` projection) is
    preferred over ``skill`` so the line isn't the generic skill package name
    (e.g. ``k8s-chaos-skills``) that makes every experiment look identical.
    """
    tid = experiment.get("task_id", "?")
    fault = experiment.get("fault_type") or experiment.get("skill") or "?"
    target = _target_descriptor(experiment)

    when = format_relative_time(experiment.get("gmt_create", ""))
    time_prefix = f"[{when}] " if when else ""

    head = (
        f"  {idx}. {time_prefix}task_id={tid}  "
        f"fault: {fault}  target: {target}"
    )

    summary = (experiment.get("plan_summary") or "").strip()
    if summary:
        first_line = summary.splitlines()[0][:80]
        if first_line:
            head += f"\n      description: {first_line}"
    carrier_fact = _carrier_fact_line(experiment)
    if carrier_fact:
        head += f"\n      {carrier_fact}"
    return head
