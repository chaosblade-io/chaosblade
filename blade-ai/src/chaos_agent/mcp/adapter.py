"""MCP tool → LangChain StructuredTool adapter.

Key simplification: ``StructuredTool.args_schema`` accepts a dict
form (JSON Schema) natively — we don't need to generate Pydantic.
This eliminates ~150 lines of conversion code AND removes the
Pydantic v1/v2 compatibility headache that ``factory.py`` already
has to patch for langchain-openai's reasoning_content quirk.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

from langchain_core.tools import StructuredTool

from chaos_agent.mcp.client import McpClient, McpToolDescriptor

logger = logging.getLogger(__name__)

# OpenAI tool name max length is 64 characters.
_OPENAI_TOOL_NAME_MAX = 64
# When truncating, leave room for "_" separator + 6-char hash suffix.
_TRUNCATE_HASH_LEN = 6
# OpenAI tool name allowed character set. MCP server names come from
# mcp.json dict keys and are user-chosen (k8s-docs, auth@v2, corp.fin.kb,
# ...); anything outside [a-zA-Z0-9_-] would make the OpenAI API reject
# the tool definition and silently hide the whole server's tools from
# the LLM. We collapse runs of invalid characters into a single ``_``.
_OPENAI_NAME_INVALID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _sanitize_part(name: str) -> str:
    """Replace any run of OpenAI-disallowed chars with a single ``_``.

    Empty result (only invalid chars) falls back to ``_`` so the
    resulting joined name still parses.
    """
    out = _OPENAI_NAME_INVALID_RE.sub("_", name)
    return out or "_"


def _safe_tool_name(server_name: str, tool_name: str) -> str:
    """``{sanitized_server}__{sanitized_tool}`` if it fits in 64 chars;
    otherwise truncate to 57 chars and append ``_{6-hex-of-md5}`` to
    keep uniqueness even after truncation.

    Character sanitisation happens BEFORE length check so the hash is
    computed over the post-sanitisation joined name (deterministic +
    OpenAI-safe). Two distinct ``(server, tool)`` pairs always produce
    different final names, even if their sanitised forms collide on
    prefix.
    """
    safe_server = _sanitize_part(server_name)
    safe_tool = _sanitize_part(tool_name)
    joined = f"{safe_server}__{safe_tool}"
    if len(joined) <= _OPENAI_TOOL_NAME_MAX:
        return joined
    # Hash the post-sanitisation joined name for uniqueness
    h = hashlib.md5(joined.encode()).hexdigest()[:_TRUNCATE_HASH_LEN]
    # Reserve "_" + hash = 7 chars; truncate body to fit
    body_max = _OPENAI_TOOL_NAME_MAX - 1 - _TRUNCATE_HASH_LEN  # 57
    return f"{joined[:body_max]}_{h}"


def make_langchain_tool(
    client: McpClient,
    descriptor: McpToolDescriptor,
    timeout_seconds: int,
) -> StructuredTool:
    """Wrap an MCP tool descriptor as a LangChain StructuredTool.

    The coroutine catches all exceptions and returns error text as
    the tool result (rather than raising) so the LLM can recover by
    trying another approach. Timeout per call is enforced via
    ``asyncio.wait_for``.
    """
    full_name = _safe_tool_name(client.name, descriptor.name)
    mcp_name = descriptor.name  # name as the server knows it

    async def _coroutine(**kwargs: Any) -> str:
        try:
            return await asyncio.wait_for(
                client.call_tool(mcp_name, kwargs),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "mcp tool %s timed out after %ds", full_name, timeout_seconds
            )
            return f"[tool timeout] {full_name} did not respond within {timeout_seconds}s"
        except Exception as e:
            logger.exception("mcp tool %s call failed", full_name)
            return f"[tool error] {type(e).__name__}: {e}"

    # StructuredTool accepts dict form of JSON Schema for args_schema.
    # If the schema is empty / missing properties, pass an empty dict
    # so LangChain treats the tool as no-arg.
    schema = descriptor.input_schema or {"type": "object", "properties": {}}

    return StructuredTool(
        name=full_name,
        description=descriptor.description or f"MCP tool {full_name}",
        args_schema=schema,
        coroutine=_coroutine,
    )
