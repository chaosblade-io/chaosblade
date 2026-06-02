"""Tests for chaos_agent.l4.runtime_shim — NullRuntime."""

from chaos_agent.l4.runtime_shim import NullRuntime


class TestNullRuntime:
    """Test NullRuntime no-op behavior."""

    def test_step_returns_context_manager(self):
        rt = NullRuntime()
        ctx = rt.step("test_step")
        assert hasattr(ctx, "__enter__")
        assert hasattr(ctx, "__exit__")

    def test_step_context_manager_works(self):
        rt = NullRuntime()
        with rt.step("inject", attrs={"k": "v"}) as s:
            s.attrs["result"] = "ok"
        assert s.attrs["result"] == "ok"

    def test_step_instances_isolated(self):
        """Each step() call returns a fresh context with its own attrs dict."""
        rt = NullRuntime()
        s1 = rt.step("a")
        s2 = rt.step("b")
        s1.__enter__()
        s1.attrs["x"] = 1
        s2.__enter__()
        assert "x" not in s2.attrs

    def test_tool_execute(self):
        rt = NullRuntime()
        result = rt.tool.execute("blade_create", {"cmd": "test"})
        assert result.status == "ok"
        assert result.payload == {}

    def test_heal_returns_not_healed(self):
        rt = NullRuntime()
        result = rt.heal("step_result", error_class="tool_error")
        assert result.healed is False

    def test_require_approval_auto_approves(self):
        rt = NullRuntime()
        assert rt.require_approval(risk_level="high") is True

    def test_finish_is_noop(self):
        rt = NullRuntime()
        assert rt.finish(status="passed") is None

    def test_trajectory_is_none(self):
        rt = NullRuntime()
        assert rt.trajectory is None
