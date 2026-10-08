"""Task 6.1 / 6.2 — end-to-end fixture tests driven by real case logs.

These tests replay the actual derive / retry outputs captured in
``.b4tmp/c61_run1.log`` and ``.b4tmp/c60_run1.log`` through the
post-change validation pipeline, and assert the observable contract
changes the spec promises:

* Case #61 (task 6.1) — the LLM emitted 4 selector-form exec probes
  plus 4 legal API reads. Before the change, all 8 entered the execute
  batch; the 4 exec probes failed at runtime with ``unknown shorthand
  flag: 'l'`` and were then laundered through retry into
  ``expected_absence``, so the receipt said ``8/8`` while four
  dimensions went unmeasured. After: the shape gate rejects 4 at
  validation time, receipt honestly reports ``4/8``-shaped success, and
  the rejected commands are surfaced as facts.

* Case #60 (task 6.2) — the retry-round substitution pattern (a
  ``container_internal``-class failure replaced by an ``api_object``
  probe) is refused by the class gate, the original failed observation
  is kept in play, and the reason is stamped onto it for the next
  round's error_feedback.

Both fixtures use the exact command text from the run logs — no
paraphrase, no synthetic substitution — so a future relaxation of the
gates shows up here first.
"""

from __future__ import annotations

from chaos_agent.agent.nodes.baseline._llm_derive import (
    _split_retry_decisions,
    _validate_and_filter_commands,
)


# ---------------------------------------------------------------------------
# Case #61 fixture — the exact 8-command derive output from c61_run1.log
# ---------------------------------------------------------------------------
#
# Extraction: ``grep -oE '"command":\\s*"kubectl [^"]*"' .b4tmp/c61_run1.log
# | sort -u``. All 8 commands are verbatim; nothing is paraphrased.

C61_DERIVE_OUTPUT: list[dict] = [
    # 4 × illegal selector-form exec (Case #61 root cause).
    {
        "description": "container uid/gid",
        "command": "kubectl exec -l app=drill-perms-target -n default -- id",
        "mode": "simple",
    },
    {
        "description": "container /tmp listing",
        "command": "kubectl exec -l app=drill-perms-target -n default -- ls -laR /tmp",
        "mode": "simple",
    },
    {
        "description": "container mount table",
        "command": "kubectl exec -l app=drill-perms-target -n default -- mount",
        "mode": "simple",
    },
    {
        "description": "container process list",
        "command": "kubectl exec -l app=drill-perms-target -n default -- ps aux",
        "mode": "simple",
    },
    # 4 × legal API reads (these are the honest baseline the case ended up with).
    {
        "description": "target deployment status",
        "command": "kubectl get deployment drill-perms-target -n default",
        "mode": "simple",
    },
    {
        "description": "target pod listing",
        "command": "kubectl get pod -l app=drill-perms-target -n default",
        "mode": "simple",
    },
    {
        "description": "target pod name",
        "command": "kubectl get pods -l app=drill-perms-target -n default "
                   "-o jsonpath={.items[0].metadata.name}",
        "mode": "simple",
    },
    {
        "description": "target pod CPU/mem",
        "command": "kubectl top pod -l app=drill-perms-target -n default",
        "mode": "simple",
    },
]


