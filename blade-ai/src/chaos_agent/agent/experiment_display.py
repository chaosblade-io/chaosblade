"""Human-readable rendering of active fault experiments for disambiguation.

Both the ``query_active_experiments`` LLM tool and the ``recover_handler``
fallback list active experiments so the user (or the LLM) can pick which one
to recover. They must show the *same* discriminating fields — injection time,
target resource, real fault type, plan summary — so this single formatter is
their shared source of truth.

Presentation only: depends on nothing beyond ``utils.time`` and degrades
gracefully on any missing field (never raises).
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
    return head
