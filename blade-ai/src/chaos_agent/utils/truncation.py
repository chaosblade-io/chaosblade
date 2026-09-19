"""Shared truncation contract: notices, markers, and preview forms.

Single home for both halves of the truncation morphology:

* **Notice contract** (governance side): every truncation that enters the
  LLM context carries a machine-parseable marker, an honestly-reported
  original size, and a retrieval path (or an explicit, whitelisted
  omission). One family of markers instead of a dozen private dialects —
  downstream parsers (e.g. the recover baseline cache bridge) parse the
  ``Cache:`` / ``Full output cached at:`` wordings this module emits, so
  wording drift here is a contract break, not a cosmetic change.
* **Preview forms** (display side): ``elided_preview`` keeps BOTH ends of
  a failure preview (the causal line can live at either end), and
  ``truncate_head_tail`` is the tool-layer safety-valve form — a high
  byte ceiling that middle-cuts runaway output instead of guessing which
  end matters.

Formalities kept distinct on purpose: head truncation is the governance
default (compactor semantics); the head-tail middle cut belongs to error
echoes and safety valves. Do not unify the two forms.
"""

import re

__all__ = [
    "TRUNCATION_MARKERS",
    "TRUNCATION_CACHE_RE",
    "TOOL_OUTPUT_SAFETY_VALVE_BYTES",
    "build_truncation_notice",
    "truncate_head_tail",
    "apply_output_safety_valve",
    "elided_preview",
]


# ---------------------------------------------------------------------------
# Truncation marker family (machine-parseable contract)
# ---------------------------------------------------------------------------
# Two prefix members, both inherited from the compactor's existing notices
# (the dialect the LLM — now the notice's only reader — and the contract
# tests already know). Every notice this module builds starts with one of
# them; new scenarios join the family via a parenthesized kind annotation,
# never via a new prefix.

TRUNCATION_MARKERS: tuple[str, ...] = ("⚠️ OUTPUT_TRUNCATED", "⚠️ TRUNCATED")

# marker chosen per kind — "success-output" and "error" reuse the detailed
# marker (actionable guidance follows), the rest use the compact one.
_KIND_MARKER = {
    "success-output": "⚠️ OUTPUT_TRUNCATED",
    "error": "⚠️ OUTPUT_TRUNCATED",
    "historical": "⚠️ TRUNCATED",
    "baseline-evidence": "⚠️ TRUNCATED",
    "state-evidence": "⚠️ TRUNCATED",
    "file-content": "⚠️ TRUNCATED",
}

# Cache-path reference embedded in truncation notices (both the recent
# "Full output cached at:" and the historical "Cache:" forms). Single
# source of truth for the constructor side (any notice built with a
# retrieve_path emits one of these wordings) and the contract's own
# round-trip tests — constructor wording and parse pattern must evolve
# as a pair. The machine consumer (the recover baseline cache bridge)
# was retired in round-41; no runtime parser exists today.
TRUNCATION_CACHE_RE = re.compile(r"(?:Cache:|Full output cached at:)\s*(\S+)")

# Kinds whose retrieval path is deliberately omitted from the notice.
# "file-content": re-reading the same file yields the same capped read, so
# a retrieval path would promise something the tool cannot deliver. The
# omission is whitelisted HERE and nowhere else — the three-field
# invariant is not silently eroded.
_RETRIEVE_PATH_OMITTED_KINDS = frozenset({"file-content"})