class TestCase61EndToEnd:
    """End-to-end replay of the #61 derive output through the post-change
    validation pipeline. The observable contract:

    1. All 4 selector-form exec probes are rejected at validation time
       (they never enter the execute batch, so they cannot exit non-zero
       at runtime and get laundered through retry).
    2. All 4 legal API reads are accepted, with class derived as
       ``api_object`` (label-only derivation — the LLM in the original
       run did not emit a class field).
    3. The receipt count reflects honest success (4 accepted, not 8).
    4. Every rejection carries a reason that names the offending form
       (selector flag) and points at the fix (``{target_pod}``).
    """

    def test_c61_derive_output_splits_4_rejected_4_accepted(self):
        accepted, rejected = _validate_and_filter_commands(
            C61_DERIVE_OUTPUT, "k8s",
        )
        assert len(accepted) == 4, (
            f"expected exactly 4 legal API reads to pass, got {len(accepted)}"
        )
        assert len(rejected) == 4, (
            f"expected exactly 4 selector-form exec probes to be rejected, "
            f"got {len(rejected)}"
        )

    def test_c61_all_rejected_commands_are_the_selector_form_exec(self):
        """Membership-level assertion (not set-level slogan): every one
        of the four original selector-form commands appears in the
        rejected list, and every rejected entry IS one of them.
        """
        _accepted, rejected = _validate_and_filter_commands(
            C61_DERIVE_OUTPUT, "k8s",
        )
        rejected_cmds = {cmd for cmd, _reason in rejected}
        expected_rejected = {
            "kubectl exec -l app=drill-perms-target -n default -- id",
            "kubectl exec -l app=drill-perms-target -n default -- ls -laR /tmp",
            "kubectl exec -l app=drill-perms-target -n default -- mount",
            "kubectl exec -l app=drill-perms-target -n default -- ps aux",
        }
        assert rejected_cmds == expected_rejected

    def test_c61_all_accepted_commands_are_the_legal_api_reads(self):
        _accepted, _rejected = _validate_and_filter_commands(
            C61_DERIVE_OUTPUT, "k8s",
        )
        accepted_cmds = {c.command for c in _accepted}
        expected_accepted = {
            "kubectl get deployment drill-perms-target -n default",
            "kubectl get pod -l app=drill-perms-target -n default",
            "kubectl get pods -l app=drill-perms-target -n default "
            "-o jsonpath={.items[0].metadata.name}",
            "kubectl top pod -l app=drill-perms-target -n default",
        }
        assert accepted_cmds == expected_accepted

    def test_c61_rejection_reasons_name_selector_and_target_pod_fix(self):
        """Reason-fix pairing (design.md decision 2): every rejection
        names the offending shape AND points at ``{target_pod}`` so the
        LLM has an actionable correction path.
        """
        _accepted, rejected = _validate_and_filter_commands(
            C61_DERIVE_OUTPUT, "k8s",
        )
        for cmd, reason in rejected:
            assert reason, f"empty reason for {cmd!r}"
            # Names the offending form.
            assert "selector" in reason, (
                f"reason for {cmd!r} does not name the selector form: {reason}"
            )
            # Points at the fix.
            assert "{target_pod}" in reason, (
                f"reason for {cmd!r} does not point at the {{target_pod}} "
                f"fix: {reason}"
            )

    def test_c61_accepted_commands_derive_api_object_class(self):
        """Label-only derivation: the original LLM output carries no
        ``class`` field, so the accepted commands get their class
        stamped from the form. All four are non-node API reads →
        ``api_object``.
        """
        accepted, _rejected = _validate_and_filter_commands(
            C61_DERIVE_OUTPUT, "k8s",
        )
        for cmd in accepted:
            assert cmd.class_value == "api_object", (
                f"expected api_object derivation for {cmd.command!r}, "
                f"got {cmd.class_value!r}"
            )

    def test_c61_receipt_count_is_honest_not_8_of_8(self):
        """The receipt-count contract: ``success_count / total_count``
        must reflect the honest number of executable observations, not
        the LLM's original command count. Under the pre-change
        pipeline, all 8 commands entered the execute batch and the
        receipt read ``8/8`` after retry-laundering; post-change, only
        4 enter, so an all-successful execute yields ``4/4`` against a
        derive-side total of 8 — the gap is the surfaced rejection
        count, not a hidden spin.
        """
        accepted, rejected = _validate_and_filter_commands(
            C61_DERIVE_OUTPUT, "k8s",
        )
        # Simulate: accepted commands all execute successfully.
        success_count = len(accepted)
        derive_total = len(C61_DERIVE_OUTPUT)
        rejected_count = len(rejected)
        assert success_count == 4
        assert rejected_count == 4
        assert derive_total == 8
        # The "8/8 laundered" shape is impossible post-change: the 4
        # rejected commands never become observations, so they cannot
        # be counted as successes.
        assert success_count < derive_total
        # And the gap is exactly the surfaced rejection count (nothing
        # silently disappears).
        assert derive_total - success_count == rejected_count


class TestCase61AllRejectedFallbackBoundary:
    """Boundary documentation: if EVERY derive-side command is rejected,
    ``observations`` is empty at 4.0.7's decision point. The existing
    4.0.7 gate is ``if observations and not any(success...)`` — an
    empty ``observations`` short-circuits to False, so the strategy
    fallback does NOT fire in that specific shape. This is pre-existing
    behaviour (not a regression introduced by this change); the
    surfaced-rejection channel still leaves the fact visible to the
    verifier and to post-hoc analysis. A future change may extend 4.0.7
    to also fire on ``len(rejected) == len(commands)``, but that is out
    of scope here.
    """

    def test_all_rejected_yields_empty_accepted_list(self):
        all_illegal = [
            {"description": "a", "command": "kubectl exec -l app=x -n ns -- id"},
            {"description": "b", "command": "kubectl exec -l app=x -n ns -- df -h"},
        ]
        accepted, rejected = _validate_and_filter_commands(all_illegal, "k8s")
        assert accepted == []
        assert len(rejected) == 2


