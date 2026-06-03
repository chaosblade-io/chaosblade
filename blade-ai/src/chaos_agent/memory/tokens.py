"""Model-aware token counting with a 4-layer fallback chain.

Replaces the legacy ``estimate_tokens`` / ``count_tokens_approx`` /
``TOKEN_ESTIMATE_SAFETY_MARGIN`` triad in ``context_manager``. The
legacy CJK char/4 heuristic was model-blind and quietly off by up to
30% for non-OpenAI models, which meant ``PreReasoningHook`` triggered
compaction at the wrong time (premature or late) and ``/tokens`` /
cost panels showed numbers users couldn't act on.

The new design:

  1. Always returns a ``TokenCount`` dataclass — not a bare int. The
     caller knows whether the count is exact (tiktoken native /
     HuggingFace match), an approximation (cross-family BPE encoding),
     or a heuristic (legacy CJK char/4 fallback).
  2. Tags each count with a ``safety_margin`` (1.0 / 1.05 / 1.20) so
     threshold-comparing callers can multiply consistently without
     hardcoding a global "fudge factor" like the old 1.2 constant.
  3. Routes by model name through 4 layers (see ``count_tokens``):

     Layer 1: ``tiktoken.encoding_for_model(model)`` — exact, OpenAI
     Layer 2: family-prefix → best-guess tiktoken encoding + per-family
              accuracy margin (Qwen2/3, DeepSeek, Claude, GLM, Yi, …)
     Layer 3: HuggingFace ``AutoTokenizer.from_pretrained(model)`` —
              exact, for self-hosted Llama/Mistral/Gemma/etc.
              Off by default (transformers is ~50MB); enable via
              ``settings.tokenizer_use_hf``
     Layer 4: Legacy CJK-aware char/4 heuristic — model-blind, marked
              as HEURISTIC quality + 1.20 safety margin

Model families known to be tokenizer-incompatible with tiktoken
(Llama / Mistral / Gemini / Gemma / Phi / Cohere) skip Layer 1+2 and
go directly to Layer 3 (if HF tokenizer enabled) or Layer 4. Using a
tiktoken encoding for these would yield 15-30% deviation without any
quality signal — worse than the explicit HEURISTIC label.

Settings (chaos_agent.config.settings):

  - ``tokenizer_model_override`` — if set, used in place of
    ``settings.model_name`` for tokenizer selection. Useful when the
    actual model is a fine-tune of a known base ("my-internal-qwen3"
    → set override to "qwen3-max-preview" for APPROXIMATE quality).
  - ``tokenizer_use_hf`` — enable Layer 3 (HF AutoTokenizer). Pulls
    in ``transformers`` lazily on first call. Recommended only for
    self-hosted inference; vendor APIs already work via Layer 1/2.
  - ``tokenizer_use_vendor_api`` — reserved for future Layer 5
    (Anthropic client.count_tokens for exact Claude counts).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend availability — checked lazily so import always succeeds even when
# tiktoken / transformers are absent (e.g. minimal-install CI).
# ---------------------------------------------------------------------------

try:
    import tiktoken
    _HAS_TIKTOKEN = True
except ImportError:
    _HAS_TIKTOKEN = False
    tiktoken = None  # type: ignore[assignment]


_HAS_TRANSFORMERS: Optional[bool] = None  # None = not yet probed


def _check_transformers() -> bool:
    """Lazy probe for ``transformers``. Caches the result so repeated
    Layer-3 attempts don't re-do the import dance."""
    global _HAS_TRANSFORMERS
    if _HAS_TRANSFORMERS is None:
        try:
            import transformers  # noqa: F401
            _HAS_TRANSFORMERS = True
        except ImportError:
            _HAS_TRANSFORMERS = False
    return _HAS_TRANSFORMERS


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class TokenCountQuality(str, Enum):
    """Accuracy tier for a token count.

    The intended use is for callers to decide:
      - Display: show or hide a quality badge (``~``/``≈``) next to numbers.
      - Threshold logic: multiply by ``safety_margin`` so HEURISTIC counts
        skew toward "trigger compaction" rather than "skip it".
      - Cost reporting: HEURISTIC counts deserve a "±20%" disclaimer.
    """

    EXACT = "exact"               # tiktoken native / HF AutoTokenizer
    APPROXIMATE = "approximate"   # tiktoken cross-family (Qwen, DeepSeek, Claude, …)
    HEURISTIC = "heuristic"       # legacy CJK char/4 (Llama, Gemini, unknown)


