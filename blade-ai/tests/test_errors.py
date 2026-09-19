"""Tests for the error classification hierarchy."""

import pytest

from chaos_agent.errors import (
    ChaosAgentError,
    ErrorClass,
    ErrorSeverity,
    BladeExecutionError,
    BladeTransientError,
    InvalidParameterError,
    KubectlConnectionError,
    LLMContextOverflowError,
    LLMRateLimitError,
    SafetyBlockedError,
    SkillNotFoundError,
    TargetNotFoundError,
    ToolGuardError,
    ToolTimeoutError,
    _CLASSIFY_RULES,
    classify_error,
    is_recoverable,
    is_transient,
    should_auto_replan,
)


class TestErrorSeverity:
    """Test ErrorSeverity enum values."""

    def test_severity_values(self):
        assert ErrorSeverity.TRANSIENT.value == "transient"
        assert ErrorSeverity.PERMANENT.value == "permanent"
        assert ErrorSeverity.RECOVERABLE.value == "recoverable"


class TestChaosAgentError:
    """Test base ChaosAgentError class."""

    def test_default_severity_is_permanent(self):
        err = ChaosAgentError("test error")
        assert err.severity == ErrorSeverity.PERMANENT

    def test_default_error_code(self):
        err = ChaosAgentError("test error")
        assert err.error_code == 4001

    def test_custom_error_code(self):
        err = ChaosAgentError("test error", error_code=9999)
        assert err.error_code == 9999

    def test_message_preserved(self):
        err = ChaosAgentError("something went wrong")
        assert err.message == "something went wrong"
        assert str(err) == "something went wrong"

    def test_is_exception(self):
        with pytest.raises(ChaosAgentError):
            raise ChaosAgentError("boom")


class TestTransientErrors:
    """Test transient error types."""

    @pytest.mark.parametrize(
        "error_cls, expected_code",
        [
            (ToolTimeoutError, 4002),
            (KubectlConnectionError, 4003),
            (LLMRateLimitError, 4001),
            (BladeTransientError, 4002),
        ],
    )
    def test_transient_severity(self, error_cls, expected_code):
        err = error_cls("test")
        assert err.severity == ErrorSeverity.TRANSIENT
        assert err.error_code == expected_code

    def test_is_transient_returns_true(self):
        err = ToolTimeoutError("timed out")
        assert is_transient(err) is True

    def test_is_transient_returns_false_for_permanent(self):
        err = BladeExecutionError("failed")
        assert is_transient(err) is False

    def test_is_transient_returns_false_for_plain_exception(self):
        assert is_transient(ValueError("not chaos")) is False


class TestPermanentErrors:
    """Test permanent error types."""

    @pytest.mark.parametrize(
        "error_cls, expected_code",
        [
            (BladeExecutionError, 4002),
            (TargetNotFoundError, 1003),
            (SafetyBlockedError, 3001),
            (SkillNotFoundError, 1002),
            (InvalidParameterError, 1001),
            (ToolGuardError, 4001),
        ],
    )
    def test_permanent_severity(self, error_cls, expected_code):
        err = error_cls("test")
        assert err.severity == ErrorSeverity.PERMANENT
        assert err.error_code == expected_code


class TestRecoverableErrors:
    """Test recoverable error types."""

    def test_context_overflow_is_recoverable(self):
        err = LLMContextOverflowError("overflow")
        assert err.severity == ErrorSeverity.RECOVERABLE
        assert err.error_code == 4001

    def test_is_recoverable_returns_true(self):
        err = LLMContextOverflowError("overflow")
        assert is_recoverable(err) is True

    def test_is_recoverable_returns_false_for_transient(self):
        err = ToolTimeoutError("timed out")
        assert is_recoverable(err) is False

    def test_is_recoverable_returns_false_for_plain_exception(self):
        assert is_recoverable(ValueError("nope")) is False


