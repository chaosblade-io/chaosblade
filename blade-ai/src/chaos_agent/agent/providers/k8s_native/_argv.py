"""Argv positional/flag parsing helpers for the k8s-native kubectl classifier.

Leaf of the classifier dependency DAG (multiconcern-module-extraction, task
2.2): the pure argv-reading primitives every ``_classify_kubectl_*`` verb
handler is built on -- locating the subcommand, walking positionals, deciding
flag arity, splitting ``kind/name``, coercing tool_args into a list. Pure move
out of ``classifier.py``, text byte-identical; re-exported there for
back-compat (consumers import these by name from the classifier module path:
``tools/guard.py`` reads ``_first_positional`` and ``k8s_native/provider.py``
reads ``_coerce_args_list``).

Reaches only UP to ``target_guard.classifier`` (``KIND_ALIASES`` /
``iter_flag_assignments``), never back into the k8s-native core, so it stays a
leaf with no import cycle.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from typing import Any

from chaos_agent.agent.target_guard.classifier import (
    KIND_ALIASES,
    iter_flag_assignments,
)


def _find_subcommand_index(args: list[str]) -> int | None:
    """Skip leading global flags to find the kubectl subcommand index.

    Global flags (kubectl --help shows ~40) start with -- or -, and
    most take a value. We skip both flag-only (``--v=4``) and
    flag+value (``--context my-ctx`` / ``--kubeconfig ~/.kube/x``)
    forms. The subcommand is the first non-flag arg.

    The 1-vs-2 token decision is delegated to ``_is_valueless_flag`` so this
    and ``_list_positionals`` cannot disagree about a flag's arity.
    """
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            return i
        i += 1 if _is_valueless_flag(a) else 2
    return None


# kubectl flags that DON'T take a value. Used by ``_first_positional``
# and ``_find_subcommand_index`` to decide whether to skip 1 token
# (boolean flag) or 2 (flag + value). Without this list, ``kubectl
# delete --all pod -n ns`` would parse as ``--all`` consuming ``pod``
# and lose the actual positional.
#
# Includes both global flags and the most common subcommand-level
# boolean flags. Not exhaustive — uncommon boolean flags fall through
# to the 2-token assumption (worst case: we mis-skip one positional
# and return UNKNOWN, which the screener default-denies in enforcing
# mode rather than letting a wrong call through).
_BOOLEAN_FLAGS: frozenset[str] = frozenset(
    {
        # Help / verbose
        "-h",
        "--help",
        "-v",
        "--version",
        "-W",
        "--warnings-as-errors",
        "-q",
        "--quiet",
        # All-namespaces / all
        "-A",
        "--all-namespaces",
        "--all",
        # Recursive
        "-R",
        "--recursive",
        # Force / safety
        "--force",
        "--ignore-not-found",
        "--prune",
        "--insecure-skip-tls-verify",
        # Watch
        "-w",
        "--watch",
        "--watch-only",
        # Output formatting
        "--show-labels",
        "--show-kind",
        "--no-headers",
        "--server-side",
        "--client",
        # Misc
        "--include-uninitialized",
        "--keep-annotations",
        "--validate",
        "--save-config",
        "--rm",  # kubectl run --rm (delete on exit)
        "-i",
        "--stdin",
        "-t",
        "--tty",  # kubectl exec / run
        "--allow-missing-template-keys",
        "--overwrite",  # kubectl label / annotate
        "--local",  # kubectl set ... --local
    }
)


def _is_valueless_flag(token: str) -> bool:
    """Whether *token* is a flag that consumes NO following token.

    Two shapes qualify:
      - ``--flag=value`` / ``-n=ns`` — the value is glued on,
      - an exact member of ``_BOOLEAN_FLAGS``.

    Anything else is assumed to take a value (skip 2). That assumption is
    deliberately conservative: mis-skipping loses a positional and lands in
    ``SCOPE_UNKNOWN``, which the screener default-denies, rather than letting a
    call through against an unverified target.

    Exists as a named function, rather than inline in each caller, because
    ``_find_subcommand_index`` and ``_iter_positionals`` both need the answer and
    used to carry separate copies of it — they can no longer disagree about a
    flag's arity.

    NOT handled on purpose: stacked short-flag CLUSTERS (``-it`` == ``-i -t``).
    A rule admitting all-boolean clusters was written and then reverted. It
    worked — 292561 exhaustive argv combinations showed no other behaviour
    change, and the only kubectl forms it altered (``-vi 5``, which kubectl
    itself rejects with ``invalid argument``) were already invalid. It was
    dropped because the two skill cases that motivated it (``kubectl debug -it
    ... -- tc -Version``) were the real defect: a TTY is meaningless for an
    agent, and those docs were corrected instead. With no evidence the model
    produces ``-it`` on its own — zero occurrences across the recorded session
    logs — the rule was carrying a parsing special case for a hypothetical.

    Consequence to keep in mind: ``kubectl exec -it <pod> ...`` is refused, since
    ``-it`` swallows the pod name. It is refused WITH a reason now
    (``_classify_kubectl_exec`` names the missing pod), so the model can see the
    shape it needs rather than only that something failed.
    """
    if "=" in token:
        return True
    return token in _BOOLEAN_FLAGS


def _rest_has_namespace(rest: list[str]) -> bool:
    """True if ``rest`` already carries a ``-n`` / ``--namespace`` flag.

    Used by ``_classify_kubectl`` to decide whether to inject the
    pre-subcommand global namespace. We don't want to clobber an
    explicit per-subcommand ns with a global one. Reads through
    ``iter_flag_assignments`` so combined shorthand bundles
    (``-nprod``) count as carrying a namespace too.

    Stops scanning at the ``--`` separator — anything after it is the
    INNER command of ``kubectl exec`` (or similar) and its ``-n`` would
    bind to the inner program's namespace flag, not kubectl's outer ns.
    Without this stop, a ``kubectl exec POD -- prog -n inner`` call
    would falsely report that the OUTER kubectl carries a namespace,
    suppressing global-ns propagation.
    """
    return any(
        name == "--namespace" for name, _value, _origin in iter_flag_assignments(rest)
    )


def _is_known_kind(token: str) -> bool:
    """True iff ``token`` is a known kubectl kind (any spelling)."""
    if not token:
        return False
    head = token.split(".", 1)[0].lower().strip()
    return head in KIND_ALIASES


def _option_pair_value(args: list[str], option: str) -> str:
    """Value of ``option value`` / ``option=value`` in argv ("" when absent)."""
    for index, token in enumerate(args):
        if token == option and index + 1 < len(args):
            return args[index + 1]
        if token.startswith(f"{option}="):
            return token.split("=", 1)[1]
    return ""


def _first_positional(args: list[str]) -> str:
    """First non-flag arg in a kubectl-subcommand-rest list.

    Skips ``--flag=value``, known boolean flags (``--all`` / ``-A`` /
    ``--force`` / ...), and assumes any other ``-x`` / ``--x`` takes
    a value (skip 2 tokens). Returns "" if no positional found.

    Conservative on unknown flags: assumes they take a value so we
    don't accidentally treat ``ns`` in ``-n ns`` as a positional.
    The trade-off is that an unknown boolean flag can cause us to
    miss a real positional — but in that case the classifier falls
    back to UNKNOWN, which the screener default-denies, instead of
    silently letting a wrong call through.
    """
    return next((value for _, value in _iter_positionals(args)), "")


def _list_positionals(args: list[str]) -> list[str]:
    """Every non-flag positional in a kubectl-subcommand-rest, in order."""
    return [value for _, value in _iter_positionals(args)]


def _first_positional_index(args: list[str]) -> int | None:
    """Index of the first non-flag positional, or ``None`` if there is none.

    Returns the POSITION rather than the value so a caller can REMOVE that
    token. ``kubectl set`` needs this: its first positional is the field being
    set, not the resource, and it has to be stripped before the generic resource
    classifier can read the target.
    """
    return next((index for index, _ in _iter_positionals(args)), None)


def _iter_positionals(args: list[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(index, value)`` for each non-flag positional.

    The SINGLE definition of the three rules that separate a target from the
    noise around it: where the flag/positional boundary is, how many tokens a
    flag consumes (delegated to ``_is_valueless_flag``), and that ``--`` ends the
    outer command — everything past it is an inner ``exec`` payload and must not
    be read as an outer positional.

    ``_first_positional`` / ``_list_positionals`` / ``_first_positional_index``
    are all views onto this one walk, so they cannot disagree about any of the
    three. An earlier version of ``_first_positional_index`` carried its own copy
    of the loop, which left the ``--`` stop duplicated with no mechanism keeping
    the copies in step.
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            return
        if not token.startswith("-"):
            yield i, token
            i += 1
        else:
            i += 1 if _is_valueless_flag(token) else 2


def _split_kind_name(token: str) -> tuple[str, str]:
    """Split ``kind/name`` into (kind, name). ``name`` alone → ("", name)."""
    if "/" in token:
        kind, _, name = token.partition("/")
        return kind, name
    return "", token


def _has_help_flag(args: list[str]) -> bool:
    """True if ``-h`` or ``--help`` appears anywhere in *args*.

    Used to short-circuit classification: any command invoked with a
    help flag only prints usage text and never mutates state.
    """
    return "-h" in args or "--help" in args


def _coerce_args_list(tool_args: Any) -> list[str]:
    """Best-effort coerce of kubectl tool_args into a list[str].

    Recognises the actual production shape of ``chaos_agent.tools.kubectl_cli``
    (``{subcommand: str, v_args: str, kubeconfig?: str, context?: str,
    cluster?: str, task_id?: str}``) — without this branch, every real
    kubectl tool_call would coerce to ``[]`` and classify as UNKNOWN.

    Also accepts legacy shapes for back-compat with synthetic test
    fixtures and future re-binding of kubectl as a list-arg tool:
        - list[str] directly
        - dict with ``command`` / ``args`` / ``argv`` / ``cmd`` key
        - str shell-quoted

    Ignores ``kubeconfig`` / ``context`` / ``cluster`` because they
    select the cluster, not the target resource — the guard's job is
    target identity, not cluster identity.
    """
    if isinstance(tool_args, list):
        return [str(x) for x in tool_args]
    if isinstance(tool_args, dict):
        # Production shape — subcommand + v_args
        if "subcommand" in tool_args:
            out: list[str] = [str(tool_args.get("subcommand") or "")]
            v_args = tool_args.get("v_args") or ""
            if v_args:
                try:
                    out.extend(shlex.split(str(v_args)))
                except ValueError:
                    out.extend(str(v_args).split())
            return [x for x in out if x]
        # Legacy / synthetic shapes
        for key in ("command", "args", "argv", "cmd"):
            v = tool_args.get(key)
            if isinstance(v, list):
                return [str(x) for x in v]
            if isinstance(v, str):
                try:
                    return shlex.split(v)
                except ValueError:
                    return v.split()
    if isinstance(tool_args, str):
        try:
            return shlex.split(tool_args)
        except ValueError:
            return tool_args.split()
    return []