def build_truncation_notice(
    kind: str,
    original_size: int,
    *,
    retrieve_path: str = "",
    unit: str = "bytes",
    strategy_hint: str | None = None,
    state_hint: str | None = None,
) -> str:
    """Build a truncation notice under the shared three-field contract.

    Three-field invariant (every notice carries):
      1. a machine-parseable marker from ``TRUNCATION_MARKERS``;
      2. the original size, honestly reported in ``unit`` (the caller
         converts; this function never guesses the unit);
      3. a retrieval path — a concrete cache/state path via
         ``retrieve_path`` (emitted as a ``Cache:``/``Full output cached
         at:`` wording the parser side can extract), or the kind's
         built-in retrieval guidance. ``file-content`` is the one
         whitelisted omission.

    Kind variants (guidance follows the content's lifecycle):

    * ``"success-output"`` — re-query with a narrowed scope (the four
      strategies the compactor's recent notice already carries; success
      content is regenerable, so retrieval == re-query, no cache needed).
    * ``"historical"`` — NEVER-destructive-action warning (the
      compactor's old-output notice; the full original sits in the cache).
    * ``"error"`` — the verdict typically lives at the TAIL (kept by the
      head-tail form); the compactor will cache this near-complete output
      on the next LLM turn. Errors are NOT worth re-running: a transient
      failure re-run yields a *different* result, the cache is what
      actually happened.
    * ``"baseline-evidence"`` — the full observation is preserved in
      ``state.baseline_data`` (off-graph aux calls have no compactor
      safety net, so this guidance is their only retrieval defense).
    * ``"state-evidence"`` — full content preserved somewhere the caller
      describes via ``state_hint`` (a state key, a re-readable file);
      the hint must be a promise the caller can keep. This is the
      general sibling ``"baseline-evidence"`` stays a special case of
      (wording pinned by existing tests — deliberately NOT merged).
    * ``"file-content"`` — first-N-bytes read of a larger file
      (``strategy_hint`` describes what is shown; retrieval omitted by
      whitelist).

    ``strategy_hint`` is read by ``"file-content"`` (what is shown) and,
    optionally, by ``"success-output"`` (replaces the default kubectl
    narrowing strategies when the oversized output is not a kubectl
    query); other kinds carry their guidance natively.
    """
    marker = _KIND_MARKER.get(kind)
    if marker is None:
        raise ValueError(f"Unknown truncation notice kind: {kind!r}")

    # state_hint is owned by "state-evidence" ALONE — enforced
    # symmetrically, same style as the retrieve_path whitelist below: a
    # missing hint on state-evidence (or a stray one on any other kind)
    # is a contract violation, not a silent default.
    if kind == "state-evidence":
        if not state_hint:
            # state_hint IS the third field of the invariant here (the
            # retrieval guidance): an omitted hint would produce a notice
            # that says "something was cut" with no way back.
            raise ValueError(
                "Truncation kind 'state-evidence' requires a non-empty "
                "state_hint describing where the full content lives"
            )
    elif state_hint:
        raise ValueError(
            f"Truncation kind {kind!r} does not take state_hint "
            f"(state-evidence only); got state_hint={state_hint!r}"
        )

    if kind in _RETRIEVE_PATH_OMITTED_KINDS:
        if retrieve_path:
            # The whitelist is ENFORCED, not just declared: a call site
            # passing a retrieval path for an omitted kind is a contract
            # violation (it would promise what the tool cannot deliver) —
            # fail fast instead of silently dropping the path.
            raise ValueError(
                f"Truncation kind {kind!r} omits the retrieval path by "
                f"whitelist; got retrieve_path={retrieve_path!r}"
            )
        hint = strategy_hint or "content cut at the read cap"
        return (
            f"\n\n{marker} (file content): {hint} "
            f"(total {original_size} {unit})."
        )

    if kind == "success-output":
        notice = (
            f"\n\n{marker}: output was reduced or truncated "
            f"(original {original_size}{unit}); only key fields are kept."
        )
        if retrieve_path:
            notice += f"\nFull output cached at: {retrieve_path}"
        notice += "\nDo NOT repeat the same query!"
        # Guidance follows the CONTENT's lifecycle: the default strategies
        # are the compactor's kubectl narrowing list (preserved verbatim —
        # the LLM already knows this dialect). A caller whose oversized
        # output is NOT a kubectl query (e.g. the skill script executor)
        # overrides with its own narrowing guidance via ``strategy_hint``
        # — kubectl jsonpath advice on a Python script's stdout would be
        # actively misleading.
        if strategy_hint:
            notice += f"\n{strategy_hint}"
        else:
            notice += (
                " If you need the full data, use one of these strategies:"
                "\n- Narrow the scope with --field-selector (e.g. kubectl subcommand=\"get\" --field-selector spec.nodeName=<node>)"
                "\n- Use -o name for a compact list"
                "\n- Extract specific fields with the kubectl tool's -o jsonpath"
                "\n- Query a single resource by name instead of listing them all"
            )
        return notice

    if kind == "historical":
        notice = (
            f"\n{marker} (compacted historical output, original "
            f"{original_size} {unit} — structure may be invisible)."
        )
        if retrieve_path:
            notice += f" Cache: {retrieve_path}"
        # Action directive — deliberately command-AGNOSTIC. The
        # producing command is still visible to the model one message
        # up (the tool call this result hangs off), so the notice never
        # hardcodes tool-specific narrowing flags: any fixed example
        # (--no-headers, -o name, field-selector …) is wrong for some
        # command family (a jsonpath probe narrows by scope, not
        # output form). Pointing the model at deriving a narrower
        # re-run from the command itself is both universal and MORE
        # precise — the model sees the exact command; the template
        # never can. And the action is a re-run, NOT a cache re-read:
        # a read_file of the cache path returns the full original as
        # a tool result — which the same window arithmetic can demote
        # again (#13-R: the notice-directed re-read survived 0.3s
        # before re-truncation to the same 1018B head). The cache
        # line above stays as a forensic fact only.
        notice += (
            " NEVER execute a destructive or structural change based on "
            "this output: re-run the command that produced it (the tool "
            "call directly above) in a narrower form — scoped to the "
            "exact resource or field you need — so the result is small "
            "enough to arrive intact."
        )
        return notice

    if kind == "error":
        notice = (
            f"\n\n{marker} (error output): runaway output was cut by the "
            f"tool-layer safety valve (original {original_size} {unit}); "
            f"head and tail are kept — the error verdict typically lives "
            f"at the TAIL."
        )
        if retrieve_path:
            # A cache exists NOW: state it and stop — the "no cache" line
            # below would directly contradict this one in the same notice.
            notice += f"\nFull output cached at: {retrieve_path}"
        else:
            notice += (
                "\nNo cache is written at the tool layer: the context compactor "
                "will cache this near-complete output on the next LLM turn and "
                "its notice will carry the cache path."
            )
        return notice

    if kind == "state-evidence":
        # General state-side evidence: the full content lives somewhere the
        # CALLER can describe (a state key, a re-readable file) — the hint
        # must be a promise the caller can keep. "baseline-evidence" below
        # stays a pinned SPECIAL CASE rather than merging into this branch:
        # its wording ("preserved in state.baseline_data") is anchored by
        # the recover bridge and injection tests, and re-anchoring them
        # for zero behavioural gain is not worth the churn.
        return (
            f"\n{marker} (state evidence): {state_hint} "
            f"(original {original_size} {unit})."
        )

    # kind == "baseline-evidence"
    notice = (
        f"\n{marker} (baseline evidence): observation output was truncated "
        f"(original {original_size} {unit})."
    )
    if retrieve_path:
        notice += f"\nFull output cached at: {retrieve_path}"
    notice += "\nFull observation preserved in state.baseline_data."
    return notice


