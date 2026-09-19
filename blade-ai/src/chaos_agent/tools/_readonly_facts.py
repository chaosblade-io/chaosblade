"""Facts-based read-only judge — the bashfacts engine behind the Phase-2
readonly surfaces (design doc 4.6/4.7).

Re-judges the RAW-STRING surfaces of ``readonly.py`` on structural
facts instead of substring screens, plus the one pure-ARGV surface whose
structural hole is recoverable post-tokenisation:

  - ``host_command_rejection_reason_facts``  — bare host command
  - ``contains_shell_metachar_facts``        — host_inject's skip_guard screen
  - ``kubectl_exec_rejection_reason_facts``  — kubectl exec/debug inner command
  - ``argv_rejection_reason_facts``          — argv vector (face 8: adds the
    ``watch`` re-parse the legacy argv chain never modelled)

The legacy chain asks "does the raw string CONTAIN a metacharacter"; this
engine asks "does the command CARRY structure" — a quoted ``>`` is a
literal, an unquoted one is a redirect. That single distinction is the P1
fix (``awk 'NR>1{print $1}'`` stops being refused) and it is more precise
in BOTH directions: quoted literals stop tripping the screen, while real
structure that substring scans misread is still structure (every parse
issue fails closed).

What does NOT change: the per-binary verdict itself. Words are rendered to
tokens (``bashfacts.word_token``) and judged by the SAME ``_classify_argv``
the legacy chain uses, so the dual-use vocabulary stays single-source
(including the awk in-program guard, which judges the dequoted program
argument).

Intentional deviations from the legacy chain (each one surfaces in the
dual-run diff and is registered in the adjudication list):

  - ``sh -c`` unwrap is the strict three-word form (``unwrap_sh_c``): flags
    before ``-c`` (``bash --init-file /tmp/x -c id``) are NO LONGER peeled —
    they fall to ``_classify_argv`` and fail closed as an unknown binary.
  - nested ``sh -c`` layers peel recursively within the nesting budget
    (legacy peeled exactly one layer, then rejected the inner ``sh``).
  - a ``sh -c`` layer is retried after wrapper stripping, so
    ``timeout 5 sh -c 'df -h'`` is seen through (legacy stopped there).
  - parse issues on ``contains_shell_metachar`` return True (legacy's pure
    substring scan returned False for an unbalanced-but-metachar-free
    string) — fail-closed.
  - ANSI-C words dequote to their DECODED value (``$'-s'`` judges as the
    ``-s`` flag bash would deliver), where shlex left them opaque.
  - ``watch`` payloads are RE-PARSED the way watch itself runs them (see
    ``_watch_payload``) — the legacy chain only stayed safe here because an
    upstream substring screen happened to fire first (design doc P2's
    implicit-ordering dependency); the check is now folded into the judge.
"""

from __future__ import annotations

import shlex

from chaos_agent.bashfacts import (
    Budget,
    CommandFacts,
    IssueKind,
    PartKind,
    ScriptFacts,
    WordFacts,
    has_shell_structure,
    iter_parts,
    parse_script,
    unwrap_sh_c,
    word_token,
)
from chaos_agent.tools.readonly import (
    _COMMAND_WRAPPERS,
    _DURATION_RE,
    _ESCAPE_PRIMITIVES,
    _WRAPPER_VALUE_FLAGS,
    _classify_argv,
    _strip_wrappers,
    _unwrap_escape,
)

__all__ = [
    "KUBECTL_EXEC_VALUE_FLAGS",
    "argv_rejection_reason_facts",
    "contains_shell_metachar_facts",
    "exec_command_without_double_dash",
    "exec_separator_index",
    "exec_separator_shape",
    "host_command_rejection_reason_facts",
    "inner_raw_after_double_dash",
    "kubectl_exec_rejection_reason_facts",
    "kubectl_flag_takes_value",
]

# Message tails mirror the deleted legacy chain's phrasing so a model
# refused by the facts engine gets the same fix path it always saw.
_TAIL_PROBE = (
    " (redirect/command chain/background/substitution), which a read-only"
    " probe does not allow"
)
_TAIL_HOST = (
    " (host_read only runs a single read-only diagnostic with no pipe/redirect)"
)


# --- word → token rendering ------------------------------------------------

# The renderer is ``bashfacts.word_token`` (fact layer, single source): a
# literal-class word dequotes to its value; a word carrying structure keeps
# the structure's raw source text (``$(id)`` stays ``$(id)``), so a flag
# table can never match a partially-dequoted shape.


def _segment_words(cmd: CommandFacts) -> list[WordFacts]:
    words: list[WordFacts] = []
    if cmd.name is not None:
        words.append(cmd.name)
    words.extend(cmd.args)
    return words


# --- watch: the shell-executing wrapper (design doc P2) ----------------------

# ``watch`` is the ONLY shell-executing wrapper in ``_COMMAND_WRAPPERS``:
# procps watch joins its argv and hands the string to ``sh -c`` — outer
# quotes do NOT survive the re-parse, so ``watch echo '$(rm -rf /)'`` REALLY
# runs ``rm`` (bash delivers ``$(rm -rf /)`` as a literal argv word; watch's
# sh -c then expands it). Exec-style wrappers (timeout/nice/env/...) deliver
# argv verbatim, where a quoted literal stays a literal. Model watch
# faithfully: join the dequoted payload exactly as watch does and re-judge
# the joined text as a fresh script. This removes the legacy implicit
# ordering dependency (an upstream substring screen happened to catch the
# shape first) by folding the check into the judge itself.
_WATCH_REASON_PREFIX = (
    "'watch' hands its arguments to sh -c (the only shell-executing wrapper),"
    " and the re-parsed payload is not read-only: "
)

