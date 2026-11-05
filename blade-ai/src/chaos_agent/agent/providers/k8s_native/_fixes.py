"""Repair-hint wording constants for the k8s-native carrier's classifier.

Extracted from ``classifier.py`` (multiconcern-module-extraction, task 2.1) —
pure move, text byte-identical. These are the leaf of the classifier's
dependency DAG: every ``_classify_*`` reject site pairs its ``reject_detail``
with one of these ``reject_suggestion`` strings, so they carry no internal
dependencies and are imported by every sibling module.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Repair-hint constants, paired one-to-one with the reject causes below —
# see the pairing rationale in target_guard/classifier.py (task-866648cc).
# ---------------------------------------------------------------------------

_FIX_UNKNOWN_SUBCOMMAND = (
    "The subcommand NAME is what the guard cannot place, so no argument will "
    "help. Check the real spelling with `--help` in v_args (the live help text "
    "is authoritative — more so than any documentation), then re-issue. If the "
    "intent genuinely has no kubectl form, express it as a blade command."
)
_FIX_UNKNOWN_VOCABULARY = (
    "The name itself is what the guard cannot place — correct it to one of the "
    "accepted values named in the reason, rather than adding more arguments."
)
_FIX_NAME_THE_TARGET = (
    "Add the missing positional argument in the shape the reason quotes. The "
    "guard cannot compare a target it could not parse, so this is a form "
    "issue, not a blocked target — once named, the target will be compared "
    "against the approved one (which is a separate check, and only passes if "
    "it matches)."
)
_FIX_QUALIFY_KIND = (
    "Write the target as '<kind>/<name>' (e.g. 'deployment/myapp') so the kind "
    "is unambiguous, then re-issue."
)
_FIX_STATE_SUBCOMMAND = (
    "Put the kubectl subcommand first, before its flags — e.g. "
    "'get pods -n <ns>', not '-n <ns>' alone."
)
_FIX_ESCAPE_VIA_CARRIER = (
    "This path IS available once expressed correctly: run the host operation "
    "through an approved-node privileged debug pod — `kubectl exec <debug-pod> "
    "-- <host-entry> ...` — and make the mutation self-recover by pairing the "
    "forward command with its own inverse behind a time bound, e.g. "
    "`<mutation> && sleep <N> && <inverse>` or "
    "`<mutation> && systemd-run --on-active=<N>s <inverse>`. The inverse that "
    "counts is family-specific and the guard names it when it rejects; a timer "
    "on its own, with no forward mutation, does not qualify."
)
_FIX_EXEC_USE_SEPARATOR = (
    "kubectl requires `--` before an exec/debug inner command: write it as "
    "`POD [flags] -- COMMAND` (the command-mode form without the separator is "
    "refused by kubectl itself: \"exec [POD] [COMMAND] is not supported "
    "anymore\"). If the trailing tokens are flag VALUES, write the flag and "
    "its value together (`--image=busybox`, `-n default`) so the entry and its "
    "flags stay the whole command line."
)
_FIX_EXEC_ONE_ENTRY = (
    "The separator closes the ENTRY: write `POD [flags] -- COMMAND` with "
    "exactly one positional before the `--`. Tokens between the entry and "
    "the separator are refused because their effect is client-dependent — "
    "some kubectl versions run them as the command head, others drop them — "
    "so the guard cannot say what would execute. If the extra token was a "
    "flag VALUE, glue it to its flag (`--image=busybox`, `-n default`)."
)
_FIX_DEBUG_ONE_TARGET = (
    "One target per call: `debug POD [flags] -- COMMAND` (or `debug "
    "node/<node> ... -- COMMAND`). Additional positionals are not extra "
    "flags — kubectl debug resolves each one as a SEPARATE target, creating "
    "a privileged pod / ephemeral container per target, so the guard cannot "
    "compare them against the single approved target."
)
