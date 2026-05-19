"""Internet search tool for chaos engineering verification.

Provides a web search capability that agents can use when local tools and
skill files are insufficient.  The tool is deliberately placed LAST in
priority — the LLM should prefer kubectl / blade / file tools first and
only reach for web search when it genuinely needs external knowledge
(e.g. unfamiliar error messages, ChaosBlade parameter docs, etc.).
"""

import logging
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Maximum results to return — keep it small to avoid token bloat
_MAX_RESULTS = 5


@tool
async def web_search(query: str, max_results: Optional[int] = None) -> str:
    """Search the internet for information.

    This tool accesses external knowledge that is NOT available through local
    tools (kubectl, blade_status, read_skill_resource, etc.) or skill files.

    Valid use-cases (external knowledge required):
    - Unfamiliar ChaosBlade/kubectl error not explained by local skill files
    - Verifying whether a specific CLI flag or parameter is correct when
      skill references do not cover it
    - Any information that cannot be obtained from the cluster or skill files

    This tool does NOT replace local tools. It cannot:
    - Check pod/node status (use kubectl)
    - Read skill instructions (use read_skill_resource)
    - Query experiment state (use blade_status)

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (1-10, default 5).

    Returns:
        Formatted search results with titles, URLs, and snippets.
    """
    n = min(max_results or _MAX_RESULTS, 10)

    try:
        from duckduckgo_search import DDGS

        results = []
        with DDGS() as ddgs:
            for idx, r in enumerate(ddgs.text(query, max_results=n)):
                title = r.get("title", "")
                href = r.get("href", "")
                body = r.get("body", "")
                results.append(f"{idx + 1}. {title}\n   URL: {href}\n   {body}")

        if not results:
            return f"No search results found for: {query}"

        header = f"Search results for '{query}' ({len(results)} results):\n\n"
        return header + "\n\n".join(results)

    except ImportError:
        return (
            "Error: duckduckgo-search package is not installed. "
            "Install it with: pip install duckduckgo-search"
        )
    except Exception as e:
        logger.warning(f"Web search failed for query '{query}': {e}")
        return f"Web search error: {e}"
