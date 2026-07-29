"""Every kubectl command written in a skill case must survive ToolGuard.

A skill case is the drill script the agent is told to follow. If the guard
refuses a command the case prescribes, the drill is unexecutable by
construction: the agent retries, gets refused again, and the step self-check
keeps reporting the action as not-yet-performed. No unit test of the guard in
isolation can catch that — the two live in different files and each looks
correct on its own.

This is not hypothetical. Two such breaks shipped and were only found by
running the real corpus through the guard:

  - ``kubectl label`` was missing from ``KUBECTL_ALLOWED_SUBCOMMANDS`` while
    two cases prescribe it (``Pod_Pending_节点Taint无对应Toleration`` spells out
    ``kubectl label node <node> chaos-target=<app-name>``);
  - a first version of the ``drain`` guard banned ``--delete-emptydir-data``,
    which is exactly what ``Node_维护_节点排空Drain`` uses — reasoning about the
    flag's danger in the abstract had missed that an emptyDir dies with its pod
    anyway, and that pods mounting one are common enough that drain refuses to
    evict without it.

Scope: only guard verdicts the AGENT would hit are asserted. Shell pipes and
redirects are excluded — a case may show ``kubectl get ... | grep`` as
human-readable shorthand, and the guard rejecting it (exec-form runs no shell)
is long-standing intended behaviour with its own actionable message.
"""

import pathlib
import re
import shlex

import pytest

from chaos_agent.tools.guard import ToolGuard

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"

# Guard rejections that are NOT a case/guard mismatch: a shell pipe/redirect
# means the case is showing human shorthand rather than a runnable call.
#
# ``<`` and ``>`` may only be judged AFTER placeholders are substituted — cases
# write targets as ``<node-name>`` / ``<ns>``, so screening the raw text drops
# almost every command and makes this file silently vacuous (which is what
# ``test_corpus_is_not_empty`` exists to catch).
_SHELL_METACHARS = ("|", ">", "<", "`", "$(", ";", "&&")
_PLACEHOLDER_RE = re.compile(r"<[^<>\s]+>")


def _kubectl_commands() -> list[tuple[str, str]]:
    """Extract ``(case_path, command)`` for every kubectl line in every case.

    Placeholders are substituted with a literal token so the command has the
    shape the agent would actually send.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(_SKILLS_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        # Join shell line continuations so a multi-line command is one command.
        text = re.sub(r"\\\s*\n\s*", " ", text)
        for line in text.splitlines():
            cmd = line.strip().lstrip("#").strip()
            cmd = re.sub(r"^\d+\.\s*", "", cmd).strip("`- ")
            if not cmd.startswith("kubectl "):
                continue
            cmd = _PLACEHOLDER_RE.sub("PLACEHOLDER", cmd)
            if any(m in cmd for m in _SHELL_METACHARS):
                continue
            try:
                argv = shlex.split(cmd)
            except ValueError:
                continue  # unbalanced quotes — a prose line, not a command
            # A real command's second token is a subcommand: ASCII, no spaces.
            if len(argv) < 2 or not re.fullmatch(r"[a-z][a-z-]*", argv[1]):
                continue
            out.append((str(path.relative_to(_SKILLS_DIR)), cmd))
    return out


_COMMANDS = _kubectl_commands()


def test_corpus_is_not_empty():
    """Guards the extractor itself: a silently empty corpus would make every
    assertion below vacuous."""
    assert len(_COMMANDS) > 100, (
        f"only {len(_COMMANDS)} kubectl commands extracted from "
        f"{_SKILLS_DIR} — the extractor or the skills tree changed shape"
    )


@pytest.mark.parametrize("case,cmd", _COMMANDS, ids=[c for c, _ in _COMMANDS])
def test_skill_case_command_passes_guard(case, cmd):
    allowed, reason = ToolGuard().check(shlex.split(cmd))
    assert allowed, (
        f"{case} prescribes a command the guard refuses:\n"
        f"  {cmd}\n  → {reason}\n"
        "Either admit the form in ToolGuard (the case documents a real drill "
        "need) or fix the case. Leaving them disagreed makes the drill step "
        "impossible to satisfy."
    )
