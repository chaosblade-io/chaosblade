"""FaultDrill CRD lazy-install and channel-unavailability decision family.

Design D7 of openspec change ``faultdrill-cr-channel``: the CR channel is
NOT assumed available — restricted credentials, platform CRD governance,
OPA/Kyverno policies and multi-tenant clusters make "cannot install" a
NORMAL branch, not an error. Every unavailability below is a ROUTING
signal (degrade to the SOP recovery form), never a task failure:

- ``probe-forbidden``    — cannot even read the CRD (RBAC denies get)
- ``crd-incompatible``   — an old CRD exists but its schema lacks the
                           load-bearing declarations (items-level
                           preserve-unknown-fields, invalidSecret source
                           form): "exists" is NOT "usable"
- ``apply-forbidden``    — the install apply was denied (no CRD create)
- ``apply-error``        — the install apply failed otherwise
- ``established-timeout``— applied but never reached ``Established``
                           within ``faultdrill_crd_established_timeout_seconds``
- ``probe-error`` / ``invalid-json`` — connectivity / parse failures

The install itself rides the programmatic transport seam
(``exec_kubectl_raw`` → ``execute_via_transport``): ``kubectl apply`` is
ToolGuard-whitelisted at the COMMAND level (guard.py), while the
manifest-kind allowlist that guards the LLM apply face never runs on this
path — the CRD is installed by the channel, deliberately NOT exposed on
the LLM face (design D2: no LLM-triggered CRD installs).

All kubectl execution goes through :func:`_kubectl` so tests can patch
one seam; the module itself stays import-light (lazy tool imports, same
discipline as the provider package).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from .crd import (
    build_crd_yaml,
    crd_full_name,
    verify_crd_compatibility,
)

logger = logging.getLogger(__name__)

# Availability verdicts: "ready" (existing & compatible), "installed"
# (freshly created & Established), "unavailable" (degradation signal —
# the caller routes to the SOP recovery form).
_STATUS_READY = "ready"
_STATUS_INSTALLED = "installed"
_STATUS_UNAVAILABLE = "unavailable"

# Established-poll cadence (seconds). The experiment measured ~2.2s to
# Established; the budget itself comes from settings (default 60s).
_ESTABLISHED_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class CrdAvailability:
    """Result of the CRD availability decision family."""

    status: str
    reason: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status != _STATUS_UNAVAILABLE

    def __str__(self) -> str:  # probe-observation rendering
        if self.usable:
            return f"faultdrill CRD {self.status}" + (
                f" ({self.detail})" if self.detail else ""
            )
        return f"faultdrill CRD unavailable ({self.reason}): {self.detail}"


async def _kubectl(
    subcommand: str, v_args: list[str], kubeconfig: str, *,
    stdin_data: str = "", timeout: float = 30.0,
):
    """Single execution seam (patch point for tests; lazy tool import)."""
    from chaos_agent.tools.kubectl import exec_kubectl_raw

    return await exec_kubectl_raw(
        subcommand, v_args, kubeconfig, timeout=timeout, stdin_data=stdin_data,
    )


def _group() -> str:
    from chaos_agent.config.settings import settings

    return settings.faultdrill_crd_group


def _established_budget() -> float:
    from chaos_agent.config.settings import settings

    return float(settings.faultdrill_crd_established_timeout_seconds)


def _is_not_found(stderr: str) -> bool:
    text = (stderr or "").lower()
    return "not found" in text or "(notfound)" in text


def _is_forbidden(stderr: str) -> bool:
    return "forbidden" in (stderr or "").lower()


def _established(crd_json: dict) -> bool:
    """True when the CRD's Established condition is True."""
    try:
        conditions = crd_json["status"]["conditions"] or []
    except (KeyError, TypeError):
        return False
    for cond in conditions:
        if isinstance(cond, dict) and cond.get("type") == "Established":
            return cond.get("status") == "True"
    return False