class TestLLMContextOverflowErrorIsReservedNotWired:
    """``LLMContextOverflowError`` is a RESERVED type: the tests above pin its
    severity contract, and that is exactly what makes it look live. It is not.
    Nothing in ``src/`` raises it and nothing consumes ``is_recoverable``, so a
    provider's context-length 400 is classified as ``LLMProviderRejectError``
    (PERMANENT/4006) instead — see openspec change llm-provider-compat-fixes,
    design.md D5a.

    These tests exist so that wiring it up (or deciding to "fix" the 400
    classification by routing overflow here) is a deliberate act that turns a
    test red, not a silent one. AST rather than a text search: a docstring or
    comment mentioning the class is not wiring, and counting those would make
    the test fail for documentation edits that change no behaviour.
    """

    @staticmethod
    def _references_outside_errors_py() -> list[str]:
        """Every src/ module (besides errors.py) that names the class."""
        import ast
        import inspect
        from pathlib import Path

        pkg_root = Path(inspect.getfile(ChaosAgentError)).parent
        hits: list[str] = []
        for path in sorted(pkg_root.rglob("*.py")):
            if path.name == "errors.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == "LLMContextOverflowError"
            }
            if names:
                hits.append(str(path.relative_to(pkg_root.parent)))
        return hits

    def test_nothing_in_src_raises_or_handles_it(self):
        assert self._references_outside_errors_py() == [], (
            "LLMContextOverflowError was wired into src/. That is allowed, but "
            "it invalidates design.md D5a reason 1 (the raise point has no "
            "degradation path) — re-read D5a and update it, the spec "
            "Requirement '上下文超窗 4xx 归 PERMANENT', and the class docstring "
            "in errors.py in the same change."
        )

    def test_is_recoverable_has_no_consumer_in_src(self):
        """The other half of reason 2: even if the class WERE raised, the
        RECOVERABLE tier has nowhere to go. ``retry_if_transient`` refuses both
        PERMANENT and RECOVERABLE, so this predicate is currently decorative.
        If a consumer appears, marking an overflow RECOVERABLE starts to mean
        something — and D5a has to be re-argued, not just re-read.
        """
        import ast
        import inspect
        from pathlib import Path

        pkg_root = Path(inspect.getfile(ChaosAgentError)).parent
        consumers: list[str] = []
        for path in sorted(pkg_root.rglob("*.py")):
            if path.name == "errors.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if any(
                isinstance(n, ast.Name) and n.id == "is_recoverable"
                for n in ast.walk(tree)
            ):
                consumers.append(str(path.relative_to(pkg_root.parent)))
        assert consumers == [], (
            f"is_recoverable gained a consumer: {consumers}. The RECOVERABLE "
            "tier is no longer decorative — re-argue design.md D5a before "
            "relying on LLMContextOverflowError's severity."
        )

    def test_the_docstring_says_it_is_reserved(self):
        """The trap is defused where the next contributor looks: the class
        itself. If this text is rewritten, the misreading ("it has a severity
        contract, so the 4006 classification must be the bug") comes back.
        """
        doc = LLMContextOverflowError.__doc__ or ""
        assert "RESERVED AND UNWIRED" in doc
        assert "LLMProviderRejectError" in doc
        assert "D5a" in doc


# ---------------------------------------------------------------------------
# Tests for extract_llm_diagnosis
# ---------------------------------------------------------------------------

from chaos_agent.errors import (
    _DIAGNOSIS_FALLBACK,
    extract_llm_diagnosis,
)
from langchain_core.messages import AIMessage, HumanMessage


class TestExtractLlmDiagnosis:
    """Test extract_llm_diagnosis helper."""

    def test_finds_last_ai_message(self):
        msgs = [
            HumanMessage(content="inject memory fault"),
            AIMessage(content="Early analysis of the situation"),
            AIMessage(content="Target node cn-hongkong lacks ChaosBlade Agent"),
        ]
        result = extract_llm_diagnosis(msgs)
        assert "Target node cn-hongkong lacks ChaosBlade Agent" in result

    def test_empty_messages_returns_fallback(self):
        assert extract_llm_diagnosis([]) == _DIAGNOSIS_FALLBACK

    def test_skips_tool_only_ai_messages(self):
        tool_msg = AIMessage(
            content="",
            tool_calls=[{"name": "blade_create", "args": {}, "id": "1"}],
        )
        msgs = [tool_msg]
        assert extract_llm_diagnosis(msgs) == _DIAGNOSIS_FALLBACK

    def test_truncation(self):
        long_text = "A" * 600
        msgs = [AIMessage(content=long_text)]
        result = extract_llm_diagnosis(msgs, max_length=100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_reasoning_content_fallback(self):
        msg = AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "Deep reasoning about the failure root cause here"},
        )
        result = extract_llm_diagnosis([msg])
        assert "Deep reasoning about the failure root cause" in result

    def test_skips_short_content(self):
        msgs = [AIMessage(content="ok")]
        assert extract_llm_diagnosis(msgs) == _DIAGNOSIS_FALLBACK

    def test_prefers_content_over_reasoning(self):
        msg = AIMessage(
            content="Content diagnosis is the primary source of truth",
            additional_kwargs={"reasoning_content": "reasoning backup"},
        )
        result = extract_llm_diagnosis([msg])
        assert "Content diagnosis" in result