# Rank used to aggregate qualities across a batch of token counts:
# higher rank = worse quality. Aggregation just keeps the max rank seen,
# which is cleaner than the if/elif chain it replaces.
_QUALITY_RANK: dict[TokenCountQuality, int] = {
    TokenCountQuality.EXACT: 0,
    TokenCountQuality.APPROXIMATE: 1,
    TokenCountQuality.HEURISTIC: 2,
}
_RANK_TO_QUALITY: dict[int, TokenCountQuality] = {
    v: k for k, v in _QUALITY_RANK.items()
}


@dataclass(frozen=True)
class TokenCount:
    """One token count + provenance.

    ``safety_margin`` is the multiplier callers SHOULD apply when
    comparing ``count`` against any threshold (compaction trigger, max-
    context-window check, "this won't fit" preflight). The value
    expresses how much slack to add to be safe given the quality tier.
    """

    count: int
    quality: TokenCountQuality
    encoding_used: str            # "o200k_base" / "cl100k_base" / "hf:org/model" / "heuristic-cjk" / "empty"
    safety_margin: float          # 1.0 / 1.05 / 1.20

    @property
    def safe_count(self) -> int:
        """``count × safety_margin`` rounded up — the value a threshold
        check SHOULD use when "false positive = wasted compaction" is
        acceptable but "false negative = context overflow" is not.

        ``math.ceil`` over a single product avoids the float ``!=``
        trap from the manual round-up that used to live here (where
        ``0.1 + 0.2 != 0.3`` style accidents could flip the result by
        one token on edge cases).
        """
        return math.ceil(self.count * self.safety_margin)


# ---------------------------------------------------------------------------
# Family-prefix table
# ---------------------------------------------------------------------------
#
# Order matters: more specific prefixes first (qwen3 before qwen,
# deepseek-v3 before deepseek). Each entry is (compiled_regex, encoding, margin):
#
#   regex     — match against lowercased model name (precompiled at module load)
#   encoding  — the tiktoken encoding we judge "closest" for this family
#   margin    — 1.0 means we believe the encoding matches exactly
#               1.05+ means we expect a small drift (cross-family BPE)
#
# Sources for the mappings:
#   - OpenAI: tiktoken's own MODEL_TO_ENCODING + MODEL_PREFIX_TO_ENCODING
#   - Qwen2/2.5: Alibaba DashScope docs explicitly note tiktoken-cl100k
#                compatibility
#   - Qwen3: 151k vocab BPE, structurally closest to o200k_base
#   - DeepSeek-V3/R1: custom BPE on HuggingFace; o200k_base is the closest
#                     tiktoken encoding (vocab size similar)
#   - Claude: Anthropic's BPE is cl100k-derived; ~5-15% drift on long text
#   - GLM (智谱) / Yi: tiktoken-cl100k compatible per their respective docs
_FAMILY_PREFIXES: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(p), enc, margin) for p, enc, margin in [
        # ---- OpenAI native --------------------------------------------
        # (kept here for transparency even though Layer 1 catches these
        # first; serves as a fallback when ``encoding_for_model`` doesn't
        # know a brand-new OpenAI suffix like ``gpt-5-2026-12-31``)
        (r"^(gpt-5|gpt-4\.5|gpt-4\.1|chatgpt-4o|gpt-4o|o[134])", "o200k_base", 1.0),
        (r"^(gpt-4|gpt-3\.5|gpt-35)", "cl100k_base", 1.0),

        # ---- Qwen series ----------------------------------------------
        (r"^qwen3", "o200k_base", 1.05),
        (r"^qwen2", "cl100k_base", 1.02),
        (r"^qwen", "o200k_base", 1.10),  # qwen-max / qwen-plus / qwen-turbo (各代不同，o200k 折衷)

        # ---- DeepSeek -------------------------------------------------
        (r"^deepseek-(v3|r1|reasoner)", "o200k_base", 1.05),
        (r"^deepseek", "cl100k_base", 1.10),

        # ---- Anthropic ------------------------------------------------
        (r"^claude", "cl100k_base", 1.10),

        # ---- 中文 LLM 同族 --------------------------------------------
        (r"^glm-", "cl100k_base", 1.10),       # 智谱
        (r"^yi-", "cl100k_base", 1.10),        # 零一万物
        (r"^moonshot|^kimi-", "cl100k_base", 1.10),
        (r"^baichuan", "cl100k_base", 1.10),
        (r"^abab|^minimax", "cl100k_base", 1.10),
    ]
]


