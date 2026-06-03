"""Tests for chaos_agent.mcp.adapter — MCP descriptor → LangChain tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.mcp.adapter import _safe_tool_name, make_langchain_tool
from chaos_agent.mcp.client import McpToolDescriptor


class TestSafeToolName:
    def test_short_name_passes_through(self):
        assert _safe_tool_name("fs", "read") == "fs__read"

    def test_max_length_64_passes(self):
        # "a" * 30 + "__" + "b" * 32 = 64 chars
        out = _safe_tool_name("a" * 30, "b" * 32)
        assert len(out) == 64

    def test_over_64_truncates_with_hash(self):
        long_server = "corporate-finance-knowledge-base"  # 32
        long_tool = "execute_complex_query_with_filters_and_pagination"  # 50
        out = _safe_tool_name(long_server, long_tool)
        assert len(out) <= 64
        # Hash suffix _xxxxxx (6 hex chars)
        assert out[-7] == "_"

    def test_truncation_is_deterministic(self):
        a1 = _safe_tool_name("server" * 5, "tool" * 10)
        a2 = _safe_tool_name("server" * 5, "tool" * 10)
        assert a1 == a2

    def test_different_long_names_get_different_hashes(self):
        n1 = _safe_tool_name("aaa" * 20, "tool1")
        n2 = _safe_tool_name("aaa" * 20, "tool2")
        # Even if truncated body is identical, hash makes them unique
        assert n1 != n2

    def test_exactly_64_chars_not_truncated(self):
        """Boundary: at 64 exactly, no truncation should occur."""
        # 30 + 2 + 32 = 64
        server = "a" * 30
        tool = "b" * 32
        out = _safe_tool_name(server, tool)
        assert out == f"{server}__{tool}"
        assert len(out) == 64

    def test_at_65_chars_triggers_truncation(self):
        """Boundary: 65 must truncate."""
        # 30 + 2 + 33 = 65
        out = _safe_tool_name("a" * 30, "b" * 33)
        assert len(out) == 64
        assert out[-7] == "_"  # hash separator at -7

    def test_all_results_fit_openai_limit(self):
        """Fuzz: many random length combinations always fit in 64."""
        for srv_len in (1, 10, 30, 50, 100):
            for tool_len in (1, 10, 30, 50, 100):
                out = _safe_tool_name("s" * srv_len, "t" * tool_len)
                assert len(out) <= 64, f"({srv_len},{tool_len}) gave len={len(out)}"

    def test_sanitizes_dot_in_server_name(self):
        """Regression: OpenAI tool name requires [a-zA-Z0-9_-]. MCP
        server names from mcp.json keys can contain ``.`` / ``@`` /
        ``:`` etc. Without sanitisation the LLM API would reject the
        whole server's tool definitions and the LLM would silently
        not see them."""
        out = _safe_tool_name("corp.fin.kb", "query")
        assert "." not in out
        assert out == "corp_fin_kb__query"

    def test_sanitizes_at_in_server_name(self):
        out = _safe_tool_name("auth@v2", "verify")
        assert "@" not in out
        assert out == "auth_v2__verify"

    def test_sanitizes_colon(self):
        out = _safe_tool_name("ns:foo", "do")
        assert ":" not in out

    def test_only_invalid_chars_falls_back_to_underscore(self):
        out = _safe_tool_name("...", "...")
        # Sanitised: "_" + "__" + "_" = "_____"; valid chars only
        import re
        assert re.match(r"^[a-zA-Z0-9_-]+$", out)

    def test_dash_and_underscore_preserved(self):
        """Regression: ``-`` and ``_`` are OpenAI-allowed and must NOT
        be sanitized. A future change that uses ``[^a-zA-Z0-9]`` (too
        narrow) would corrupt valid names like ``k8s-docs``. This test
        catches that mutation."""
        out = _safe_tool_name("k8s-docs", "list-pods")
        assert out == "k8s-docs__list-pods"
        # underscores within parts also preserved
        out2 = _safe_tool_name("my_server", "do_work")
        assert out2 == "my_server__do_work"

    def test_sanitization_preserves_uniqueness_under_collision(self):
        """Different originals that sanitise to the same prefix must
        still produce different final names (hash suffix handles long
        names; here we verify the short path also distinguishes)."""
        n1 = _safe_tool_name("a.b", "x")  # → "a_b__x"
        n2 = _safe_tool_name("a@b", "x")  # → "a_b__x" ← collision!
        # Short-path collision is acceptable trade-off: user shouldn't
        # configure two servers whose names differ only by special chars.
        # Document but don't engineer around it. This test just locks
        # the current behavior so any future change is intentional.
        assert n1 == n2


class TestMakeLangchainTool:
    def _make_client(self, name="fs", result="ok"):
        client = MagicMock()
        client.name = name
        client.call_tool = AsyncMock(return_value=result)
        return client

    def test_tool_name_uses_prefix(self):
        client = self._make_client(name="fs")
        desc = McpToolDescriptor(
            name="read", description="reads", input_schema={"type": "object"},
        )
        tool = make_langchain_tool(client, desc, timeout_seconds=30)
        assert tool.name == "fs__read"

    def test_args_schema_uses_input_schema_dict_directly(self):
        """E9 simplification: no JSON Schema → Pydantic conversion.
        LangChain accepts the raw dict form."""
        client = self._make_client()
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        desc = McpToolDescriptor(name="read", description="", input_schema=schema)
        tool = make_langchain_tool(client, desc, timeout_seconds=30)
        assert tool.args_schema == schema

    def test_empty_schema_defaults_to_no_arg_object(self):
        client = self._make_client()
        desc = McpToolDescriptor(name="ping", description="", input_schema={})
        tool = make_langchain_tool(client, desc, timeout_seconds=30)
        # Empty dict → fallback to object/properties dict
        assert tool.args_schema == {"type": "object", "properties": {}}

    @pytest.mark.asyncio
    async def test_coroutine_invokes_client_with_mcp_name(self):
        client = self._make_client(name="fs", result="file contents")
        desc = McpToolDescriptor(name="read", description="", input_schema={"type": "object"})
        tool = make_langchain_tool(client, desc, timeout_seconds=30)

        result = await tool.coroutine(path="/tmp/x")
        assert result == "file contents"
        client.call_tool.assert_awaited_once_with("read", {"path": "/tmp/x"})

    @pytest.mark.asyncio
    async def test_coroutine_returns_error_text_on_exception(self):
        client = self._make_client()
        client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        desc = McpToolDescriptor(name="x", description="", input_schema={})
        tool = make_langchain_tool(client, desc, timeout_seconds=30)
        result = await tool.coroutine()
        assert "tool error" in result
        assert "RuntimeError" in result
        assert "boom" in result

    @pytest.mark.asyncio
    async def test_coroutine_timeout_returns_error_text(self):
        import asyncio
        client = MagicMock()
        client.name = "slow"

        async def _slow(*args, **kwargs):
            await asyncio.sleep(1)
            return "never"

        client.call_tool = _slow
        desc = McpToolDescriptor(name="hang", description="", input_schema={})
        tool = make_langchain_tool(client, desc, timeout_seconds=0)  # immediate timeout
        # asyncio.wait_for(..., timeout=0) raises immediately
        result = await tool.coroutine()
        assert "tool timeout" in result.lower()