# Per-binary value-taking flags for the WATCH walk (review finding:
# adjudication-list defect E). The shared legacy table over-approximates —
# ``-i`` takes a value for stdbuf but is VALUELESS for env
# (``--ignore-environment``). Over-skipping inside ``_strip_wrappers`` only
# shortens the suffix that gets classified, which fails closed (an unknown
# binary refuses); over-skipping HERE can eat the ``watch`` token itself —
# ``env -i watch echo '$(rm -rf /)'` slipped the payload past the re-parse
# check on all three surfaces (legacy stayed safe via its substring screen).
# ``env -S/--split-string`` is deliberately NOT modelled as value-taking: its
# value is itself a command line, so the walk breaks there and the legacy
# classifier refuses the opaque head (fail-closed) instead of guessing.
_WATCH_WALK_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "watch": frozenset({"-n", "--interval", "-q", "--equexit"}),
}
# Missing entries in this table fail CLOSED, never open: at a pre-watch layer
# the flag's value becomes the walk's break token and gets classified as an
# unknown binary (deny); at the watch layer itself the value is swept into
# the re-parsed payload (a superset — more structure, not less). Likewise
# ``watch -x/--exec`` (which really uses exec(2), not sh -c) is modelled
# conservatively as a sh -c layer: an over-deny, registered.


def _watch_payload(tokens: list[str]) -> list[str] | None:
    """Tokens after the first ``watch`` layer in a wrapper chain, else None.

    Mirrors ``_strip_wrappers``' flag walk layer by layer, but with two
    deliberate divergences (both from post-delivery review findings):

    - per-binary value flags (``_WATCH_WALK_VALUE_FLAGS``, defect E) — a
      shared over-approximating table can eat the ``watch`` token itself;
    - NO depth cap (defect F) — a capped walk that returns None has NOT
      proven the absence of watch, and the downstream argv classifier keeps
      stripping past the cap (``timeout 5 timeout 5 timeout 5 watch echo
      '$(rm -rf /)'` failed open on all three surfaces). The walk strictly
      shrinks ``rest`` every layer, so an uncapped walk always terminates
      and its None is a PROOF of absence;
    - ``VAR=VAL`` skipping only for env (defect H) — env is the only wrapper
      that TAKES assignments; watch does not parse them, it joins them into
      the sh -c string, where a substitution in the RHS REALLY runs
      (``watch 'A=$(id)' df`` executes id). For every other wrapper the
      assignment word starts the payload.

    A wrapper chain with no watch in it — or a watch with no payload — is
    not this function's concern.
    """
    rest = tokens
    while rest:
        binary = rest[0].rsplit("/", 1)[-1]
        if binary not in _COMMAND_WRAPPERS:
            return None
        value_flags = _WATCH_WALK_VALUE_FLAGS.get(binary, _WRAPPER_VALUE_FLAGS)
        i = 1
        while i < len(rest):
            tok = rest[i]
            if tok in value_flags:
                i += 2  # flag consuming a separate value (``watch -n 1``)
            elif tok.startswith("-") or ("=" in tok and binary == "env"):
                i += 1  # valueless flag; ``VAR=VAL`` assignment (env only —
                # defect H: watch re-parses assignments through sh -c, so an
                # ``A=$(...)`` RHS there is real execution, not a setting)
            elif binary == "timeout" and _DURATION_RE.match(tok):
                i += 1  # timeout's DURATION positional
            else:
                break  # first real token of the wrapped command
        payload = rest[i:]
        if not payload:
            return None  # nothing wrapped — judge the wrapper itself
        if binary == "watch":
            return payload
        rest = payload
    return None


# --- structural scan --------------------------------------------------------


def _issue_reason(issue_kind: IssueKind, pos: int) -> str:
    if issue_kind is IssueKind.BUDGET_EXCEEDED:
        return (
            "analysis capacity exceeded (nesting budget) at pos"
            f" {pos}; refusing to guess"
        )
    if issue_kind is IssueKind.UNTERMINATED_QUOTE:
        return "command cannot be parsed (unbalanced shell quotes)"
    return f"command cannot be parsed ({issue_kind.value} at pos {pos}); failing closed"


