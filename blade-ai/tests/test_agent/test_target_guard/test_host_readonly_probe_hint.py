"""Layer 2 of ``single-source-readonly-probe-presentation`` — the refusal
feedback a model gets on a NON-read-only ``host_inject`` command must carry the
read-only capability fact from the SAME single source the docstring states, so
the model can self-correct in one step instead of enumerating alternatives.

Root cause (O-C2-1, live 2026-10-27 python Case #2 ``inject-956b3105``): the
``python3 -c`` urllib probe was correctly refused — ``_classify_host_inject``
found the banned verb ``python3`` with an empty fault family, and
``_cross_family_scope_change`` returned ``REJECT_DRIFT`` — but that branch
DISCARDED the classifier's ``reject_detail`` cause and rendered only
``_build_suggestion(approved)`` (an echo of the approved target). The model saw
neither the true cause nor a compliant probe shape, so it burned ~51.6s + 1
drift enumerating 6 alternatives before falling back to the read-only curl that
was admissible all along. This is the ``b7cd803d`` cause-fix pairing violation.

The fix is branch-local and carrier-scoped (it must NOT touch the shared
``_build_suggestion``, which nine refusal branches — including every k8s path —
consume; ``7eb13170``/``8207f9f1``):
  1. ``readonly.py`` gains ``HOST_READONLY_PROBE_HINT``, a single-source,
     binary-agnostic statement of the read-only shape ``host_inject`` admits,
     isomorphic to ``READONLY_PROBE_FIX_HINT``;
  2. ``_classify_host_inject`` attaches it as the ``reject_suggestion`` paired
     with its ``reject_detail`` cause on the non-read-only empty-family
     fall-through;
  3. ``_cross_family_scope_change`` appends the classifier's cause + paired fix
     to ITS OWN reason/suggestion (never ``_build_suggestion``).

These tests lock the constant (this file) and the wiring + k8s isolation
(``TestHintWiring`` below). The constant is pinned to the LIVE authority
(``is_readonly_host_command``), not a frozen literal, so if the judge ever
evolves the hint is forced to be re-checked rather than silently drifting —
the exact failure mode O-C2-1 names.
"""

from __future__ import annotations

import re

from chaos_agent.agent.providers.host_shell.provider import _classify_host_inject
from chaos_agent.agent.target_guard.drift_policy import _build_suggestion
from chaos_agent.agent.target_guard.types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardVerdict,
)
from chaos_agent.tools.guard_gateway import decision_to_feedback, get_guard_gateway
from chaos_agent.tools.readonly import (
    HOST_READONLY_PROBE_HINT,
    is_readonly_host_command,
)

# Read-verb (HTTP GET) forms of the dual-use probes the hint may name. Each is
# canonical, category-level — the application-level probe a python-scope inject
# issues against the target app's own endpoint.
_READ_VERB_PROBES = {
    "curl": "curl -s http://127.0.0.1:8080/call",
    "wget": "wget -qO- http://127.0.0.1:8080/call",
}