async def _read_crd(name: str, kubeconfig: str):
    """``kubectl get crd <name> -o json`` → (exit_code, parsed_json_or_None, stderr)."""
    result = await _kubectl("get", ["crd", name, "-o", "json"], kubeconfig)
    if result.exit_code != 0:
        return result.exit_code, None, result.stderr or ""
    try:
        parsed = json.loads(result.stdout)
    except (ValueError, TypeError):
        return -1, None, "crd get returned unparseable JSON"
    if not isinstance(parsed, dict):
        return -1, None, "crd get returned non-object JSON"
    return 0, parsed, ""


async def probe_crd(kubeconfig: str = "") -> CrdAvailability:
    """Read-only availability check (probe face, safe to call anywhere).

    Never installs — the preplan probe (M2 task 2.5) and the replan path
    call this to observe; only :func:`ensure_crd` mutates.
    """
    name = crd_full_name(_group())
    code, crd_json, stderr = await _read_crd(name, kubeconfig)
    if code != 0:
        if _is_not_found(stderr):
            return CrdAvailability(
                _STATUS_UNAVAILABLE, "not-found",
                "CRD not installed (fresh install possible via ensure_crd)",
            )
        if _is_forbidden(stderr):
            return CrdAvailability(
                _STATUS_UNAVAILABLE, "probe-forbidden",
                "RBAC denies reading customresourcedefinitions",
            )
        return CrdAvailability(
            _STATUS_UNAVAILABLE, "probe-error", (stderr or "crd read failed")[:200],
        )
    ok, why = verify_crd_compatibility(crd_json)
    if not ok:
        return CrdAvailability(
            _STATUS_UNAVAILABLE, "crd-incompatible",
            f"existing CRD lacks load-bearing declarations: {why}",
        )
    return CrdAvailability(
        _STATUS_READY, "", f"group {_group()}, schema compatible",
    )


async def ensure_crd(kubeconfig: str = "") -> CrdAvailability:
    """Lazy install + full unavailability decision family (D7).

    Decision order: probe → (exists: schema compatibility — an old CRD
    counts as NOT installable) | (absent: programmatic apply + Established
    poll with the configured budget — timeout is a degradation signal) |
    (denied anywhere: degradation signal).
    """
    probed = await probe_crd(kubeconfig)
    if probed.status == _STATUS_READY:
        return probed
    if probed.reason != "not-found":
        # Forbidden / incompatible / error — degrade without an install
        # attempt (an incompatible CRD would only be mutated by an owner
        # we do not have; a denied read predicts a denied create).
        return probed

    # Absent → lazy install (programmatic declarative apply, D2).
    name = crd_full_name(_group())
    result = await _kubectl(
        "apply", ["-f", "-"], kubeconfig,
        stdin_data=build_crd_yaml(_group()),
    )
    if result.exit_code != 0:
        stderr = result.stderr or ""
        if _is_forbidden(stderr):
            return CrdAvailability(
                _STATUS_UNAVAILABLE, "apply-forbidden",
                "RBAC denies creating customresourcedefinitions",
            )
        return CrdAvailability(
            _STATUS_UNAVAILABLE, "apply-error", stderr[:200] or "apply failed",
        )

    # Applied → wait for Established (apiserver accepts the type) within
    # the configured budget. Over-budget = degradation signal (D7), not
    # a task failure — a late-Established CRD serves the NEXT task.
    budget = _established_budget()
    deadline = asyncio.get_running_loop().time() + budget
    while True:
        code, crd_json, stderr = await _read_crd(name, kubeconfig)
        if code == 0 and _established(crd_json):
            return CrdAvailability(
                _STATUS_INSTALLED, "",
                f"created, Established within {budget:.0f}s budget",
            )
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(_ESTABLISHED_POLL_SECONDS)
    return CrdAvailability(
        _STATUS_UNAVAILABLE, "established-timeout",
        f"applied but not Established within {budget:.0f}s "
        f"(last read: {(stderr or 'no Established condition')[:120]})",
    )