def _structure_reason(
    script: ScriptFacts, *, allow_pipes: bool, tail: str, allow_chains: bool = False
) -> str | None:
    """First structural element a read-only probe may not carry, or None.

    One script level only: nested scripts' issues bubble into
    ``script.errors``, and any substitution body is rejected wholesale by
    the substitution rule below, so no recursive walk is needed here.

    ``allow_chains`` (B46) admits the ``;``/``&&``/``||`` chain separators:
    every segment is then judged independently by the caller (same policy
    vocabulary as target_guard's ``_PROBE_SEPARATOR_OPS`` — the execute-phase
    guard already admits all-readonly compound chains, and the read-only
    exec channel now speaks the same dialect instead of refusing a shape the
    other guard accepts). Redirects, substitutions, background and newlines
    stay refused on every surface.
    """
    for issue in script.errors:
        return _issue_reason(issue.kind, issue.pos)
    for op in script.operators:
        if op == "|" and allow_pipes:
            continue
        if allow_chains and op in (";", "&&", "||"):
            continue  # chain separator — inert; each segment judged on its own
        display = op.strip() or repr(op)
        # Wording mirrors the legacy chain's "shell control operator" so
        # either engine's refusal reads the same to the model.
        return f"contains the shell control operator '{display}'{tail}"
    for seg in script.segments:
        if isinstance(seg.command, ScriptFacts):
            # No offset available: ScriptFacts/Segment carry no pos fields
            # (fact-layer model unchanged by design 4.7).
            return f"contains a subshell '(...)'{tail}"
        cmd = seg.command
        if cmd.redirects:
            red = cmd.redirects[0]
            return (
                f"contains a shell redirect ('{red.operator}') at pos "
                f"{red.pos}{tail}"
            )
        words = _segment_words(cmd)
        for red in cmd.redirects:  # unreachable today (rejected above) —
            # kept so a future allow-list still scans redirect words
            if red.target is not None:
                words.append(red.target)
            if red.body is not None:
                words.append(red.body)
        for word in words:
            for part in iter_parts(word):
                if part.kind in (PartKind.COMMAND_SUBST, PartKind.BACKTICK_SUBST):
                    return (
                        f"contains command substitution ('{part.text}') at pos "
                        f"{part.pos}{tail}"
                    )
                if part.kind is PartKind.PROCESS_SUBST:
                    return (
                        f"contains process substitution ('{part.text}') at pos "
                        f"{part.pos}{tail}"
                    )
                if part.kind is PartKind.ARITH_EXPANSION and part.text.startswith(
                    "$(("
                ):
                    return (
                        f"contains arithmetic expansion ('$((...))') at pos "
                        f"{part.pos}{tail}"
                    )
    return None


# --- kubectl exec inner (pipeline of read-only stages) ----------------------


def _judge_stages(
    stages: list[list[WordFacts]], *, budget: Budget, depth: int,
    allow_chains: bool = False,
) -> tuple[bool, str | None]:
    """Classify the remaining pipeline stages via the shared argv judge.

    A tail stage ALSO gets the watch re-parse check (defect G): ``watch``
    re-runs its argv through sh -c no matter which pipeline stage it heads,
    so ``df | watch echo '$(id)'`` must not wave the payload through. Tail
    stages deliberately keep the legacy width otherwise (no sh -c peel, no
    escape unwrap) — only the fail-open direction is closed here.
    """
    for words in stages:
        if not words:
            continue  # an empty stage between two pipes — legacy skips it too
        tokens = [word_token(w) for w in words]
        watch_payload = _watch_payload(tokens)
        if watch_payload is not None:
            # Tokens are dequoted argv — exactly what watch joins for sh -c.
            joined = " ".join(watch_payload)
            if joined.strip():
                script = parse_script(joined, budget=budget)
                ok, reason = _judge_exec_script(
                    script, budget=budget, depth=depth + 1,
                    allow_chains=allow_chains,
                )
                if not ok:
                    return False, _WATCH_REASON_PREFIX + reason
                continue
        ok, reason = _classify_argv(tokens)
        if not ok:
            return False, f"a pipeline stage is not read-only: {reason}"
    return True, None


def _judge_exec_words(
    words: list[WordFacts],
    rest_stages: list[list[WordFacts]],
    *,
    budget: Budget,
    depth: int,
    allow_chains: bool = False,
) -> tuple[bool, str | None]:
    """Judge the head pipeline stage (peel/wrapper/escape) then the rest."""
    nested = unwrap_sh_c(list(words), budget=budget) if words else None
    if nested is not None:
        if nested.errors:
            return False, _issue_reason(nested.errors[0].kind, nested.errors[0].pos)
        if not nested.segments:
            return False, "sh -c body is empty or cannot be parsed"
        ok, reason = _judge_exec_script(
            nested, budget=budget, depth=depth, allow_chains=allow_chains,
        )
        if not ok:
            return False, reason
        return _judge_stages(
            rest_stages, budget=budget, depth=depth, allow_chains=allow_chains,
        )

    tokens = [word_token(w) for w in words]
    watch_payload = _watch_payload(tokens)
    if watch_payload is not None:
        # Tokens are dequoted argv — exactly what watch joins for sh -c.
        joined = " ".join(watch_payload)
        if joined.strip():
            script = parse_script(joined, budget=budget)
            ok, reason = _judge_exec_script(
                script, budget=budget, depth=depth + 1, allow_chains=allow_chains,
            )
            if not ok:
                return False, _WATCH_REASON_PREFIX + reason
            return _judge_stages(
                rest_stages, budget=budget, depth=depth, allow_chains=allow_chains,
            )

    stripped = _strip_wrappers(tokens)
    base = len(tokens) - len(stripped)  # both helpers return a pure SUFFIX

    # Registered deviation: retry the peel AFTER wrapper stripping
    # (``timeout 5 sh -c 'df -h'`` — legacy strips the wrapper and then
    # rejects ``sh`` as an unknown binary; the facts engine sees through).
    if base:
        nested = unwrap_sh_c(list(words[base:]), budget=budget)
        if nested is not None:
            if nested.errors:
                return False, _issue_reason(nested.errors[0].kind, nested.errors[0].pos)
            if not nested.segments:
                return False, "sh -c body is empty or cannot be parsed"
            ok, reason = _judge_exec_script(
                nested, budget=budget, depth=depth, allow_chains=allow_chains,
            )
            if not ok:
                return False, reason
            return _judge_stages(
                rest_stages, budget=budget, depth=depth, allow_chains=allow_chains,
            )

    entry = stripped[0].rsplit("/", 1)[-1] if stripped else ""
    if entry in _ESCAPE_PRIMITIVES:
        if depth >= 2:
            return False, (
                f"'{entry}' nesting is too deep to determine read-only status reliably"
            )
        unwrapped = _unwrap_escape(stripped)
        if not unwrapped:
            return False, (
                f"'{entry}' is followed by no parseable command, so it is"
                " treated as unsafe (a read-only probe must look like:"
                " chroot /host <read-only command>)"
            )
        cut = base + (len(stripped) - len(unwrapped))
        ok, reason = _judge_exec_words(
            words[cut:],
            rest_stages,
            budget=budget,
            depth=depth + 1,
            allow_chains=allow_chains,
        )
        if ok:
            return True, None
        return False, (
            f"'{entry}' does not run a read-only command once on the host: {reason}"
        )

    ok, reason = _classify_argv(stripped)
    if not ok:
        return False, reason
    return _judge_stages(rest_stages, budget=budget, depth=depth)


