"""Tests for reject node."""

import pytest

from chaos_agent.agent.nodes.reject import reject


class TestReject:
    """Tests for the reject node function.

    reject() uses:
        reason = state.get("safety_reason", "Unknown reason")
        error_val = state.get("error", reason)
    If state["error"] exists (even as None), it takes precedence over reason.
    """

    @pytest.mark.asyncio
    async def test_reject_with_safety_reason_no_error(self):
        """When safety_reason is set and error key is absent."""
        state = {"safety_reason": "Namespace 'kube-system' is blacklisted"}

        result = await reject(state)
        assert result["result"]["status"] == "rejected"
        assert "blacklist" in result["result"]["reason"]

    @pytest.mark.asyncio
    async def test_reject_with_error(self):
        state = {"safety_reason": None, "error": "Agent loop exceeded max iterations (10)"}

        result = await reject(state)
        assert result["result"]["status"] == "rejected"
        assert "exceeded" in result["result"]["reason"]
        assert result["error"] == "Agent loop exceeded max iterations (10)"

    @pytest.mark.asyncio
    async def test_error_takes_precedence_over_safety_reason(self):
        state = {"safety_reason": "Blacklisted namespace", "error": "Loop exceeded"}

        result = await reject(state)
        assert result["result"]["reason"] == "Loop exceeded"
        assert result["error"] == "Loop exceeded"

    @pytest.mark.asyncio
    async def test_reject_with_no_reason(self):
        state = {}

        result = await reject(state)
        assert result["result"]["status"] == "rejected"
        assert result["result"]["reason"] == "Unknown reason"

    @pytest.mark.asyncio
    async def test_result_structure(self):
        state = {"safety_reason": "Test reason"}

        result = await reject(state)
        assert "result" in result
        assert "error" in result
        assert "failure_reason" in result
        assert set(result["result"].keys()) == {"status", "reason"}
        assert set(result.keys()) == {"result", "error", "finished_at", "failure_reason"}
        # failure_reason should contain a categorized reason
        assert result["failure_reason"].startswith("safety_rejected:")

    @pytest.mark.asyncio
    async def test_user_rejected_message(self):
        state = {"safety_reason": "User rejected the execution"}

        result = await reject(state)
        assert "User rejected" in result["result"]["reason"]

    @pytest.mark.asyncio
    async def test_safety_reason_with_error_none(self):
        """When safety_reason is set but error is explicitly None."""
        state = {"safety_reason": "Blacklisted", "error": None}

        result = await reject(state)
        # state.get("error", "Blacklisted") returns None (key exists with value None)
        assert result["result"]["reason"] is None

    @pytest.mark.asyncio
    async def test_empty_safety_reason_with_error(self):
        state = {"safety_reason": "", "error": "Something went wrong"}

        result = await reject(state)
        assert result["result"]["reason"] == "Something went wrong"