# Prefixes for models whose tokenizers are KNOWN to be incompatible
# with any tiktoken encoding. We don't pretend by using cl100k/o200k —
# those would silently mis-count by 15-30%. Instead we jump to Layer 3
# (HF AutoTokenizer) or Layer 4 (heuristic) so the caller's quality
# tag is honest.
_SKIP_TIKTOKEN_PREFIXES: tuple[str, ...] = (
    "llama", "meta-",        # Meta SentencePiece
    "mistral", "mixtral",    # Mistral SentencePiece
    "gemini", "gemma",       # Google SentencePiece
    "phi-",                  # Microsoft Phi (custom)
    "command-",              # Cohere Command
    "stable-",               # Stability AI
    "falcon",                # TII Falcon
)


# ---------------------------------------------------------------------------
# Layer 1+2 helpers — tiktoken
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _get_tiktoken_by_name(encoding_name: str):
    """Cached ``tiktoken.get_encoding`` — encoding objects are stateless
    so caching them across all calls is safe and significantly faster."""
    if not _HAS_TIKTOKEN:
        return None
    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception as e:
        logger.warning("tiktoken.get_encoding(%s) failed: %s", encoding_name, e)
        return None


@lru_cache(maxsize=32)
def _get_tiktoken_for_model(model: str):
    """Layer 1 only: tiktoken's native ``encoding_for_model`` (OpenAI).
    Returns ``None`` on KeyError; caller should fall through to Layer 2."""
    if not _HAS_TIKTOKEN or not model:
        return None
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return None
    except Exception as e:
        logger.warning("tiktoken.encoding_for_model(%s) error: %s", model, e)
        return None


# ---------------------------------------------------------------------------
# Layer 3 helper — HuggingFace transformers (optional)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _get_hf_tokenizer(model_id: str):
    """Lazy load + cache an HF tokenizer.

    Only called when ``settings.tokenizer_use_hf`` is True. Returns
    ``None`` on any failure (transformers missing, network error
    fetching repo, model_id invalid) so callers fall through to Layer
    4 — there is no scenario where this should raise.
    """
    if not _check_transformers():
        return None
    try:
        from transformers import AutoTokenizer  # type: ignore[import-not-found]
        return AutoTokenizer.from_pretrained(model_id, trust_remote_code=False)
    except Exception as e:
        logger.warning("HF AutoTokenizer.from_pretrained(%s) failed: %s", model_id, e)
        return None


# ---------------------------------------------------------------------------
# Layer 4 — legacy CJK-aware heuristic (the ONLY model-blind tier)
# ---------------------------------------------------------------------------
#
# Replaces the public ``estimate_tokens`` that used to live in
# context_manager. Kept private here because callers should always go
# through ``count_tokens`` so the quality tag travels with the number.

_CJK_CHARS_PER_TOKEN = 1.5
_ASCII_CHARS_PER_TOKEN = 4


def _is_cjk(ch: str) -> bool:
    """True if ``ch`` is in a CJK or CJK-adjacent Unicode block.

    Covers Han ideographs plus the fullwidth punctuation ranges that
    BPE tokenizers (cl100k / o200k / Qwen-BPE) all tokenize at
    codepoint density. Without including fullwidth punctuation,
    realistic Chinese system prompts deviated >30% from tiktoken
    because chars like ``，：；`` got counted at ASCII density.
    """
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F      # CJK Symbols and Punctuation
        or 0x3400 <= code <= 0x4DBF   # CJK Unified Ideographs Extension A
        or 0x4E00 <= code <= 0x9FFF   # CJK Unified Ideographs
        or 0xFF00 <= code <= 0xFFEF   # Halfwidth and Fullwidth Forms
    )


