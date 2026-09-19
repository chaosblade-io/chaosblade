"""Phase 2 tool-error messages must not hand the model a menu of alternatives.

Of LangGraph's four templates only ``INVALID_TOOL_NAME_ERROR_TEMPLATE``
enumerates the bound tools ("try one of [...]"). Phase 1 has rewritten that case
since task-ce9647931ce1, where the model was told ``blade_create`` was
unavailable and immediately reached for ``kubectl exec ... blade create`` from
the suggestion list — walking straight past the safety pipeline.

Phase 2 kept the default and hit the same anti-pattern in task-c758cdbdb: a
``save_fault_plan`` call was answered with ``try one of [execute_skill_script,
..., blade_create, blade_destroy, ...]``.

Genuine execution/validation errors are passed through untouched — their detail
is exactly what the model needs to fix a real typo, and they list nothing.
"""

from chaos_agent.agent.graph import _phase2_handle_tool_error

PHASE2_TOOL_NAMES = [
    "execute_skill_script",
    "read_knowledge_resource",
    "time_wait",
    "request_replan",
    "blade_create",
    "blade_destroy",
    "blade_help",
    "blade_status",
    "kubectl",
]


class TestUnknownToolIsRewritten:
    def _invalid_tool_error(self, requested: str = "save_fault_plan") -> Exception:
        return Exception(
            f"Error: {requested} is not a valid tool, "
            f"try one of [{', '.join(PHASE2_TOOL_NAMES)}]."
        )

    def test_no_alternatives_are_listed(self):
        """The exact regression from task-c758cdbdb [90]."""
        out = _phase2_handle_tool_error(self._invalid_tool_error())
        assert "try one of" not in out
        for name in PHASE2_TOOL_NAMES:
            assert name not in out, f"leaked bound tool name: {name}"

    def test_offending_tool_is_named(self):
        out = _phase2_handle_tool_error(self._invalid_tool_error())
        assert "save_fault_plan" in out

    def test_states_nothing_ran(self):
        out = _phase2_handle_tool_error(self._invalid_tool_error())
        assert "nothing changed" in out

    def test_forbids_substituting_another_tool(self):
        """The failure mode is approximating the action with a different tool."""
        out = _phase2_handle_tool_error(self._invalid_tool_error())
        assert "do not substitute a different tool" in out

    def test_unparseable_tool_name_still_safe(self):
        out = _phase2_handle_tool_error(Exception("is not a valid tool, try one of [kubectl]."))
        assert "try one of" not in out
        assert "kubectl" not in out


class TestGenuineErrorsPassThrough:
    """These carry the detail needed to fix a real mistake, and list nothing."""

    def test_execution_error_detail_is_preserved(self):
        err = Exception(
            "Error executing tool 'kubectl' with kwargs {'subcommand': 'patch'} "
            "with error:\n node not found\n Please fix the error and try again."
        )
        out = _phase2_handle_tool_error(err)
        assert "node not found" in out
        assert "kubectl" in out

    def test_validation_error_detail_is_preserved(self):
        err = Exception(
            "Error invoking tool 'kubectl_read' with kwargs {'subcommand': 'delete'} "
            "with error:\n Input should be 'get' or 'describe'"
        )
        out = _phase2_handle_tool_error(err)
        assert "Input should be" in out
        assert "kubectl_read" in out


class TestToolSurfaceAttribution:
    """The history-inertia attribution sentence (openspec
    phase-boundary-declaration; #29 first-run evidence recover-98caf0cd
    msg[7]-[11]: three ``kubectl_read`` calls copied from the grafted
    inject history were rejected — the model spent a full turn inferring
    "the tool set changed" on its own because the feedback said "do not
    guess" while the model was not guessing, it was reusing calls it had
    seen).

    Additive only: the pre-existing clauses (including the
    task-ce9647931-driven "do not substitute" wording) are preserved
    verbatim; the new sentence states the three semantic elements —
    history provenance, phase ownership, current-binding authority.
    """

    def _rewrite(self, requested: str = "kubectl_read") -> str:
        err = Exception(
            f"Error: {requested} is not a valid tool, try one of [kubectl]."
        )
        return _phase2_handle_tool_error(err)

    def test_existing_clauses_preserved_verbatim(self):
        """The pre-existing safety clauses survive the addition untouched."""
        out = self._rewrite()
        assert "does not exist in this phase" in out
        assert "No tool ran and nothing changed" in out
        assert "do not guess at other tool names" in out
        assert "say so in plain text" in out
        assert "do not substitute a different tool" in out

    def test_history_provenance_element_present(self):
        """Element 1: the name may come from the conversation history."""
        out = self._rewrite()
        assert "conversation history" in out

    def test_phase_ownership_element_present(self):
        """Element 2: history tools belong to an earlier phase's surface."""
        out = self._rewrite()
        assert "earlier phase" in out
        assert "tool surface" in out

    def test_current_binding_authority_element_present(self):
        """Element 3: the currently bound tools are the authority."""
        out = self._rewrite()
        assert "currently bound" in out
        assert "authority" in out

    def test_genuine_errors_still_pass_through(self):
        """The attribution sentence must NOT leak into pass-through errors."""
        err = Exception(
            "Error executing tool 'kubectl' with kwargs {} "
            "with error:\n node not found\n"
        )
        out = _phase2_handle_tool_error(err)
        assert "conversation history" not in out
        assert "node not found" in out
