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


# ---------------------------------------------------------------------------
# Namespace anchor — same discipline as the node anchor, one dimension over.
#
# Why this exists (inject-b6b02ebd root cause): in CLI NL mode the victim's
# concrete identity (namespace + name) has NO authoritative source and is
# reverse-engineered from probe ORDER by agent_loop's per-field write-once
# derivation. For a MECHANISM case the plan probes TWO same-kind pods — the
# victim (app pod) and the mechanism target (e.g. kube-proxy in kube-system).
# Whichever is probed by name first wins the ``names`` slot, and because
# ``namespace`` locks independently it can come from a DIFFERENT probe, freezing
# a self-contradictory franken target (name from kube-system, namespace from
# drill-lb). Its victim_node bridge then collapses and every node/host write
# REJECT_DRIFTs into a ~28-minute slow death.
#
# The victim NAMESPACE, unlike the victim name, IS stated explicitly in the
# user's text ("drill-lb 命名空间里应用 Pod" / "in namespace drill-lb") and is a
# property of the FIXED input text — so parsing it is a pure, order-independent
# function, exactly like the node anchor. Pre-filling ``spec.namespace`` gives
# the write-once derivation an authoritative namespace to lock first, after
# which agent_loop's namespace-consistency gate can reject any name probed in a
# DIFFERENT namespace (the mechanism target) — probe order no longer decides
# the victim.
#
# Narrow by design, mirroring the node anchor:
# - Only an explicit mention anchors: a DNS-label token immediately before
#   "命名空间", or after "namespace" / "-n" / "--namespace". A bare "跨命名空间"
#   captures "跨", which fails the label shape and is rejected.
# - Candidates must be a valid k8s namespace (RFC1123 LABEL: lowercase alnum +
#   '-', NO dots — stricter than the node subdomain shape, max 63 chars).
# - AMBIGUITY FAILS SAFE: if the text names two or more DISTINCT namespaces
#   ("drill-lb 命名空间的应用，重启 kube-system 命名空间的 kube-proxy"), no
#   anchor is returned — the victim ns is left to the existing derivation path
#   rather than guessing which of the two is the victim's.
# ---------------------------------------------------------------------------

_NAMESPACE_ANCHOR_RES: tuple[re.Pattern[str], ...] = (
    # Chinese: "<ns> 命名空间" (the label token immediately precedes 命名空间).
    # The leading ``(?<![A-Za-z0-9._-])`` enforces WHOLE-TOKEN capture, the
    # same discipline the node anchor gets from its ``\S+?`` group: without
    # it a malformed adjacent token donates its trailing ascii run —
    # "Prod-1 命名空间" would anchor "rod-1", "my.ns 命名空间" would anchor
    # "ns" — a valid-looking but WRONG namespace. A CJK/space/start boundary
    # still matches ("让drill-lb命名空间" / " drill-lb 命名空间"), so real
    # intents are unaffected; only ascii-identifier continuation is refused.
    # (The English/flag forms below are already left-anchored by their literal
    # ``namespace`` / ``-n`` / ``--namespace`` prefix and need no lookbehind.)
    re.compile(r"(?<![A-Za-z0-9._-])([a-z0-9][-a-z0-9]*)\s*命名空间"),
    # English: "namespace <ns>" / "in namespace <ns>".
    re.compile(r"\bnamespace\s+([a-z0-9][-a-z0-9]*)", re.IGNORECASE),
    # kubectl flag forms pasted into the intent: "-n <ns>" / "--namespace <ns>"
    # / "--namespace=<ns>".
    re.compile(r"(?:^|\s)-n\s+([a-z0-9][-a-z0-9]*)"),
    re.compile(r"--namespace[=\s]+([a-z0-9][-a-z0-9]*)"),
)

# K8s namespace = RFC1123 LABEL (no dots, unlike a node's subdomain name).
_NAMESPACE_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_NAMESPACE_LEN = 63


def _is_valid_namespace(candidate: str) -> bool:
    if not candidate or len(candidate) > _MAX_NAMESPACE_LEN:
        return False
    return bool(_NAMESPACE_LABEL_RE.match(candidate))


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


def extract_explicit_namespace_anchor(text: str) -> str:
    """Return the single explicitly named victim namespace, or ``""``.

    Same authority model as :func:`extract_explicit_node_anchor` — the user's
    own text outranks every probe — but returns ONE namespace (``spec.namespace``
    is a scalar, not a tuple). Deduplicates while preserving first-mention
    order; if the text names two or more DISTINCT namespaces the anchor is
    AMBIGUOUS and ``""`` is returned (fail safe: leave the victim ns to the
    existing derivation path rather than guess which mention is the victim's).
    Callers must treat ``""`` as "no anchor".
    """
    if not text:
        return ""
    found: list[str] = []
    for pattern in _NAMESPACE_ANCHOR_RES:
        for match in pattern.finditer(text):
            candidate = _TRAILING_PUNCT_RE.sub("", match.group(1))
            if _is_valid_namespace(candidate) and candidate not in found:
                found.append(candidate)
    # Uniqueness is the safety condition: exactly one distinct namespace named.
    return found[0] if len(found) == 1 else ""
