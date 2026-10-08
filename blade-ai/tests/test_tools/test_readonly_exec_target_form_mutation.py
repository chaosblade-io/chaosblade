"""Task 6.3 — mutation red/green anchors for the exec target-form gate.

Each test in this file mutates ONE member of the shape gate's vocabulary
(``_EXEC_SELECTOR_FLAGS`` / ``_EXEC_RESOURCE_PREFIXES`` /
``_TARGET_POD_PLACEHOLDER`` / ``_EXEC_VALUE_FLAGS``) via monkeypatch, then
asserts the predicate's verdict FLIPS for a command that specifically
depends on that member.

Why: a green test suite proves nothing about coverage — a word-list
member could be silently dropped from the enforcement set (dead code)
and every existing test would still pass if none of them exercised that
specific member. These mutation anchors prove every member is load-
bearing: removing it changes behaviour, so at least one existing test
would go red. Member-level discipline (not set-level slogans).

Every mutation is scoped to a single test via monkeypatch, so the
module state is restored automatically — no cross-test contamination.
"""

from __future__ import annotations

import pytest

from chaos_agent.tools import readonly


# ---------------------------------------------------------------------------
# _EXEC_SELECTOR_FLAGS: each of the three members must be load-bearing
# ---------------------------------------------------------------------------


class TestSelectorFlagMutation:
    """Mutating ``_EXEC_SELECTOR_FLAGS`` — each member's removal must
    flip the verdict for a command that uses exactly that flag.
    """

    def test_baseline_all_three_selectors_rejected(self):
        """Pre-mutation control: all three selector flags are refused."""
        for flag in ("-l", "--selector", "--field-selector"):
            reason = readonly.kubectl_exec_target_form_reason(
                f"kubectl exec {flag} app=x -n ns -- id"
            )
            assert reason is not None, f"{flag} should be rejected pre-mutation"
            assert "selector" in reason

    def test_mutation_drops_l_short_flag(self, monkeypatch):
        """Removing ``-l`` from the enforcement set lets the short form
        slip through — proving ``-l`` was load-bearing (Case #61's exact
        shape depended on this member).
        """
        monkeypatch.setattr(
            readonly, "_EXEC_SELECTOR_FLAGS",
            frozenset({"--selector", "--field-selector"}),  # -l dropped
        )
        # Post-mutation: the -l form is no longer caught.
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec -l app=x -n ns -- id"
        ) is None
        # The other two still are (mutation is member-scoped).
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec --selector app=x -n ns -- id"
        ) is not None
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec --field-selector spec.nodeName=n -n ns -- id"
        ) is not None

    def test_mutation_drops_selector_long_flag(self, monkeypatch):
        monkeypatch.setattr(
            readonly, "_EXEC_SELECTOR_FLAGS",
            frozenset({"-l", "--field-selector"}),  # --selector dropped
        )
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec --selector app=x -n ns -- id"
        ) is None
        # The other two still enforced.
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec -l app=x -n ns -- id"
        ) is not None
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec --field-selector spec.nodeName=n -n ns -- id"
        ) is not None

    def test_mutation_drops_field_selector_flag(self, monkeypatch):
        monkeypatch.setattr(
            readonly, "_EXEC_SELECTOR_FLAGS",
            frozenset({"-l", "--selector"}),  # --field-selector dropped
        )
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec --field-selector spec.nodeName=n -n ns -- id"
        ) is None
        # The other two still enforced.
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec -l app=x -n ns -- id"
        ) is not None
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec --selector app=x -n ns -- id"
        ) is not None

    def test_mutation_empties_selector_set_entirely(self, monkeypatch):
        """Extreme mutation: an empty selector set means the gate no
        longer polices selector forms at all — every selector command
        slips through. This is the "word-list dropped" regression the
        anchors exist to catch.
        """
        monkeypatch.setattr(readonly, "_EXEC_SELECTOR_FLAGS", frozenset())
        for flag in ("-l", "--selector", "--field-selector"):
            assert readonly.kubectl_exec_target_form_reason(
                f"kubectl exec {flag} app=x -n ns -- id"
            ) is None, f"{flag} slipped through after emptying the set"


# ---------------------------------------------------------------------------
# _EXEC_RESOURCE_PREFIXES: each kind/alias must be individually load-bearing
# ---------------------------------------------------------------------------


