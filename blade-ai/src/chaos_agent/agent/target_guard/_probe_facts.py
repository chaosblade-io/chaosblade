"""Facts-based host-probe judge (design 4.6, face 3).

``carriers.is_readonly_host_probe`` recognises a narrow class of host
inspection commands that may run through an approved debug pod. The legacy
implementation tokenises with shlex and substring-scans each token for
dangerous metacharacters; this module re-judges the same surface on
bashfacts STRUCTURE. Unified at the fact layer (parsing, peeling, part
kinds); deliberately PRESERVED at the policy layer (design 4.6 row 3):

  (a) separator semantics — ``;`` / ``&&`` / ``||`` / ``|`` are legal
      BETWEEN probe segments (each segment independently a read-only
      probe); unlike the readonly face, a chain is not refused wholesale.
      The pipe joined the trio at the B34 fix: a pipe is kernel plumbing
      between two commands, not a mutation, so a pipeline whose every
      stage is a read-only probe mutates nothing — the same verdict the
      readonly face's ``allow_pipes=True`` already reaches for single
      ``sh -c`` bodies. ``|&`` (stderr tee) stays refused, mirroring that
      face. One command, one semantic question ("does it mutate the
      host?"), one answer per form — B34 fell through the two faces'
      COMPLEMENTARY blind spots (readonly face: pipes only, no ``;``
      chains; probe face: chains only, no pipes), and the fallout landed
      in ``recoverability.assess`` as a misleading "add iptables -D"
      guidance for a command with no ``-I`` at all;
  (b) host-entry form — the command must reach the host through
      ``chroot`` / ``nsenter`` / ``unshare`` or a ``/host/`` path,
      optionally ``sh -c`` wrapped; a bare command is NOT a host probe;
  (c) parameter-expansion strictness — any ``$var`` / ``${...}`` / ``$1``
      expansion refuses (probe determinism); the readonly face lets the
      same PartKind through. Same facts, different policy — the
      fact/policy split made concrete;
  (d) per-segment vocabulary — single-sourced to the shared judge
      ``tools.readonly._classify_argv`` (the same ~100-binary vocabulary
      with per-binary argument guards the classifier fast path and the
      kubectl_read tool layer already use). The deleted private table
      (``carriers._is_single_readonly_probe``, 16 binaries + metadata
      flags for fault binaries) was the second half of the one-question/
      two-answers defect: ``chroot /host iptables -S INPUT`` read as
      read-only by the classifier fast path and NOT read-only here.
      Structural policy (no substitution, no expansion, no redirect, no
      subshell) stays local — only the per-binary verdict delegates.

Behaviour changes vs the legacy chain, each registered in the adjudication
list (design 5.x):

  - quoted literals stop tripping the dangerous-substring scan (the P1 fix
    reaches this face: ``echo '$HOME'`` no longer refuses — the legacy
    ``$`` substring could not tell a single-quoted literal from an
    expansion);
  - ``sh -c`` peeling is the strict three-word ``unwrap_sh_c``: a
    flag-before-``-c`` shape (``bash --init-file /tmp/x -c 'ls'``) no
    longer peels, closing the legacy hole where the peel hid the init
    file's execution (tightening, same class as the readonly face's
    registered strict-peel deviation);
  - a tail after the script word is no longer dropped: the legacy
    index-based peel returned the script's tokens and DISCARDED everything
    after the script word, so ``sh -c 'chroot /host ls' ; rm -rf /``
    judged as the probe alone; the facts path parses the whole command and
    the tail segment is judged on its own (tightening);
  - operators glued to words (``ls /;df -h``) segment the way bash reads
    them instead of failing on a ``;`` substring (precision, same class
    as P1).

One deliberate parity: words consumed by the entry form itself (the
``chroot`` ROOT argument, ``nsenter`` options) skip the expansion scan,
exactly like the legacy chain, which scanned only the post-unwrap payload
tokens.
"""

from __future__ import annotations

from chaos_agent.bashfacts import (
    Budget,
    CommandFacts,
    PartKind,
    ScriptFacts,
    WordFacts,
    iter_parts,
    parse_script,
    unwrap_sh_c,
    word_token,
)
from chaos_agent.agent.target_guard.carriers import _BANNED_HOST_VERBS
from chaos_agent.tools.readonly import _classify_argv, _strip_wrappers

__all__ = ["host_payload_tokens_facts", "is_readonly_host_probe_facts"]

# Policy (a): the only operators legal between probe segments — the deleted
# legacy chain's trio plus the pipe the B34 fix admitted (module docstring).
# ``|&`` (stderr tee) is its own operator token in the scanner and is
# deliberately absent, mirroring the readonly face's refusal of it.
_PROBE_SEPARATOR_OPS = frozenset({";", "&&", "||", "|"})

