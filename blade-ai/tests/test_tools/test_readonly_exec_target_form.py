"""Target-zone form gate for ``kubectl exec`` (Case #61 / W-61-1).

Background: baseline LLM in Case #61 (Pod_文件权限异常) generalized ``-l``
from ``get``/``top`` (where it is legitimate) to ``exec`` (where it is not),
producing four commands like ``kubectl exec -l app=drill-perms-target
-n cms-demo -- id`` that failed at runtime with ``unknown shorthand flag:
'l'`` and were then laundered as ``expected_absence`` by the retry LLM's
half-way replacement — the receipt said ``7/7 succeeded`` while four
container-internal dimensions were never actually measured.

The predicate ``kubectl_exec_target_form_reason`` closes the prefix blind
spot that ``kubectl_exec_rejection_reason`` deliberately leaves inert (its
docstring: "judgement starts at the ``--`` boundary"). Both must pass for
an exec command to be permitted; this file only tests the FORM half.

Design contract tested here:

  * Rejections are closed-syntax (selector flags, missing positional,
    unsupported kind/, empty name after kind/) — never existence checks
    or vocabulary judgements.
  * Permitted forms are the closed set {placeholder, bare pod name,
    ``<kind>/<name>`` with an allowed kind}.
  * Out-of-domain commands (non-exec, exec without ``--``) return None
    so the caller's other gates handle them.
  * The predicate is self-consistent on its own input domain — no
    reliance on caller if/elif ordering (memory 04dfe25d).
"""

import pytest

from chaos_agent.tools.readonly import (
    kubectl_exec_rejection_reason,
    kubectl_exec_target_form_reason,
)


class TestSelectorFlagsRejected:
    """Case #61 empirical failure mode: selector flags in the target zone."""

    @pytest.mark.parametrize("cmd,flag", [
        # The exact four commands from .b4tmp/c61_run1.log (LLM's ``-l``
        # generalization from get/top to exec)
        ("kubectl exec -l app=drill-perms-target -n cms-demo -- id", "-l"),
        ("kubectl exec -l app=drill-perms-target -n cms-demo -- stat /app/config.yaml", "-l"),
        # --selector and --field-selector equivalents
        ("kubectl exec --selector app=foo -n cms-demo -- id", "--selector"),
        ("kubectl exec --field-selector status.phase=Running -n cms-demo -- id",
         "--field-selector"),
        # Selector flag positioned after other flags
        ("kubectl exec -n cms-demo -l app=foo -- id", "-l"),
        ("kubectl exec -n cms-demo -c app -l app=foo -- id", "-l"),
    ])
    def test_rejected_with_selector_word_in_reason(self, cmd, flag):
        reason = kubectl_exec_target_form_reason(cmd)
        assert reason is not None, f"expected rejection for {cmd}"
        # Reason-fix pairing: names the offending flag AND points to the fix
        assert flag in reason
        assert "selector" in reason.lower()
        assert "{target_pod}" in reason  # the fix hint


class TestPermittedTargetForms:
    """The closed set of permitted target forms."""

    @pytest.mark.parametrize("cmd", [
        # 1. Placeholder form (baseline-side extension)
        "kubectl exec {target_pod} -n cms-demo -- id",
        "kubectl exec {target_pod} -n cms-demo -- stat /app/config.yaml",
        "kubectl exec -n cms-demo {target_pod} -- id",  # placeholder after -n
        "kubectl exec -n cms-demo -c app {target_pod} -- id",  # with -c
        # 2. Bare literal pod name
        "kubectl exec drill-perms-target-6d4f8-x2k9p -n cms-demo -- id",
        "kubectl exec my-pod -- id",  # minimal form
        # 3. <kind>/<name> with allowed kinds (each alias exercised)
        "kubectl exec deploy/drill-perms-target -n cms-demo -- id",
        "kubectl exec deployment/drill-perms-target -n cms-demo -- id",
        "kubectl exec pod/foo -n cms-demo -- id",
        "kubectl exec pods/foo -n cms-demo -- id",
        "kubectl exec po/foo -n cms-demo -- id",
        "kubectl exec sts/bar -n cms-demo -- id",
        "kubectl exec statefulset/bar -n cms-demo -- id",
        "kubectl exec ds/baz -n cms-demo -- id",
        "kubectl exec daemonset/baz -n cms-demo -- id",
        "kubectl exec svc/qux -n cms-demo -- id",
        "kubectl exec service/qux -n cms-demo -- id",
    ])
    def test_form_gate_passes(self, cmd):
        assert kubectl_exec_target_form_reason(cmd) is None


class TestOtherRejections:
    """Non-selector rejection paths."""

    def test_missing_positional_target_rejected(self):
        reason = kubectl_exec_target_form_reason("kubectl exec -n cms-demo -- id")
        assert reason is not None
        assert "positional target" in reason.lower()
        assert "{target_pod}" in reason

    def test_unsupported_resource_prefix_rejected(self):
        # ``node/foo`` is not a valid exec target kind
        reason = kubectl_exec_target_form_reason(
            "kubectl exec node/foo -n cms-demo -- id"
        )
        assert reason is not None
        assert "unsupported" in reason.lower()
        assert "node/" in reason

    def test_empty_name_after_prefix_rejected(self):
        reason = kubectl_exec_target_form_reason(
            "kubectl exec deploy/ -n cms-demo -- id"
        )
        assert reason is not None
        assert "empty name" in reason.lower()


class TestOutOfDomainReturnsNone:
    """Predicate abstains (returns None) outside its input domain."""

    @pytest.mark.parametrize("cmd", [
        "kubectl get pods -l app=foo -n cms-demo",  # not exec — -l is fine here
        "kubectl top pod -l app=foo -n cms-demo",  # not exec
        "kubectl exec foo",  # no ``--`` separator (caller handles this)
        "kubectl exec",  # degenerate
        "kubectl debug node/foo -it --image=ubuntu -- chroot /host bash",  # not exec
        "",  # empty
        "ls -la",  # not kubectl at all
    ])
    def test_returns_none(self, cmd):
        assert kubectl_exec_target_form_reason(cmd) is None


class TestPredicateSelfConsistency:
    """Memory 04dfe25d discipline: the predicate's verdict must not depend
    on caller if/elif ordering — direct calls return the same truth as
    composed calls."""

    def test_form_gate_orthogonal_to_inner_judge(self):
        # ``-- rm -rf /`` is not read-only (inner judge rejects) but the
        # target FORM (``foo``) is fine — the two predicates must disagree
        # without either corrupting the other.
        cmd = "kubectl exec foo -- rm -rf /"
        assert kubectl_exec_target_form_reason(cmd) is None  # form OK
        assert kubectl_exec_rejection_reason(cmd) is not None  # inner rejected

    def test_form_gate_rejects_what_inner_passes(self):
        # ``-l`` selector with a read-only inner command — inner judge
        # accepts (its judgement starts at ``--``), form gate rejects.
        cmd = "kubectl exec -l app=foo -n cms-demo -- id"
        assert kubectl_exec_target_form_reason(cmd) is not None  # form rejected
        # Inner judge takes the full command but only inspects after ``--``;
        # ``id`` is read-only so it returns None.
        assert kubectl_exec_rejection_reason(cmd) is None

    def test_isolated_call_matches_composed_semantics(self):
        """Direct-call verdict = verdict via any composed caller (no
        ordering dependence)."""
        # The rejection is stable across repeated calls
        cmd = "kubectl exec -l app=foo -- id"
        first = kubectl_exec_target_form_reason(cmd)
        second = kubectl_exec_target_form_reason(cmd)
        assert first == second
        assert first is not None
