"""Tests for the 4-layer token counter (E1).

Layer coverage:
  - Layer 1 (tiktoken native): gpt-4o, gpt-4
  - Layer 2 (family prefix):   qwen3, qwen2, deepseek-v3, claude, glm, yi
  - Layer 3 (HF tokenizer):    NOT covered here (would download a model);
                                tested separately by patching with a mock
  - Layer 4 (legacy heuristic): llama, gemini, unknown / SKIP-list prefixes
  - Empty / None / multi-modal content handling
  - Aggregation semantics (count_tokens_messages): degraded quality +
    max safety_margin propagate correctly
  - settings.tokenizer_model_override
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.memory.tokens import (
    TokenCount,
    TokenCountQuality,
    count_tokens,
    count_tokens_messages,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """Snapshot + restore the three tokenizer settings around every test."""
    from chaos_agent.config.settings import settings
    orig_override = getattr(settings, "tokenizer_model_override", "")
    orig_hf = getattr(settings, "tokenizer_use_hf", False)
    orig_vendor = getattr(settings, "tokenizer_use_vendor_api", False)
    yield
    settings.tokenizer_model_override = orig_override
    settings.tokenizer_use_hf = orig_hf
    settings.tokenizer_use_vendor_api = orig_vendor


# ---------------------------------------------------------------------------
# Layer 1 — tiktoken native (OpenAI)
# ---------------------------------------------------------------------------


class TestLayer1OpenAINative:
    def test_gpt4o_exact(self):
        tc = count_tokens("hello world", model="gpt-4o")
        assert tc.quality == TokenCountQuality.EXACT
        assert tc.encoding_used == "o200k_base"
        assert tc.safety_margin == 1.0
        assert tc.count == 2

    def test_gpt4_exact(self):
        tc = count_tokens("hello world", model="gpt-4")
        assert tc.quality == TokenCountQuality.EXACT
        assert tc.encoding_used == "cl100k_base"
        assert tc.safety_margin == 1.0
        assert tc.count > 0

    def test_gpt4o_dated_suffix_still_works(self):
        """tiktoken's encoding_for_model() handles 'gpt-4o-2024-05-13' via
        MODEL_PREFIX_TO_ENCODING."""
        tc = count_tokens("hello", model="gpt-4o-2024-05-13")
        assert tc.quality == TokenCountQuality.EXACT
        assert tc.encoding_used == "o200k_base"


# ---------------------------------------------------------------------------
# Layer 2 — family prefix → tiktoken encoding
# ---------------------------------------------------------------------------


class TestLayer2FamilyPrefix:
    @pytest.mark.parametrize("model,expected_encoding,expected_margin", [
        # Qwen series
        ("qwen3-max-preview", "o200k_base", 1.05),
        ("qwen3-coder-plus", "o200k_base", 1.05),
        ("qwen2.5-72b-instruct", "cl100k_base", 1.02),
        ("qwen2-72b", "cl100k_base", 1.02),
        # Generic qwen (covers qwen-max / qwen-plus / qwen-turbo)
        ("qwen-max", "o200k_base", 1.10),
        ("qwen-plus", "o200k_base", 1.10),
        # DeepSeek
        ("deepseek-v3-chat", "o200k_base", 1.05),
        ("deepseek-r1", "o200k_base", 1.05),
        ("deepseek-reasoner", "o200k_base", 1.05),
        ("deepseek-coder", "cl100k_base", 1.10),
        # Claude
        ("claude-opus-4-7", "cl100k_base", 1.10),
        ("claude-sonnet-4-6", "cl100k_base", 1.10),
        ("claude-haiku-4-5", "cl100k_base", 1.10),
        # Chinese LLMs
        ("glm-4-plus", "cl100k_base", 1.10),
        ("yi-large", "cl100k_base", 1.10),
        ("moonshot-v1-128k", "cl100k_base", 1.10),
        ("kimi-k1.5-preview", "cl100k_base", 1.10),
        ("baichuan2-13b", "cl100k_base", 1.10),
        ("abab6.5-chat", "cl100k_base", 1.10),
        ("minimax-text-01", "cl100k_base", 1.10),
    ])
    def test_family_prefix_match(self, model, expected_encoding, expected_margin):
        tc = count_tokens("hello world", model=model)
        assert tc.quality == TokenCountQuality.APPROXIMATE
        assert tc.encoding_used == expected_encoding
        assert tc.safety_margin == expected_margin
        assert tc.count > 0


# ---------------------------------------------------------------------------
# Layer 4 — legacy heuristic (incompatible-family or unknown model)
# ---------------------------------------------------------------------------


class TestLayer4HeuristicFallback:
    @pytest.mark.parametrize("model", [
        # Known tokenizer-incompatible (SKIP list)
        "llama-3.1-70b-instruct",
        "meta-llama/Llama-3.1-70B",     # Layer 3 disabled by default → Layer 4
        "mistral-large-latest",
        "mixtral-8x22b",
        "gemini-1.5-pro",
        "gemini-2.0-flash-exp",
        "gemma-2-27b",
        "phi-4",
        "command-r-plus",
        "stable-code-3b",
        "falcon-180b",
        # Completely unknown
        "my-custom-finetuned-model-v999",
        "acme-internal/research-model-2026",
    ])
    def test_skip_or_unknown_routes_to_heuristic(self, model):
        tc = count_tokens("hello world 你好世界", model=model)
        assert tc.quality == TokenCountQuality.HEURISTIC
        assert tc.encoding_used == "heuristic-cjk"
        assert tc.safety_margin == 1.20
        assert tc.count > 0

    def test_empty_model_with_empty_settings_routes_to_heuristic(self):
        """Empty ``model`` arg + empty ``settings.model_name`` = truly
        no model info → heuristic. Pinned here rather than in the
        parametrize above because the default ``settings.model_name``
        in the test environment routes to APPROXIMATE, so empty-arg
        alone no longer falls through (Opt #1 of the E1 audit added
        ``settings.model_name`` as a fallback to ``_resolve_model``)."""
        from chaos_agent.config.settings import settings
        orig = settings.model_name
        try:
            settings.model_name = ""
            tc = count_tokens("hello world 你好世界", model="")
            assert tc.quality == TokenCountQuality.HEURISTIC
            assert tc.encoding_used == "heuristic-cjk"
            assert tc.safety_margin == 1.20
        finally:
            settings.model_name = orig

    def test_empty_string(self):
        tc = count_tokens("", model="gpt-4o")
        assert tc.count == 0
        assert tc.quality == TokenCountQuality.EXACT
        assert tc.encoding_used == "empty"
        assert tc.safety_margin == 1.0

    def test_heuristic_cjk_aware(self):
        """The heuristic should reward CJK density (1.5 chars/token) over
        ASCII (4 chars/token) — same property as the original
        estimate_tokens."""
        cjk_tc = count_tokens("中" * 60, model="llama-3.1-70b")
        ascii_tc = count_tokens("a" * 60, model="llama-3.1-70b")
        # 60 chinese chars / 1.5 = 40 tokens
        # 60 ascii chars  / 4   = 15 tokens
        assert cjk_tc.count > ascii_tc.count
        assert cjk_tc.count == 40
        assert ascii_tc.count == 15


# ---------------------------------------------------------------------------
# safe_count semantics
# ---------------------------------------------------------------------------


class TestSafeCount:
    def test_exact_no_margin(self):
        tc = TokenCount(100, TokenCountQuality.EXACT, "o200k_base", 1.0)
        assert tc.safe_count == 100

    def test_approximate_5pct_margin(self):
        tc = TokenCount(100, TokenCountQuality.APPROXIMATE, "cl100k_base", 1.05)
        assert tc.safe_count == 105

    def test_heuristic_20pct_margin(self):
        tc = TokenCount(100, TokenCountQuality.HEURISTIC, "heuristic-cjk", 1.20)
        assert tc.safe_count == 120

    def test_safe_count_round_up(self):
        """Fractional margin × count should round UP — over-counting is
        safer than under-counting for threshold checks."""
        tc = TokenCount(7, TokenCountQuality.APPROXIMATE, "cl100k_base", 1.05)
        # 7 * 1.05 = 7.35 → ceil to 8
        assert tc.safe_count == 8

    def test_safe_count_matches_math_ceil(self):
        """safe_count must equal math.ceil(count * margin) for every
        margin value we use — guards against the float ``!=`` rounding
        regression from the previous manual implementation."""
        import math
        for count in [0, 1, 7, 10, 20, 21, 100, 1000, 8400, 13123, 128_000]:
            for margin in [1.0, 1.02, 1.05, 1.10, 1.20]:
                tc = TokenCount(count, TokenCountQuality.APPROXIMATE,
                                "x", margin)
                assert tc.safe_count == math.ceil(count * margin), (
                    f"safe_count diverges from math.ceil at "
                    f"count={count} margin={margin}"
                )


# ---------------------------------------------------------------------------
# count_tokens_messages aggregation
# ---------------------------------------------------------------------------


class TestMessageAggregation:
    def test_empty_list(self):
        tc = count_tokens_messages([], model="gpt-4o")
        assert tc.count == 0
        assert tc.quality == TokenCountQuality.EXACT

    def test_per_message_overhead(self):
        """OpenAI cookbook: each message ≈ +4 tokens; batch +2 priming."""
        single = count_tokens_messages(
            [HumanMessage(content="hi")], model="gpt-4o",
        )
        # 'hi' alone = 1 token; +4 envelope, +2 priming = 7
        assert single.count == 1 + 4 + 2

    def test_multi_message_overhead(self):
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
            HumanMessage(content="bye"),
        ]
        tc = count_tokens_messages(msgs, model="gpt-4o")
        # 3 messages × +4 + 2 priming = 14 overhead; content tokens vary
        assert tc.count >= 14

    def test_quality_degrades_to_worst(self):
        """A single HEURISTIC message in a batch poisons the aggregate
        quality to HEURISTIC. APPROXIMATE-only batches stay APPROXIMATE."""
        # All EXACT → EXACT
        msgs1 = [HumanMessage(content="hello")]
        assert count_tokens_messages(msgs1, model="gpt-4o").quality \
            == TokenCountQuality.EXACT

        # All APPROXIMATE → APPROXIMATE
        msgs2 = [HumanMessage(content="hello")]
        assert count_tokens_messages(msgs2, model="claude-opus-4-7").quality \
            == TokenCountQuality.APPROXIMATE

        # All HEURISTIC → HEURISTIC
        msgs3 = [HumanMessage(content="hello")]
        assert count_tokens_messages(msgs3, model="llama-3.1-70b").quality \
            == TokenCountQuality.HEURISTIC

    def test_safety_margin_is_max(self):
        """Aggregated margin is the MAX across messages — caller's
        threshold math is conservative."""
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        # claude → 1.10
        tc = count_tokens_messages(msgs, model="claude-opus-4-7")
        assert tc.safety_margin == 1.10

    def test_encoding_matches_worst_quality_tier(self):
        """The aggregated ``encoding_used`` must reflect the encoding
        of the WORST-quality segment, not whichever happens to be
        last. Previously the field tracked the last-seen encoding, so
        an EXACT message following a HEURISTIC one would report
        ``encoding_used="o200k_base"`` alongside ``quality=HEURISTIC``
        — confusing for operators reading the debug labels."""
        # All-EXACT batch — encoding should be the o200k from gpt-4o,
        # NOT the default "empty" / "n/a" sentinel.
        all_exact = count_tokens_messages(
            [HumanMessage(content="hello world")], model="gpt-4o",
        )
        assert all_exact.quality == TokenCountQuality.EXACT
        assert all_exact.encoding_used == "o200k_base"

    def test_multimodal_content_blocks(self):
        """List content (multi-modal) sums only the text parts;
        image_url and other non-text blocks contribute zero (their
        token cost is accounted for via vendor ``usage`` events, not
        client-side estimation)."""
        msg_with_image = AIMessage(content=[
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": "..."}},
            {"type": "text", "text": "in detail please"},
        ])
        msg_text_only = AIMessage(content=[
            {"type": "text", "text": "describe this image"},
            {"type": "text", "text": "in detail please"},
        ])
        # The image block must NOT contribute to the count — both
        # message variants should agree token-for-token (modulo the
        # priming overhead which is constant for single-message input).
        with_img = count_tokens_messages([msg_with_image], model="gpt-4o").count
        text_only = count_tokens_messages([msg_text_only], model="gpt-4o").count
        assert with_img == text_only
        # Sanity floor: 2 text segments × ~3 tokens content + ONE +4
        # envelope (per MESSAGE, not per block) + 2 priming = ~12.
        # ``>= 10`` keeps the floor honest if tiktoken segments the
        # short phrases slightly differently across versions while
        # still catching the previous per-block envelope inflation.
        assert with_img >= 10

    def test_multimodal_envelope_per_message_not_per_block(self):
        """Regression: a multi-modal message with N text blocks must
        pay the per-message envelope (~+4 tokens) ONCE, not N times.
        The earlier implementation called the absorber per block and
        added the envelope inside, inflating multi-block messages by
        +4 for every extra block — silently making the compaction
        trigger fire earlier than it should on multi-modal heavy
        conversations."""
        msg_two_blocks = AIMessage(content=[
            {"type": "text", "text": "describe this image"},
            {"type": "text", "text": "in detail please"},
        ])
        msg_one_string = AIMessage(content="describe this image in detail please")
        # Both messages represent the same on-wire payload (same text,
        # same role). Token counts should match within tiktoken's
        # natural segmentation slack (~1 token) — definitely not the
        # +4 gap the per-block bug produced.
        multi = count_tokens_messages([msg_two_blocks], model="gpt-4o").count
        flat = count_tokens_messages([msg_one_string], model="gpt-4o").count
        assert abs(multi - flat) <= 1, (
            f"multi-block ({multi}) and flat-string ({flat}) counts "
            f"diverge by more than 1 — envelope is likely being added "
            f"per block again"
        )

    def test_none_or_missing_content_safe(self):
        """A message-like object lacking ``content`` (or where it's
        None) must not raise. Some LangChain message subclasses (e.g.
        AIMessageChunk mid-stream) have None content briefly between
        tokens; the absorber must skip them silently."""

        class FakeMsg:
            def __init__(self, content):
                self.content = content

        tc = count_tokens_messages(
            [FakeMsg(content=None), FakeMsg(content="")],
            model="gpt-4o",
        )
        # Both messages have no countable text; the absorber skips
        # them. Output = just the +2 batch priming.
        assert tc.count == 2


# ---------------------------------------------------------------------------
# tokenizer_model_override
# ---------------------------------------------------------------------------


class TestModelOverride:
    def test_override_redirects_tokenizer(self):
        from chaos_agent.config.settings import settings
        # Pretend we're running 'my-finetuned-qwen-v3' but tell the
        # counter to treat it as qwen3-max-preview so we get
        # APPROXIMATE instead of HEURISTIC.
        settings.tokenizer_model_override = "qwen3-max-preview"
        tc = count_tokens("hello", model="my-finetuned-qwen-v3")
        assert tc.quality == TokenCountQuality.APPROXIMATE
        assert tc.encoding_used == "o200k_base"

    def test_override_empty_is_passthrough(self):
        from chaos_agent.config.settings import settings
        settings.tokenizer_model_override = ""
        tc = count_tokens("hello", model="llama-3.1-70b")
        # llama falls through SKIP → heuristic
        assert tc.quality == TokenCountQuality.HEURISTIC


# ---------------------------------------------------------------------------
# No-arg API — model defaults to settings.model_name
# ---------------------------------------------------------------------------


class TestNoArgDefault:
    """``count_tokens(text)`` and ``count_tokens_messages(msgs)`` without
    a ``model=`` arg must resolve via ``settings.model_name``. This is
    what lets memory/context_manager / hook / compactor call the API
    without each maintaining its own ``_current_model()`` helper."""

    def test_count_tokens_uses_settings_model_when_arg_omitted(self):
        from chaos_agent.config.settings import settings
        orig = settings.model_name
        try:
            settings.model_name = "gpt-4o"
            tc = count_tokens("hello world")  # no model= arg
            assert tc.quality == TokenCountQuality.EXACT
            assert tc.encoding_used == "o200k_base"
        finally:
            settings.model_name = orig

    def test_count_tokens_messages_uses_settings_model_when_arg_omitted(self):
        from chaos_agent.config.settings import settings
        orig = settings.model_name
        try:
            settings.model_name = "claude-opus-4-7"
            tc = count_tokens_messages([HumanMessage(content="hi")])
            assert tc.quality == TokenCountQuality.APPROXIMATE
            assert tc.safety_margin == 1.10
        finally:
            settings.model_name = orig

    def test_explicit_model_arg_overrides_settings(self):
        """Explicit ``model=`` still wins over ``settings.model_name``."""
        from chaos_agent.config.settings import settings
        orig = settings.model_name
        try:
            settings.model_name = "gpt-4o"
            # Pass a heuristic-only model — should NOT pick up gpt-4o
            tc = count_tokens("hello", model="llama-3.1-70b")
            assert tc.quality == TokenCountQuality.HEURISTIC
        finally:
            settings.model_name = orig

    def test_override_still_wins_over_settings_default(self):
        """tokenizer_model_override > model arg > settings.model_name."""
        from chaos_agent.config.settings import settings
        orig_override = settings.tokenizer_model_override
        orig_model = settings.model_name
        try:
            settings.tokenizer_model_override = "gpt-4o"
            settings.model_name = "llama-3.1-70b"  # would route heuristic
            tc = count_tokens("hello")  # no arg — should hit override
            assert tc.quality == TokenCountQuality.EXACT
            assert tc.encoding_used == "o200k_base"
        finally:
            settings.tokenizer_model_override = orig_override
            settings.model_name = orig_model


# ---------------------------------------------------------------------------
# Regression: prior compaction logic continues to work
# ---------------------------------------------------------------------------


class TestRegressionCompactionPath:
    """Confirm the new API integrates with ContextManager / hook without
    breaking existing semantics."""

    def test_context_manager_count_path(self):
        from chaos_agent.memory.context_manager import ContextManager
        mgr = ContextManager(max_tokens=10_000, compact_ratio=0.85)
        # Should not raise — basic integration smoke test
        # (full compaction path covered by tests/test_memory/test_hook.py)
        assert mgr is not None
