"""Task 5.3 — retry replacement double gate: original obs retention + feedback loop.

Anchors the Case #61 regression fix on the retry side. Before this change:

* ``_split_retry_decisions`` dropped rejected replacements on the floor —
  the corresponding failed observation was silently deleted from
  ``all_pairs`` at merge time (not in success_pairs, not in absence_pairs,
  no entry in zip(retry_resolved, retry_obs)).
* The next retry round saw a shrunk ``failed_obs`` list with no trace of
  WHY the prior substitution was refused, so the LLM was free to re-emit
  the same shape.

After:

* ``_split_retry_decisions`` returns ``rejected_replacements:
  [(obs, cmd_text, reason), ...]`` alongside the accepted list.
* baseline_capture keeps the ORIGINAL (resolved, obs) pair in
  ``all_pairs`` and stamps ``retry_rejection_reason`` onto the obs.
* The retry prompt's ``error_feedback`` block reads that field and shows
  it to the LLM — closing the feedback loop (ReAct-style, per the
  gate-design discipline: program-side enforcement + LLM-visible reason).
* ``tried_commands`` records the refused command text too. NOTE: this list
  was described as a "hard block" from task 5.2 onward, but it only reached
  the next round as prompt text — nothing enforced it, because neither gate
  accepted the list. ``TestAttemptGate`` below anchors the enforcement that
  makes the description true.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.agent.nodes.baseline._llm_derive import (
    _command_fingerprint,
    _llm_retry_failed_commands,
    _split_retry_decisions,
    _validate_and_filter_commands,
)


# ---------------------------------------------------------------------------
# _split_retry_decisions: rejected_replacements channel
# ---------------------------------------------------------------------------


class TestSplitRetryDecisionsRejectedChannel:
    """The retry splitter must expose refused replacements as a
    first-class return channel, paired with the observation each was
    meant to replace.
    """

    def test_returns_three_keys(self):
        """Return shape contract: expected / replace / rejected_replacements."""
        result = _split_retry_decisions([], [], "k8s")
        assert set(result.keys()) == {"expected", "replace", "rejected_replacements"}
        assert result["expected"] == []
        assert result["replace"] == []
        assert result["rejected_replacements"] == []

    def test_c61_halfway_replacement_rejected_and_paired_with_obs(self):
        """Case #61 concrete regression: a ``container_internal``-declared
        replacement using ``kubectl get pods -o jsonpath=...`` (api_object
        shape) MUST be refused, and the refusal MUST be paired with the
        original failed obs so baseline_capture can keep it in play.
        """
        failed_obs = [{
            "description": "container fs usage",
            "command": "kubectl exec -l app=drill-perms-target -n default -- df -h",
            "exit_code": 1,
            "stderr": "error: unknown shorthand flag: 'l' in -l",
        }]
        decisions = [{
            "verdict": "replace",
            "description": "pod name discovery",
            "command": "kubectl get pods -n default -l app=drill-perms-target "
                       "-o jsonpath={.items[0].metadata.name}",
            "mode": "simple",
            "class": "container_internal",
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert result["replace"] == []
        assert result["expected"] == []
        assert len(result["rejected_replacements"]) == 1
        obs, cmd_text, reason = result["rejected_replacements"][0]
        # Same obs object (identity, not equality) — baseline_capture uses
        # ``id(obs)`` to find the pair in ``all_pairs``.
        assert obs is failed_obs[0]
        assert cmd_text.startswith("kubectl get pods")
        # Reason names BOTH sides of the mismatch: declared class + expected form.
        assert "container_internal" in reason
        assert "kubectl exec" in reason

    def test_well_formed_replacement_passes_gate(self):
        """Control: a shape-consistent replacement is accepted normally,
        and the rejected channel stays empty.
        """
        failed_obs = [{
            "description": "container fs",
            "command": "kubectl exec stale-pod -n ns -- df -h",
            "exit_code": 1,
            "stderr": "Error from server (NotFound)",
        }]
        decisions = [{
            "verdict": "replace",
            "description": "container fs (corrected target)",
            "command": "kubectl exec {target_pod} -n ns -- df -h",
            "mode": "simple",
            "class": "container_internal",
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert len(result["replace"]) == 1
        assert result["replace"][0].class_value == "container_internal"
        assert result["rejected_replacements"] == []

    def test_mixed_batch_preserves_pairing(self):
        """Position-aware filtering: a batch of 3 with 1 accepted, 1
        rejected, 1 expected_absence keeps each obs paired with its own
        decision (no cross-contamination).
        """
        obs_a = {"description": "a-original", "command": "cmd-a",
                 "exit_code": 1, "stderr": ""}
        obs_b = {"description": "b", "command": "cmd-b", "exit_code": 1, "stderr": ""}
        obs_c = {"description": "c", "command": "cmd-c", "exit_code": 1, "stderr": ""}
        failed = [obs_a, obs_b, obs_c]
        decisions = [
            # Accepted: honest api_object declaration for a get. Its
            # description differs from the obs's on purpose — see the pin
            # assertion below.
            {"verdict": "replace", "description": "a-fixed",
             "command": "kubectl get pod p1 -n ns", "mode": "simple",
             "class": "api_object"},
            # Rejected: api_object command declared as container_internal.
            {"verdict": "replace", "description": "b-fixed",
             "command": "kubectl describe pod p2 -n ns", "mode": "simple",
             "class": "container_internal"},
            # Expected absence.
            {"verdict": "expected_absence",
             "reason": "planned-creation asset, absent pre-injection"},
        ]

        result = _split_retry_decisions(decisions, failed, "k8s")

        assert len(result["replace"]) == 1
        # W-67-4 dimension pin: the description names the dimension in the
        # receipt AND feeds ``evidence.coverage``'s record text, so a retry
        # may not rewrite it. Taking it from the obs (not the decision) is
        # also the stronger pairing proof — "a-original" can only have come
        # from position 0.
        assert result["replace"][0].description == "a-original"
        assert len(result["rejected_replacements"]) == 1
        rejected_obs, rejected_cmd, rejected_reason = result["rejected_replacements"][0]
        assert rejected_obs is obs_b  # identity pairing preserved
        assert "describe pod p2" in rejected_cmd
        assert rejected_reason
        assert len(result["expected"]) == 1
        assert result["expected"][0][0] is obs_c

    def test_absent_class_still_derives_and_passes(self):
        """Legacy output shape (no class field) → derivation stamps a
        value, replacement is accepted.
        """
        failed_obs = [{"description": "x", "command": "old", "exit_code": 1}]
        decisions = [{
            "verdict": "replace",
            "description": "pod listing",
            "command": "kubectl get pods -n ns",
            "mode": "simple",
            # no "class" key
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert len(result["replace"]) == 1
        assert result["replace"][0].class_value == "api_object"
        assert result["rejected_replacements"] == []

    def test_selector_form_replacement_rejected_by_shape_gate(self):
        """A replacement that repeats the #61 selector form
        (``kubectl exec -l app=... -- <cmd>``) is refused by the
        exec-target form gate inside ``validate_command_with_reason``
        — the refusal surfaces through the same rejected_replacements
        channel.
        """
        failed_obs = [{
            "description": "container fs",
            "command": "kubectl exec stale-pod -n ns -- df -h",
            "exit_code": 1, "stderr": "",
        }]
        decisions = [{
            "verdict": "replace",
            "description": "same wrong shape",
            "command": "kubectl exec -l app=my-app -n ns -- df -h",
            "mode": "simple",
            "class": "container_internal",
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert result["replace"] == []
        assert len(result["rejected_replacements"]) == 1
        _obs, _cmd, reason = result["rejected_replacements"][0]
        # Reason comes from the shape gate (task 1.1) — mentions selector.
        assert "selector" in reason

    def test_empty_command_replacement_routed_to_rejected_channel(self):
        """Garbage LLM output (replace verdict with empty command) must
        NOT silently evaporate the obs from all_pairs. Before the fix,
        ``_validate_and_filter_commands`` returned both lists empty, the
        else branch was a no-op comment, and the obs disappeared from
        the merge (not in success/absence/rejected/zip). Now it routes
        through rejected_replacements so baseline_capture keeps it in
        play for the next retry round.
        """
        obs = {"description": "container fs", "command": "old", "exit_code": 1}
        decisions = [{
            "verdict": "replace",
            "description": "fixed",
            "command": "",   # ← empty: LLM garbage
            "mode": "simple",
        }]

        result = _split_retry_decisions(decisions, [obs], "k8s")

        # Must NOT be accepted (empty command).
        assert result["replace"] == []
        # Must be routed to rejected so the obs stays in all_pairs.
        assert len(result["rejected_replacements"]) == 1
        rejected_obs, cmd_text, reason = result["rejected_replacements"][0]
        assert rejected_obs is obs  # identity preserved
        assert "empty" in reason.lower() or "malformed" in reason.lower()

    def test_mode_defaults_to_simple_when_absent(self):
        """Retry schema does not teach ``mode``; absent/empty mode must
        normalize to 'simple' (not propagate as empty string) for
        consistency with the initial derive path.
        """
        obs = {"description": "x", "command": "old", "exit_code": 1}
        decisions = [{
            "verdict": "replace",
            "description": "pod listing",
            "command": "kubectl get pods -n ns",
            # no "mode" key at all
            "class": "api_object",
        }]

        result = _split_retry_decisions(decisions, [obs], "k8s")

        assert len(result["replace"]) == 1
        assert result["replace"][0].mode == "simple"


# ---------------------------------------------------------------------------
# Feedback-loop closure: retry prompt shows the previous rejection
# ---------------------------------------------------------------------------


class TestRetryPromptSurfacesRejectionReason:
    """The next retry round's error_feedback block MUST show the LLM
    why its previous replacement was refused — otherwise the loop has
    no way to converge (Case #61's launder-through-retry pattern).
    """

    @pytest.mark.asyncio
    async def test_rejection_reason_appears_in_human_prompt(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"verdict": "expected_absence", "reason": "give up"},
        ])))

        failed_obs = [{
            "description": "container fs",
            "command": "kubectl exec stale-pod -n ns -- df -h",
            "exit_code": 1,
            "stderr": "Error from server (NotFound)",
            # Stamped by baseline_capture in the previous retry round.
            "retry_rejection_reason": (
                "replacement 'kubectl get pods -n ns -o jsonpath=...' "
                "refused by the shape/class gate: class 'container_internal' "
                "requires ``kubectl exec`` form (got subcommand 'get')"
            ),
        }]

        await _llm_retry_failed_commands(
            mock_llm, "skill content", "pod", "cpu", "fullload", failed_obs,
        )

        human_content = mock_llm.ainvoke.call_args[0][0][1].content
        # The rejection reason block is present, and it names the gate.
        assert "Previous retry replacement REFUSED" in human_content
        assert "shape/class gate" in human_content
        # The specific reason text is passed through so the LLM sees the
        # exact mismatch (declared class vs. required form).
        assert "container_internal" in human_content
        assert "kubectl exec" in human_content

    @pytest.mark.asyncio
    async def test_no_rejection_field_no_extra_block(self):
        """Control: an obs without retry_rejection_reason produces the
        pre-existing error_feedback shape — no noise on the common path.
        """
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"verdict": "expected_absence", "reason": "planned absence"},
        ])))

        failed_obs = [{
            "description": "existence check",
            "command": "ls /etc/hosts.bak",
            "exit_code": 2,
            "stderr": "No such file or directory",
        }]

        await _llm_retry_failed_commands(
            mock_llm, "skill content", "node", "disk", "fill", failed_obs,
        )

        human_content = mock_llm.ainvoke.call_args[0][0][1].content
        assert "Previous retry replacement REFUSED" not in human_content
        # Standard error feedback still present.
        assert "No such file or directory" in human_content

    @pytest.mark.asyncio
    async def test_multi_round_rejections_accumulate(self):
        """Two consecutive rounds of refusal both appear — the obs's
        retry_rejection_reason is a joined record, so the LLM sees the
        full history (and cannot pretend the earlier refusal didn't
        happen).
        """
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"verdict": "expected_absence", "reason": "give up"},
        ])))

        failed_obs = [{
            "description": "container fs",
            "command": "kubectl exec stale-pod -n ns -- df -h",
            "exit_code": 1, "stderr": "",
            "retry_rejection_reason": (
                "replacement 'kubectl get pods -o jsonpath=...' refused by "
                "the shape/class gate: class mismatch\n"
                "replacement 'kubectl describe node n1' refused by the "
                "shape/class gate: class mismatch"
            ),
        }]

        await _llm_retry_failed_commands(
            mock_llm, "skill", "pod", "cpu", "fullload", failed_obs,
        )

        human_content = mock_llm.ainvoke.call_args[0][0][1].content
        # Both historical refusals are visible.
        assert "kubectl get pods -o jsonpath" in human_content
        assert "kubectl describe node n1" in human_content


# ---------------------------------------------------------------------------
# Baseline_capture merge semantics (task 5.2 zero-drift anchor)
# ---------------------------------------------------------------------------


class TestMergeSemanticsAnchor:
    """Anchor the four-block merge shape introduced in baseline_capture.py:

        all_pairs = success_pairs + absence_pairs + rejected_pairs
                    + list(zip(retry_resolved, retry_obs))

    The three pre-existing blocks keep their behaviour byte-for-byte;
    ``rejected_pairs`` is the new fourth block, and it exists ONLY when
    ``rejected_replacements`` is non-empty. This class exercises the
    block-construction logic in isolation (no LLM, no transport) so a
    future refactor cannot silently drop it.
    """

    def _build_pairs(self, all_pairs, rejected_replacements):
        """Mirror of the baseline_capture.py block-construction logic
        (kept in sync by test_baseline_retry_double_gate maintenance).
        """
        def _is_success(o):
            return o.get("exit_code") == 0 and not o.get("expected_absence")

        success_pairs = [
            (r, o) for r, o in all_pairs
            if _is_success(o) and not o.get("expected_absence")
        ]
        absence_pairs = [
            (r, o) for r, o in all_pairs if o.get("expected_absence")
        ]
        rejected_pairs = []
        if rejected_replacements:
            rejected_obs_ids = {id(_o) for _o, _, _ in rejected_replacements}
            rejected_pairs = [
                (r, o) for r, o in all_pairs
                if id(o) in rejected_obs_ids and not o.get("expected_absence")
            ]
            for _obs, _cmd_text, _reason in rejected_replacements:
                _existing = _obs.get("retry_rejection_reason", "")
                _stamp = (
                    f"replacement '{_cmd_text[:120]}' refused by the "
                    f"shape/class gate: {_reason}"
                )
                _obs["retry_rejection_reason"] = (
                    f"{_existing}\n{_stamp}" if _existing else _stamp
                )
        return success_pairs, absence_pairs, rejected_pairs

    def test_rejected_pairs_block_populated_when_replacements_refused(self):
        resolved_a = {"command": "cmd-a"}
        obs_a = {"description": "a", "command": "cmd-a", "exit_code": 1, "stderr": ""}
        resolved_b = {"command": "cmd-b"}
        obs_b = {"description": "b", "command": "cmd-b", "exit_code": 0, "stderr": ""}
        all_pairs = [(resolved_a, obs_a), (resolved_b, obs_b)]

        rejected_replacements = [(obs_a, "get-pods-substitution", "class mismatch")]

        success, absence, rejected = self._build_pairs(all_pairs, rejected_replacements)

        # obs_b (exit 0) → success block. obs_a (exit 1, refused
        # replacement) → rejected block, NOT dropped.
        assert len(success) == 1
        assert success[0][1] is obs_b
        assert absence == []
        assert len(rejected) == 1
        assert rejected[0][0] is resolved_a
        assert rejected[0][1] is obs_a
        # Reason stamped.
        assert "retry_rejection_reason" in obs_a
        assert "get-pods-substitution" in obs_a["retry_rejection_reason"]
        assert "class mismatch" in obs_a["retry_rejection_reason"]

    def test_rejected_pairs_empty_when_no_replacements_refused(self):
        """Zero-drift anchor: without refused replacements, the block is
        empty and the merge collapses to the pre-existing three-block
        shape (success + absence + zip(retry_resolved, retry_obs)).
        """
        resolved_a = {"command": "cmd-a"}
        obs_a = {"description": "a", "command": "cmd-a", "exit_code": 1, "stderr": ""}
        all_pairs = [(resolved_a, obs_a)]

        success, absence, rejected = self._build_pairs(all_pairs, [])

        assert success == []
        assert absence == []
        assert rejected == []
        # Obs untouched (no stamp).
        assert "retry_rejection_reason" not in obs_a

    def test_expected_absence_takes_precedence_over_rejected(self):
        """If an obs was marked expected_absence, it belongs to the
        absence block — NOT the rejected block, even if the LLM also
        proposed a (refused) replacement for it. This preserves the
        pre-existing absence-block semantics byte-for-byte.
        """
        resolved_a = {"command": "cmd-a"}
        obs_a = {
            "description": "a", "command": "cmd-a",
            "exit_code": 2, "expected_absence": "planned-creation",
        }
        all_pairs = [(resolved_a, obs_a)]

        rejected_replacements = [(obs_a, "some-substitution", "reason")]

        success, absence, rejected = self._build_pairs(all_pairs, rejected_replacements)

        assert success == []
        assert len(absence) == 1
        assert absence[0][1] is obs_a
        # Rejected block excludes it (guarded by ``not expected_absence``).
        assert rejected == []

    def test_reason_accumulates_across_rounds(self):
        """Two consecutive rounds of refusal against the same obs append
        to retry_rejection_reason (newline-separated), so the LLM sees
        the full history in the next error_feedback.
        """
        resolved_a = {"command": "cmd-a"}
        obs_a = {"description": "a", "command": "cmd-a", "exit_code": 1, "stderr": ""}
        all_pairs = [(resolved_a, obs_a)]

        # Round 1.
        self._build_pairs(all_pairs, [(obs_a, "sub-1", "reason-1")])
        first = obs_a["retry_rejection_reason"]
        assert "sub-1" in first and "reason-1" in first

        # Round 2 (same obs, different refused substitution).
        self._build_pairs(all_pairs, [(obs_a, "sub-2", "reason-2")])
        combined = obs_a["retry_rejection_reason"]
        # Round 1 content preserved.
        assert "sub-1" in combined and "reason-1" in combined
        # Round 2 content appended.
        assert "sub-2" in combined and "reason-2" in combined
        # Newline-separated, not overwritten.
        assert combined != first
        assert "\n" in combined


# ---------------------------------------------------------------------------
# Attempt gate: already_tried as an enforced dimension, not prompt text
# ---------------------------------------------------------------------------


_DS_QUERY = "kubectl get daemonset kube-proxy -n kube-system -o wide"


class TestAttemptGate:
    """Fourth gate: an already-attempted command is refused, not re-run.

    ``already_tried`` used to reach the retry LLM only as prompt text ("do not
    emit these again, nor a variant that would fail the same way"), which made it
    an appeal to model self-discipline. inject-6ebf341c is the field sample:
    retry 3 re-emitted retry 1's DaemonSet query verbatim, so the dimension was
    probed twice, failed twice, and the receipt settled at ``5/7`` with
    ``baseline_confidence=partial``.

    The gate reuses the existing ``rejected_replacements`` channel rather than
    inventing a new one, so the original observation stays in the failed set and
    the refusal reason reaches the next round's error_feedback (task 5.1's loop).
    """

    @staticmethod
    def _cmd(command: str, description: str = "d") -> dict:
        return {"description": description, "command": command, "mode": "simple"}

    def test_verbatim_repeat_is_refused(self):
        accepted, rejected = _validate_and_filter_commands(
            [self._cmd(_DS_QUERY)], "k8s", already_tried=(_DS_QUERY,),
        )
        assert accepted == []
        assert len(rejected) == 1
        assert rejected[0][0] == _DS_QUERY
        assert "already attempted" in rejected[0][1]
        # The reason must point at a way out, not just say no.
        assert "expected_absence" in rejected[0][1]

    def test_flag_reordering_is_still_the_same_command(self):
        """Reordering independent flags cannot launder a repeat."""
        reordered = "kubectl get daemonset kube-proxy -o wide -n kube-system"
        accepted, rejected = _validate_and_filter_commands(
            [self._cmd(reordered)], "k8s", already_tried=(_DS_QUERY,),
        )
        assert accepted == []
        assert len(rejected) == 1

    def test_extra_whitespace_is_still_the_same_command(self):
        accepted, rejected = _validate_and_filter_commands(
            [self._cmd("kubectl  get   daemonset kube-proxy   -n kube-system -o wide")],
            "k8s", already_tried=(_DS_QUERY,),
        )
        assert accepted == []
        assert len(rejected) == 1

    def test_a_different_argument_is_not_a_repeat(self):
        """Under-folding is the safe direction: a false positive permanently loses
        a baseline dimension, a false negative costs one redundant probe."""
        other_ns = "kubectl get daemonset kube-proxy -n other-ns -o wide"
        accepted, rejected = _validate_and_filter_commands(
            [self._cmd(other_ns)], "k8s", already_tried=(_DS_QUERY,),
        )
        assert len(accepted) == 1
        assert rejected == []

    def test_a_different_resource_is_not_a_repeat(self):
        other_kind = "kubectl get statefulset kube-proxy -n kube-system -o wide"
        accepted, _rejected = _validate_and_filter_commands(
            [self._cmd(other_kind)], "k8s", already_tried=(_DS_QUERY,),
        )
        assert len(accepted) == 1

    def test_empty_tried_list_leaves_the_gate_off(self):
        """The primary derive path has no retry history and must be untouched."""
        accepted, rejected = _validate_and_filter_commands(
            [self._cmd(_DS_QUERY)], "k8s",
        )
        assert len(accepted) == 1
        assert rejected == []
        accepted2, rejected2 = _validate_and_filter_commands(
            [self._cmd(_DS_QUERY)], "k8s", already_tried=(),
        )
        assert len(accepted2) == 1
        assert rejected2 == []

    def test_whitelist_rejection_takes_precedence(self):
        """An illegal command is refused for THAT reason — the more actionable
        one — even when it is also a repeat."""
        illegal = "rm -rf /"
        accepted, rejected = _validate_and_filter_commands(
            [self._cmd(illegal)], "k8s", already_tried=(illegal,),
        )
        assert accepted == []
        assert len(rejected) == 1
        assert "already attempted" not in rejected[0][1]

    def test_repeat_routes_through_rejected_replacements(self):
        """End to end through the splitter: the obs is PAIRED with the refusal,
        so baseline_capture keeps the original observation in the failed set
        instead of losing the dimension."""
        obs = {
            "description": "daemonset state",
            "command": _DS_QUERY,
            "exit_code": 1,
            "stdout": "",
            "stderr": "error",
        }
        decisions = [{"verdict": "replace", "command": _DS_QUERY, "mode": "simple"}]
        result = _split_retry_decisions(
            decisions, [obs], "k8s", "t-attempt", already_tried=(_DS_QUERY,),
        )
        assert result["replace"] == []
        assert len(result["rejected_replacements"]) == 1
        refused_obs, refused_cmd, refused_reason = result["rejected_replacements"][0]
        assert refused_obs is obs
        assert refused_cmd == _DS_QUERY
        assert "already attempted" in refused_reason


class TestCommandFingerprint:
    """The normalization must fold form, never rewrite a value."""

    def test_flag_order_is_folded(self):
        assert _command_fingerprint("kubectl get pods -n a -o wide") == \
            _command_fingerprint("kubectl get pods -o wide -n a")

    def test_equals_and_space_forms_match(self):
        assert _command_fingerprint("kubectl get pods --namespace=a") == \
            _command_fingerprint("kubectl get pods --namespace a")

    def test_whitespace_is_folded(self):
        assert _command_fingerprint("kubectl   get  pods") == \
            _command_fingerprint("kubectl get pods")

    def test_positional_order_is_preserved(self):
        assert _command_fingerprint("kubectl get pods deployments") != \
            _command_fingerprint("kubectl get deployments pods")

    def test_values_are_not_rewritten(self):
        assert _command_fingerprint("kubectl get pods -n a") != \
            _command_fingerprint("kubectl get pods -n b")

    def test_boolean_flag_is_stable_across_both_sides(self):
        """``-n foo`` mis-groups a boolean flag with a positional token, but the
        grouping is applied identically to both sides of every comparison, so the
        fingerprint stays consistent — which is all the gate needs."""
        a = _command_fingerprint("kubectl top node -n kube-system")
        b = _command_fingerprint("kubectl top node   -n   kube-system")
        assert a == b
