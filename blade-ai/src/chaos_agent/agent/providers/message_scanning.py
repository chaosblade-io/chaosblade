"""Stateless message-history scanning primitives shared by all FaultProviders.

Phase-14 lateral retirement (G1): these scans previously lived in
``providers/chaosblade/detection.py`` and were module-level imported by the
host_shell and k8s_native carriers — a cross-carrier dependency. They are
physically moved here, line-for-line, as a FLAT providers module so no
carrier subpackage owns them. The blade carrier's own output-format
knowledge (experiment-UID extraction, blade-evidence scans) stayed in the
chaosblade domain (``chaosblade/verify.py``); everything here is
carrier-agnostic — the *data* (which tool names / kubectl subcommands count
as a given carrier's injection) is declared on the provider class, and these
functions take that set as a parameter.

This module imports NOTHING from any carrier subpackage (zero carrier
dependency, verified by the phase-9/14 import guards) — only langchain
messages, the generic tools layer, and stdlib.
"""

from __future__ import annotations

import logging
import re
import shlex

from langchain_core.messages import AIMessage, ToolMessage

# kubectl tool-domain vocabulary — SINGLE SOURCE, physically in the tools
# layer (R44): the read-only judge (``tools._readonly_facts``) walks the same
# table for the separator-less exec shape, and the tools layer may not import
# the agent layer. Imported here (allowed direction) so this module stays the
# flat name the carrier parsers already alias.
from chaos_agent.tools._readonly_facts import KUBECTL_EXEC_VALUE_FLAGS  # noqa: F401

logger = logging.getLogger(__name__)


def build_tool_call_args_lookup(messages: list) -> dict:
    """Map ``tool_call_id`` → tool call args by scanning AIMessages.

    Lets a ToolMessage be cross-referenced back to the originating tool call
    arguments (e.g. ``subcommand`` / ``v_args``). Entries with missing/empty
    id are skipped.
    """
    lookup: dict[str, dict] = {}
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tc_id = tc.get("id", "")
                args = tc.get("args", {})
            else:
                tc_id = getattr(tc, "id", "")
                args = getattr(tc, "args", {})
            if tc_id:
                lookup[tc_id] = args
    return lookup


# Markers that mean a command NEVER reached the target (pre-execution
# rejection): guard reject, phase-1 read-only enforcement, unknown
# subcommand, arg validation error. Everything else — success, timeout,
# non-zero exit — counts as an ATTEMPTED action. This is the HIGH-TOLERANCE
# rule shared by the per-provider executed-action scans: a mutation that
# reached the cluster/host (even if it timed out or failed) counts as "done",
# so an ambiguous timeout no longer produces a false "missing step"
# (the original INCOMPLETE-INJECTION false-positive).
PRE_EXEC_REJECTION_MARKERS = (
    "[target_guard]",
    "phase1_readonly_violation",
    "does not accept subcommand",
    "validationerror",
    "validation error",
)


def reached_target(content: object) -> bool:
    """True unless the ToolMessage content is a pre-execution rejection."""
    low = (content if isinstance(content, str) else "").lower()
    return not any(m in low for m in PRE_EXEC_REJECTION_MARKERS)


def is_budget_expiry_unknown(content: object) -> bool:
    """True when a tool result renders a CALLER-BUDGET EXPIRY — the
    outcome-UNKNOWN third state, neither success nor failure.

    R57 (the ToolTimeoutError exception branch) and R59 (the wiz
    ``task timed out`` receipt branch) BOTH render this state behind an
    ``Error:`` contract head — the local wait was killed while "the
    command may STILL be running server-side". A budget expiry is
    therefore UNJUDGEABLE: consuming it as a failure verdict is the
    B39/B40 family defect (an ``Error:`` prefix read as a failure
    judgement), the disease that recurred at every consumer that reads
    results in two states only.

    This is the single-source THIRD-STATE predicate. It lives here, in
    the neutral home, for the same reason the vocabularies below do:
    every judgement face that reads tool results (the native carrier,
    the faultdrill carrier, any future carrier) MUST agree on what
    "unknown" means or their attributions drift apart. Consumers decide
    the DIRECTION of the third state per face (counter-evidence: keep
    the attribution; attribution: count it as having reached; query:
    report an unknown status — see ``chaosblade/verify._QueryK8sResult
    ("unknown", ...)`` for the project's canonical query-side sample);
    the predicate only names it. An earlier inline copy lived in
    ``scan_native_issue_disproven``'s judge loop (R65) and its
    faultdrill twin fed a bare ``Error:`` prefix reading (R66) — both
    now route here.

    Matching the FEATURE (``timed out``), not the fix branches' exact
    wording: a future re-wording of the R57/R59 notes degrades to this
    same reading instead of silently reverting to the failure verdict.
    """
    low = (content if isinstance(content, str) else "").lower()
    return "timed out" in low


# ---------------------------------------------------------------------------
# kubectl tool-domain vocabulary (phase-14 G2, design D2)
#
# These word lists and the fail-safe exec-mutation judgement are KUBECTL
# TOOL knowledge, not any single carrier's property: the k8s-native
# provider consumes them to attribute its OWN injections, while the blade
# domain consumes the same lists for its boundary judgements (embedded
# ``kubectl exec ... blade create`` delivery; native takeover after a
# failed blade_create). Both domains MUST see the identical
# vocabulary/judgement or their attributions drift apart — hence this
# neutral home. Previously class attributes / module functions on
# ``k8s_native.provider`` (physically moved here, phase-14 G2).
# ---------------------------------------------------------------------------

#: kubectl OBJECT-WRITE subcommands — the verb itself IS the mutation (the
#: API-server result is trustworthy evidence). Single source of truth for
#: the kubectl-native carrier's injection attribution; the target-guard
#: invariant (test_kubectl_verb_consistency) pins this set as a subset of
#: ``classifier.DESTRUCTIVE_KUBECTL_SUBS``.
#:
#: TEARDOWN≠MUTATION CONTRACT (B76 family): ``delete`` on a REGISTERED
#: vehicle (recovery carrier / its RBAC family) is ASSET REMOVAL, never
#: fault injection — but this vocabulary CANNOT make that distinction:
#: it has no access to the ``execution_artifacts`` registry. Consumers of
#: this set (the scan primitives below, the provider hooks) apply the
#: teardown exemption by THREADING the ``is_teardown`` matcher
#: (``execution_artifacts.make_teardown_matcher`` — P3, landed): the
#: exemption is applied INSIDE the vocabulary layer at CALL granularity,
#: mixed batches included. Passing ``is_teardown=None`` (the default)
#: requests RAW mutation evidence — legitimate for tests and for callers
#: whose evidence shapes can never be a registered-vehicle delete, but an
#: agent-side seam feeding attribution consumers must thread the matcher.
#: The obligation is enforced structurally by
#: ``tests/test_agent/test_teardown_vocab_sentinel.py`` (the threaded
#: parameter name ``is_teardown`` is an exemption marker) and pinned by
#: the family teeth (``TestIssueTimeTeardownAttribution``).
KUBECTL_WRITE_SUBCOMMANDS = frozenset(
    {
        "scale",
        "patch",
        "cordon",
        "taint",
        "set",
        "delete",
        "drain",
        "label",
    }
)

