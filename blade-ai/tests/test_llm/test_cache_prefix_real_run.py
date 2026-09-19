"""Optional key-gated REAL-run cache-hit assertion (design D5, task 1.8).

Why this file exists
--------------------
The mock-layer prefix-stability guards (``tests/test_agent/prompts/
test_prefix_stability.py``) prove the request HEAD is byte-identical across
adjacent rounds — but a byte-stable prefix only *enables* a provider cache
hit, it does not *prove* one happens. Real hit rate depends on server-side
block granularity (DashScope caches prefixes ≥ ~256 tokens) and TTL, which
no mock can exercise.

So this is the end-to-end confidence complement: it fires TWO adjacent real
calls that share a long stable prefix and asserts the second call reports
``cache_read > 0``. It is gated behind a real API key and SKIPS by default —
mirroring the deepseek ``describe.skipIf(!process.env.DEEPSEEK_API_KEY)``
pattern — so CI stays green with no key and never blocks on the network.

To run it locally::

    BLADE_AI_LLM_API_KEY=sk-real pytest \
        tests/test_llm/test_cache_prefix_real_run.py -v -s
"""

from __future__ import annotations

import os

import pytest

# Gate: a REAL key must be present. An absent / empty / obvious-placeholder
# key skips the whole module — CI has no key, so this never runs there.
_API_KEY = os.getenv("BLADE_AI_LLM_API_KEY", "").strip()
_HAS_REAL_KEY = bool(_API_KEY) and not _API_KEY.lower().startswith(("sk-test", "test", "dummy"))

pytestmark = pytest.mark.skipif(
    not _HAS_REAL_KEY,
    reason="key-gated real run: set BLADE_AI_LLM_API_KEY to a real key to exercise "
    "the live provider cache-hit path (default skip keeps CI green / offline).",
)

# A stable prefix LONG enough to clear the provider's minimum cacheable block
# (DashScope ≈ 256 tokens). Padding with deterministic filler keeps it byte
# identical across the two calls; only the short user tail varies, which is
# exactly the append-only shape the production execute/verify loops present.
_STABLE_SYSTEM = (
    "You are a deterministic cache-probe assistant. Answer with a single word.\n"
    + ("The quick brown fox jumps over the lazy dog. " * 80)
)


def _cache_read(response) -> int:
    """Pull prompt-cache-hit tokens off a LangChain response via the same
    authoritative extractor production uses (tracer._cache_read_from_usage_metadata)."""
    from chaos_agent.observability.tracer import _cache_read_from_usage_metadata

    um = getattr(response, "usage_metadata", None) or {}
    return _cache_read_from_usage_metadata(um)[0]


@pytest.mark.network
def test_second_adjacent_call_hits_provider_cache():
    """Two calls sharing a long stable prefix → the 2nd reports cache_read>0."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from chaos_agent.agent.factory import make_llm

    llm = make_llm()

    # First call warms the provider-side cache for the shared prefix.
    first = llm.invoke(
        [SystemMessage(content=_STABLE_SYSTEM), HumanMessage(content="Reply: warm")]
    )
    # The warm call itself may or may not report a hit (cold on first sight);
    # we only require it to have a measurable prompt so the prefix is real.
    assert (getattr(first, "usage_metadata", None) or {}).get("input_tokens", 0) > 0

    # Second call re-presents the IDENTICAL prefix with only the tail changed.
    second = llm.invoke(
        [SystemMessage(content=_STABLE_SYSTEM), HumanMessage(content="Reply: hot")]
    )

    cached = _cache_read(second)
    assert cached > 0, (
        "expected a provider cache hit on the second adjacent call sharing a "
        f"long stable prefix, got cache_read={cached}. Either the prefix is "
        "below the provider's minimum cacheable block, the vendor's cache "
        "field name is not probed by _cache_read_from_usage_metadata, or the "
        "provider does not cache this model — inspect usage_metadata."
    )
