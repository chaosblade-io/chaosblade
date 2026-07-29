"""Compaction budgets must scale with the model's window, not ignore it.

Two absolute constants were lifted from Claude Code, whose window is 200K:

  * ``POST_COMPACT_SKILLS_TOKEN_BUDGET = 25,000`` — skill text restored on top of
    the summary. 15% of a 200K threshold, but 95% of a 32K model's (26,214),
    where it would hand straight back the room compaction had just freed.
  * ``MAX_COMPACTION_INPUT_CHARS = 100,000`` — the compaction request's own
    input. In CHARACTERS, so on this project's CJK traffic it is ~50-68K tokens:
    it alone overflows a 32K or 64K window. Compaction runs when the context is
    fullest, so a budget that lets its own request overflow fails at the only
    moment it matters, and the failure path is ``_simple_compact``.

Both are now ceilings, capped against the configured model by
``resolve_skill_budgets()`` / ``resolve_compaction_input_chars()``. Derived from
the RAW window, not from the provider-anchored overhead the main agent sees: the
compaction call goes through a bare llm with no tools bound, so none of that
overhead is present.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

import chaos_agent.config.settings as settings_module
import chaos_agent.memory.compactor as compactor
from chaos_agent.config.settings import Settings
from chaos_agent.memory.context_manager import resolve_auto_compact_threshold

_SMALL_WINDOW_MODELS = ("qwen-max", "qwen-plus")
_LARGE_WINDOW_MODELS = ("qwen3-max", "claude-sonnet-4", "gpt-4o")

# Worst chars-per-token observed in this project's real traffic: 404 messages of
# >=200 chars pulled from the local checkpoint database gave min 1.49, p5 1.64,
# median 4.15. Dense CJK tool output is the 1.49 end.
#
# The budget must be validated against THIS, not against invented filler. A first
# version of these tests used a hand-written Chinese string that happened to
# tokenise at 2.0 chars/token — twice as generous as the real worst case — and as
# a result it passed with ``_CHARS_PER_TOKEN`` raised to 1.5, the exact value
# that overflows a 32K window.
_WORST_CHARS_PER_TOKEN = 1.49


@contextmanager
def _as_model(name: str):
    """Swap the module-level settings the resolvers read."""
    original = settings_module.settings
    settings_module.settings = Settings(model_name=name)
    try:
        yield settings_module.settings
    finally:
        settings_module.settings = original


class TestSkillBudgetScalesDown:
    @pytest.mark.parametrize("model", _SMALL_WINDOW_MODELS)
    def test_small_window_gets_a_fraction_of_the_absolute_ceiling(self, model):
        with _as_model(model) as s:
            max_tokens, ratio = s.resolve_context_budget()
            threshold = resolve_auto_compact_threshold(max_tokens, ratio)
            _, total = compactor.resolve_skill_budgets()

        assert total < compactor.POST_COMPACT_SKILLS_TOKEN_BUDGET
        assert total <= threshold * compactor.SKILLS_BUDGET_SHARE_OF_THRESHOLD + 1
        assert total < threshold * 0.5, (
            f"{model}: restored skill text would claim {total}/{threshold} of the "
            f"threshold, refilling what compaction just freed"
        )

    @pytest.mark.parametrize("model", _LARGE_WINDOW_MODELS)
    def test_large_window_is_unaffected_by_the_cap(self, model):
        """No regression for the models the absolute ceilings were sized for."""
        with _as_model(model):
            _, total = compactor.resolve_skill_budgets()
        assert total > 20_000

    @pytest.mark.parametrize("model", _SMALL_WINDOW_MODELS + _LARGE_WINDOW_MODELS)
    def test_per_skill_stays_below_the_total(self, model):
        """One skill must not be able to consume the whole allowance."""
        with _as_model(model):
            per_skill, total = compactor.resolve_skill_budgets()
        assert 0 < per_skill <= total
        assert per_skill <= compactor.POST_COMPACT_MAX_TOKENS_PER_SKILL


class TestCompactionInputFitsTheWindow:
    @pytest.mark.parametrize(
        # ``deepseek`` (64K) is the mid-window case; ``deepseek-chat`` is a
        # SEPARATE 128K entry in the budget table, so naming it here would have
        # tested a large window while reading as a small one.
        "model", _SMALL_WINDOW_MODELS + _LARGE_WINDOW_MODELS + ("deepseek",)
    )
    def test_a_full_budget_of_worst_case_text_plus_output_reserve_fits(self, model):
        """The property that matters, against the WORST real ratio.

        Filling the budget with hand-written Chinese is not enough — invented
        filler tokenises more favourably than dense tool output does. The bound
        is therefore computed from ``_WORST_CHARS_PER_TOKEN``, measured on this
        project's own traffic.
        """
        with _as_model(model) as s:
            max_tokens, _ = s.resolve_context_budget()
            budget_chars = compactor.resolve_compaction_input_chars()

        worst_case_tokens = int(budget_chars / _WORST_CHARS_PER_TOKEN)
        total = worst_case_tokens + compactor.COMPACTION_OUTPUT_RESERVE_TOKENS

        assert total <= max_tokens, (
            f"{model}: a full compaction request would be {total} tokens against "
            f"a {max_tokens} window — the request meant to relieve context "
            f"pressure would itself be rejected"
        )

    @pytest.mark.parametrize("model", _SMALL_WINDOW_MODELS)
    def test_small_window_budget_is_below_the_absolute_ceiling(self, model):
        with _as_model(model):
            assert (
                compactor.resolve_compaction_input_chars()
                < compactor.MAX_COMPACTION_INPUT_CHARS
            )

    @pytest.mark.parametrize("model", _LARGE_WINDOW_MODELS)
    def test_large_window_keeps_the_full_ceiling(self, model):
        with _as_model(model):
            assert (
                compactor.resolve_compaction_input_chars()
                == compactor.MAX_COMPACTION_INPUT_CHARS
            )

    def test_chars_per_token_does_not_exceed_the_worst_real_ratio(self):
        """Guards the direction of the conversion, not just its value.

        Raising it above the measured worst case silently re-introduces the
        overflow, which is why the bound is the observed minimum (1.49) and not
        an average or a hand-written sample.
        """
        assert compactor._CHARS_PER_TOKEN <= _WORST_CHARS_PER_TOKEN

    def test_output_reserve_is_actually_reserved(self):
        with _as_model("qwen-max") as s:
            max_tokens, _ = s.resolve_context_budget()
            budget = compactor.resolve_compaction_input_chars()
        implied_input_tokens = budget / compactor._CHARS_PER_TOKEN
        assert implied_input_tokens <= max_tokens - compactor.COMPACTION_OUTPUT_RESERVE_TOKENS + 1


class TestBudgetsAreDerivedNotFrozen:
    """Both must read the CURRENT model, or a model switch is silently ignored."""

    def test_switching_models_changes_both_budgets(self):
        with _as_model("qwen3-max"):
            big_skills = compactor.resolve_skill_budgets()[1]
            big_input = compactor.resolve_compaction_input_chars()
        with _as_model("qwen-max"):
            small_skills = compactor.resolve_skill_budgets()[1]
            small_input = compactor.resolve_compaction_input_chars()

        assert small_skills < big_skills
        assert small_input < big_input