def _chain_groups(
    script: ScriptFacts,
) -> list[tuple[list[WordFacts], list[list[WordFacts]]]]:
    """Group segments into ``;``/``&&``/``||``-separated chain groups.

    Within a group, ``|``-connected segments stay pipeline stages (head +
    tails — the same judgement width the non-chained path gives them); the
    chain separator itself is inert. Every group is judged independently by
    the caller and ALL must be read-only.
    """
    groups: list[tuple[list[WordFacts], list[list[WordFacts]]]] = []
    head: list[WordFacts] | None = None
    tails: list[list[WordFacts]] = []
    segs = script.segments
    ops = script.operators
    for i, seg in enumerate(segs):
        words = _segment_words(seg.command)
        if head is None:
            head = words
        else:
            tails.append(words)
        if i < len(ops) and ops[i] in (";", "&&", "||"):
            groups.append((head, tails))
            head, tails = None, []
    if head is not None:
        groups.append((head, tails))
    return groups


def _judge_exec_script(
    script: ScriptFacts, *, budget: Budget, depth: int,
    allow_chains: bool = False,
) -> tuple[bool, str | None]:
    bad = _structure_reason(
        script, allow_pipes=True, tail=_TAIL_PROBE, allow_chains=allow_chains,
    )
    if bad is not None:
        return False, bad
    if not script.segments:
        return True, None  # bare exec (no inner command) — read-only
    if allow_chains:
        # B46: a chained probe gets the FULL judgement width per group —
        # the head stage peels wrappers/escapes (``nsenter ...; nsenter ...``
        # is the canonical verify-phase shape), tails keep the pipeline
        # width. Every group must pass on its own.
        for head, tails in _chain_groups(script):
            ok, reason = _judge_exec_words(
                head, tails, budget=budget, depth=depth,
                allow_chains=allow_chains,
            )
            if not ok:
                return False, reason
        return True, None
    stages = [_segment_words(seg.command) for seg in script.segments]
    return _judge_exec_words(stages[0], stages[1:], budget=budget, depth=depth)


# --- kubectl exec/debug: the no-``--`` shape (R44) ---------------------------

#: kubectl exec/debug flags that consume the NEXT token as their value —
#: SINGLE SOURCE for this kubectl tool-domain vocabulary. Physically moved
#: here (R44) from ``providers.message_scanning``: the read-only judge lives
#: in the tools layer and must walk the same table, and the agent layer may
#: import FROM the tools layer (never the reverse); the carrier parsers keep
#: importing the name from their own layer, so there is still exactly one
#: declaration. Vocabulary diffed against the installed kubectl (v1.34.1):
#: every value-taking flag of ``exec``/``debug`` (their ``--help``) plus every
#: value-taking GLOBAL flag (``kubectl options``). A value flag left out here
#: has its VALUE read as a positional, which refuses a legitimate attach shape
#: — the over-deny direction the walker registers; it can never hide a
#: command. R49 verified the disjointness claim mechanically: every consumer
#: (``_strip_kubectl_prefix`` / ``_walk_separator`` /
#: ``exec_command_without_double_dash`` / the exec+debug identity readers)
#: walks EXEC/DEBUG shapes only, so ``-f``'s double life (value-taking here,
#: boolean under ``logs`` via the guard's subcommand table) has no shared
#: call site. ``run``/``attach`` value flags are deliberately NOT collected:
#: run's identity goes through the classifier's ``_first_positional``
#: (unknown-takes-value polarity), and a walker miss stays in the over-deny
#: direction documented above.
KUBECTL_EXEC_VALUE_FLAGS = frozenset({
    # exec / debug own
    "-c",
    "--container",
    "-f",
    "--filename",
    "--pod-running-timeout",
    "--image",
    "--image-pull-policy",
    "--profile",
    "--profile-output",
    "--copy-to",
    "--custom",
    "--env",
    "--set-image",
    "--target",
    # globals (kubectl options) that reach exec/debug
    "-n",
    "--namespace",
    "-s",
    "--server",
    "-v",
    "--v",
    "--vmodule",
    "--as",
    "--as-group",
    "--as-uid",
    "--cache-dir",
    "--cluster",
    "--context",
    "--kubeconfig",
    "--kuberc",
    "--user",
    "--username",
    "--password",
    "--token",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--tls-server-name",
    "--request-timeout",
    "--log-flush-frequency",
})