#: kubectl COMMAND-MODE subcommands — these ENTER a pod/host to run a
#: command (``kubectl exec`` / ``kubectl debug``), so whether they mutate is
#: judged on the INNER command (:func:`exec_inner_command_mutates`), not
#: the verb. Kept separate from the object-write list so the object-write
#: invariant above is untouched.
KUBECTL_COMMAND_SUBCOMMANDS = frozenset({"exec", "debug"})


def exec_inner_command_mutates(v_args: str) -> bool:
    """True if a ``kubectl exec``/``debug`` inner command mutates state (an
    injection), False for a read-only probe.

    Fail-safe attribution: exec/debug default to MUTATING; only the shared
    read-only vocabulary (``tools.readonly.is_readonly_kubectl_exec`` — the
    single source shared with the guard-scope classifier and ``host_read``)
    is excluded. This inverts the former fault-family blacklist, which
    silently missed shell CPU loops (``while true``), ``/etc/hosts`` edits,
    ``dmsetup`` IO-error maps and ``nc`` port listeners — all real
    skill-case injections. Dual-use tools (iptables / ip / tc / systemctl /
    mount / dmesg) are judged at the ARGUMENT level there (``iptables -L``
    read, ``iptables -A`` mutating), so a novel injection shape is
    attributed by default rather than slipping through as "not an
    injection".

    Public since phase-7 T4 (originally as
    ``k8s_native.provider.exec_inner_command_mutates``): the issue-time
    attribution hook and the verifier-side reverse scans consult it as the
    carrier's owned classifier. Phase-14 G2: physically moved here — kubectl
    tool-domain knowledge, consumed identically by both carrier domains
    (attribution drift between them would mis-route recovery).
    """
    from chaos_agent.tools.readonly import is_readonly_kubectl_exec

    return not is_readonly_kubectl_exec(v_args)


# ---------------------------------------------------------------------------
# exec/debug payload SYNTAX parser (round-15 root fix)
#
# The eleven copy-pasted word-containment gates (``"blade" in v_args and
# "create" in v_args``) across the blade scans, the provider faces, and the
# shared attribution scans judged VOCABULARY, not SYNTAX: a composite decoy
# payload (``sh -c 'kubectl get pods -o json; echo blade create done'``)
# passed every one of them while carrying no blade command at all — the
# decoy words live in an ``echo`` argument, never at a command position.
# This parser is the structural half of the cure: it walks the payload the
# way the shell would RUN it and yields command segments (head token at
# index 0), so callers judge WHAT runs instead of which WORDS appear.
# Carrier-agnostic by construction — it knows kubectl/shell structure, and
# the blade verb filtering rides on top in the carrier's own domain
# (``chaosblade/verify.classify_blade_exec_payload``).
# ---------------------------------------------------------------------------

#: kubectl exec/debug flags that consume the NEXT token as their value —
#: the structural walk must skip both tokens to reach the pod slot / the
#: ``--`` separator. CANONICAL declaration lives in ``tools._readonly_facts``
#: (R44 move: the tools-layer read-only judge walks the same table, and the
#: tools layer cannot import the agent layer); this module imports it above
#: and the carrier parsers alias it from here — one declaration, two faces.

#: Shell interpreters whose ``-c`` argument is a SCRIPT the interpreter
#: expands into multiple commands (``sh -c 'a; b'`` runs TWO commands).
_SCRIPT_SHELLS = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh"})

#: Wrapper commands that DELAY the real command behind their own arguments
#: (``timeout 30 cmd``, ``nohup cmd``, ``env VAR=1 cmd``, ``xargs cmd``).
#: Their own name at the command position is NOT the command that runs —
#: the real verb follows the wrapper's own argument prefix (round-16 C:
#: a wrapped ``blade destroy`` was judged as a non-blade head and the
#: death ledger missed the real kill).
_WRAPPER_COMMANDS = frozenset(
    {"timeout", "nohup", "env", "nice", "xargs", "setsid", "stdbuf", "ionice"}
)

#: ``VAR=value`` token (a bare assignment may precede the command).
_ASSIGNMENT_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: ``timeout``'s duration argument (``30``, ``30s``, ``1.5m``).
_DURATION_TOKEN_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")

#: Redirection tokens (``2>&1``, ``>``, ``>>log``, ``2>/tmp/x``, ``<file``)
#: — shell SYNTAX around the command, never an argument of it.
_REDIRECTION_TOKEN_RE = re.compile(r"^(?:\d*(?:>{1,2}|<&?)|<>|>&)")

#: A numeric redirection prefix at a BUFFER's tail (``2>``, ``3>``, ``>``)
#: — the lexer-side check that tells a ``&``/``|`` INSIDE a redirection
#: token (``2>&1``, ``2>|``) from a real command separator (round-17 S1).
_REDIR_PREFIX_AT_END = re.compile(r"\d*>$")

#: Value flags per wrapper — SEPARATED spelling (``--signal KILL``)
#: consumes the NEXT token as the flag's own value, so the prefix walk
#: must skip BOTH tokens or the value (``KILL``) is mistaken for the
#: command head and the wrapped verb is never reached (round-17 S2:
#: ``timeout --signal KILL 30 blade destroy`` and ``xargs -I {} blade
#: destroy`` both missed their real kill). Glued spellings
#: (``--signal=KILL``, ``-I{}``, ``-oL``) are single tokens already covered
#: by the plain flag skip. Flags whose value is OPTIONAL (xargs ``-i``/
#: ``--replace``) are deliberately NOT listed — skipping their next token
#: would eat the command head itself when the value is omitted; an
#: unlisted MANDATORY value flag degrades fail-closed (the wrapped verb
#: is missed, a death-ledger loss, never a decoy promotion).
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"--signal", "-s"}),
    "xargs": frozenset({
        "-I", "-n", "--max-args", "-L", "--max-lines",
        "-P", "--max-procs", "-s", "--max-chars",
        "-E", "-e", "-a", "--arg-file",
    }),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--class-data"}),
    "stdbuf": frozenset({
        "-i", "-o", "-e", "--input", "--output", "--error",
    }),
    "env": frozenset({"-u", "--unset", "-S", "--split-string"}),
    "nohup": frozenset(),
    "setsid": frozenset(),
}

