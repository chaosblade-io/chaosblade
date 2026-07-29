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
    """Read-only. Search the public internet for external knowledge NOT
    available from local tools or skill/knowledge files.

    Last-resort lookup: prefer kubectl / blade_* / read_skill_resource /
    read_knowledge_resource FIRST; reach for web search only when the answer
    genuinely lives outside the cluster and local docs.

    When to use:
      - Decode an unfamiliar ChaosBlade / kubectl error the skill and knowledge
        files don't explain.
      - Confirm a CLI flag / parameter / API field that local references don't cover.
      - Any general external fact not obtainable from the cluster or local files.
      - Do NOT use for things local tools answer: pod/node state (use kubectl),
        skill instructions (read_skill_resource), experiment state (blade_status).
        It returns web pages, NEVER live cluster/experiment state.

    Inputs:
      - query: the search query string (be specific: include the exact error
        text, tool name, and version when relevant).
      - max_results: number of results, 1-10 (default 5; clamped to 10).

    Output: numbered results, each with title / URL / snippet; a
      "No search results found" line when nothing matched.

    Side effects: None (read-only network fetch). Sends the query to a public
      search engine — do not include secrets/kubeconfig contents in ``query``.
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