#: Single-dash shorthands that consume a value. pflag applies EVERY letter of
#: a cluster, and the first value-taking letter takes the REST of the cluster
#: as its value — or the next token when the cluster ends there (``-qc c1``
#: == ``-q -c c1``, the container consumes ``c1``). A shorthand NOT listed
#: here reads as valueless, which is the deliberate direction: a missed value
#: lands in the positional stream and refuses the shape (over-deny), whereas
#: the opposite reading would SWALLOW a real command (blind spot).
KUBECTL_VALUE_SHORTHANDS = frozenset({"c", "f", "n", "s", "v"})


def _flag_consumes_next_token(tok: str) -> bool:
    """Whether flag token *tok* takes the FOLLOWING token as its value."""
    if "=" in tok:
        return False  # ``--flag=value`` / ``-n=ns`` — the value is glued on
    if tok in KUBECTL_EXEC_VALUE_FLAGS:
        return True
    if tok.startswith("--"):
        return False  # unknown long option: valueless (fail-closed walk)
    body = tok[1:]
    for pos, letter in enumerate(body):
        if letter in KUBECTL_VALUE_SHORTHANDS:
            return pos == len(body) - 1  # value glued on, else the next token
    return False


def kubectl_flag_takes_value(tok: str) -> bool:
    """Whether a kubectl-side flag token consumes the NEXT token (R46).

    Public alias of the shared arity table above. The exec/debug parsers
    (``kubectl._debug_target_pod_name`` / ``_debug_target_node_name`` /
    ``_namespace_from_args``, ``execution_artifacts._exec_pod_identity``)
    used to hand-write PARTIAL copies of it — 7 / 12 / 2 items against the
    40 here — and then read a value flag's VALUE as the first positional:
    ``debug --request-timeout 30s node/n1`` returned ``30s`` as the
    "target pod", the node-scoped call took the ephemeral-container arm,
    and the created node-debugger pod leaked unregistered. One table, one
    reader.
    """
    return _flag_consumes_next_token(tok)


def _strip_kubectl_prefix(tokens: list[str]) -> list[str]:
    """Drop a leading ``kubectl [global flags] exec|debug`` prefix when present.

    Two faces hand this walker different shapes: the tool layer hands the
    bare ``POD [flags] [--] COMMAND`` (the exec/debug ``v_args``), while the
    classifier's nested-recursion face hands a FULL command line (``kubectl
    exec pod -- cmd`` found inside a parent exec payload). The walk must
    start at the same place for both. Peeling fires only on an exact
    ``kubectl`` + ``exec|debug`` pair; anything else — a pod literally named
    ``kubectl`` followed by a command named ``exec`` — keeps its tokens, and
    the walk then refuses the shape on the second positional: fail-closed
    either way.
    """
    if not tokens or tokens[0].rsplit("/", 1)[-1] != "kubectl":
        return tokens
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--" or tok == "-" or not tok.startswith("-"):
            break
        i += 2 if _flag_consumes_next_token(tok) else 1
    if i < len(tokens) and tokens[i] in ("exec", "debug"):
        return tokens[i + 1 :]
    return tokens


def exec_command_without_double_dash(tokens: list[str]) -> list[str] | None:
    """The command tokens of a no-``--`` exec/debug shape, else ``None``.

    ``kubectl exec POD COMMAND`` (no separator) is the shape a model writes
    when it believes the deprecated form still runs. kubectl refuses it
    outright — measured on v1.34.1: ``error: exec [POD] [COMMAND] is not
    supported anymore. Use exec [POD] -- [COMMAND] instead`` — so the shape's
    own command can never be executed by TODAY's client. That backstop is not
    a guarantee (the form ran for years and a different client version is not
    this judge's to assume), and the pre-R44 entry-only judgement blessed it
    as a read-only probe: the command rode the entry's verdict unclassified.

    Walk (pflag's interspersed model, the same one ``exec_separator_shape``
    presumes): flags are skipped, their values with them; the FIRST
    positional is the pod slot and any positional AFTER it is the command
    written without the separator — returned so the caller can refuse it and
    name it. An unknown ``-x``/``--x`` reads as VALUELESS, so its would-be
    value lands in that positional stream rather than swallowing a real
    command. ``-`` is an operand, not a flag (getopt).
    """
    rest = _strip_kubectl_prefix(tokens)
    entry_seen = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            return None  # the separator IS the delimiter — not this shape
        if tok == "-" or not tok.startswith("-"):
            if entry_seen:
                return rest[i:]
            entry_seen = True
            i += 1
            continue
        i += 2 if _flag_consumes_next_token(tok) else 1
    return None


def _walk_separator(
    rest: list[str],
) -> tuple[list[str], list[str] | None, int | None]:
    """Shared value-aware walk behind the R45 separator model.

    ``rest`` is already stripped of the ``kubectl [global flags]
    exec|debug`` prefix. Returns ``(positionals, after, index)``: every
    POSITIONAL ahead of the first TRUE separator, the tokens past it (or
    ``None``), and the separator's index in ``rest`` (or ``None``).

    "True" is the whole point. pflag is value-FIRST: a ``--`` that arrives
    while a value-taking flag is hungry (``-c --`` / ``-n --`` /
    ``--container --``) is that flag's VALUE, never a boundary, so the
    flag+value pairs are skipped with the shared
    ``_flag_consumes_next_token`` table and only the surviving ``--`` is a
    separator.
    """
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            return positionals, rest[i + 1 :], i
        if tok == "-" or not tok.startswith("-"):
            positionals.append(tok)
            i += 1
            continue
        i += 2 if _flag_consumes_next_token(tok) else 1
    return positionals, None, None


