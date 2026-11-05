"""The ``host_inject`` docstring must present the read-only admission rule the
way the AUTHORITY actually decides it — by command SHAPE, not by binary.

Root cause (openspec ``single-source-readonly-probe-presentation``, Layer 1;
observed live 2026-10-27 python Case #2 ``inject-956b3105``): read-only
admission is decided deterministically by ``readonly.py`` (``_classify_argv`` /
``is_readonly_host_command``) on the *shape* of the command — a dual-use binary
on a read verb (an HTTP GET via ``curl``/``wget``) is ADMITTED, a write flag /
pipe / redirect is rejected. But the ``host_inject`` docstring mirrored that
shape classifier as a static *per-binary* blacklist: channel-1 enumerated
"non-fault MUTATING binaries rm/curl/systemctl rejected" and the read-only
examples ("top/mpstat/df/ps/ss/cat") omitted ``curl``/``wget``. The two models
differ structurally, so the prose list could never faithfully track the judge —
``curl`` was only the first collision (``wget``/``find``/``awk``/``ss`` and any
future classifier evolution re-drift). The model read "curl rejected", reached
for a ``python3 -c`` urllib probe, was correctly REJECT_DRIFT'd by the fault-
family guard, and burned ~51.6s + 1 drift enumerating 6 alternatives before
falling back to the very ``curl`` the docstring had wrongly banned.

This is the PRESENTATION-face half of the "read-only ``host_inject`` =
observation" principle; the DECISION-face half (the classifier emits
``SCOPE_READONLY``, single-sourced from ``is_readonly_host_command``) is locked
by ``test_agent/test_target_guard/test_host_inject_readonly_exemption.py``. The
decision face was fixed in Tier 1; the presentation face never caught up. These
tests nail the docstring to the SAME single source so the two can never diverge:

  1. the docstring states admission is by command SHAPE, not by binary;
  2. the old false per-binary enumeration ("rm/curl/systemctl rejected") does
     not resurrect, and no authority-admitted read-verb binary appears among the
     docstring's rejected mutating shapes;
  3. single-source hot-tracking — every dual-use probe the docstring NAMES as
     admitted is admitted by the live ``readonly.py`` authority, so if the judge
     ever evolves the prose is forced to be re-checked rather than silently
     drifting (the exact O-C2-1 failure mode).
"""

from __future__ import annotations

import re

from chaos_agent.tools.host_cmd import host_inject
from chaos_agent.tools.readonly import is_readonly_host_command

# The dual-use read-verb probes the docstring NAMES as admitted. Each maps a
# binary to a canonical read-verb (HTTP GET) form the authority must admit.
NAMED_ADMITTED_PROBES = {
    "curl": "curl -s http://127.0.0.1:8080/call",
    "wget": "wget -qO- http://127.0.0.1:8080/call",
}


def _flat_description() -> str:
    """The presentation surface the LLM actually sees, whitespace-normalised.

    ``host_inject.description`` is the runtime value serialized into the tool
    schema — assert against IT (the real presented text), never a frozen copy of
    the docstring, so the invariant tracks the live surface.
    """
    return re.sub(r"\s+", " ", host_inject.description)


class TestShapeRuleStated:
    """Invariant 1 — admission is presented as by SHAPE, not by binary."""

    def test_docstring_states_binary_agnostic_shape_rule(self):
        flat = _flat_description()
        assert "by command SHAPE, not binary" in flat, (
            "host_inject's read-only channel must state that admission is by "
            "command SHAPE, not by binary — the binary-agnostic rule that keeps "
            "the prose from re-drifting off the shape classifier (O-C2-1)."
        )


class TestFalseEnumerationNeverResurrects:
    """Invariant 2 — the old per-binary blacklist stays dead."""

    def test_old_rm_curl_systemctl_triple_is_gone(self):
        # OLD-VALUE negative assertion: pin the exact pre-fix false enumeration
        # so it cannot silently return.
        assert "rm/curl/systemctl" not in _flat_description(), (
            "the false 'rm/curl/systemctl rejected' enumeration resurrected — "
            "curl is admitted by the authority on a read verb (O-C2-1)."
        )

    def test_no_admitted_read_verb_binary_listed_as_rejected_shape(self):
        desc = _flat_description()
        # Extract channel-2's rejected-shape example clause. Assert the scan
        # MATCHED before asserting on its content, so a reworded clause fails
        # loudly instead of collapsing to a vacuous "0 violations" pass.
        m = re.search(r"mutating shape \(([^)]*)\) is rejected", desc)
        assert m, (
            "channel-2's rejected-shape clause changed form; re-anchor this "
            "test to the new wording (do NOT delete the assertion below)."
        )
        rejected_examples = m.group(1)
        # Scan-output floor: the clause is non-empty and still enumerates a
        # genuinely mutating binary, proving the extraction is live.
        assert "rm" in rejected_examples, (
            f"rejected-shape clause {rejected_examples!r} no longer names a "
            "mutating binary — the extraction floor is not met."
        )
        for binary in NAMED_ADMITTED_PROBES:
            assert binary not in rejected_examples, (
                f"{binary!r} is admitted by readonly.py on a read verb, but the "
                f"docstring lists it among rejected mutating shapes "
                f"({rejected_examples!r}) — the O-C2-1 false-enumeration bug."
            )


class TestSingleSourceAgreementWithAuthority:
    """Invariant 3 — the docstring's named probes agree with the live judge.

    This is the load-bearing anti-drift anchor: it ties every binary the
    docstring NAMES as an admitted read-only probe to the authority's actual
    verdict. If ``readonly.py`` ever evolves to reject a read-verb GET, the
    first assertion flips red and forces the prose to be re-checked — the
    docstring can never silently claim an admission the judge does not honour
    (nor, symmetrically, deny one it does).
    """

    def test_named_probes_are_admitted_by_the_authority(self):
        for binary, probe in NAMED_ADMITTED_PROBES.items():
            assert is_readonly_host_command(probe), (
                f"the docstring names {binary!r} read-verb GET as an admitted "
                f"read-only probe, but readonly.py rejects {probe!r} — the "
                f"docstring and the authority have drifted; re-check "
                f"host_inject's channel-2 shape rule."
            )

    def test_named_probes_appear_in_the_presentation(self):
        flat = _flat_description()
        for binary in NAMED_ADMITTED_PROBES:
            assert binary in flat, (
                f"host_inject's docstring must name {binary!r} as an admitted "
                f"read-verb probe — it is the only app-level observation channel "
                f"for an in-process (python) target."
            )
