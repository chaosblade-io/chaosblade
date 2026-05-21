"""Patch D — pluggable target health checker.

Why this module exists:

    The user-reported turn (task-9209c7052240) burned 5+ minutes
    trying to inject a CPU fullload onto a node that had been
    ``DiskPressure=True`` for 103 days. Kubernetes reported the node
    as ``Ready`` (the only signal the agent_loop was reading), so
    the LLM happily picked it as the inject target. The kubelet
    eviction loop made the ChaosBlade Agent pod unschedulable and
    every ``blade create`` attempt failed.

    The fix: before the LLM enters the confirm gate, run a
    scope-specific *health pre-check* that surfaces blocker
    conditions in the confirm card payload. The user (or the LLM in
    auto-mode) can see "this target has DiskPressure=True for 103d"
    and pick a different node.

Design (kept deliberately small):

    1. ``HealthSeverity`` is the routing-relevant outcome
       (``OK / WARN / BLOCK``).
    2. ``HealthIssue`` carries a stable ``code`` (e.g.
       ``node.disk_pressure``) so downstream logging / metrics /
       i18n can key off it without parsing the human ``message``.
    3. ``TargetHealthChecker`` is a Protocol — each scope (node /
       pod / namespace / future deployment / kafka topic) plugs in
       its own checker. Built-in node + pod checkers cover the
       chaos-engineering 80%; skill packs can register more.
    4. ``assess_target_health`` is the single entry — agent_loop
       calls it once, gets a ``HealthReport``, attaches it to the
       confirm payload. No graph topology change.

The checkers themselves do *not* run shell commands directly here —
they're stubs that document the kubectl logic. Wiring real kubectl
calls is the job of the integration layer (``inject_context.py`` /
existing kubectl wrapper). Tests mock the checker output.

Backwards-compat:

    Default ``settings.target_health_check_enabled = True`` but
    ``settings.target_health_check_block_on_blocker = False`` — i.e.
    we attach the report to the confirm card but **never** silently
    veto an inject. The user / LLM still gets to decide. Set the
    block flag to ``True`` to opt in to hard blocking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class HealthSeverity(Enum):
    """How worried should the operator be about this target."""

    OK = "ok"
    """No issues found."""

    WARN = "warn"
    """Injectable but flagged — the inject may still work but the
    target has anomalies (e.g. high but not extreme load)."""

    BLOCK = "block"
    """Likely-fatal precondition — inject will fail or is unsafe.

    Whether this is enforced as a hard block depends on
    ``settings.target_health_check_block_on_blocker``; the default is
    ``False`` (warn-only).
    """


@dataclass
class HealthIssue:
    """A single problem found by a checker."""

    severity: HealthSeverity
    code: str
    """Stable machine-readable identifier (e.g. ``node.disk_pressure``).

    Convention: ``<scope>.<condition>``. Used for log keying, i18n,
    metrics. Never localised, never user-facing.
    """

    message: str
    """Human-readable description suitable for confirm card display."""

    duration_hint: str = ""
    """Rough duration the condition has been active, e.g. ``103d``.

    Empty if the checker can't tell. Useful for the LLM / operator
    to gauge whether retry would help — a 103d-old DiskPressure is
    different from a 30s blip.
    """


@dataclass
class HealthReport:
    """Aggregated outcome of a target health pre-check."""

    target: dict
    """The target spec the checker examined (echoed for audit)."""

    overall: HealthSeverity
    """Worst severity found across ``issues``. Drives routing."""

    issues: list[HealthIssue] = field(default_factory=list)
    """All problems found, ordered by ``severity`` desc then ``code``."""

    def is_blocking(self) -> bool:
        """True iff a hard-block condition was found."""
        return self.overall == HealthSeverity.BLOCK

    def has_warnings(self) -> bool:
        """True iff any non-OK issue was found."""
        return self.overall != HealthSeverity.OK

    def summary(self) -> str:
        """Compact one-liner for log lines / confirm card subtitle."""
        if not self.issues:
            return "healthy"
        return "; ".join(
            f"{i.code}({i.severity.value})" for i in self.issues
        )

    def to_dict(self) -> dict:
        """Serialised form embedded into confirm-card payload."""
        return {
            "target": self.target,
            "overall": self.overall.value,
            "issues": [
                {
                    "severity": i.severity.value,
                    "code": i.code,
                    "message": i.message,
                    "duration_hint": i.duration_hint,
                }
                for i in self.issues
            ],
            "summary": self.summary(),
        }


class TargetHealthChecker(Protocol):
    """Plugin interface — one implementation per inject scope."""

    scope: str
    """Scope this checker covers (e.g. ``"node"`` / ``"pod"``)."""

    async def check(
        self, target: dict, kubeconfig: str
    ) -> HealthReport:
        """Inspect ``target`` and return a ``HealthReport``."""
        ...


# ---------------------------------------------------------------------------
# Built-in checkers — small, pure, easy to test
# ---------------------------------------------------------------------------


class NodeHealthChecker:
    """Detects node-level conditions that block kubelet from scheduling.

    Inspects the four pressure conditions kubelet enforces:
      - DiskPressure
      - MemoryPressure
      - NetworkUnavailable
      - PIDPressure

    Any of these in ``status: "True"`` is a BLOCK. The kubelet will
    refuse to schedule new pods, which means the ChaosBlade Agent
    DaemonSet pod (the thing that does the actual fault injection)
    won't come back if it gets evicted.

    Real implementation runs ``kubectl get node {name} -o json`` and
    parses ``.status.conditions``. The stub below documents the
    expected return shape so tests / wiring can mock it.
    """

    scope = "node"

    async def check(
        self, target: dict, kubeconfig: str
    ) -> HealthReport:
        from chaos_agent.utils.coerce import coerce_to_list

        names = coerce_to_list(
            target.get("names"), context="NodeHealthChecker:names"
        )
        if not names:
            return HealthReport(
                target=target,
                overall=HealthSeverity.OK,
                issues=[],
            )

        # Defer real kubectl to the integration layer; the helper
        # below is the only function patched in unit tests.
        conditions = await _query_node_conditions(names[0], kubeconfig)
        return _build_node_report(target, conditions)


class PodHealthChecker:
    """Detects pod-level conditions that prevent fault execution.

    A pod is BLOCK when:
      - ``status.phase`` is ``Pending`` / ``Failed`` / ``Unknown``
      - ``status.conditions`` has ``Ready=False`` and
        ``reason`` matches ``Evicted`` / ``CrashLoopBackOff`` /
        ``ImagePullBackOff``

    Real implementation: ``kubectl get pod {name} -n {namespace} -o
    json``. Stub below.
    """

    scope = "pod"

    async def check(
        self, target: dict, kubeconfig: str
    ) -> HealthReport:
        from chaos_agent.utils.coerce import coerce_to_list

        names = coerce_to_list(
            target.get("names"), context="PodHealthChecker:names"
        )
        namespace = target.get("namespace", "default")
        if not names:
            return HealthReport(
                target=target,
                overall=HealthSeverity.OK,
                issues=[],
            )

        status = await _query_pod_status(names[0], namespace, kubeconfig)
        return _build_pod_report(target, status)


# ---------------------------------------------------------------------------
# Registry — extensible by skill packs
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, TargetHealthChecker] = {
    "node": NodeHealthChecker(),
    "pod": PodHealthChecker(),
}


def register_health_checker(checker: TargetHealthChecker) -> None:
    """Register a custom checker for a new scope.

    Skill packs / third-party plugins call this at import time. The
    last registration for a scope wins (same convention as Python
    package overrides).
    """
    _REGISTRY[checker.scope] = checker
    logger.info(
        "registered target health checker for scope=%s", checker.scope
    )


async def assess_target_health(
    scope: str,
    target: dict,
    kubeconfig: str = "",
) -> HealthReport:
    """Single entry point — agent_loop calls this once per turn.

    Returns a ``HealthReport`` regardless of scope; an unknown scope
    returns an empty OK report (graceful degradation — never raise,
    never block on a checker bug).
    """
    checker = _REGISTRY.get(scope)
    if checker is None:
        logger.debug(
            "no health checker for scope=%s, skipping", scope
        )
        return HealthReport(
            target=target, overall=HealthSeverity.OK, issues=[]
        )
    try:
        return await checker.check(target, kubeconfig)
    except Exception as exc:
        # A checker bug must NOT take down the inject pipeline. Log
        # and degrade to OK so confirm proceeds with no info.
        logger.warning(
            "health checker for scope=%s failed: %s",
            scope,
            exc,
        )
        return HealthReport(
            target=target, overall=HealthSeverity.OK, issues=[]
        )


# ---------------------------------------------------------------------------
# Pure helpers (separated so they're trivially testable without async)
# ---------------------------------------------------------------------------


_NODE_BLOCKING_CONDITIONS = {
    "DiskPressure": "node.disk_pressure",
    "MemoryPressure": "node.memory_pressure",
    "NetworkUnavailable": "node.network_unavailable",
    "PIDPressure": "node.pid_pressure",
}


def _build_node_report(target: dict, conditions: list[dict]) -> HealthReport:
    """Translate kubectl ``status.conditions`` array into a HealthReport.

    ``conditions`` shape::

        [{"type": "DiskPressure", "status": "True",
          "lastTransitionTime": "2026-02-08T12:34:56Z", ...}, ...]
    """
    issues: list[HealthIssue] = []
    for cond in conditions or []:
        ctype = cond.get("type", "")
        cstatus = cond.get("status", "")
        if cstatus != "True":
            continue
        code = _NODE_BLOCKING_CONDITIONS.get(ctype)
        if not code:
            continue
        duration = _format_condition_duration(
            cond.get("lastTransitionTime", "")
        )
        issues.append(
            HealthIssue(
                severity=HealthSeverity.BLOCK,
                code=code,
                message=f"Node has {ctype}=True for {duration or 'unknown duration'}",
                duration_hint=duration,
            )
        )

    overall = HealthSeverity.BLOCK if issues else HealthSeverity.OK
    return HealthReport(target=target, overall=overall, issues=issues)


def _build_pod_report(target: dict, status: dict) -> HealthReport:
    """Translate kubectl ``status`` block into a HealthReport.

    ``status`` shape::

        {"phase": "Running", "conditions": [...], "reason": "Evicted"?}
    """
    issues: list[HealthIssue] = []
    phase = status.get("phase", "")
    reason = status.get("reason", "")

    if phase in {"Pending", "Failed", "Unknown"}:
        severity = HealthSeverity.BLOCK if phase != "Pending" else HealthSeverity.WARN
        issues.append(
            HealthIssue(
                severity=severity,
                code=f"pod.phase.{phase.lower()}",
                message=f"Pod phase is {phase}"
                + (f" (reason: {reason})" if reason else ""),
            )
        )

    if reason in {"Evicted", "CrashLoopBackOff", "ImagePullBackOff"}:
        issues.append(
            HealthIssue(
                severity=HealthSeverity.BLOCK,
                code=f"pod.reason.{reason.lower()}",
                message=f"Pod reason: {reason}",
            )
        )

    if issues:
        # Aggregate severity = max
        overall = HealthSeverity.BLOCK if any(
            i.severity == HealthSeverity.BLOCK for i in issues
        ) else HealthSeverity.WARN
    else:
        overall = HealthSeverity.OK

    return HealthReport(target=target, overall=overall, issues=issues)


def _format_condition_duration(iso_timestamp: str) -> str:
    """Format ``2026-02-08T12:34:56Z`` → ``"103d"`` style hint.

    Best effort — returns empty string on parse failure (caller
    handles missing duration gracefully).
    """
    if not iso_timestamp:
        return ""
    from datetime import datetime, timezone

    try:
        ts = iso_timestamp.replace("Z", "+00:00")
        then = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc)
        delta = now - then
        days = delta.days
        if days >= 1:
            return f"{days}d"
        hours = delta.total_seconds() // 3600
        if hours >= 1:
            return f"{int(hours)}h"
        return f"{int(delta.total_seconds() // 60)}m"
    except (ValueError, AttributeError):
        return ""


# ---------------------------------------------------------------------------
# Async stubs — patched by integration layer / mocked by tests
# ---------------------------------------------------------------------------


async def _query_node_conditions(
    node_name: str, kubeconfig: str
) -> list[dict]:
    """Real impl: ``kubectl get node {name} -o json``, return
    ``.status.conditions``.

    Errors are swallowed — health-check failures must NOT crash the
    inject pipeline. Returns empty list (= OK report) on any kubectl
    issue. The kubectl wrapper itself emits structured error logs so
    the swallow is observable upstream.
    """
    if not node_name:
        return []
    from chaos_agent.tools.kubectl import kubectl as _kubectl

    try:
        raw = await _kubectl(
            subcommand="get",
            v_args=f"node {node_name} -o json",
            kubeconfig=kubeconfig or "",
        )
    except Exception as exc:
        logger.warning(
            "_query_node_conditions: kubectl error for %s: %s",
            node_name, exc,
        )
        return []

    if not raw:
        return []
    # The kubectl wrapper returns either raw JSON / formatted output /
    # an error string ("Error: ..."). Parse defensively.
    import json as _json
    s = raw.strip()
    if s.startswith("Error"):
        logger.debug("_query_node_conditions: kubectl reported error: %s", s[:200])
        return []
    try:
        data = _json.loads(s)
    except _json.JSONDecodeError:
        logger.debug("_query_node_conditions: non-JSON output for %s", node_name)
        return []
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        return []
    conds = status.get("conditions") or []
    if not isinstance(conds, list):
        return []
    return conds


async def _query_pod_status(
    pod_name: str, namespace: str, kubeconfig: str
) -> dict:
    """Real impl: ``kubectl get pod -n {ns} {name} -o json``, return
    ``.status``.

    Same defensive pattern as ``_query_node_conditions`` — never
    raises, returns ``{}`` on any failure.
    """
    if not pod_name:
        return {}
    from chaos_agent.tools.kubectl import kubectl as _kubectl

    args = f"pod {pod_name}"
    if namespace:
        args += f" -n {namespace}"
    args += " -o json"

    try:
        raw = await _kubectl(
            subcommand="get",
            v_args=args,
            kubeconfig=kubeconfig or "",
        )
    except Exception as exc:
        logger.warning(
            "_query_pod_status: kubectl error for %s/%s: %s",
            namespace, pod_name, exc,
        )
        return {}

    if not raw:
        return {}
    import json as _json
    s = raw.strip()
    if s.startswith("Error"):
        logger.debug("_query_pod_status: kubectl reported error: %s", s[:200])
        return {}
    try:
        data = _json.loads(s)
    except _json.JSONDecodeError:
        return {}
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        return {}
    # Surface the top-level reason if any — used by _build_pod_report
    # to detect Evicted / CrashLoopBackOff at a glance without walking
    # the conditions array.
    out = {
        "phase": status.get("phase", ""),
        "reason": status.get("reason", "")
        or data.get("metadata", {}).get("annotations", {}).get(
            "kubernetes.io/eviction-reason", ""
        ),
        "conditions": status.get("conditions") or [],
    }
    # Inspect container statuses for CrashLoopBackOff / ImagePullBackOff
    # which surface in waiting.reason rather than top-level reason.
    for cs in status.get("containerStatuses") or []:
        waiting = (cs.get("state") or {}).get("waiting") or {}
        wreason = waiting.get("reason", "")
        if wreason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
            out["reason"] = wreason
            break
    return out