# Part kinds that never appear in a plain probe (policy: no substitution of
# any flavour). PARAM_EXPANSION is handled separately (policy c) only to
# keep the two policy reasons distinguishable in a debugger.
_STRUCTURE_PART_KINDS = frozenset({
    PartKind.COMMAND_SUBST,
    PartKind.BACKTICK_SUBST,
    PartKind.PROCESS_SUBST,
    PartKind.ARITH_EXPANSION,
})


def _segment_words(cmd: CommandFacts) -> list[WordFacts]:
    words: list[WordFacts] = []
    if cmd.name is not None:
        words.append(cmd.name)
    words.extend(cmd.args)
    return words


def _segment_is_probe(words: list[WordFacts]) -> bool:
    """One segment: no substitution/expansion parts (policy c), then the
    shared per-binary judge (policy d — single-sourced to
    ``tools.readonly._classify_argv`` at the B34 fix; the private carriers
    vocabulary and its one-question/two-answers split are gone), overlaid
    with the carriers-face head ban below."""
    values: list[str] = []
    for word in words:
        for part in iter_parts(word):
            if part.kind in _STRUCTURE_PART_KINDS:
                return False
            if part.kind is PartKind.PARAM_EXPANSION:
                return False  # policy (c): probe determinism
        value = word.value
        if value is None:
            return False  # defensive: the scan above makes this unreachable
        values.append(value)
    if not values:
        # An empty segment is not a probe. ``_classify_argv`` returns True
        # for an empty argv (a sentinel its own callers rely on), so this
        # check must come BEFORE the head-token access below it.
        return False
    ok, _reason = _classify_argv(values)
    if not ok:
        return False
    # Carriers-face overlay: a banned verb as the segment's BINARY never
    # routes through the readonly bypass, whatever guarded read-only form
    # the shared judge admits for it (``curl -fsSL <url>`` GET-to-stdout,
    # ``systemctl status``, bare ``mount``). The readonly face's width
    # belongs to pod-exec probes; on HOST entry those binaries stay banned
    # at word level (benign and hostile forms are indistinguishable there).
    # ARGUMENT position is data, not code — ``which curl`` and
    # ``iptables -S | grep curl`` stay fine. The head is taken AFTER
    # wrapper stripping (``timeout 5 curl …`` / ``env curl …`` / nested),
    # mirroring the strip ``_classify_argv`` itself performs before it
    # judges the wrapped binary — anchoring on the raw first token left the
    # wrapped binary outside the ban. Iterated to a fixed point because
    # ``_strip_wrappers`` caps its own depth at three layers while the
    # judge recurses past that.
    head = values
    for _ in range(4):
        stripped = _strip_wrappers(head)
        if stripped == head:
            break
        head = stripped
    return _BANNED_HOST_VERBS.search(head[0]) is None


def _script_is_probe_chain(script: ScriptFacts) -> bool:
    """Every operator a legal separator, every segment a probe (policy a).
    Used for a peeled ``sh -c`` body — the host-entry form is checked ONCE
    at the outer head, never re-required inside the script (legacy parity:
    the legacy peel returned the script tokens straight into the separator
    split)."""
    if script.errors:
        return False
    for op in script.operators:
        if op not in _PROBE_SEPARATOR_OPS:
            return False
    for seg in script.segments:
        if isinstance(seg.command, ScriptFacts):
            return False  # subshell
        if seg.command.redirects:
            return False
        if not _segment_is_probe(_segment_words(seg.command)):
            return False
    return True


def _entry_payload(
    words: list[WordFacts], *, budget: Budget, allow_head_peel: bool = True
) -> tuple[str, list[WordFacts] | ScriptFacts] | None:
    """Unwrap the host-entry form (policy b).

    Returns ``("head-script", nested)`` for a peeled leading ``sh -c``
    layer (the entry check is re-required inside — the peel result IS the
    new token-stream head, legacy parity), ``("payload-script", nested)``
    for a peel AFTER an entry primitive (entry consumed — the payload is
    judged as plain probe segments), ``("words", rest)`` for a direct
    payload, or None when the head is not a host-entry form. The order is
    the deleted legacy chain's, step for step: a leading ``sh -c`` peel
    first, then chroot / nsenter / unshare / ``/host/`` — each consuming
    a SECOND peel attempt on its payload.

    ``allow_head_peel=False`` mirrors the legacy one-peel-per-position
    limit: after a head peel, the nested script's head goes through the
    entry check WITHOUT another peel first, so ``sh -c 'sh -c "chroot …"'``
    keeps failing closed exactly like the legacy chain (whose peeled token
    stream starts with ``sh``, not an entry form).
    """
    if not words:
        return None
    head = words[0].value
    if head is None:
        return None  # structure at the head — not a recognisable entry
    if allow_head_peel:
        nested = unwrap_sh_c(words, budget=budget)
        if nested is not None:
            return ("head-script", nested)
    if head == "chroot":
        if len(words) < 3:
            return None
        rest = words[2:]
        nested = unwrap_sh_c(rest, budget=budget)
        return ("payload-script", nested) if nested is not None else ("words", rest)
    if head in ("nsenter", "unshare"):
        idx = next((i for i, w in enumerate(words) if w.value == "--"), None)
        if idx is None:
            return None
        rest = words[idx + 1:]
        if not rest:
            return None
        nested = unwrap_sh_c(rest, budget=budget)
        return ("payload-script", nested) if nested is not None else ("words", rest)
    if head.startswith("/host/"):
        return ("words", words)
    return None