# ---------------------------------------------------------------------------
# Safety-valve form: head-tail middle cut under a UTF-8 byte budget
# ---------------------------------------------------------------------------

# Tool-layer safety valve ceiling — the shared spec for every tool that
# returns near-complete output (kubectl, the skill script executor): 64KB
# UTF-8 bytes = 4× the compactor's 16KB recent budget. NOT a governance
# mechanism — governance is the compactor's job (it caches every oversized
# message in full). The valve only pins an upper bound on runaway output
# (infinite exec loops, unbounded cats, runaway scripts) so a single ToolMessage
# cannot crash the context window. Success and error outputs share the
# SAME ceiling: the runaway shape exists on both sides, and a one-sided
# valve would just move the crash to the other side.
TOOL_OUTPUT_SAFETY_VALVE_BYTES = 64 * 1024


def apply_output_safety_valve(
    text: str,
    max_bytes: int = TOOL_OUTPUT_SAFETY_VALVE_BYTES,
    *,
    kind: str,
    strategy_hint: str | None = None,
) -> str:
    """Apply the tool-layer safety valve to ``text``.

    Within budget: passthrough, zero overhead, zero change. Over budget:
    the head-tail middle cut (verdicts live at the tail; success value
    position is unknowable) + a shared notice whose kind the caller picks
    by outcome (``"error"`` for exit != 0, ``"success-output"`` otherwise
    — retrieval guidance differs by content lifecycle).

    Budget contract — the SAME semantics the compactor enforces
    (``truncate_budget = max_bytes - notice_bytes``): the notice counts
    against ``max_bytes``, so the RETURNED message stays within the
    ceiling. Appending the notice on top of a full-budget cut would
    silently return max_bytes + ~0.7KB — a ceiling that lies by 1%.
    The half-budget floor mirrors the compactor's: on a degenerate tiny
    ceiling (notice larger than half the budget) an honest visible cut
    beats a zero-length one, and the overrun is bounded by the notice.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    # Re-decode from the sanitized bytes: lone surrogates (which would
    # make plain utf-8 encoding raise) become U+FFFD here; the untouched
    # passthrough above keeps non-triggering outputs byte-identical.
    sanitized = encoded.decode("utf-8")
    notice = build_truncation_notice(kind, len(encoded), strategy_hint=strategy_hint)
    notice_bytes = len(notice.encode("utf-8"))
    budget = max(max_bytes - notice_bytes, max_bytes // 2)
    return truncate_head_tail(sanitized, budget) + notice


def truncate_head_tail(
    text: str,
    max_bytes: int,
    *,
    head_ratio: float = 2 / 3,
) -> str:
    """Middle-cut ``text`` under a UTF-8 byte budget, keeping both ends.

    The tool-layer safety-valve form (a ceiling far above governance
    budgets — e.g. 64KB vs the compactor's 16KB): it exists to pin an
    upper bound on runaway output (infinite exec loops, unbounded cats),
    not to govern. Governance is the compactor's job; this cut only keeps
    the message from crashing the context window.

    Why both ends: error verdicts typically live at the TAIL while the
    head carries the command echo; success output has no knowable value
    position (table headers first, log tails last). A one-sided cut bets
    on output shape, and the losing shape hides the root cause. The
    quantified elision marker makes the hidden middle visible rather
    than silent.

    UTF-8 safety: cuts land on byte offsets, and a multi-byte character
    split by a cut is dropped entirely (``errors="ignore"``) rather than
    decoded into mojibake.

    Texts within budget pass through unchanged (no marker).
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text

    # Reserve room for the elision marker (worst-case numeric width) so
    # the RETURNED text — head + marker + tail — stays within max_bytes.
    marker_reserve = 64
    budget = max_bytes - marker_reserve
    if budget <= 0:
        # Degenerate ceiling: an honest head cut beats a budget breach.
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    head_bytes = int(budget * head_ratio)
    tail_bytes = budget - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = (
        encoded[len(encoded) - tail_bytes:].decode("utf-8", errors="ignore")
        if tail_bytes > 0
        else ""
    )
    # omitted is mathematically always positive here: len(encoded) >
    # max_bytes > budget = head_bytes + tail_bytes, and the "ignore"
    # decodes above can only SHRINK the re-encoded head/tail — the
    # multi-byte character splits they drop are bounded by 3 bytes per
    # cut, nowhere near the 64-byte reserve. No defensive fallback
    # branch is needed (and returning the original over-budget text,
    # as a fallback would, would itself breach the contract).
    omitted = len(encoded) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    return (
        f"{head}"
        f"\n...[{omitted} bytes elided]...\n"
        f"{tail}"
    )


# ---------------------------------------------------------------------------
# Preview form: both-ends character preview with a quantified marker
# ---------------------------------------------------------------------------
# (Relocated verbatim from observability/status_tracker.py — see the
# module docstring for why both truncation morphologies live here.)

def elided_preview(text: str, head_chars: int, tail_chars: int) -> str:
    """Both-ends preview with a quantified elision marker.

    A truncated preview must never guess WHERE the causal line lives:
    kubectl prints its warning banner first and the error LAST; a traceback
    names the failing entrypoint FIRST and the exception LAST; some tools
    print the fatal error FIRST and dump diagnostics after. Keeping BOTH
    ends covers every convention — a one-sided cut (head-only or tail-only)
    is a bet on output shape, and the losing shape hides the root cause
    (#31: a head-only cut kept only the "Warning: Immediate deletion..."
    banner and cut "Error from server (NotFound)", misdirecting a
    multi-hour diagnosis). The marker also announces HOW MUCH was elided,
    so a hidden middle is visible rather than silent — the reader knows to
    pull the full output from the session store.

    Short texts pass through unchanged (no marker). Empty input → "".
    """
    if not text:
        return ""
    if len(text) <= head_chars + tail_chars:
        return text
    omitted = len(text) - head_chars - tail_chars
    return (
        f"{text[:head_chars]}"
        f"\n...[{omitted} chars elided]...\n"
        f"{text[len(text) - tail_chars:]}"
    )