#: Top-level command-boundary operators. A BARE ``&&``/``||``/``;``/``|``/
#: ``&`` token is shell SYNTAX — the same command boundary the script
#: face (:func:`_split_script_segments`) applies character-level inside
#: ``sh -c`` quotes. At the token-stream level these used to ride along as
#: inert "argument" tokens, so a bare composite stayed ONE segment
#: (round-25 K2: ``blade destroy A && blade destroy B`` registered only
#: the first kill — and the ``--uid`` spelling lost the FIRST instead,
#: collect_flag_values last-wins — while an ``echo`` companion riding the
#: same composite penetrated the ``pure_create`` receipt-trust gate:
#: round-25 K2c). Glued spellings (``A&&B``, one token) stay UNSPLIT —
#: fail-closed: a token's interior is value space, and only the script
#: face (character-level) may judge it.
_SEGMENT_BOUNDARY_TOKENS = frozenset({"&&", "||", ";", "|", "&"})


def _structure_open_len(script: str, i: int, *, at_head: bool) -> int:
    """Length of the shell structure opener at ``script[i]``, 0 if none.

    ``$(`` (command substitution, 2), a backtick (substitution, 1), a
    bare ``(`` (subshell, 1) and a command-POSITION ``{`` (command
    group, 1) open a nesting level — the shell EXECUTES the command
    inside each, exactly as it executes the command after a ``;``
    (R33/G-12: a primitive riding one of these structures was invisible
    to every segment-based consumer while the shell ran it). Value
    expansions never open: ``${VAR}`` expands a variable, ``$((expr))``
    is arithmetic, an escaped ``\\$(…)`` stays literal, and a mid-token
    ``{`` is brace expansion (``cat {a,b}``) — the group reading needs
    the brace at command position. A bare ``(`` opens regardless of
    position: shell grammar has no argument-position ``(`` (it is a
    syntax error, never a runnable command), so opening is the safe
    reading. The backtick branch also runs inside DOUBLE quotes (they
    execute substitution); single quotes never reach this function.
    """
    ch = script[i]
    n = len(script)
    if ch == "`":
        return 1
    if ch == "(":
        return 1
    if ch == "{":
        return 1 if at_head else 0
    if ch == "$" and i + 1 < n and script[i + 1] == "(":
        if i + 2 < n and script[i + 2] == "(":
            return 0  # $((expr)) — arithmetic expansion, no command
        return 2
    return 0