class TestHostReadonlyProbeHintConstant:
    """The single-source hint states the SHAPE rule and tracks the authority."""

    def test_hint_is_exported_and_non_empty(self):
        assert isinstance(HOST_READONLY_PROBE_HINT, str)
        assert HOST_READONLY_PROBE_HINT.strip(), (
            "HOST_READONLY_PROBE_HINT must be a non-empty single-source string "
            "exported from readonly.py (the authority's home), so consumers "
            "import it by reference instead of copying a private word list "
            "(a2f0cce7)."
        )

    def test_hint_states_shape_rule_not_binary_enumeration(self):
        flat = re.sub(r"\s+", " ", HOST_READONLY_PROBE_HINT)
        assert "by command SHAPE, not by binary" in flat, (
            "the hint must state the binary-agnostic SHAPE rule — the model-level "
            "invariant that cannot drift when the next dual-use binary appears "
            "(O-C2-1: a per-binary list mirrored a shape classifier and lied "
            "about curl)."
        )

    def test_any_named_probe_is_admitted_by_the_authority(self):
        # Single-source hot-tracking (779169a1 ④): every dual-use binary the hint
        # NAMES as an admitted read-verb probe must actually be admitted by the
        # live authority. If readonly.py ever evolves to reject a read-verb GET,
        # this flips red and forces the hint to be re-checked — the prose can
        # never silently claim an admission the judge does not honour.
        named = [b for b in _READ_VERB_PROBES if b in HOST_READONLY_PROBE_HINT]
        # Scan-output floor (39aaf74c): the hint is expected to name at least one
        # probe; if it names none, the hot-tracking below is vacuous — fail loud.
        assert named, (
            "HOST_READONLY_PROBE_HINT names no dual-use read-verb probe; the "
            "hot-tracking assertion would be vacuous. Re-anchor this test."
        )
        for binary in named:
            probe = _READ_VERB_PROBES[binary]
            assert is_readonly_host_command(probe), (
                f"the hint names {binary!r} read-verb GET as admitted, but the "
                f"authority rejects {probe!r} — hint and readonly.py have "
                f"drifted; re-check HOST_READONLY_PROBE_HINT."
            )

    def test_hint_framed_as_coverage_not_a_new_admission_rule(self):
        # 8207f9f1 item 8: the hint must be framed as "this refusal covers the
        # command you ISSUED, not read-only observation" and must NOT read as a
        # new admission rule that loosens the drift guard. Pin both halves: the
        # issued-command scoping and the genuine-drift-still-refused clause.
        flat = re.sub(r"\s+", " ", HOST_READONLY_PROBE_HINT).lower()
        assert "you issued" in flat, (
            "the hint must scope itself to the command the model ISSUED, so it "
            "cannot be misread as relaxing the guard."
        )
        assert "stays refused" in flat, (
            "the hint must state a genuine second fault on a drifted target "
            "STAYS refused — the admit/deny decision is untouched (fail-closed)."
        )

    def test_hint_never_lists_an_admitted_probe_as_rejected(self):
        # The exact O-C2-1 lie in hint form: a dual-use read-verb probe the
        # authority ADMITS presented as rejected. The hot-tracking test above
        # only proves "named ⊆ admitted"; this closes the other direction — a
        # named probe must sit on the ADMITTED side of the hint, never in a
        # rejected/mutating enumeration.
        flat = re.sub(r"\s+", " ", HOST_READONLY_PROBE_HINT)
        # OLD-VALUE negative: the docstring's false per-binary triple must not
        # migrate into the hint.
        assert "rm/curl/systemctl" not in flat, (
            "the false 'rm/curl/systemctl rejected' triple migrated into the "
            "hint — curl/wget are admitted by the authority on a read verb."
        )
        # Partition on the genuine-drift pivot; a named probe must appear BEFORE
        # it (the admission clause), not after (the refusal clause). Assert the
        # pivot exists so a reworded hint fails loud instead of passing vacuously.
        admitted_side, pivot, _ = flat.partition("stays refused")
        assert pivot, (
            "hint lost its 'stays refused' genuine-drift pivot; re-anchor this "
            "test to the new wording."
        )
        for binary in _READ_VERB_PROBES:
            if binary in flat:
                assert binary in admitted_side, (
                    f"{binary!r} appears on the REFUSED side of the hint — the "
                    f"O-C2-1 'curl rejected' lie reproduced in the refusal text."
                )


# The O-C2-1 probe: a python3 -c urllib call under a python approval — banned
# verb, empty fault family, so it falls through _cross_family_scope_change.
_PY_PROBE = (
    "python3 -c 'import urllib.request as u; "
    'u.urlopen("http://127.0.0.1:8080/call")\''
)


def _python_approval() -> ApprovedTarget:
    """The frozen contract for an in-process python http-delay inject."""
    return ApprovedTarget(scope="python", namespace="", fault_target="http")