def _locate_boundary_token(
    tokens: list[str], target: int, v_args: str,
) -> int | None:
    """Raw offset of ``tokens[target]`` in ``v_args`` — must be a ``--``.

    Walks the quote-preserving tokens from the start: every step before
    ``target`` must find its token at or after the cursor (cursor moves
    past it), the token AT ``target`` must find and read exactly ``--``.
    Returns the raw offset of that ``--``, or ``None`` when the walk
    cannot vouch for the position (mis-split view, token not a literal
    substring, boundary slot holding something else). Used by both
    locators of ``inner_raw_after_double_dash``.
    """
    cursor = 0
    for at_index, tok in enumerate(tokens):
        if at_index > target:
            # This view split more words before the target than the
            # other one — the index stopped mapping to a position.
            return None
        at = v_args.find(tok, cursor)
        if at == -1:  # defensive: posix=False tokens are exact substrings
            return None
        cursor = at + len(tok)
        if at_index == target:
            return at if tok == "--" else None
    return None


def exec_separator_shape(tokens: list[str]) -> tuple[list[str], list[str] | None]:
    """pflag's view of an exec/debug line's ``--`` (R45).

    Returns ``(before, after)``: ``before`` is every POSITIONAL token ahead
    of the first TRUE separator (the entry included), ``after`` the tokens
    past it — or ``None`` when the line has no true separator.

    "True" is the whole point. pflag is value-FIRST: a ``--`` that arrives
    while a value-taking flag is hungry (``-c --`` / ``-n --`` /
    ``--container --``) is that flag's VALUE, never a boundary. The legacy
    first-standalone-``--`` split read those lines as "separator at N" and
    handed the tail to the inner judge while every client still saw the
    separator-less ``POD COMMAND`` form — which the deprecated path RUNS
    (kubectl v1.11 ``Complete``: ``p.Command = argsIn[1:]``; v1.23 keeps the
    same branch behind its warning).

    With a true separator, index 0 of ``before`` is the entry and anything
    beyond it is the R45 stray class: v1.11 exec prepends the strays to the
    command (``argsIn[1:]``), current exec drops them, and ``kubectl debug``
    resolves EVERY positional as a separate target. ``_strip_kubectl_prefix``
    runs first so both call shapes (bare ``POD ...`` and a full ``kubectl
    exec POD ...`` line) walk identically; the flag model is the shared
    ``_flag_consumes_next_token`` table.
    """
    positionals, after, _index = _walk_separator(_strip_kubectl_prefix(tokens))
    return positionals, after


def exec_separator_index(tokens: list[str]) -> int | None:
    """Index of the TRUE ``--`` separator in the ORIGINAL ``tokens`` (R45).

    ``None`` when no ``--`` survives pflag's value-FIRST scan (every dash
    sat in a flag's value slot, or there is none): the line has no
    separator, so a content view that slices at "the first standalone
    ``--``" starts INSIDE a flag value while every client runs the payload
    after the surviving separator (``exec pod -c -- -- chroot /host
    iptables -F``: the judge never saw ``chroot``, the classifier ruled the
    call pod-scoped, and an identity match on the approved pod passed the
    whole chain). The index counts from 0 of ``tokens`` itself — the peeled
    kubectl prefix included — so callers slice their own list directly.
    """
    rest = _strip_kubectl_prefix(tokens)
    _positionals, _after, index = _walk_separator(rest)
    if index is None:
        return None
    return len(tokens) - len(rest) + index


def _pre_separator_extras_reason(extras: list[str]) -> str:
    """Refusal for positionals between the entry and a TRUE ``--`` (R45).

    The stretch before the separator may hold exactly ONE positional — the
    entry. Anything else is a token whose execution this judge cannot name:
    kubectl v1.11 runs the strays as the command head (``exec pod rm
    /data/x -- cat`` ran ``rm /data/x cat``), current exec silently drops
    them, and ``kubectl debug`` reads every positional as a separate target.
    Admitting the trailing command would sign off on a call whose real
    effect is version- and subcommand-dependent, so the line is refused and
    the strays are named.
    """
    shown = " ".join(extras[:8]) + (" ..." if len(extras) > 8 else "")
    return (
        f"the entry is followed by extra tokens ('{shown}') before the '--'"
        " separator, and what runs then is not this judge's to assume:"
        " current kubectl exec silently drops them while older clients run"
        " them as the command head, and kubectl debug reads every positional"
        " as a SEPARATE TARGET; write: POD [flags] -- COMMAND (one entry,"
        " then the separator)"
    )


def _exec_without_separator_reason(v_args: str) -> str | None:
    """Refusal for a no-``--`` exec/debug shape that carries a command.

    A shape with NO trailing positional stays read-only: a bare entry (or an
    entry plus flags) runs nothing at all. The moment a command follows the
    pod slot without the separator, judging the entry alone would bless a
    command nobody classified — refuse, with the fix path in the reason.
    """
    try:
        tokens = shlex.split(v_args)
    except ValueError:
        return "command cannot be parsed (unbalanced shell quotes)"
    command = exec_command_without_double_dash(tokens)
    if command is None:
        return None
    shown = " ".join(command[:8]) + (" ..." if len(command) > 8 else "")
    return (
        f"the entry is followed by a command ('{shown}') written without the"
        " '--' separator, which kubectl does not run (\"exec [POD] [COMMAND]"
        " is not supported anymore. Use exec [POD] -- [COMMAND] instead\");"
        " write the probe as: POD [flags] -- COMMAND"
    )


