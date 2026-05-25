"""Structured field extractors for baseline kubectl outputs.

Each extractor is a pure function ``(stdout: str, state: dict) -> dict``
that parses one baseline command's stdout and returns a dict of fields
to merge into ``state["target_metadata"]``. Downstream nodes (FCAT
adaptations in direct_execute / execute_loop, OOMKill risk checks,
future verify-side diff logic) read those fields by name and skip the
fresh ``kubectl`` call they would otherwise need.

### Adding a new field

1. Write a pure extractor function in this module:

      def extract_X(stdout: str, state: dict) -> dict[str, Any]:
          ...
          return {"my_new_field": value}  # or {} on parse failure

2. Attach it to the relevant ``BaselineCommand`` in
   ``baseline_capture.BASELINE_COMMANDS``:

      BaselineCommand(
          "Pod CPU/Memory", "top", "pod -n {namespace} {label_selector}",
          extractors=[extract_pod_top_metrics, extract_X],
      )

That's the entire integration. The baseline runner picks the extractor
up automatically — no registry, no dependency resolution.

### Contract

- **Purity**: must not call ``kubectl`` / hit the network. Parsing only.
- **Failure mode**: return ``{}`` when the input shape isn't recognised
  (e.g. format drift across kubectl versions). DO NOT raise — extractor
  errors are caught by the runner with ``logger.debug`` and the
  downstream consumer falls back to its own fresh fetch.
- **State access**: read-only. Use ``state`` to disambiguate (e.g.
  pick the right pod from a multi-pod ``top`` output). NEVER mutate
  state — return the new fields and let the caller merge.
- **Idempotency**: extractors may be called twice on the same input
  (rare, but theoretically possible if baseline retries); identical
  input must produce identical output.

### Why not a registry / dependency graph?

Considered, rejected: the failure mode of registry-based extraction
("which extractor produces this field?") is opaque at debug time.
Direct attachment to commands is grep-able: starting from a missing
``target_metadata["X"]`` field, ``grep -n "X" baseline_extractors.py``
gives you the producer, ``grep -n extract_FOO baseline_capture.py``
gives you which command runs it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Public type alias — keep stable so external callers can annotate.
Extractor = Callable[[str, dict], dict[str, Any]]


# ---------------------------------------------------------------------------
# Resource-quantity parsing helpers
# ---------------------------------------------------------------------------
# kubectl prints CPU / memory quantities with k8s suffixes:
#   CPU:    "1500m" (millicores), "2" (cores), "0" (idle)
#   Memory: "120Mi", "1.5Gi", "1024Ki", "850M", "2G" (binary vs decimal)
# These helpers normalise to integers (millicores / MiB) so downstream
# consumers can do arithmetic without re-parsing.


_CPU_MC_RE = re.compile(r"^(\d+(?:\.\d+)?)(m?)$")


def _parse_cpu_text_to_mc(text: str) -> int | None:
    """Parse a kubectl CPU value to millicores.

    Examples: ``"1500m" -> 1500``, ``"2" -> 2000``, ``"0" -> 0``.
    Returns ``None`` on unrecognised input.
    """
    m = _CPU_MC_RE.match(text.strip())
    if m is None:
        return None
    n = float(m.group(1))
    suffix = m.group(2)
    if suffix == "m":
        return int(n)
    return int(n * 1000)


# Memory suffix → multiplier in MiB. Binary suffixes (Mi/Gi/Ki) are the
# kubectl norm; decimal suffixes (M/G/K) appear occasionally on older
# clusters. Both round to the nearest MiB.
_MEM_SUFFIX_MIB: dict[str, float] = {
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
    "K": 1000 / (1024 * 1024),
    "M": 1000 * 1000 / (1024 * 1024),
    "G": 1000 * 1000 * 1000 / (1024 * 1024),
    "T": 1000 * 1000 * 1000 * 1000 / (1024 * 1024),
    "": 1 / (1024 * 1024),  # bare bytes
}
_MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]i?)?$")


def _parse_mem_text_to_mb(text: str) -> int | None:
    """Parse a kubectl memory value to integer MiB.

    Examples: ``"120Mi" -> 120``, ``"1.5Gi" -> 1536``, ``"850M" -> 810``.
    Returns ``None`` on unrecognised input. We call the unit "MB" in
    field names to stay consistent with the rest of the codebase, but
    the underlying math is in MiB (binary) — the difference rounds to
    less than 5% and is well below the precision FCAT P0 needs.
    """
    m = _MEM_RE.match(text.strip())
    if m is None:
        return None
    n = float(m.group(1))
    suffix = m.group(2) or ""
    mul = _MEM_SUFFIX_MIB.get(suffix)
    if mul is None:
        return None
    return int(n * mul)


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def extract_pod_top_metrics(stdout: str, state: dict) -> dict[str, Any]:
    """Parse ``kubectl top pod`` output → CPU + memory usage for the
    target pod.

    Produces (when matched):
      - ``pod_cpu_usage_mc``      — int millicores
      - ``pod_memory_usage_mb``   — int MiB

    Multi-pod handling: ``kubectl top pod -l <selector>`` returns one
    row per matching pod. We pick the row whose first column matches
    ``state["target"]["names"][0]`` so a label selector that happens
    to match an unrelated co-tenant pod doesn't contaminate the value
    used by FCAT P0.

    Returns ``{}`` if no target pod name is available or no row matches
    — callers MUST treat absence as "not collected" and fall back to a
    fresh ``kubectl top`` (see ``direct_execute._fetch_pod_memory_usage_mb``
    for the canonical fallback pattern).
    """
    from chaos_agent.agent.fault_spec import read_fault_spec
    spec = read_fault_spec(state)
    names = list(spec.names) if spec else []
    if not names:
        return {}
    target_pod = names[0]

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip header. kubectl top pod prints ``NAME   CPU(cores)
        # MEMORY(bytes)``; without --no-headers it's always present.
        if line.startswith("NAME"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        if parts[0] != target_pod:
            continue
        cpu_mc = _parse_cpu_text_to_mc(parts[1])
        mem_mb = _parse_mem_text_to_mb(parts[2])
        result: dict[str, Any] = {}
        if cpu_mc is not None:
            result["pod_cpu_usage_mc"] = cpu_mc
        if mem_mb is not None:
            result["pod_memory_usage_mb"] = mem_mb
        return result

    # No row matched — the target pod isn't in the output. This
    # happens when the label selector form returned only sibling pods
    # (none matching the target name), or when metrics-server hasn't
    # yet observed the pod. Caller falls back to direct fetch.
    return {}