def _split_script_segments(script: str) -> list[str]:
    """Split a shell script into its command segments (quote-aware).

    ``;``, ``&&``, ``||``, ``|`` and newlines each start a NEW command;
    quoted spans (single/double) and backslash escapes protect their
    contents, so a separator inside a string literal never splits a
    command — and a decoy word inside a literal never becomes its own
    segment. Doubled separators (``&&``/``||``) leave an empty middle
    segment that is dropped. Returns the non-empty segment texts.

    Shell STRUCTURES run commands too (R33/G-12): ``$(cmd)``, backtick
    substitution, ``( cmd )`` subshells and ``{ cmd; }`` command groups
    execute their interior just as ``;`` executes its tail. Each opener
    ends the current segment (the opener itself is dropped — the
    interior is judged as its own command stream, with its own
    separators, quotes and NESTED structures) and the closer ends the
    segment then resumes the suspended quote: ``"$(cmd)"`` executes
    under double quotes, so the structure opens INSIDE them, while a
    single-quoted ``'$(…)'`` stays literal and never enters the
    structure branch. An opener inside double quotes splits the segment
    mid-quote (the residual text may be unlexable — an unlexable
    segment contributes nothing, fail-closed as everywhere here);
    ``${VAR}``/``$((expr))``/``\\$(…)`` are value forms, never
    structures. Pre-fix, the splitter's vocabulary was the six
    separators alone: a primitive riding a structure headed no segment,
    and the escape scope / fault-binary / blade payload / death-ledger
    consumers were blind to a command the shell runs (probe: seven
    hidden forms classified scope=pod and passed the screener
    end-to-end with an approved-pod identity match).

    R35/G-13 — the closer's ARGUMENT TAIL. Words following a closer
    with no separator between are the HOST command's parameter text
    (``echo done $(date) stress-ng`` runs echo and date; stress-ng is
    printed, never executed) — they merge back into the host's segment
    instead of heading their own (the round-15 contract: an
    argument-position word never produces a segment of its own). The
    context releases at a boundary (a word after ``;``/``&&`` is a new
    command again) and never opens after a bare-assignment host
    (``VAR=$(date) cmd`` — the tail is the post-assignment command,
    which runs). Pre-fix the G-12 closer split promoted the tail to a
    segment head: escape scope rejected a legal form, fbm withheld the
    demolition exemption, ``has_destroy`` read echo's parameter text,
    and the death ledger aligned 3 ghost destroy events.

    R36/G-14 — a HEREDOC's body is stdin text. ``cat > f <<EOF`` feeds
    the following lines to cat's stdin: they land IN THE FILE, nothing
    executes, so the operator + delimiter end the command line and the
    body up to (and including) the terminator line is skipped wholesale
    instead of heading its own segments. Quoted delimiters
    (``<<"EOF"``/``<<'EOF'`` — the carrier-blessed restore-script form)
    and ``<<-`` tab stripping are honored; quotes anywhere inside the
    delimiter word strip out (``<<E"O"F`` terminates at ``EOF``, and a
    quoted space or operator is part of the word); an unterminated body
    runs to
    EOF (fail-closed — the shell itself refuses the script); ``<<<`` is
    a herestring, its word an argument, not a body. Pre-fix every body
    line headed a segment: a restore script WRITTEN via the quoted-
    heredoc form got killed by the escape guard whenever its text
    mentioned a banned primitive (probe: scope=__escape__, route=retry,
    reject_banned naming 'nsenter'), and the demolition exemption was
    withheld for a cleanup script whose body said ``stress-ng``.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    # Quote suspended when a structure opened (R33/G-12): the interior
    # parses its own quotes; the closer resumes the suspended one. The
    # stack also carries the HOST index — the segment the opener split,
    # whose parameter tail follows the closer (None when the opener sat
    # at a command position or after a bare ``VAR=`` assignment).
    suspended: list[tuple[str | None, int | None]] = []

    # R35/G-13: the open argument-tail host (a segment index), or None.
    arg_tail: int | None = None

    # R36/G-14: the open heredoc delimiter and the line where its body
    # starts. A heredoc's body is STDIN TEXT — lines fed to the command
    # (they land IN THE FILE), never a command stream — so the body is
    # skipped wholesale instead of being split into command segments.
    heredoc_delim: str | None = None
    heredoc_body_start: int = -1

    def _settle_arg_tail() -> None:
        """Merge the buffered tail into its host segment and close it."""
        nonlocal arg_tail, buf
        if arg_tail is None:
            return
        text = "".join(buf)
        if text.strip():
            segments[arg_tail] += text
        buf = []
        arg_tail = None

    def _open_host(raw: str) -> int | None:
        """The arg-tail host for a structure opening after ``raw``.

        The segment the opener splits hosts the closer's tail — unless
        the raw text is a bare assignment prefix (``VAR=$(…) cmd``: the
        tail is the post-assignment command, which runs) or blank (the
        opener sits INSIDE a still-open arg-tail — ``echo $(date)
        $(pgrep x) tail`` — and inherits it: every tail is the one host
        command's parameter).
        """
        stripped = raw.strip()
        if not stripped:
            return arg_tail
        if _ASSIGNMENT_TOKEN_RE.match(stripped.split()[0]):
            return None
        return len(segments)

    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if heredoc_delim is not None and i >= heredoc_body_start:
            # Inside the heredoc body (R36/G-14): scan line by line —
            # the terminator line closes it, every other line is stdin
            # text (``<<-`` strips leading tabs). An unterminated body
            # runs to EOF — fail-closed, the shell itself refuses the
            # script.
            eol = script.find("\n", i)
            line = script[i : n if eol == -1 else eol]
            if line.lstrip("\t") == heredoc_delim:
                heredoc_delim = None
            i = n if eol == -1 else eol + 1
            continue
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(script[i : i + 2])
                i += 2
                continue
            if quote == '"' and ch in ("$", "`"):
                opener_len = _structure_open_len(script, i, at_head=False)
                if opener_len:
                    inherited = arg_tail
                    if inherited is not None and buf and "".join(buf).strip():
                        _settle_arg_tail()
                    host = None
                    if buf:
                        host = _open_host("".join(buf))
                        segments.append("".join(buf))
                        buf = []
                    elif inherited is not None:
                        host = inherited
                    suspended.append((quote, host))
                    quote = None
                    i += opener_len
                    continue
            if ch == quote:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(script[i : i + 2])
            i += 2
            continue
        if (
            ch == "<"
            and script[i + 1 : i + 2] == "<"
            and script[i : i + 3] != "<<<"
        ):
            # R36/G-14: a heredoc operator plus its delimiter ends the
            # COMMAND LINE (the newline after them is the boundary —
            # `cat <<EOF; cmd` still runs cmd on the same line); every-
            # thing from the NEXT line to the terminator line is stdin
            # text, skipped wholesale. ``<<<`` is a herestring — its
            # word is an argument, not a line-oriented body.
            j = i + 2
            if script[j : j + 1] == "-":
                j += 1
            while j < n and script[j] in " \t":
                j += 1
            # POSIX: quotes ANYWHERE in the delimiter word make that part
            # literal — ``<<E"O"F`` terminates at a bare ``EOF`` — so the
            # word scan must skip quoted spans (a quoted space or operator
            # is PART of the word) and the delimiter is the word with
            # EVERY quote stripped. Keeping a raw word-internal quote in
            # the delimiter made the terminator line never match and the
            # body ran to EOF, swallowing the command AFTER the heredoc
            # (a MISS: a real post-heredoc command vanished from every
            # segment consumer; shell probe: dash ran the same form fine).
            dstart = j
            delim_quote: str | None = None
            while j < n:
                cj = script[j]
                if delim_quote is not None:
                    if cj == delim_quote:
                        delim_quote = None
                elif cj in "'\"":
                    delim_quote = cj
                elif cj in " \t\n;&|":
                    break
                j += 1
            delim = script[dstart:j].replace("'", "").replace('"', "")
            if not delim:
                # No delimiter word: not a heredoc the shell would run
                # — keep the operator as inert text (fail-closed no-op).
                buf.append(script[i : i + 2])
                i += 2
                continue
            heredoc_delim = delim
            nl = script.find("\n", j)
            heredoc_body_start = n if nl == -1 else nl + 1
            i = j
            continue
        if ch in ("$", "`", "(", "{"):
            opener_len = _structure_open_len(
                script, i, at_head=not "".join(buf).strip()
            )
            if opener_len:
                inherited = arg_tail
                if inherited is not None and buf and "".join(buf).strip():
                    _settle_arg_tail()
                host = None
                if buf:
                    host = _open_host("".join(buf))
                    segments.append("".join(buf))
                    buf = []
                elif inherited is not None:
                    host = inherited
                suspended.append((quote, host))
                i += opener_len
                continue
        if ch == ")" or (ch in ("}", "`") and suspended):
            # A closer ENDS the segment. With a suspended quote it also
            # resumes it (the ``"$(cmd)"`` tail). A bare ``)`` with an
            # EMPTY suspend stack is still a boundary (R33/G-12b): the
            # case pattern terminator (``case x in a) cmd;; esac``)
            # OPENS the branch command list — the shell executes the
            # branch, so its command must head its own segment. The
            # closer character itself is dropped either way; ``}`` and
            # a backtick keep the suspended-only reading (a value-form
            # ``${VAR}`` tail would otherwise shred an argument). Words
            # gathered so far belong to the still-open ARG-TAIL host
            # (R35/G-13), not to the structure being closed.
            _settle_arg_tail()
            if buf:
                segments.append("".join(buf))
                buf = []
            if suspended:
                quote, arg_tail = suspended.pop()
            else:
                arg_tail = None
            i += 1
            continue
        if ch in (";", "\n", "&", "|"):
            # A ``&``/``|`` glued to a numeric redirection prefix is shell
            # SYNTAX inside the token (``2>&1``, ``3>&1``, ``2>|``), not a
            # command boundary — the separator belongs to the redirection
            # (round-17 S1: ``blade 2>&1 destroy X`` was torn at the ``&``
            # into ``blade 2>`` + ``1 destroy X`` and the real kill's
            # segment grew a non-blade head, so the death ledger missed
            # it; the round-16 C4 anchor had pinned only the direct
            # token-stream form, never the in-script form).
            if ch in ("&", "|") and _REDIR_PREFIX_AT_END.search("".join(buf)):
                buf.append(ch)
                i += 1
                continue
            _settle_arg_tail()
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    _settle_arg_tail()
    segments.append("".join(buf))
    return [seg for seg in (s.strip() for s in segments) if seg]


def _segments_from_script(
    script: str, *, chroot_delegation: bool = True,
) -> list[list[str]]:
    """Command segments of a shell script text (quote-aware split)."""
    segments: list[list[str]] = []
    for seg_text in _split_script_segments(script):
        try:
            seg_tokens = shlex.split(seg_text)
        except ValueError:
            # Unlexable segment contributes nothing — fail-closed: no
            # segments, no judgements, no ledger entries.
            continue
        segments.extend(
            _segments_from_tokens(seg_tokens, chroot_delegation=chroot_delegation)
        )
    return segments


def _is_command_head(tok: str) -> bool:
    """Whether a token sits at a COMMAND position head (basename form).

    Accepts both the bare name (``sh``, ``blade``) and full paths
    (``/usr/bin/blade``) — the delivered command may spell either.
    """
    base = tok.rsplit("/", 1)[-1]
    return base in _SCRIPT_SHELLS or base in ("blade", "chroot")


def _drop_redirection_tokens(tokens: list[str]) -> list[str]:
    """Remove redirection tokens — they carry no argument semantics.

    ``2>&1``-style tokens are shell SYNTAX around the command, not
    arguments of it: dropping them before the verb/argument walk keeps
    ``blade 2>&1 destroy <uid>`` judged by its real verb and arguments
    (round-16 C4: the verb sat behind an interleaved redirection, the
    verb slot read ``2>&1`` and the ledger missed the real kill).
    """
    return [t for t in tokens if not _REDIRECTION_TOKEN_RE.match(t)]


def _strip_wrapper_prefix(tokens: list[str]) -> list[str]:
    """Drop the command-position WRAPPER prefix (``VAR=x timeout 30 cmd``).

    ``timeout``/``nohup``/``env``/``xargs`` & co. delay the real command
    behind their own arguments: timeout consumes its flags (a SEPARATED
    value flag eats its value token too — round-17 S2) plus one duration;
    env and bare shell assignments consume ``NAME=value`` runs. The first
    token that is neither an assignment, a redirection nor a wrapper IS
    the command head, and the remaining stream is the command.
    Fail-closed: a wrapper with nothing behind it yields no command (the
    empty list).
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ASSIGNMENT_TOKEN_RE.match(tok) or _REDIRECTION_TOKEN_RE.match(tok):
            i += 1
            continue
        base = tok.rsplit("/", 1)[-1]
        if base not in _WRAPPER_COMMANDS:
            break
        i += 1
        value_flags = _WRAPPER_VALUE_FLAGS.get(base, frozenset())
        while i < len(tokens):
            tok = tokens[i]
            if not (tok.startswith("-") and tok != "-"):
                break
            if tok in value_flags and i + 1 < len(tokens):
                i += 2  # separated value flag: its VALUE follows it
            else:
                i += 1  # boolean / glued-value flag
        if (
            base == "timeout"
            and i < len(tokens)
            and _DURATION_TOKEN_RE.match(tokens[i])
        ):
            i += 1
    return tokens[i:]


def _segments_from_tokens(
    cmd_tokens: list[str], *, chroot_delegation: bool = True,
) -> list[list[str]]:
    """Command segments of a delivered container command (token form).

    Redirection tokens are dropped and the command-position WRAPPER
    prefix (``timeout 30``/``env VAR=``/``nohup``/bare assignments) is
    stripped first (round-16 C: a wrapped verb is still the verb the
    container runs). ``chroot NEWROOT CMD`` delegates to the command after
    the new-root path (the debug-pod habit: ``chroot /host bash``); a
    script shell expands its ``-c`` script into segments — recursively,
    since a script may itself wrap another script. Any other head is ONE
    command segment with its head at index 0.

    ``chroot_delegation`` (R26/G-10) selects the PROJECTION, never the
    parsing: ``True`` (default) delegates ``chroot NEWROOT CMD`` down to
    the CMD segment — the "what actually runs" view every existing
    consumer (fault-binary marker, blade payload classification, death
    ledger) is built on; ``False`` KEEPS the chroot token as the
    segment head — the "how the host was entered" view the escape-
    primitive check needs, because a delegated tail segment
    (``cat f; chroot /host bash`` → ``[bash]``) would otherwise erase
    the very primitive being legislated.

    Top-level boundary operators (round-25 K2 root fix): a bare
    ``&&``/``||``/``;``/``|``/``&`` token splits the stream into
    independent segments — each side then runs through this same
    function (its own wrapper/chroot/script judgement), exactly as the
    ``sh -c`` script face already judges the same operators inside
    quotes. Shell itself draws no quote-based distinction here: an
    unquoted operator IS a command boundary, and the pre-fix parser
    silently disagreed with the very shell it modelled — the second
    command of a bare composite was invisible to every segment-based
    consumer (death ledger, provenance gate, receipt-trust gate).
    """
    if any(tok in _SEGMENT_BOUNDARY_TOKENS for tok in cmd_tokens):
        segments: list[list[str]] = []
        buf: list[str] = []
        for tok in cmd_tokens:
            if tok in _SEGMENT_BOUNDARY_TOKENS:
                if buf:
                    segments.extend(
                        _segments_from_tokens(
                            buf, chroot_delegation=chroot_delegation,
                        )
                    )
                    buf = []
                continue
            buf.append(tok)
        if buf:
            segments.extend(
                _segments_from_tokens(buf, chroot_delegation=chroot_delegation)
            )
        return segments
    cmd_tokens = _strip_wrapper_prefix(_drop_redirection_tokens(cmd_tokens))
    if not cmd_tokens:
        return []
    head = cmd_tokens[0]
    base = head.rsplit("/", 1)[-1]
    if (
        base == "chroot"
        and chroot_delegation
        and len(cmd_tokens) >= 3
    ):
        return _segments_from_tokens(
            cmd_tokens[2:], chroot_delegation=chroot_delegation,
        )
    if base in _SCRIPT_SHELLS:
        rest = cmd_tokens[1:]
        j = 0
        while j < len(rest):
            tok = rest[j]
            if tok.startswith("-") and tok != "-":
                # ``-c`` (and clusters like ``-lc``/``-ic``) carry the
                # script as the NEXT token.
                if "c" in tok.lstrip("-") and j + 1 < len(rest):
                    return _segments_from_script(
                        rest[j + 1], chroot_delegation=chroot_delegation,
                    )
                j += 1
                continue
            # ``sh SCRIPT_FILE``: the file name is not a script body, but
            # judging it as one is harmless — it parses to a segment whose
            # head is the file name, a command the shell would never run.
            return _segments_from_script(
                tok, chroot_delegation=chroot_delegation,
            )
        return []
    return [cmd_tokens]


def _exec_container_tokens(tokens: list[str]) -> list[str]:
    """The container-command token stream of an exec/debug payload.

    The TRUE ``--`` separator wins outright — everything after it is what
    runs inside the target. R45: "true" is pflag's own
    (``exec_separator_shape`` walks the value-FIRST model, so a ``--``
    swallowed as a flag's VALUE is not a separator — slicing at it let a
    segment view start inside a flag value while every client ran the
    payload after the surviving separator). Without a true separator,
    leading flag+value pairs are skipped and the first positional is the
    container command ONLY when it is itself a command head (bare script /
    direct blade / chroot forms); otherwise there is no container command
    to extract (kubectl wants the ``--`` form, and guessing past a pod
    slot would be fail-open). Degenerate shapes miss (fail-closed): no
    container command, no segments.
    """
    from chaos_agent.tools._readonly_facts import exec_separator_shape

    _positionals, after = exec_separator_shape(tokens)
    if after is not None:
        return after
    i = 0
    while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
        if "=" not in tokens[i] and tokens[i] in KUBECTL_EXEC_VALUE_FLAGS:
            i += 2
        else:
            i += 1
    if i >= len(tokens):
        return []
    if _is_command_head(tokens[i]):
        # No ``--``, first positional IS the command (bare script / direct
        # blade / chroot delivery — the synthetic and host-shell forms).
        return tokens[i:]
    # No ``--`` and the first positional is not a command head: it is the
    # pod slot, and kubectl requires ``--`` before the container command —
    # a shape this branch never sees done properly. Stripping the pod to
    # fish for a command would be fail-open (an ``echo blade destroy X``
    # decoy would promote its second word to command position); NOT
    # guessing is fail-closed. No container command, no segments.
    return []


def exec_command_segments(
    v_args: object, *, chroot_delegation: bool = True,
) -> list[list[str]]:
    """Every command segment a ``kubectl exec``/``debug`` payload runs.

    Parses the payload at the SYNTAX level — command position, not word
    presence: the exec/debug flag walk, the pod slot, the ``--``
    separator, ``chroot NEWROOT CMD`` delegation, and ``sh -c``-style
    script expansion (quote-aware splitting at ``;``/``&&``/``||``/``|``/
    newlines). Each returned segment is ONE command's token list with its
    head at index 0, so a caller judges WHAT runs; a word in an argument
    position (an ``echo`` decoy, a quoted literal) never produces a
    segment of its own.

    Accepts either the exec/debug ``v_args`` (the kubectl tool's arg
    string — the CALLER owns the subcommand check) or a FULL command line
    (``kubectl [global flags] exec|debug ...`` — the subcommand is judged
    here; a non-command-mode kubectl line yields no segments). A
    ``list[str]`` is the DELIVERED container-command argv (R25/G-9): no
    raw text exists (synthetic arg shapes), the caller has already
    peeled kubectl's own prefix (the ``--`` payload), and the SAME
    separator/wrapper/script expansion runs over the tokens — the
    token stream IS the definitive argv, with no quoting left to
    misread. ``chroot_delegation`` selects the chroot projection
    (R26/G-10 — see ``_segments_from_tokens``; the default keeps every
    existing consumer's "what actually runs" view). Unlexable input
    yields ``[]`` — fail-closed.
    """
    if isinstance(v_args, list):
        return _segments_from_tokens(
            [str(tok) for tok in v_args], chroot_delegation=chroot_delegation,
        )
    if not isinstance(v_args, str) or not v_args.strip():
        return []
    try:
        tokens = shlex.split(v_args)
    except ValueError:
        return []
    if tokens and tokens[0] == "kubectl":
        tokens = tokens[1:]
        i = 0
        while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
            if "=" not in tokens[i] and tokens[i] in KUBECTL_EXEC_VALUE_FLAGS:
                i += 2
            else:
                i += 1
        if i < len(tokens) and tokens[i] in KUBECTL_COMMAND_SUBCOMMANDS:
            tokens = tokens[i + 1 :]
        else:
            # Not a command-mode kubectl line — it runs no command inside
            # anything, so it has no command segments.
            return []
    return _segments_from_tokens(
        _exec_container_tokens(tokens), chroot_delegation=chroot_delegation,
    )


def _host_native_call_is_readonly(args: object) -> bool:
    """True if a host-native carrier tool_call ran a READ-ONLY diagnostic.

    ``host_inject`` is the superset of ``host_read`` (it admits read-only
    diagnostics with ``skip_guard``), so a successful ``host_inject`` ToolMessage
    is NOT necessarily an injection. Mirror the content-aware attribution the
    kubectl-native scan uses: a read-only command is not an injection.
    """
    if not isinstance(args, dict):
        return False

    from chaos_agent.tools.readonly import (
        host_command_rejection_reason,
        is_readonly_argv,
    )

    command = args.get("command")
    if isinstance(command, str) and command.strip():
        # Face 8 (design 4.6): judge the raw string on structural facts,
        # not as a shlex token soup — a pipe/substitution an argv split
        # would render as inert words is real syntax here, and a ``watch``
        # payload is re-parsed the way watch runs it.
        return host_command_rejection_reason(command) is None
    # exec_host_command shape: binary + args list — no raw text exists, so
    # it falls to the argv judge (which re-parses ``watch`` payloads the
    # same way inside).
    binary = args.get("binary")
    if isinstance(binary, str) and binary:
        extra = args.get("args") or []
        argv = [binary] + [str(a) for a in extra]
        return is_readonly_argv(argv)
    return False


def scan_host_native_injection(messages: list, tool_names: frozenset[str]) -> bool:
    """True if a host-native command tool in ``tool_names`` ran successfully.

    Reverse-scans for the most recent ToolMessage whose name is one of the
    carrier's injection tools and whose content is not an ``Error:``. The
    caller (HostShellProvider) supplies its own ``inject_tool_names`` so this
    stays carrier-agnostic. A read-only diagnostic run through ``host_inject``
    (its host_read superset role) is content-aware EXCLUDED — it is not an
    injection.
    """
    lookup = build_tool_call_args_lookup(messages)
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") not in tool_names:
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content.startswith("Error:"):
            continue
        if _host_native_call_is_readonly(lookup.get(getattr(msg, "tool_call_id", ""), {})):
            continue
        return True
    return False


def scan_kubectl_injection_after_blade(
    messages: list,
    subcommands: set[str] | frozenset[str],
    *,
    command_subcommands: set[str] | frozenset[str] = frozenset(),
    is_mutating_command=None,
    is_blade_create_delivery=None,
    is_teardown=None,
) -> bool:
    """True if a kubectl-native injection followed a ``blade_create`` attempt.

    Detects the kubectl-native alternative injection AFTER the last
    blade_create, so kubectl calls before blade_create (normal verification)
    don't count. Two attempt shapes are recognised:

    - **Object-write** — a ``subcommand`` in ``subcommands``
      (scale/patch/cordon/...) that SUCCEEDED. The verb itself IS the
      mutation and its result is trustworthy, so failed calls don't count.
    - **Command-mode** — a ``subcommand`` in ``command_subcommands``
      (``exec``/``debug``) whose inner command mutates (judgement delegated
      to ``is_mutating_command``, fed the raw ``v_args``). Keyed on the
      ATTEMPT (AIMessage tool_call), NOT the result: an exec-delivered fault
      can sever its own feedback channel, so its ToolMessage comes back as
      ``Error:`` — the forensic paradox — and an error result must not
      disprove the injection. Without a callback, command-mode calls never
      count (a bare ``exec`` is not assumed mutating). An exec carrying a
      ``blade ... create`` command is EXCLUDED — that is a ChaosBlade
      delivery channel (the ``kubectl_exec`` method), not a kubectl-native
      injection, and its attribution belongs to
      :func:`scan_kubectl_blade_success` (chaosblade/verify.py). The
      exclusion is delegated to the injected ``is_blade_create_delivery``
      classifier (round-15 root fix: the copy-pasted word gate here was one
      of the eleven same-disease sites — a composite decoy payload passed
      it while carrying no blade command). Without the callback no exec is
      excluded — callers whose attribution domain intersects the blade-exec
      delivery MUST inject it.

    CALLER CONTRACT (teardown≠mutation, P3): pass the ``is_teardown``
    matcher (``execution_artifacts.make_teardown_matcher``) and this scan
    skips registered-vehicle teardown calls at CALL granularity — a
    cleanup delete after the failed ``blade_create`` is NOT a native
    fallback injection. ``None`` (the default) is RAW evidence: the
    vehicle registry is invisible here, so a teardown delete counts as an
    object-write hit (test fixtures, and callers whose shapes can never be
    a teardown). The blade-domain caller ``was_blade_create_attempted``
    threads the matcher since O-1 closed.
    """
    lookup = build_tool_call_args_lookup(messages)

    last_blade_create_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "blade_create":
            last_blade_create_idx = i

    scan_command_mode = bool(command_subcommands) and is_mutating_command is not None

    for i, msg in enumerate(messages):
        if i <= last_blade_create_idx:
            continue
        if isinstance(msg, ToolMessage):
            if getattr(msg, "name", "") != "kubectl":
                continue
            tc_id = getattr(msg, "tool_call_id", "")
            if tc_id and tc_id in lookup:
                args = lookup[tc_id]
                if is_teardown is not None and is_teardown("kubectl", args):
                    continue
                subcommand = args.get("subcommand", "")
                if subcommand in subcommands:
                    content = msg.content or ""
                    if not content.startswith("Error:"):
                        return True
        elif scan_command_mode and isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                if isinstance(tc, dict):
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                else:
                    name = getattr(tc, "name", "")
                    args = getattr(tc, "args", {})
                if name != "kubectl" or not isinstance(args, dict):
                    continue
                if is_teardown is not None and is_teardown(name, args):
                    continue
                if args.get("subcommand", "") not in command_subcommands:
                    continue
                v_args = args.get("v_args", "")
                if not isinstance(v_args, str):
                    continue
                # ChaosBlade delivered through exec is the kubectl_exec
                # method, not a kubectl-native injection — leave it to
                # scan_kubectl_blade_success (a FAILED blade-via-exec must
                # still read as "blade attempted and failed"). Judged by the
                # injected blade-carrier classifier, not a word gate.
                if (
                    is_blade_create_delivery is not None
                    and is_blade_create_delivery(v_args)
                ):
                    continue
                if is_mutating_command(v_args):
                    return True
    return False


def scan_native_issue_disproven(
    messages: list,
    write_subcommands: set[str] | frozenset[str],
    *,
    command_subcommands: set[str] | frozenset[str] = frozenset(),
    is_mutating_command=None,
    is_blade_create_delivery=None,
    is_teardown=None,
) -> bool:
    """True when the MOST RECENT kubectl-native attempt's result explicitly
    DISPROVES the mutation — an explicit counter-evidence scan for revoking
    an issue-time (channel A) attribution whose command provably failed —
    and ONLY when no object-write in the epoch ever landed (a single landed
    write confirms the attribution irrevocably; see the confirmation guard
    below).

    The scan stops at the LATEST native attempt of ANY shape and judges it:

    - **Object-write** (``subcommand`` in ``write_subcommands``): the verb
      itself is the mutation and the API-server result is trustworthy — an
      ``Error:`` result proves the write never landed (returns True).
    - **Command-mode** (``subcommand`` in ``command_subcommands`` with a
      mutating inner command): never judgeable — its error results may be
      the fault severing its own feedback channel (the forensic paradox),
      and a later command-mode attempt must not be revoked on an EARLIER
      failed object-write. Returns False.
    - A ``blade ... create`` carried through exec is the ``kubectl_exec``
      method, not a kubectl-native attempt — skipped, scanning continues
      (delegated to the injected ``is_blade_create_delivery`` classifier;
      round-15 root fix — the word gate here was one of the eleven
      same-disease sites).

    The judged attempt's result must be PRESENT: a missing ToolMessage means
    the call is still pending (issue-time attribution in the same turn, or a
    severed channel), and absence of result is never counter-evidence.

    RESULT-BORN CONFIRMATION GUARD: if ANY object-write attempt in the epoch
    has a present, non-error result, the attribution is CONFIRMED — a mutation
    provably landed on the cluster. A LATER failed write (a multi-step skill's
    second step failing, or a self-undo retry failing) must not revoke it:
    revocation would orphan the still-live fault from the successful write.
    Counter-evidence only exists while NO write ever landed.

    CALLER CONTRACT (teardown≠mutation, P3): thread the ``is_teardown``
    matcher and the confirmation pre-pass + the judge loop BOTH skip
    registered-vehicle teardown calls at call granularity — a teardown
    delete's SUCCESS receipt no longer masquerades as "a write landed"
    (the O-2 defect) even inside a mixed batch. ``None`` (the default) is
    RAW evidence (see the contract note on ``KUBECTL_WRITE_SUBCOMMANDS``).
    """
    results = {
        getattr(msg, "tool_call_id", ""): msg
        for msg in messages
        if isinstance(msg, ToolMessage)
    }
    # Confirmation pre-pass: any landed object-write makes the attribution
    # irrevocable within this epoch (see the guard paragraph above).
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if isinstance(tc, dict):
                name = tc.get("name", "")
                args = tc.get("args", {})
                tc_id = tc.get("id", "")
            else:
                name = getattr(tc, "name", "")
                args = getattr(tc, "args", {})
                tc_id = getattr(tc, "id", "")
            if name != "kubectl" or not isinstance(args, dict):
                continue
            if is_teardown is not None and is_teardown(name, args):
                continue
            if args.get("subcommand", "") not in write_subcommands:
                continue
            result_msg = results.get(tc_id or "")
            if result_msg is None:
                continue
            content = result_msg.content if isinstance(
                result_msg.content, str
            ) else str(result_msg.content)
            if not content.startswith("Error:"):
                return False  # a write landed — attribution confirmed
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in reversed(getattr(msg, "tool_calls", None) or []):
            if isinstance(tc, dict):
                name = tc.get("name", "")
                args = tc.get("args", {})
                tc_id = tc.get("id", "")
            else:
                name = getattr(tc, "name", "")
                args = getattr(tc, "args", {})
                tc_id = getattr(tc, "id", "")
            if name != "kubectl" or not isinstance(args, dict):
                continue
            if is_teardown is not None and is_teardown(name, args):
                continue
            subcommand = args.get("subcommand", "")
            if subcommand in write_subcommands:
                # Latest attempt found — judge its result only.
                result_msg = results.get(tc_id or "")
                if result_msg is None:
                    return False  # pending / severed — never counter-evidence
                content = result_msg.content if isinstance(
                    result_msg.content, str
                ) else str(result_msg.content)
                if is_budget_expiry_unknown(content):
                    # R65 (third case of the B39/B40 family): a budget
                    # expiry is outcome-UNKNOWN, not a failure — for a
                    # millisecond object-write it most likely LANDED, so
                    # it is the forensic paradox again (same ruling as
                    # the command-mode branch below and the host
                    # carrier): never counter-evidence. Direction and
                    # wording rationale live on the predicate.
                    return False
                return content.startswith("Error:")
            if (
                subcommand in command_subcommands
                and is_mutating_command is not None
            ):
                v_args = args.get("v_args", "")
                if isinstance(v_args, str) and (
                    is_blade_create_delivery is not None
                    and is_blade_create_delivery(v_args)
                ):
                    continue  # kubectl_exec method — not a native attempt
                if isinstance(v_args, str) and is_mutating_command(v_args):
                    return False  # command-mode: paradox — never judgeable
    return False


def scan_kubectl_mutation_index(
    messages: list,
    write_subcommands: set[str] | frozenset[str],
    *,
    command_subcommands: set[str] | frozenset[str] = frozenset(),
    is_mutating_command=None,
    is_teardown=None,
) -> int:
    """Index of the most-recent AIMessage carrying a mutating kubectl attempt.

    Keyed on the ATTEMPT (AIMessage tool_calls), not the tool result — the
    same attribution rules as this module's other kubectl scans: an
    object-write verb, or a command-mode call whose inner command mutates.
    Returns the message index, or ``-1`` when no mutating kubectl call was
    attempted.

    CALLER CONTRACT (teardown≠mutation, P3): thread the ``is_teardown``
    matcher and a registered-vehicle teardown call's index is never
    returned (it is not mutation evidence — the R6-1/R8-1 ghost doors);
    ``None`` (the default) is RAW evidence (see the contract note on
    ``KUBECTL_WRITE_SUBCOMMANDS``).
    """
    last = -1
    for i, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if isinstance(tc, dict):
                name = tc.get("name", "")
                args = tc.get("args", {})
            else:
                name = getattr(tc, "name", "")
                args = getattr(tc, "args", {})
            if name != "kubectl" or not isinstance(args, dict):
                continue
            if is_teardown is not None and is_teardown(name, args):
                continue
            subcommand = args.get("subcommand", "")
            if subcommand in write_subcommands:
                last = i
                break
            if subcommand in command_subcommands and is_mutating_command is not None:
                v_args = args.get("v_args", "")
                if isinstance(v_args, str) and is_mutating_command(v_args):
                    last = i
                    break
    return last


def scan_host_native_index(
    messages: list,
    tool_names: frozenset[str],
    *,
    is_teardown=None,
) -> int:
    """Index of the most-recent successful host-native carrier ToolMessage.

    Recency-returning companion to :func:`scan_host_native_injection`
    (same content-aware rule — a read-only ``host_inject`` diagnostic is not an
    injection). Returns ``-1`` when no successful host-native command is
    attested.

    ``is_teardown`` threads the machinery≠mutation exemption (P3, R23/G-7)
    INTO this scan at CALL granularity: an arm-first systemd-run timer
    (the host skill standard "先武装定时恢复，再注入" registration) is
    recovery machinery, not native takeover evidence — without the thread,
    a state-less restored session read a bare arm as "injected". The
    matcher is built fresh per seam invocation (``make_teardown_matcher``).
    """
    lookup = build_tool_call_args_lookup(messages)
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") not in tool_names:
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content.startswith("Error:"):
            continue
        call = lookup.get(getattr(msg, "tool_call_id", ""), {})
        if _host_native_call_is_readonly(call):
            continue
        if (
            is_teardown is not None
            and is_teardown(getattr(msg, "name", ""), call)
        ):
            continue
        return i
    return -1


__all__ = [
    "PRE_EXEC_REJECTION_MARKERS",
    "build_tool_call_args_lookup",
    "is_budget_expiry_unknown",
    "reached_target",
    "scan_host_native_injection",
    "scan_host_native_index",
    "scan_kubectl_injection_after_blade",
    "scan_kubectl_mutation_index",
    "scan_native_issue_disproven",
]