# --- public surface judges --------------------------------------------------


def inner_raw_after_double_dash(v_args: str) -> tuple[str | None, str | None]:
    """Slice the raw text after the first TRUE ``--`` separator.

    Returns ``(inner_raw, None)`` on success, ``(None, None)`` when no
    standalone ``--`` exists (a pure entry) and ALSO when every ``--`` sat
    in a flag's VALUE slot (R45 — such a line has no separator in pflag's
    view, so it is the R44 separator-less form, not an inner-bearing one),
    and ``(None, reason)`` when the boundary cannot be located with
    confidence (fail-closed reason text for the caller to surface).

    R45: the boundary is pflag's OWN. A ``--`` swallowed as a value-taking
    flag's value (``-c --`` / ``-n --`` / ``--container --``) is not the
    separator; slicing there handed every content view — the inner judge,
    the escape peek, the segment parser, the host-carrier gates — the text
    of a FLAG VALUE while the true separator (and the command every client
    runs) sat further right (``exec pod -c -- -- chroot /host iptables
    -F``: the judge never saw ``chroot``, the classifier ruled the call
    pod-scoped, and an identity match on the approved pod passed the whole
    chain). ``exec_separator_index`` locates the true separator over the
    quote-aware argv; this function maps that token index back to a raw
    OFFSET — never a token re-join: re-joining shatters quote-adjacent
    words (``'a 'b''`` would gain a spurious word break and the strict
    ``sh -c`` unwrap would then refuse the four-word shape). Harness input
    rule 1 applies inside the judge itself.

    The offset mapping rides quote-PRESERVING tokens (posix=False keeps
    every token an exact substring of the source) and TWO independent
    locators must agree on the same raw ``--``:

    1. the INDEX mapping — walk the tokens up to the dequoted argv's
       boundary index (each step finds its token at or after the cursor,
       the boundary step must land on a literal ``--``);
    2. the WALK mapping — run the same value-aware walk over the
       source-preserving tokens and locate ITS boundary token.

    Each locator is blind where the other sees: a quote-glued word
    BEFORE the boundary (``-c'x y'``) splits the source view into more
    words than the argv, shifting the index map (it may land on a value
    slot's ``--``); a quote-wrapped flag (``'-c'``) stops the walk view
    from recognising the flag and it may swallow the true ``--`` as that
    value slot. Agreement on one raw offset is the fixed point; any
    disagreement — and any mis-split or non-``--`` boundary step —
    refuses rather than guesses. Tokens AFTER the agreed boundary never
    take part: quote-glued words there split differently between the two
    views (``awk --sour'{print > "f"}'`` is three posix words but six
    source words) and the boundary is already known.
    """
    try:
        argv = shlex.split(v_args)
    except ValueError:
        return None, "command cannot be parsed (unbalanced shell quotes)"
    index = exec_separator_index(argv)
    if index is None:
        return None, None
    try:
        tokens = shlex.split(v_args, posix=False)
    except ValueError:
        return None, "command cannot be parsed (unbalanced shell quotes)"
    at_mapped = _locate_boundary_token(tokens, index, v_args)
    rest = _strip_kubectl_prefix(tokens)
    _positionals, _after, offset = _walk_separator(rest)
    at_walked = (
        _locate_boundary_token(tokens, len(tokens) - len(rest) + offset, v_args)
        if offset is not None
        else None
    )
    if at_mapped is None or at_walked is None or at_mapped != at_walked:
        return None, "command cannot be parsed (token boundary lost); failing closed"
    return v_args[at_mapped + len("--") :], None


def kubectl_exec_rejection_reason_facts(v_args: str) -> str | None:
    """Facts-engine counterpart of ``kubectl_exec_rejection_reason``.

    Judges the inner command sliced from the original text by
    ``inner_raw_after_double_dash``. A quoted metachar inside the inner
    command stops tripping the screen (the P1 fix) while real structure
    still fails closed.

    The stretch before the ``--`` is judged for SHAPE, never for content:
    the pod / flags / prefix stay inert to the inner judge, but (R45) the
    separator must be pflag's own and the stretch may hold exactly ONE
    positional — the entry. A ``--`` that a value-taking flag swallowed is
    that flag's VALUE, so the line has no separator and belongs to the R44
    walker; extra positionals ahead of a true separator are refused, since
    their effect is client-dependent (old exec runs them as the command
    head, current exec drops them, debug reads every one as a target).
    """
    inner_raw, error = inner_raw_after_double_dash(v_args)
    if error is not None:
        return error
    if inner_raw is None:
        # No TRUE separator (R44 + R45): either no standalone ``--`` at all,
        # or every one sat in a flag's value slot and pflag never saw a
        # boundary. A bare entry — or an entry with flags only — runs
        # nothing and stays read-only, but a trailing COMMAND written
        # without the separator is a shape whose command the entry-only
        # judgement would wave through unclassified (the value-slot form's
        # command is what the deprecated path RUNS). The shared walker
        # refuses it.
        return _exec_without_separator_reason(v_args)
    try:
        tokens = shlex.split(v_args)
    except ValueError:
        return "command cannot be parsed (unbalanced shell quotes)"
    positionals, after = exec_separator_shape(tokens)
    if after is None:
        # Defensive twin of the branch above: this locator and the raw
        # slice walk the same true separator, so the two can only disagree
        # if one of them changes alone. Keep the refusal either way.
        return _exec_without_separator_reason(v_args)
    if len(positionals) > 1:
        return _pre_separator_extras_reason(positionals[1:])
    if not inner_raw.strip():
        return None
    budget = Budget()
    root = parse_script(inner_raw, budget=budget)
    # B46: segment-chained all-readonly probes are admitted — the same
    # dialect target_guard's execute-phase readonly bypass already speaks
    # (its ``_PROBE_SEPARATOR_OPS``). Redirects / substitutions / background
    # / newlines still fail closed (``_structure_reason``).
    ok, reason = _judge_exec_script(root, budget=budget, depth=0, allow_chains=True)
    return None if ok else reason