class TestHintWiring:
    """The hint reaches the model on the O-C2-1 path — and ONLY there."""

    def test_banned_empty_family_probe_surfaces_cause_and_paired_fix(self):
        gw = get_guard_gateway()
        et = _classify_host_inject({"command": _PY_PROBE}, f"host_inject {_PY_PROBE}")
        decision, _ = gw.check_target(et, _python_approval())
        assert decision.verdict == GuardVerdict.REJECT_DRIFT
        # b7cd803d cause-fix pairing: the reason carries the classifier's real
        # cause (the banned verb), the suggestion carries the paired compliant
        # shape (the single-source hint) — neither is dropped as it was pre-fix.
        assert "banned verb" in decision.reason, (
            "the classifier's reject_detail cause was dropped again — the model "
            "sees no reason (the O-C2-1 detour root cause)."
        )
        assert HOST_READONLY_PROBE_HINT in decision.suggestion
        # It survives to the feedback the model actually reads.
        fb = decision_to_feedback(decision)
        assert HOST_READONLY_PROBE_HINT in fb.compliant_form
        # Decision-neutral: REJECT_DRIFT is still a hard floor (unchanged by
        # carrying a suggestion — guard_gateway forces it for REJECT_DRIFT).
        assert fb.is_hard_floor is True

    def test_suggestion_is_single_source_derived_not_a_copy(self, monkeypatch):
        # 779169a1 ④ hot-tracking: patch the readonly.py constant and confirm the
        # rendered suggestion follows — proving provider/guard import it by
        # reference from the single source, not a second hardcoded copy.
        import chaos_agent.tools.readonly as ro

        sentinel = "SENTINEL-SINGLE-SOURCE-MARKER"
        monkeypatch.setattr(ro, "HOST_READONLY_PROBE_HINT", sentinel)
        gw = get_guard_gateway()
        et = _classify_host_inject({"command": _PY_PROBE}, f"host_inject {_PY_PROBE}")
        decision, _ = gw.check_target(et, _python_approval())
        assert sentinel in decision.suggestion, (
            "the refusal suggestion did not follow the patched readonly.py "
            "constant — it is a private copy, not single-sourced (a2f0cce7)."
        )

    def test_readonly_curl_still_passes_without_hint(self):
        # Tier1 exemption intact: a read-only curl is READONLY, never rejected,
        # and needs no fix hint (the docstring lie is gone AND the judge admits
        # it — the two faces now agree).
        gw = get_guard_gateway()
        et = _classify_host_inject(
            {"command": "curl -s http://127.0.0.1:8080/call"}, "host_inject curl ...",
        )
        decision, _ = gw.check_target(et, _python_approval())
        assert decision.verdict == GuardVerdict.READONLY
        assert decision.verdict != GuardVerdict.REJECT_DRIFT

    def test_genuine_host_fault_drift_gets_no_readonly_hint(self):
        # Carrier-scoped: a genuine host fault (recognized family) under a python
        # approval is a REAL drift — it must NOT be softened with the read-only
        # observation hint (that would be noise on a true second-fault attempt,
        # and the hint is attached only on the empty-family fall-through).
        gw = get_guard_gateway()
        et = _classify_host_inject(
            {"command": "iptables -I INPUT -s 10.0.0.1 -j DROP"},
            "host_inject iptables ...",
        )
        decision, _ = gw.check_target(et, _python_approval())
        assert decision.verdict == GuardVerdict.REJECT_DRIFT
        assert HOST_READONLY_PROBE_HINT not in (decision.suggestion or ""), (
            "the read-only hint leaked onto a genuine cross-family fault drift — "
            "it must stay scoped to the non-fault, non-read-only fall-through."
        )

    def test_k8s_refusal_never_carries_the_host_hint(self):
        # D5-③ k8s isolation, NEGATIVE: the shared _build_suggestion path (every
        # k8s refusal, incl. step-4 cross-profile) must never surface the
        # host-carrier hint — even when the effective target carries one. Proves
        # the branch-local change did not leak into the shared constructor
        # (7eb13170/8207f9f1).
        k8s_approved = ApprovedTarget(scope="pod", namespace="demo", fault_target="cpu")
        # (a) the shared constructor echoes only the approved target.
        assert HOST_READONLY_PROBE_HINT not in _build_suggestion(k8s_approved)
        # (b) a hint-carrying HOST effective under a K8S approval is caught by
        #     step 4 (cross-profile), which uses the shared constructor and drops
        #     reject_suggestion → the hint cannot reach a k8s refusal.
        gw = get_guard_gateway()
        host_eff = EffectiveTarget(
            scope="host",
            namespace="",
            fault_target="",
            confidence=ConfidenceLevel.HIGH,
            reject_detail="banned verb 'python3'",
            reject_suggestion=HOST_READONLY_PROBE_HINT,
            raw_command="host_inject python3 -c ...",
        )
        decision, _ = gw.check_target(host_eff, k8s_approved)
        assert decision.verdict == GuardVerdict.REJECT_DRIFT
        assert HOST_READONLY_PROBE_HINT not in (decision.suggestion or ""), (
            "the host read-only hint leaked across the profile boundary into a "
            "k8s refusal — the k8s isolation invariant is broken."
        )
