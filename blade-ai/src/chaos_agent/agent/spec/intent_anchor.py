"""Intent anchor extraction — pin the user's EXPLICITLY named target.

B76 root cause: in CLI NL mode the entry-point FaultSpec carries no identity
fields, and identity is then lazily derived (write-once) from whatever
read-only probe commands happen to run first. Probe ORDER — not user intent —
decides the contract, so a tool-health probe can lock ``scope=pod`` and an
observation-target probe can lock ``labels`` for a task whose user text
explicitly names a node (task inject-5552c6e4: three REJECT_DRIFT rejections
and a 44-minute deadlock).

This module extracts the one identity signal that outranks every probe: a
resource name the user wrote themselves. Anchoring is deliberately narrow:

- Only explicit prepositional forms are recognised ("在节点 X 上" / "on node X").
  Bare "节点 X" is NOT anchored — "观察节点 NotReady 状态变化" would otherwise
  capture NotReady as a node name.
- Candidates must satisfy a k8s DNS-subdomain shape (lowercase alnum, '-',
  '.'), which rejects Chinese fragments, CamelCase words, and prose tokens.
- Only node anchors exist today. Pod/deployment phrasings ("应用 A",
  "deployment foo") are too ambiguous to anchor safely; they stay with the
  existing lazy-derivation path.

Multi-anchor texts ("在节点 A 上注入，在节点 B 上观察") anchor EVERY named
node: the prepositional form cannot distinguish target from observation
role, and this was deliberately decided (2026-09-11, option A) in favour
of trusting explicit naming over verb-word-list heuristics — a verb list
("注入/搞挂/压一压…") misses one coinage and that node loses its anchor,
  re-opening the B76 probe-order race, whereas an over-wide names set has
a working correction channel: planning re-reads the full sentence,
  and a ``propose_plan_change`` narrowing proposal (names ⊆ anchors,
  same kind) auto-applies in CLI mode via the plan_change_confirm
  alignment check.

The anchor fills ``scope=node`` + ``names`` at the entry point, so lazy
derivation's write-once semantics protect it: scope can never be silently
re-locked, names occupy the slot so labels never qualify, and the B31
kind-consistency guard rejects cross-kind name writes.
"""

from __future__ import annotations

import re

# Prepositional anchor forms. Chinese: "在节点 X 上/中" (name may carry no
# whitespace; the 上/中 terminator plus a DNS-shape check keeps prose out).
# English: "on node X". Both require the preposition so a bare mention of
# "节点 X" / "node X" mid-sentence is not treated as an anchor.
_NODE_ANCHOR_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"在节点\s*(\S+?)\s*[上中]"),
    re.compile(r"\bon\s+node\s+(\S+)", re.IGNORECASE),
)

# K8s DNS-subdomain shape (RFC 1123, dots allowed). Rejects Chinese text,
# uppercase words (e.g. "NotReady"), and most prose tokens by construction.
_DNS_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")

_MAX_NAME_LEN = 253

# Trailing punctuation to strip from an English-form capture ("on node foo,"
# -> "foo"). Leading punctuation is impossible: the regex captures \S+ right
# after whitespace.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)'\"\u201d\u2019]+$")


def _is_valid_node_name(candidate: str) -> bool:
    if not candidate or len(candidate) > _MAX_NAME_LEN:
        return False
    return bool(_DNS_SUBDOMAIN_RE.match(candidate))


def extract_explicit_node_anchor(text: str) -> tuple[str, ...]:
    """Return the explicitly named node(s) from user intent text.

    Deduplicates while preserving first-mention order. Returns ``()`` when the
    text names no node in an anchor form — callers must treat that as "no
    anchor" and leave identity to the existing derivation paths.
    """
    if not text:
        return ()
    found: list[str] = []
    for pattern in _NODE_ANCHOR_RES:
        for match in pattern.finditer(text):
            candidate = _TRAILING_PUNCT_RE.sub("", match.group(1))
            if _is_valid_node_name(candidate) and candidate not in found:
                found.append(candidate)
    return tuple(found)