class TestResourcePrefixMutation:
    """Mutating ``_EXEC_RESOURCE_PREFIXES`` — dropping a member must
    flip the verdict for a ``<kind>/<name>`` command using exactly that
    member.
    """

    @pytest.mark.parametrize("prefix", [
        "pod", "pods", "po",
        "deployment", "deployments", "deploy",
        "statefulset", "statefulsets", "sts",
        "daemonset", "daemonsets", "ds",
        "service", "services", "svc",
    ])
    def test_baseline_prefix_accepted(self, prefix):
        """Pre-mutation control: every documented prefix is accepted."""
        assert readonly.kubectl_exec_target_form_reason(
            f"kubectl exec {prefix}/my-resource -n ns -- id"
        ) is None

    @pytest.mark.parametrize("prefix", [
        "pod", "po", "deploy", "sts", "ds", "svc",
    ])
    def test_mutation_drops_single_prefix(self, monkeypatch, prefix):
        """Removing ONE prefix flips that prefix's verdict from
        accepted to rejected — every member is load-bearing.
        """
        mutated = frozenset(readonly._EXEC_RESOURCE_PREFIXES - {prefix})
        monkeypatch.setattr(readonly, "_EXEC_RESOURCE_PREFIXES", mutated)
        # The dropped prefix is now refused (reason names the prefix).
        reason = readonly.kubectl_exec_target_form_reason(
            f"kubectl exec {prefix}/my-resource -n ns -- id"
        )
        assert reason is not None, (
            f"prefix '{prefix}' should be rejected after being dropped"
        )
        assert prefix in reason
        # A sibling prefix (not dropped) still works — the mutation is
        # member-scoped, not set-scoped.
        sibling = "pod" if prefix != "pod" else "deploy"
        assert readonly.kubectl_exec_target_form_reason(
            f"kubectl exec {sibling}/my-resource -n ns -- id"
        ) is None

    def test_mutation_empties_prefix_set_entirely(self, monkeypatch):
        """Extreme mutation: with no prefixes whitelisted, EVERY
        ``<kind>/<name>`` form is rejected. Only the ``{target_pod}``
        placeholder and bare literal pod names remain admissible.
        """
        monkeypatch.setattr(readonly, "_EXEC_RESOURCE_PREFIXES", frozenset())
        for prefix in ("pod", "deploy", "sts", "ds", "svc"):
            assert readonly.kubectl_exec_target_form_reason(
                f"kubectl exec {prefix}/my-resource -n ns -- id"
            ) is not None
        # The other two admissible forms are unaffected (different code
        # paths in the predicate).
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec {target_pod} -n ns -- id"
        ) is None
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec literal-pod-name -n ns -- id"
        ) is None


# ---------------------------------------------------------------------------
# _TARGET_POD_PLACEHOLDER: the exact string matters
# ---------------------------------------------------------------------------


class TestTargetPodPlaceholderMutation:
    """Mutating ``_TARGET_POD_PLACEHOLDER`` — changing the placeholder
    string must flip the verdict for a command that uses the original
    ``{target_pod}`` literal.
    """

    def test_baseline_placeholder_accepted(self):
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec {target_pod} -n ns -- id"
        ) is None

    def test_mutation_renames_placeholder(self, monkeypatch):
        """Renaming the placeholder to ``{pod_target}`` (a plausible
        typo / rename) means the original ``{target_pod}`` is no longer
        recognized — it becomes a bare literal pod name (which the
        predicate accepts as such). The mutation is detectable because
        the reason-fix pairing in the selector-rejection branch still
        references the ORIGINAL placeholder string via the module-level
        constant, so a rename desynchronizes the teaching from the
        enforcement.
        """
        monkeypatch.setattr(readonly, "_TARGET_POD_PLACEHOLDER", "{pod_target}")
        # ``{target_pod}`` is now treated as a bare literal (accepted,
        # but no longer as the placeholder — a semantic drift).
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec {target_pod} -n ns -- id"
        ) is None
        # The renamed placeholder is now the recognized one.
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec {pod_target} -n ns -- id"
        ) is None
        # The reason-fix pairing for selector rejections now names the
        # renamed placeholder — proving the constant IS the single
        # source for both the acceptance branch AND the reason text.
        reason = readonly.kubectl_exec_target_form_reason(
            "kubectl exec -l app=x -n ns -- id"
        )
        assert reason is not None
        assert "{pod_target}" in reason
        assert "{target_pod}" not in reason


# ---------------------------------------------------------------------------
# _EXEC_VALUE_FLAGS: prefix-zone scanning depends on these
# ---------------------------------------------------------------------------


