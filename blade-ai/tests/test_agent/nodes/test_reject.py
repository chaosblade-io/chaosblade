"""Tests for reject node."""

import pytest

from chaos_agent.agent.nodes.gates.reject import reject


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
    async def test_safety_reason_takes_precedence_over_stale_error(self):
        # W-56-5 (defect c): safety_reason is written by the gate that routed
        # here THIS time; outcome.error may be a PREVIOUS attempt's residue.
        # #56 rendered a stale "drift_terminated" (attempt 1) next to attempt
        # 2's own blast-radius reason — single-source attribution fixes that.
        state = {
            "safety_reason": (
                "Cluster-wide blast radius: execution will mutate resources "
                "beyond the target scope."
            ),
            "error": (
                "drift_terminated: Repeated target drift terminated the run "
                "without human confirmation"
            ),
        }

        result = await reject(state)
        assert result["result"]["reason"].startswith("Cluster-wide blast radius")
        assert result["error"].startswith("Cluster-wide blast radius")

    @pytest.mark.asyncio
    async def test_error_fallback_when_no_safety_reason(self):
        # No gate reason available → the error slot is still the honest
        # fallback (e.g. agent-loop terminal conclusions).
        state = {"safety_reason": None, "error": "Loop exceeded"}

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
        assert "failure_detail" in result
        assert set(result["result"].keys()) == {"status", "reason"}
        assert set(result.keys()) == {"result", "error", "finished_at", "failure_detail"}
        # failure_detail should contain a categorized reason
        assert result["failure_detail"]["category"] == "safety_rejected"

    @pytest.mark.asyncio
    async def test_user_rejected_message(self):
        state = {"safety_reason": "User rejected the execution"}

        result = await reject(state)
        assert "User rejected" in result["result"]["reason"]

    @pytest.mark.asyncio
    async def test_safety_reason_with_error_none(self):
        """When error is None, reject still surfaces the safety reason."""
        state = {"safety_reason": "Blacklisted", "error": None}

        result = await reject(state)
        assert result["result"]["reason"] == "Blacklisted"

    @pytest.mark.asyncio
    async def test_empty_safety_reason_with_error(self):
        state = {"safety_reason": "", "error": "Something went wrong"}

        result = await reject(state)
        assert result["result"]["reason"] == "Something went wrong"