def _judge_payload(
    kind: str, body: list[WordFacts] | ScriptFacts, *, budget: Budget
) -> bool:
    """Judge an unwrapped entry payload: a head peel re-requires the entry
    form inside the script; an entry-consumed payload is plain probes."""
    if kind == "head-script":
        return _entry_script_is_probe_chain(body, budget=budget)
    if kind == "payload-script":
        return _script_is_probe_chain(body)
    return _segment_is_probe(body)


def _entry_script_is_probe_chain(script: ScriptFacts, *, budget: Budget) -> bool:
    """Judge a script whose FIRST segment must carry the host-entry form
    (the entry check applies to the token stream a head peel yields, not
    to the pre-peel text — ``sh -c 'chroot /host echo ok'`` unwraps to a
    chroot entry, exactly like the deleted legacy chain). Later
    segments are plain probe segments."""
    if script.errors or not script.segments:
        return False
    for op in script.operators:
        if op not in _PROBE_SEPARATOR_OPS:
            return False
    first = script.segments[0]
    if isinstance(first.command, ScriptFacts):
        return False
    if first.command.redirects:
        return False
    payload = _entry_payload(
        _segment_words(first.command), budget=budget, allow_head_peel=False
    )
    if payload is None:
        return False
    kind, body = payload
    if not _judge_payload(kind, body, budget=budget):
        return False
    for seg in script.segments[1:]:
        if isinstance(seg.command, ScriptFacts):
            return False
        if seg.command.redirects:
            return False
        if not _segment_is_probe(_segment_words(seg.command)):
            return False
    return True


def is_readonly_host_probe_facts(command: str) -> bool:
    """Facts-engine counterpart of ``carriers.is_readonly_host_probe``.

    The entry form wraps only the FIRST segment (legacy unwraps the head of
    the token stream — the same thing, since a separator ends the segment
    the head lives in); every later segment is judged as a plain probe.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    budget = Budget()
    root = parse_script(command, budget=budget)
    if root.errors or not root.segments:
        return False
    for op in root.operators:
        if op not in _PROBE_SEPARATOR_OPS:
            return False
    first = root.segments[0]
    if isinstance(first.command, ScriptFacts):
        return False
    if first.command.redirects:
        return False
    payload = _entry_payload(_segment_words(first.command), budget=budget)
    if payload is None:
        return False
    kind, body = payload
    if not _judge_payload(kind, body, budget=budget):
        return False
    for seg in root.segments[1:]:
        if isinstance(seg.command, ScriptFacts):
            return False
        if seg.command.redirects:
            return False
        if not _segment_is_probe(_segment_words(seg.command)):
            return False
    return True


def host_payload_tokens_facts(command: str) -> list[str] | None:
    """The entry-unwrapped payload as a flat token stream (operators
    included as separator tokens, mirroring the deleted legacy shlex
    stream), or None when the command carries no recognisable host-entry
    form. Regex fuel for ``carriers.classify_host_operation`` — rendering,
    not judging, so words render via ``word_token`` (structure keeps its
    raw text)."""
    if not isinstance(command, str) or not command.strip():
        return None
    budget = Budget()
    root = parse_script(command, budget=budget)
    if root.errors or not root.segments:
        return None
    first = root.segments[0]
    if isinstance(first.command, ScriptFacts):
        return None
    payload = _entry_payload(_segment_words(first.command), budget=budget)
    if payload is None:
        return None
    kind, body = payload
    if kind in ("head-script", "payload-script"):
        if body.errors or not body.segments:
            return None
        tokens: list[str] = []
        for i, seg in enumerate(body.segments):
            if isinstance(seg.command, ScriptFacts):
                return None
            if i:
                tokens.append(body.operators[i - 1])
            tokens.extend(word_token(w) for w in _segment_words(seg.command))
    else:
        tokens = [word_token(w) for w in body]
    # The legacy stream keeps the OUTER tail after the entry-wrapped head
    # (``chroot /host df ; ls`` unwraps to ``df ; ls``) — mirror it.
    for i, seg in enumerate(root.segments[1:], start=1):
        if isinstance(seg.command, ScriptFacts):
            return None
        tokens.append(root.operators[i - 1])
        tokens.extend(word_token(w) for w in _segment_words(seg.command))
    return tokens