class TestShouldAutoReplan:
    """Test should_auto_replan pattern matching."""

    def test_unknown_flag_triggers_replan(self):
        """'unknown flag: --namespace' should trigger auto-replan."""
        assert should_auto_replan("Error: blade create failed (exit 1): unknown flag: --namespace") is True

    def test_unknown_flag_case_insensitive(self):
        """Pattern matching should be case-insensitive."""
        assert should_auto_replan("Unknown Flag: --foo") is True

    def test_unsupported_flag_triggers_replan(self):
        """'unsupported flag' was already a replanable pattern."""
        assert should_auto_replan("Error: unsupported flag: --bar") is True

    def test_resource_not_found_triggers_replan(self):
        assert should_auto_replan("Error: resource not found: pods \"mysql\"") is True

    def test_permission_denied_no_replan(self):
        """Permission errors should NOT trigger replan."""
        assert should_auto_replan("Error: permission denied") is False

    def test_timeout_no_replan(self):
        """Timeout errors should NOT trigger replan."""
        assert should_auto_replan("Error: timeout waiting for blade status") is False

    def test_unknown_flag_dominates_when_combined_with_timeout(self):
        """Patch B layered classifier: USER_CONFIG (unknown flag) ranks
        above INFRA_TRANSIENT (timeout) so a mixed string is treated
        as REPLAN-able. Rationale: an "unknown flag" is a planning
        bug LLM can fix on replan; a co-occurring "timeout" is just
        ambient network noise that doesn't change what action is
        correct. Real-world co-occurrence of both signatures in one
        error string is rare enough that biasing toward the more
        specific signal is safer than the legacy "timeout always
        wins" behaviour. See ``ErrorClass`` / ``classify_error`` in
        ``chaos_agent.errors`` for the full rule order."""
        assert should_auto_replan("unknown flag: --namespace, timeout exceeded") is True


# Adversarial receipt matrix for the B41-family wording additions
# (2026-09-09): adding a pattern to the global classifier is a
# cross-cutting change, so the blast radius needs both faces anchored —
# receipts that were already classified by legacy patterns (must keep
# their class: first-match-wins + disjoint wordings means an addition
# can only pick up receipts the legacy rules left UNKNOWN, never flip an
# established bucket), and the wording's appearances in OTHER semantic
# scenarios (other tools / other cases) landing in the intended class.
_ADVERSARIAL_WORDING_MATRIX = [
    # (label, receipt, expected class)
    (
        "kubectl-404",
        'Error from server (NotFound): pods "my-app-7f9c8" not found',
        ErrorClass.TARGET_GONE,  # legacy "not found"
    ),
    (
        "bash-tool-missing",
        "Error: kubectl exec (exit 127): bash: iostat: command not found",
        ErrorClass.DEPENDENCY_MISSING,  # legacy; rule order wins over ENOENT
    ),
    (
        "sh-enoent-tool-missing",
        "Error: kubectl exec (exit 127): sh: crictl: No such file or "
        "directory",
        # busybox/dash wording for a missing tool: ideal class is
        # DEPENDENCY_MISSING, but its action is REPLAN — identical
        # routing to TARGET_GONE, so only the reminder differs (the
        # verifier prompt carries the equivalent "switch to structured
        # status" guidance, so the LLM does not depend on it).
        ErrorClass.TARGET_GONE,
    ),
    (
        "curl-connection-timed-out",
        "Error: curl: (28) Connection timed out after 5001 milliseconds",
        ErrorClass.INFRA_TRANSIENT,  # new wording surfacing in another tool
    ),
    (
        "requests-read-timed-out",
        "Error: HTTPSConnectionPool: Read timed out. (read timeout=10)",
        ErrorClass.INFRA_TRANSIENT,  # legacy "timeout" substring
    ),
    (
        "go-deadline-exceeded",
        "Error: context deadline exceeded",
        ErrorClass.INFRA_TRANSIENT,  # legacy
    ),
    (
        "disk-full-unaffected",
        "Error: dd: error writing '/var/log/x': No space left on device",
        ErrorClass.UNKNOWN,  # no wording overlaps — must stay UNKNOWN
    ),
    (
        "kubeconfig-enoent",
        "Error: stat /root/.kube/config: No such file or directory",
        # Edge: ideal is USER_CONFIG, but pre-fix this was
        # UNKNOWN/END_FAILED (a hard kill) — REPLAN is the upgrade.
        ErrorClass.TARGET_GONE,
    ),
]