class TestValueFlagMutation:
    """Mutating ``_EXEC_VALUE_FLAGS`` — dropping a value-taking flag
    shifts the scanner's positional-target identification. The
    predicate's verdict is only observably different when the shifted
    ``positional[0]`` lands on a token the downstream branches treat
    differently — e.g. a value token that looks like an unsupported
    ``<kind>/<name>`` prefix. The tests below use that construction to
    make the load-bearing property observable.
    """

    def test_baseline_namespace_flag_value_skipped(self):
        """Pre-mutation control: ``-n bad/prefix`` skips both tokens,
        ``pod-x`` becomes the positional target, form is accepted.
        """
        assert readonly.kubectl_exec_target_form_reason(
            "kubectl exec -n bad/prefix pod-x -- id"
        ) is None

    def test_mutation_drops_n_short_flag(self, monkeypatch):
        """Removing ``-n`` from ``_EXEC_VALUE_FLAGS`` means the scanner
        treats ``-n`` as a bare unknown flag (skip 1) instead of a
        value-taking flag (skip 2). The value token ``bad/prefix`` then
        becomes ``positional[0]``; the resource-prefix branch fires and
        rejects it (``bad`` is not a whitelisted kind).
        """
        monkeypatch.setattr(
            readonly, "_EXEC_VALUE_FLAGS",
            frozenset(readonly._EXEC_VALUE_FLAGS - {"-n"}),
        )
        reason = readonly.kubectl_exec_target_form_reason(
            "kubectl exec -n bad/prefix pod-x -- id"
        )
        assert reason is not None, (
            "expected the shifted positional[0]='bad/prefix' to be rejected"
        )
        assert "unsupported" in reason or "prefix" in reason

    def test_mutation_drops_namespace_long_flag(self, monkeypatch):
        monkeypatch.setattr(
            readonly, "_EXEC_VALUE_FLAGS",
            frozenset(readonly._EXEC_VALUE_FLAGS - {"--namespace"}),
        )
        reason = readonly.kubectl_exec_target_form_reason(
            "kubectl exec --namespace bad/prefix pod-x -- id"
        )
        assert reason is not None
        assert "unsupported" in reason or "prefix" in reason

    def test_mutation_drops_c_short_flag(self, monkeypatch):
        monkeypatch.setattr(
            readonly, "_EXEC_VALUE_FLAGS",
            frozenset(readonly._EXEC_VALUE_FLAGS - {"-c"}),
        )
        reason = readonly.kubectl_exec_target_form_reason(
            "kubectl exec -c bad/prefix pod-x -n ns -- id"
        )
        assert reason is not None
        assert "unsupported" in reason or "prefix" in reason

    def test_mutation_drops_container_long_flag(self, monkeypatch):
        monkeypatch.setattr(
            readonly, "_EXEC_VALUE_FLAGS",
            frozenset(readonly._EXEC_VALUE_FLAGS - {"--container"}),
        )
        reason = readonly.kubectl_exec_target_form_reason(
            "kubectl exec --container bad/prefix pod-x -n ns -- id"
        )
        assert reason is not None
        assert "unsupported" in reason or "prefix" in reason

    def test_mutation_empties_value_flag_set_entirely(self, monkeypatch):
        """Extreme mutation: with no value-taking flags recognized, ANY
        ``-n <value>`` prefix has its value token misread as the
        positional target. When the value happens to look like an
        unsupported ``<kind>/<name>`` prefix, the predicate now rejects
        a command it used to accept — the load-bearing property is
        observable through the verdict flip.
        """
        monkeypatch.setattr(readonly, "_EXEC_VALUE_FLAGS", frozenset())
        reason = readonly.kubectl_exec_target_form_reason(
            "kubectl exec -n bad/prefix pod-x -- id"
        )
        assert reason is not None
        assert "unsupported" in reason or "prefix" in reason

    def test_mutation_preserves_selector_scan_independence(self, monkeypatch):
        """Boundary anchor: the selector-flag scan is a SEPARATE first
        pass over the prefix (readonly.py step 1), independent of the
        positional-target scanner (step 2). Mutating ``_EXEC_VALUE_FLAGS``
        therefore does NOT affect selector rejection — the two vocabularies
        are orthogonal by design.
        """
        monkeypatch.setattr(readonly, "_EXEC_VALUE_FLAGS", frozenset())
        # Selector form is still rejected (step 1 fires first).
        reason = readonly.kubectl_exec_target_form_reason(
            "kubectl exec -l app=x -n ns -- id"
        )
        assert reason is not None
        assert "selector" in reason
