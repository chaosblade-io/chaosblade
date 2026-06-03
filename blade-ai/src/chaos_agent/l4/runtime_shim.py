"""NullRuntime — no-op runtime for standalone testing.

When blade-ai runs in TUI/CLI/Server mode (without ai-testing-platform),
NullRuntime ensures all runtime.step()/tool.execute()/finish() calls
are safely absorbed without side effects.
"""

from __future__ import annotations


class NullRuntime:
    """No-op runtime. All methods are safe no-ops."""

    class _StepCtx:
        def __init__(self) -> None:
            self.attrs: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def step(self, name: str, attrs: dict | None = None) -> _StepCtx:
        return self._StepCtx()

    class tool:
        @staticmethod
        def execute(name: str, params: dict | None = None, **kw):
            return type("ToolResult", (), {"status": "ok", "payload": {}})()

    def heal(self, *a, **kw):
        return type("HealResult", (), {"healed": False})()

    def require_approval(self, risk_level: str = "high") -> bool:
        return True  # Test mode: auto-approve

    def emit_event(self, event_type: str, data: dict) -> None:
        pass

    def finish(self, status: str = "passed") -> None:
        return None

    trajectory = None