# ---------------------------------------------------------------------------
# Case #60 fixture — retry-round half-way substitution
# ---------------------------------------------------------------------------
#
# c60_run1.log's derive output is all legal (no selector-form exec), so
# the case's contribution to this change is on the RETRY side: a
# container_internal failure whose proposed replacement is an api_object
# probe. The pattern is the same as #61's launder-through-retry shape,
# and the class gate refuses it identically.


class TestCase60EndToEnd:
    """Retry-side half-way substitution interception, exercised against
    a fixture shaped like ``c60_run1.log``'s target (a StatefulSet pod
    ``drill-sts-pvc-target-0`` in ``default``). The failed observation
    is a ``container_internal`` probe (``df -k /``); the LLM's proposed
    replacement is an ``api_object`` probe (``kubectl get pod ... -o
    jsonpath=...``). Pre-change, the replacement would pass the
    whitelist and enter the execute batch, exit 0, and the container
    dimension would go unmeasured. Post-change, the class gate refuses
    the mismatch, the failed observation stays in play, and the reason
    is stamped for the next round.
    """

    def test_c60_halfway_substitution_refused_by_class_gate(self):
        failed_obs = [{
            "description": "container disk usage",
            "command": "kubectl exec drill-sts-pvc-target-0 -n default -- df -k /",
            "exit_code": 1,
            "stderr": "Error from server (NotFound): pods \"drill-sts-pvc-target-0\" not found",
        }]
        # LLM's proposed replacement: an api_object probe declared as
        # container_internal (the honest declaration would be
        # api_object, but the LLM was trying to "stay in the same
        # dimension" and got the taxonomy wrong).
        decisions = [{
            "verdict": "replace",
            "description": "pod identity check",
            "command": "kubectl get pod drill-sts-pvc-target-0 -n default -o wide",
            "mode": "simple",
            "class": "container_internal",
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        # Refused.
        assert result["replace"] == []
        # Original obs kept in play (paired with the refused command).
        assert len(result["rejected_replacements"]) == 1
        obs, cmd_text, reason = result["rejected_replacements"][0]
        assert obs is failed_obs[0]
        assert "kubectl get pod" in cmd_text
        # Reason names both sides: declared class + required form.
        assert "container_internal" in reason
        assert "kubectl exec" in reason

    def test_c60_well_formed_replacement_accepted(self):
        """Control: an honest container_internal replacement using the
        ``{target_pod}`` placeholder (task 3) passes the gate — the
        refusal above is specifically about the class↔form mismatch,
        not about the target identity being stale.
        """
        failed_obs = [{
            "description": "container disk usage",
            "command": "kubectl exec drill-sts-pvc-target-0 -n default -- df -k /",
            "exit_code": 1,
            "stderr": "Error from server (NotFound)",
        }]
        decisions = [{
            "verdict": "replace",
            "description": "container disk usage (corrected target)",
            "command": "kubectl exec {target_pod} -n default -- df -k /",
            "mode": "simple",
            "class": "container_internal",
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert len(result["replace"]) == 1
        assert result["replace"][0].class_value == "container_internal"
        assert result["rejected_replacements"] == []

    def test_c60_api_object_declaration_would_pass_gate_documenting_boundary(self):
        """Boundary: the class gate polices class↔form consistency only.

        This fixture hand-builds an observation WITHOUT the ``_class`` stamp
        the executor chokepoint writes, so the dimension-preservation gate
        (W-67-4) has nothing to pin to and the LLM's own honest ``api_object``
        declaration stands — the pre-W-67-4 behaviour, kept as the explicit
        fallback for legacy / registry-sourced / hand-built observation lists.

        With the stamp present the substitution IS refused: see
        ``TestCase67DimensionPreservation`` below. Case #60 originally
        documented this as a deferred boundary ("a future change may add a
        dimension-preservation gate"); case #67 is the field sample that made
        the gap concrete — a ``node_level`` replacement refused on form was
        re-submitted verbatim as ``container_internal`` one round later and
        the receipt read ``5/5 commands succeeded``.
        """
        failed_obs = [{
            "description": "container disk usage",
            "command": "kubectl exec drill-sts-pvc-target-0 -n default -- df -k /",
            "exit_code": 1, "stderr": "",
        }]
        decisions = [{
            "verdict": "replace",
            "description": "pod identity check",
            "command": "kubectl get pod drill-sts-pvc-target-0 -n default -o wide",
            "mode": "simple",
            "class": "api_object",  # honest declaration
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        # No ``_class`` on the obs → nothing to pin → accepted as declared.
        assert len(result["replace"]) == 1
        assert result["rejected_replacements"] == []


class TestCase67DimensionPreservation:
    """W-67-4: a retry may repair the COMMAND, never redefine the DIMENSION.

    The observation carries ``_class`` (stamped by
    ``_executors._execute_observations`` from the resolved command's
    ``class_value``), so the replacement's declared class is pinned to it
    before the existing class↔form gate runs. A command that genuinely
    measures the original dimension passes even if the model mislabelled it;
    a command that substitutes a different dimension is refused through the
    ``rejected_replacements`` channel — the original observation stays in the
    failed set, the reason reaches the next round's feedback, and the
    dimension shows up as uncovered instead of silently redefined.
    """

    def test_c67_relabelled_replacement_is_refused(self):
        """The exact case-#67 replay: refused as ``node_level`` in round 1,
        re-submitted verbatim as ``container_internal`` in round 2."""
        failed_obs = [{
            "description": "Node disk IO counters",
            "command": "kubectl exec {debug_pod} -n chaosblade -- cat /proc/diskstats",
            "exit_code": -1,
            "stderr": "No debug pod or tool pod available for node n1",
            "_class": "node_level",
        }]
        decisions = [{
            "verdict": "replace",
            "reason": "the debug pod was unavailable, so read /proc/diskstats "
                      "inside the victim pod instead",
            "description": "Node disk IO counters",
            "command": "kubectl exec drill-sts-pvc-target-0 -n default -- "
                       "cat /proc/diskstats",
            "mode": "simple",
            "class": "container_internal",  # the relabel
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert result["replace"] == []
        assert len(result["rejected_replacements"]) == 1
        obs, cmd_text, reason = result["rejected_replacements"][0]
        assert obs is failed_obs[0]
        assert "cat /proc/diskstats" in cmd_text
        # The reason must name BOTH failures, or the next round just relabels
        # again: the form mismatch AND the non-negotiable dimension.
        assert "node_level" in reason
        assert "container_internal" in reason
        assert "dimension" in reason

    def test_c67_faithful_repair_passes_under_the_pinned_class(self):
        """Non-degradation pin: pinning must not refuse a repair that really
        does measure the original dimension — only the label was wrong. The
        accepted command keeps the PINNED class, not the declared one."""
        failed_obs = [{
            "description": "Node conditions",
            "command": "kubectl describe node typo-node",
            "exit_code": 1,
            "stderr": 'Error from server (NotFound): nodes "typo-node" not found',
            "_class": "node_level",
        }]
        decisions = [{
            "verdict": "replace",
            "description": "Node conditions (corrected name)",
            "command": "kubectl describe node cn-node-1",
            "mode": "simple",
            "class": "api_object",  # wrong label, right dimension
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert result["rejected_replacements"] == []
        assert len(result["replace"]) == 1
        assert result["replace"][0].class_value == "node_level"
        # The description is pinned too: it names the dimension, so a retry
        # that rewrote it would be redefining what the receipt reports.
        assert result["replace"][0].description == "Node conditions"

    def test_c67_absent_class_on_obs_falls_back_to_the_declaration(self):
        """Registry-sourced and legacy observations carry no stamp; the gate
        must then behave exactly as before (no pinning, no refusal)."""
        failed_obs = [{
            "description": "Node disk usage",
            "command": "kubectl exec {debug_pod} -n chaosblade -- df -h",
            "exit_code": -1, "stderr": "No debug pod",
            "_class": None,
        }]
        decisions = [{
            "verdict": "replace",
            "description": "Node disk usage",
            "command": "kubectl exec victim-0 -n default -- df -h",
            "mode": "simple",
            "class": "container_internal",
        }]

        result = _split_retry_decisions(decisions, failed_obs, "k8s")

        assert result["rejected_replacements"] == []
        assert len(result["replace"]) == 1
        assert result["replace"][0].class_value == "container_internal"