class TestClassifyErrorWordingGaps:
    """B41 wording-variant patterns the classifier missed (#30/#29-R2
    deep audits).

    All receipts below are EXPECTED forms in the skill corpus, not
    interface failures — the RUNTIME EVIDENCE introspection reminder must
    stay dark on them (denylist classes), and the router action must match
    the connected form's semantics. Fixtures are verbatim task receipts
    (systemctl exit-4 + ls exit-2 residue pre-check PASS forms, harness
    30s task-ceiling truncation).
    """

    def test_systemctl_could_not_be_found_is_target_gone(self):
        """systemctl inserts "be": "Unit xxx could not be found" (exit 4).
        The legacy "not found" pattern does not substring-match it."""
        receipt = (
            "Error: kubectl exec (exit 4): Unit blade-restore-drain.timer "
            "could not be found.\ncommand terminated with exit code 4"
        )
        result = classify_error(receipt)
        assert result.error_class is ErrorClass.TARGET_GONE
        assert result.matched_pattern == "could not be found"

    def test_ls_no_such_file_or_directory_is_target_gone(self):
        """POSIX ls wording for a missing target (#29-R2 verbatim planning
        receipt; the recover phase re-check hits the same wording). The
        phrase contains no "not found" substring — the broad TARGET_GONE
        pattern never caught it and the reminder lit on a PASS form."""
        receipt = (
            "Error: one-shot debug command failed with exit_code=2.\n"
            '[debug-pod-meta: {"name":"node-debugger-xc6p8","namespace":'
            '"default","phase":"Failed","exit_code":2,"cleaned":true,'
            '"oneshot":true}]\n'
            "The debug pod has been removed.\n"
            "Command output (logs tail):\n"
            "ls: cannot access '/var/log/app-archive.log': "
            "No such file or directory"
        )
        result = classify_error(receipt)
        assert result.error_class is ErrorClass.TARGET_GONE
        assert result.matched_pattern == "no such file or directory"

        # The recover-phase variant (kubectl exec wording, capital N):
        # matching is case-insensitive on the lowered receipt.
        exec_form = (
            "Error: kubectl exec (exit 2): ls: cannot access "
            "'/var/log/app-archive.log': No such file or directory\n"
            "command terminated with exit code 2"
        )
        result = classify_error(exec_form)
        assert result.error_class is ErrorClass.TARGET_GONE
        assert result.matched_pattern == "no such file or directory"

    def test_ls_absence_receipt_does_not_ignite_introspection(self):
        """B41-family fourth member: the expected-absence ls receipt must
        keep the RUNTIME EVIDENCE reminder dark (#29-R2 planning msg [43]
        lit it; the model's 42s legislation-lookup round carried the
        digest)."""
        from chaos_agent.agent.nodes.execute.react_helpers import (
            _should_trigger_introspection,
        )

        absence = (
            "ls: cannot access '/var/log/app-archive.log': "
            "No such file or directory"
        )
        assert _should_trigger_introspection(
            classify_error(absence).error_class
        ) is False

    def test_harness_timed_out_is_infra_transient(self):
        """The harness/wiz wording splits the word: "task timed out after
        30s". The legacy "timeout" pattern does not substring-match it."""
        receipt = (
            "Error: kubectl drain (exit 1): Error: task timed out after 30s "
            "(task_uuid: TK-20260908-71730CAE)"
        )
        result = classify_error(receipt)
        assert result.error_class is ErrorClass.INFRA_TRANSIENT
        assert result.matched_pattern == "timed out"

    def test_expected_receipts_do_not_ignite_introspection(self):
        """The whole point of B41: both denylist classes → the RUNTIME
        EVIDENCE reminder (detect_tool_error_hint →
        _should_trigger_introspection) stays dark on expected-absence
        and truncation receipts. Each misclassification previously cost
        one wasted digestion round (#30: two in one task)."""
        from chaos_agent.agent.nodes.execute.react_helpers import (
            _should_trigger_introspection,
        )

        absence = (
            "Error: kubectl exec (exit 4): Unit blade-restore-drain.timer "
            "could not be found."
        )
        truncation = "Error: task timed out after 30s (task ceiling)"
        assert _should_trigger_introspection(
            classify_error(absence).error_class
        ) is False
        assert _should_trigger_introspection(
            classify_error(truncation).error_class
        ) is False

    def test_timed_out_no_replan(self):
        """The split form inherits the connected form's routing: transient
        errors do not auto-replan (mirrors test_timeout_no_replan)."""
        assert should_auto_replan("Error: task timed out after 30s") is False

    @pytest.mark.parametrize(
        "label,receipt,expected",
        _ADVERSARIAL_WORDING_MATRIX,
        ids=[entry[0] for entry in _ADVERSARIAL_WORDING_MATRIX],
    )
    def test_adversarial_wording_matrix(self, label, receipt, expected):
        """The B41-family additions are standard-wording dictionary
        entries (POSIX ENOENT / systemctl / English past-tense timeout),
        not case-specific outputs: every appearance — in other tools,
        other semantic scenarios, other cases — must land in the intended
        class. See _ADVERSARIAL_WORDING_MATRIX for per-entry intent."""
        result = classify_error(receipt)
        assert result.error_class is expected, (
            f"{label}: {result.error_class.name} "
            f"(pattern {result.matched_pattern!r})"
        )

    def test_wording_additions_only_converge_the_unknown_bucket(self):
        """Structural safety property of the B41-family patch: strip the
        three added wordings from a copy of the rules and re-classify
        every matrix receipt — any receipt the legacy rules already
        classified must classify to the SAME class with the current
        rules (an addition can never flip an established bucket), and
        the receipts the legacy rules left UNKNOWN are exactly where the
        additions are allowed to act. Anchors "fix one case without
        breaking the others" as a permanent invariant."""
        added_wordings = {
            "could not be found",
            "no such file or directory",
            "timed out",
        }
        legacy_rules = [
            (ec, [p for p in patterns if p not in added_wordings])
            for ec, patterns in _CLASSIFY_RULES
        ]

        def classify_with(rules, message):
            msg_lower = message.lower()
            for ec, patterns in rules:
                for pattern in patterns:
                    if pattern in msg_lower:
                        return ec
            return ErrorClass.UNKNOWN

        converged = []
        for label, receipt, expected in _ADVERSARIAL_WORDING_MATRIX:
            legacy = classify_with(legacy_rules, receipt)
            current = classify_error(receipt).error_class
            if legacy is not ErrorClass.UNKNOWN:
                # The invariant: established buckets are immutable under
                # pattern additions (first-match-wins + disjoint wordings).
                assert current is legacy, (
                    f"{label}: addition flipped an established bucket "
                    f"({legacy.name} → {current.name})"
                )
            else:
                # The only allowed effect: picking up an UNKNOWN receipt
                # (or legitimately leaving it UNKNOWN — disk-full).
                assert current is expected, label
                if current is not ErrorClass.UNKNOWN:
                    converged.append(label)
        # Sanity: the matrix must actually contain both faces, otherwise
        # it degrades into a tautology.
        assert len(converged) >= 3, (
            "matrix no longer probes any converged receipt — the legacy "
            "rules may have gained overlapping patterns; refresh it"
        )
