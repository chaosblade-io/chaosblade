"""Capability-probe guidance must be true and must actually work.

Background: three places told the LLM that ``which`` / ``command -v`` "are not
available" on a host and prescribed ``ls /usr/bin/<name>`` instead. Both halves
were wrong in a way that costs drills:

  - The capability claim is false. Both host channels hand a command STRING to
    the remote side (``ssh -- host "<str>"`` / ``wiz task exec --command
    "<str>"``), so a shell interprets it and ``command -v`` — a POSIX builtin —
    always works. The read-only validator accepted these all along.
  - The prescribed replacement is path-dependent, and it is WRONG for exactly
    the binaries host network faults use: ``iptables`` / ``ip6tables`` / ``nft``
    / ``tc`` live in sbin, not ``/usr/bin``. A wrong path reads as "not
    installed", so the agent abandons a feasible method.

These tests pin the corrected guidance: every probe form we recommend must pass
the validator, and no source may resurrect the false capability claim.
"""

from pathlib import Path

import pytest

from chaos_agent.agent.nodes.baseline._baseline_profiles import (
    build_baseline_system_prompt,
    validate_command,
)
from chaos_agent.tools.readonly import host_command_rejection_reason

# Every probe form the guidance recommends, in the exact shape it is written.
_RECOMMENDED_PROBES = [
    "command -v stress-ng",
    "command -v iptables",
    "ls /usr/sbin/iptables /usr/bin/iptables /sbin/iptables",
    "ls /usr/bin/stress-ng /usr/sbin/stress-ng /usr/local/bin/stress-ng",
]

# Sources that carry capability-probe guidance to the LLM.
_GUIDANCE_SOURCES = (
    Path("src/chaos_agent/tools/host_cmd.py"),
    Path("src/chaos_agent/agent/nodes/baseline/_baseline_profiles.py"),
    Path(
        "skills/host-chaos-skills/references/catalogue/"
        "Host_CPU使用率过高/Host_CPU使用率过高_进程CPU满载.md"
    ),
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestRecommendedProbesAreExecutable:
    """Guidance the LLM cannot act on is worse than no guidance: it burns
    attempts on commands the validator rejects."""

    @pytest.mark.parametrize("command", _RECOMMENDED_PROBES)
    def test_probe_passes_the_host_validator(self, command):
        reason = host_command_rejection_reason(command)
        assert reason is None, f"recommended probe is rejected: {command} — {reason}"

    @pytest.mark.parametrize("command", _RECOMMENDED_PROBES)
    def test_probe_passes_baseline_validation(self, command):
        assert validate_command(command, "host") is True

    def test_multi_path_ls_covers_sbin(self):
        """The single-path form that caused the bug must still be REJECTED as
        guidance, not by the validator (``ls`` is fine) but by our own review:
        assert the guidance no longer teaches the /usr/bin-only pattern."""
        for source in _GUIDANCE_SOURCES:
            text = (_REPO_ROOT / source).read_text(encoding="utf-8")
            assert "ls /usr/bin/stress-ng\n" not in text, (
                f"{source} still teaches a single /usr/bin path; network fault "
                "binaries live in sbin and would read as 'not installed'"
            )


class TestNoFalseCapabilityClaim:
    """``command -v`` works on host channels; claiming otherwise is a lie the
    LLM will faithfully route around."""

    @pytest.mark.parametrize("source", _GUIDANCE_SOURCES, ids=lambda p: p.name)
    def test_source_does_not_claim_probe_is_unavailable(self, source):
        text = (_REPO_ROOT / source).read_text(encoding="utf-8")
        forbidden = (
            "are not available",
            "not available here",
            "不可用",
        )
        for phrase in forbidden:
            # Only flag the phrase when it sits next to a probe binary, so an
            # unrelated availability statement elsewhere in the file is fine.
            for line in text.splitlines():
                if phrase in line and ("command -v" in line or "which" in line):
                    pytest.fail(
                        f"{source} claims a capability probe is unavailable: "
                        f"{line.strip()}"
                    )

    def test_host_prompt_recommends_the_portable_probe(self):
        prompt = build_baseline_system_prompt("ssh")
        assert "command -v" in prompt
        # And it must warn against the /usr/bin assumption that broke sbin.
        assert "/usr/sbin" in prompt