def _heuristic_count(text: str) -> int:
    """CJK-aware char/N heuristic. Used by Layer 4 and as the seed for
    the deprecated ``estimate_tokens`` shim."""
    if not text:
        return 0
    cjk_count = sum(1 for c in text if _is_cjk(c))
    ascii_count = len(text) - cjk_count
    return int(cjk_count / _CJK_CHARS_PER_TOKEN + ascii_count / _ASCII_CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _resolve_model(model: str) -> str:
    """Resolve the model identifier used for tokenizer routing.

    Precedence (highest → lowest):
      1. ``settings.tokenizer_model_override`` — explicit reroute, used
         when the running model is a fine-tune of a known base
         ("my-internal-qwen3" → set override to "qwen3-max-preview" for
         APPROXIMATE quality instead of HEURISTIC).
      2. ``model`` argument — what the caller explicitly passed.
      3. ``settings.model_name`` — the model the LLM is actually bound
         to. Letting this be the default means call sites can write
         ``count_tokens(text)`` without manually wiring the model name
         through every function in the memory module.
      4. ``""`` — final fallback. Routes through the heuristic layer,
         which is the right default when we truly don't know the model.

    Lookup is wrapped in a try/except because settings may not be
    importable in narrow test contexts (e.g. tokenizer micro-bench
    fixtures that import this module before pydantic-settings is set
    up).
    """
    try:
        from chaos_agent.config.settings import settings
        override = getattr(settings, "tokenizer_model_override", "") or ""
        default = getattr(settings, "model_name", "") or ""
    except Exception:
        override, default = "", ""
    return (override or model or default or "").strip().lower()


def _hf_enabled() -> bool:
    try:
        from chaos_agent.config.settings import settings
        return bool(getattr(settings, "tokenizer_use_hf", False))
    except Exception:
        return False


def count_tokens(text: str, *, model: str = "") -> TokenCount:
    """4-layer fallback token counter.

    Always returns a ``TokenCount`` — never raises. ``model`` is the
    model name configured for the running task (typically
    ``settings.model_name``); leave empty when counting outside an
    inference context.

    The empty-string case returns ``(0, EXACT, "empty", 1.0)``.
    """
    if not text:
        return TokenCount(0, TokenCountQuality.EXACT, "empty", 1.0)

    resolved = _resolve_model(model)

    # ─── KNOWN-INCOMPATIBLE families ───────────────────────────────────
    # Llama / Mistral / Gemini / etc. Their tokenizers are NOT
    # tiktoken-compatible — using one would silently lie. Go straight
    # to Layer 3 (if enabled) or Layer 4.
    if any(resolved.startswith(p) for p in _SKIP_TIKTOKEN_PREFIXES):
        return _layer3_or_4(text, resolved)

    # ─── Layer 1: tiktoken native (OpenAI-recognised) ─────────────────
    enc = _get_tiktoken_for_model(resolved)
    if enc is not None:
        try:
            n = len(enc.encode(text, disallowed_special=()))
            return TokenCount(n, TokenCountQuality.EXACT, enc.name, 1.0)
        except Exception as e:
            logger.warning("tiktoken encode (layer 1) failed for %s: %s", resolved, e)

    # ─── Layer 2: family-prefix → encoding ────────────────────────────
    if _HAS_TIKTOKEN and resolved:
        for pattern, encoding_name, margin in _FAMILY_PREFIXES:
            if pattern.match(resolved):
                enc = _get_tiktoken_by_name(encoding_name)
                if enc is not None:
                    try:
                        n = len(enc.encode(text, disallowed_special=()))
                        quality = (
                            TokenCountQuality.EXACT
                            if margin == 1.0
                            else TokenCountQuality.APPROXIMATE
                        )
                        return TokenCount(n, quality, encoding_name, margin)
                    except Exception as e:
                        logger.warning(
                            "tiktoken encode (layer 2, %s) failed: %s",
                            encoding_name, e,
                        )
                # If we matched a prefix but encoder failed, don't try
                # weaker prefixes — fall through to Layer 3/4 instead.
                break

    # ─── Layer 3: HF AutoTokenizer (if enabled + HF-style name) ───────
    return _layer3_or_4(text, resolved)


def _layer3_or_4(text: str, resolved_model: str) -> TokenCount:
    """Shared tail for ``count_tokens``: try HF if enabled, else legacy."""
    if _hf_enabled() and "/" in resolved_model:
        tok = _get_hf_tokenizer(resolved_model)
        if tok is not None:
            try:
                ids = tok.encode(text, add_special_tokens=False)
                return TokenCount(len(ids), TokenCountQuality.EXACT,
                                  f"hf:{resolved_model}", 1.0)
            except Exception as e:
                logger.warning("HF tokenizer encode failed for %s: %s",
                               resolved_model, e)

    # ─── Layer 4: legacy heuristic (last resort, never fails) ──────────
    n = _heuristic_count(text)
    return TokenCount(n, TokenCountQuality.HEURISTIC, "heuristic-cjk", 1.20)


def count_tokens_messages(messages: list[Any], *, model: str = "") -> TokenCount:
    """Sum tokens across a list of messages.

    Follows the OpenAI cookbook overhead approximation: each message
    that carries content contributes ~4 tokens of envelope (role +
    delimiters) plus 2 tokens of priming for the whole batch. The +4
    is per-MESSAGE — not per-text-block — so a multi-modal message
    with three text segments still gets one envelope, matching the
    on-wire framing the model actually sees.

    The aggregated ``quality`` is the WEAKEST tier across all counted
    segments: any single HEURISTIC poisons the aggregate to HEURISTIC.
    ``safety_margin`` is the MAX across segments so the caller's
    threshold check is always safe (errs toward "trigger compaction").
    ``encoding_used`` reports the encoding of the first segment that
    hit the worst-quality tier — so the quality label and encoding
    label always tell a consistent story for the operator.
    """
    if not messages:
        return TokenCount(0, TokenCountQuality.EXACT, "empty", 1.0)

    total_content_tokens = 0
    messages_with_content = 0
    worst_rank = 0  # EXACT
    worst_encoding = "empty"
    worst_margin = 1.0

    def _absorb(text: str) -> int:
        """Count one text segment, update the worst-quality tracker,
        and return the raw token count so the caller can decide whether
        the parent message had content (and thus owes an envelope)."""
        nonlocal worst_rank, worst_encoding, worst_margin
        if not text:
            return 0
        tc = count_tokens(text, model=model)
        rank = _QUALITY_RANK[tc.quality]
        # Record the encoding from the FIRST segment that hit a given
        # worst rank — keeps the label consistent with the quality tier
        # (vs the old behaviour where ``encoding_used`` was just "last
        # message seen", which could disagree with ``quality``).
        if rank > worst_rank:
            worst_rank = rank
            worst_encoding = tc.encoding_used
        elif worst_rank == 0 and worst_encoding == "empty":
            # First content segment ever — seed the encoding even when
            # quality stays at EXACT so callers reporting an all-EXACT
            # batch get a meaningful encoding label.
            worst_encoding = tc.encoding_used
        if tc.safety_margin > worst_margin:
            worst_margin = tc.safety_margin
        return tc.count

    for msg in messages:
        content = getattr(msg, "content", "") or ""
        msg_tokens = 0
        if isinstance(content, str):
            msg_tokens = _absorb(content)
        elif isinstance(content, list):
            # Multi-modal / structured content: sum text parts only.
            # Image / tool-result blocks are accounted for elsewhere
            # (LLM SDK returns precise usage on the wire), so under-
            # counting them here is the right trade.
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    msg_tokens += _absorb(item.get("text", "") or "")
        # +4 envelope ONCE per message that actually carried text.
        # Empty / None content (e.g. mid-stream AIMessageChunk) skips
        # the envelope entirely — matches the historical behaviour the
        # ``test_none_or_missing_content_safe`` test pins down.
        if msg_tokens > 0:
            total_content_tokens += msg_tokens
            messages_with_content += 1

    total = total_content_tokens + 4 * messages_with_content + 2  # +2 batch priming
    return TokenCount(
        total,
        _RANK_TO_QUALITY[worst_rank],
        worst_encoding,
        worst_margin,
    )


__all__ = [
    "TokenCount",
    "TokenCountQuality",
    "count_tokens",
    "count_tokens_messages",
]
