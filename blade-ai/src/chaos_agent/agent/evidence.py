"""Deterministic evidence-coverage contracts for baseline and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.transports import (
    PROFILE_HOST,
    PROFILE_K8S,
    strip_execution_location as _strip_location,
)

_PRIMARY_PATTERNS = {
    "cpu": ("cpu", "top", "mpstat", "sar", "/proc/stat", "load"),
    "mem": ("memory", "mem", "free", "vmstat", "/proc/meminfo", "oom"),
    "disk": ("disk", "df ", "iostat", "du ", "/proc/diskstats", "pressure"),
    "network": ("network", "endpoint", "ss ", "ip -s", "netstat", "/proc/net"),
    "process": ("process", "ps ", "systemctl", "restart", "event", "status"),
}

_CROSS_PATTERNS = {
    "cpu": ("describe", "condition", "load", "uptime", "mpstat", "/proc/stat"),
    "mem": ("describe", "condition", "oom", "free", "vmstat", "/proc/meminfo"),
    "disk": ("describe", "pressure", "df ", "iostat", "du ", "/proc/diskstats"),
    "network": ("describe", "endpoint", "ss ", "ip -s", "netstat", "/proc/net"),
    "process": ("describe", "event", "ps ", "systemctl", "status"),
}


@dataclass(frozen=True)
class EvidenceCoverage:
    profile_id: str
    required: tuple[str, ...]
    covered: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "required": list(self.required),
            "covered": list(self.covered),
            "missing": list(self.missing),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class EvidenceProfile:
    """Minimum independent evidence required for a concrete fault target."""

    profile_id: str
    transport_profile: str
    target_names: tuple[str, ...]
    target: str
    enabled: bool = True

    @classmethod
    def for_fault(cls, spec: FaultSpec | None, transport_profile: str) -> "EvidenceProfile":
        if (
            spec is None
            or transport_profile not in (PROFILE_K8S, PROFILE_HOST)
            or not spec.blade_target
        ):
            return cls("unknown", transport_profile, (), "", enabled=False)
        return cls(
            profile_id=f"{transport_profile}:{spec.scope}:{spec.blade_target}",
            transport_profile=transport_profile,
            target_names=tuple(spec.names),
            target=spec.blade_target,
        )

    @property
    def required(self) -> tuple[str, ...]:
        return ("target_identity", "primary_metric", "independent_cross_metric") if self.enabled else ()

    def coverage(self, records: Iterable[object]) -> EvidenceCoverage:
        if not self.enabled:
            return EvidenceCoverage(self.profile_id, (), (), ())

        texts = [_record_text(record).lower() for record in records]
        texts = [text for text in texts if text]
        covered: list[str] = []
        if self._has_identity(texts):
            covered.append("target_identity")
        primary_patterns = _PRIMARY_PATTERNS.get(self.target, (self.target,))
        primary_indices = {
            index for index, text in enumerate(texts)
            if _matches(text, primary_patterns)
        }
        if primary_indices:
            covered.append("primary_metric")
        cross_patterns = _CROSS_PATTERNS.get(self.target, ("describe", "status"))
        cross_indices = {
            index for index, text in enumerate(texts)
            if _matches(text, cross_patterns)
        }
        # A single command may mention both metric vocabularies (for example
        # ``free`` is a memory measurement, not an independent cross-check).
        # Require a different observation record so verification cannot promote
        # one piece of evidence into two independent sources.
        if any(
            cross_index != primary_index
            for primary_index in primary_indices
            for cross_index in cross_indices
        ):
            covered.append("independent_cross_metric")

        missing = tuple(item for item in self.required if item not in covered)
        return EvidenceCoverage(self.profile_id, self.required, tuple(covered), missing)

    def _has_identity(self, texts: list[str]) -> bool:
        if self.transport_profile == PROFILE_HOST:
            return any("hostname" in text or "host identity" in text for text in texts)
        if any(name.lower() in text for name in self.target_names for text in texts):
            return True
        # A selector-based target may not expose individual names. Its label
        # selector still anchors the observation to the intended resource set.
        return any("-l " in text or "label selector" in text for text in texts)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)


# Read-only host probes that anchor evidence-coverage gaps. Shared by baseline
# capture and verification finalize so both anchor identity / cross the same
# way — a single source of truth prevents the two supplement paths from
# drifting apart.
_HOST_IDENTITY_SUPPLEMENT: tuple[str, tuple[str, ...]] = ("Host identity", ("hostname",))
_HOST_CROSS_SUPPLEMENTS: dict[str, tuple[str, ...]] = {
    "cpu": ("uptime",),
    "mem": ("vmstat", "-s"),
    "disk": ("df", "-h"),
    "network": ("ss", "-s"),
    "process": ("uptime",),
}


def host_evidence_supplements(
    target: str | None,
    missing: Iterable[str],
    existing_text: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """Read-only host probes that close evidence-coverage gaps.

    Returns ``(description, argv)`` pairs for the missing ``target_identity`` /
    ``independent_cross_metric`` dimensions, skipping any probe whose FULL
    command already appears in ``existing_text`` (case-insensitive). Matching
    the whole command — not just its first token — avoids a short token such as
    ``df`` / ``ss`` spuriously matching unrelated text.

    Shared by baseline capture (which renders each argv into a
    ``BaselineCommand``) and verification finalize (which executes each argv
    through the transport) so host evidence is anchored identically.
    """
    missing_set = set(missing)
    existing = existing_text.lower()
    out: list[tuple[str, tuple[str, ...]]] = []
    if "target_identity" in missing_set:
        desc, argv = _HOST_IDENTITY_SUPPLEMENT
        if " ".join(argv) not in existing:
            out.append((desc, argv))
    if "independent_cross_metric" in missing_set:
        argv = _HOST_CROSS_SUPPLEMENTS.get(target or "")
        if argv and " ".join(argv) not in existing:
            out.append(("Host cross-check", argv))
    return out


def _record_text(record: object) -> str:
    if isinstance(record, str):
        return _strip_location(record)
    if isinstance(record, dict):
        parts: list[str] = []
        for key in ("description", "command", "stdout", "stderr", "tool_name", "evidence"):
            value = record.get(key)
            if value:
                parts.append(_strip_location(str(value)))
        metrics = record.get("metrics")
        if isinstance(metrics, dict):
            parts.extend(str(key) for key in metrics)
        return " ".join(parts)
    return _strip_location(str(record))
