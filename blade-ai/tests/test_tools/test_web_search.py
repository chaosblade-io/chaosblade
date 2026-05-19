"""Tests for web_search tool."""

from unittest.mock import patch, MagicMock

import pytest

from chaos_agent.tools.web_search import web_search, _MAX_RESULTS


class TestWebSearchTool:
    """Test the web_search @tool function."""

    @pytest.mark.asyncio
    async def test_successful_search(self):
        mock_results = [
            {"title": "ChaosBlade Docs", "href": "https://chaosblade.io/docs", "body": "Chaos engineering tool"},
            {"title": "K8s Docs", "href": "https://kubernetes.io/docs", "body": "Kubernetes documentation"},
        ]

        with patch("duckduckgo_search.DDGS") as MockDDGS:
            mock_ddgs = MagicMock()
            mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
            mock_ddgs.__exit__ = MagicMock(return_value=False)
            mock_ddgs.text.return_value = iter(mock_results)
            MockDDGS.return_value = mock_ddgs

            result = await web_search.ainvoke({"query": "ChaosBlade docs"})

        assert "ChaosBlade Docs" in result
        assert "https://chaosblade.io/docs" in result
        assert "2 results" in result

    @pytest.mark.asyncio
    async def test_no_results(self):
        with patch("duckduckgo_search.DDGS") as MockDDGS:
            mock_ddgs = MagicMock()
            mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
            mock_ddgs.__exit__ = MagicMock(return_value=False)
            mock_ddgs.text.return_value = iter([])
            MockDDGS.return_value = mock_ddgs

            result = await web_search.ainvoke({"query": "xyznonexistent12345"})

        assert "No search results" in result

    @pytest.mark.asyncio
    async def test_max_results_capped_at_10(self):
        with patch("duckduckgo_search.DDGS") as MockDDGS:
            mock_ddgs = MagicMock()
            mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
            mock_ddgs.__exit__ = MagicMock(return_value=False)
            mock_ddgs.text.return_value = iter([])
            MockDDGS.return_value = mock_ddgs

            await web_search.ainvoke({"query": "test", "max_results": 999})

            # Should cap at 10
            mock_ddgs.text.assert_called_once_with("test", max_results=10)

    @pytest.mark.asyncio
    async def test_default_max_results(self):
        with patch("duckduckgo_search.DDGS") as MockDDGS:
            mock_ddgs = MagicMock()
            mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
            mock_ddgs.__exit__ = MagicMock(return_value=False)
            mock_ddgs.text.return_value = iter([])
            MockDDGS.return_value = mock_ddgs

            await web_search.ainvoke({"query": "test"})

            mock_ddgs.text.assert_called_once_with("test", max_results=_MAX_RESULTS)

    @pytest.mark.asyncio
    async def test_search_error_returns_message(self):
        with patch("duckduckgo_search.DDGS", side_effect=RuntimeError("network error")):
            result = await web_search.ainvoke({"query": "test"})

        assert "Web search error" in result

    @pytest.mark.asyncio
    async def test_import_error_returns_install_hint(self):
        with patch("duckduckgo_search.DDGS", side_effect=ImportError):
            result = await web_search.ainvoke({"query": "test"})

        assert "duckduckgo-search" in result

    def test_tool_has_capability_boundary_docstring(self):
        """The docstring should describe capability boundaries, not usage timing."""
        doc = web_search.description
        assert "NOT available through" in doc
        assert "does NOT replace local tools" in doc