def host_command_rejection_reason_facts(command: str) -> str | None:
    """Facts-engine counterpart of ``host_command_rejection_reason``.

    Structure-free commands take a plain shlex fast path (the 90% hot
    path — verdicts byte-identical to the deleted legacy engine's on this
    class); anything carrying a structural character is parsed and judged
    on facts. A bare host command stays a SINGLE diagnostic: any operator
    (pipes included), redirect, substitution or subshell refuses — matching
    the deleted legacy metachar screen's verdicts, minus the quoted
    literals that screen could not tell apart.
    """
    if not command or not command.strip():
        return "empty command"
    if not has_shell_structure(command):
        try:
            tokens = shlex.split(command)
        except ValueError:
            return "command cannot be parsed (unbalanced shell quotes)"
        if not tokens:
            return "empty command"
        ok, reason = _classify_argv(tokens)
        return None if ok else reason
    root = parse_script(command)
    return _judge_host_script(root)


def _judge_host_script(root: ScriptFacts) -> str | None:
    """Host-surface verdict for a parsed script: single diagnostic command,
    no operators/redirects/substitutions, with a watch payload re-parsed the
    way watch itself will run it."""
    bad = _structure_reason(root, allow_pipes=False, tail=_TAIL_HOST)
    if bad is not None:
        return bad
    if not root.segments:
        return "empty command"
    words = _segment_words(root.segments[0].command)
    if not words:
        return "empty command"
    tokens = [word_token(w) for w in words]
    watch_payload = _watch_payload(tokens)
    if watch_payload is not None:
        joined = " ".join(watch_payload)
        if joined.strip():
            bad = _judge_host_script(parse_script(joined))
            if bad is not None:
                return _WATCH_REASON_PREFIX + bad
            return None
    ok, reason = _classify_argv(tokens)
    return None if ok else reason


def argv_rejection_reason_facts(argv: list[str]) -> str | None:
    """Facts-engine counterpart of ``is_readonly_argv`` (design 4.6 face 8).

    The argv arrives already tokenised — quoting is gone and a real
    exec(2) delivers the vector verbatim, so no structural re-parse of
    the tokens themselves is possible (or needed). The ONE structural
    recovery still required at this level is ``watch``: procps watch
    joins its argv and hands the string to ``sh -c``, so the payload is
    re-judged as a script exactly the way watch will run it (design doc
    P2). The legacy argv chain never modelled this — the raw-string
    surfaces stayed safe only because an upstream substring screen
    happened to fire first, while the ``exec_host_command`` binary+args
    path had no screen at all (an injection wrapped as
    ``watch bash -c ...`` was mis-attributed as a read-only diagnostic).
    """
    if not argv:
        return None
    watch_payload = _watch_payload(argv)
    if watch_payload is not None:
        # Tokens are dequoted argv — exactly what watch joins for sh -c.
        joined = " ".join(watch_payload)
        if joined.strip():
            bad = _judge_host_script(parse_script(joined))
            if bad is not None:
                return _WATCH_REASON_PREFIX + bad
            return None
    ok, reason = _classify_argv(argv)
    return None if ok else reason


def contains_shell_metachar_facts(command: str) -> bool:
    """Facts-engine counterpart of ``contains_shell_metachar``.

    True exactly when the command carries shell STRUCTURE (any operator,
    redirect, substitution, subshell — quoted literals excluded) or cannot
    be parsed with confidence (fail-closed; the legacy substring scan
    returned False there — a registered tightening).

    A ``watch`` payload is ALSO structure for this screen's purpose: watch
    re-parses its argv through sh -c, so structure quoted-literal HERE is
    real syntax THERE (design doc P2). The skip_guard caller pairs this
    screen with an argv-level judge that strips wrappers without modelling
    the re-parse, so the payload check must live here.
    """
    if not has_shell_structure(command):
        return False
    root = parse_script(command)
    if _structure_reason(root, allow_pipes=False, tail=_TAIL_HOST) is not None:
        return True
    for seg in root.segments:
        if isinstance(seg.command, ScriptFacts):
            continue  # subshell — already flagged by _structure_reason
        tokens = [word_token(w) for w in _segment_words(seg.command)]
        payload = _watch_payload(tokens)
        if payload is None:
            continue
        joined = " ".join(payload)
        if joined.strip() and contains_shell_metachar_facts(joined):
            return True
    return False
