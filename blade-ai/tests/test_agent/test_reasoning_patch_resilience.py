"""The reasoning-replay monkey-patch must never take the Agent down.

Its targets are PRIVATE langchain functions and ``pyproject.toml`` pins only
``langchain-openai>=1.0`` (no upper bound), so a routine ``uv sync`` can rename
or reshape them. Before this guard, that raised at import time and the whole
Agent failed to start — an unacceptable blast radius for what is an
intent-continuity optimisation.

Two independent failure modes are covered:
  * target missing  → patch skipped, WARNING, Agent runs degraded
  * signature changed → wrappers forward *args/**kwargs, so calls still work
"""

import importlib
import sys

import pytest
from langchain_core.messages import AIMessage

import chaos_agent.agent.factory as factory
from chaos_agent.config.settings import settings


def _reload_factory():
    """Re-import factory so the import-time patch runs again in this state."""
    for name in ("chaos_agent.agent.factory",):
        sys.modules.pop(name, None)
    return importlib.import_module("chaos_agent.agent.factory")


@pytest.fixture
def pristine_langchain():
    """Snapshot and restore the three patch targets around each test."""
    from langchain_openai.chat_models import base as lc_base

    names = (
        "_convert_dict_to_message",
        "_convert_delta_to_message_chunk",
        "_convert_message_to_dict",
    )
    saved = {n: getattr(lc_base, n, None) for n in names}
    yield lc_base
    for n, fn in saved.items():
        if fn is None:
            if hasattr(lc_base, n):
                delattr(lc_base, n)
        else:
            setattr(lc_base, n, fn)
    _reload_factory()


class TestHealthyState:
    def test_no_failures_in_the_current_environment(self):
        assert factory.reasoning_patch_failures() == ()

    def test_failures_accessor_returns_an_immutable_snapshot(self):
        """A caller inspecting the record must not be able to mutate it."""
        assert isinstance(factory.reasoning_patch_failures(), tuple)


class TestMissingTargetDegrades:
    def test_import_survives_a_removed_target(self, pristine_langchain, caplog):
        """The exact upstream-rename scenario: no crash, just a WARNING."""
        delattr(pristine_langchain, "_convert_message_to_dict")
        with caplog.at_level("WARNING"):
            mod = _reload_factory()
        assert "outbound replay patch" in mod.reasoning_patch_failures()
        assert any("could not be applied" in r.getMessage() for r in caplog.records)

    def test_warning_explains_the_functional_impact(self, pristine_langchain, caplog):
        """An operator must learn WHAT degraded, not just that something did."""
        delattr(pristine_langchain, "_convert_message_to_dict")
        with caplog.at_level("WARNING"):
            _reload_factory()
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "not replayed to the model" in text
        assert "langchain-openai" in text

    def test_surviving_patches_still_apply(self, pristine_langchain):
        """Failures are per-patch: losing patch 3 must not disable 1 and 2."""
        delattr(pristine_langchain, "_convert_message_to_dict")
        mod = _reload_factory()
        assert mod.reasoning_patch_failures() == ("outbound replay patch",)

        msg = pristine_langchain._convert_dict_to_message(
            {"role": "assistant", "content": "", "reasoning_content": "意图仍被捕获"}
        )
        assert msg.additional_kwargs["reasoning_content"] == "意图仍被捕获"


class TestDegradationIsObservable:
    """Degradation must leave an inspectable record, not vanish.

    Silent degradation is the same failure shape as the loop defect this fixes.
    Deliberately NOT wired into preflight: that check would run on every drill
    and print fix guidance for a dependency problem the operator cannot act on
    mid-drill. The WARNING log plus this record are the surface; these tests are
    what keep the record honest.
    """

    def test_failure_is_recorded_under_a_stable_label(self, pristine_langchain):
        """The label must stay greppable — logs and reports key off it."""
        delattr(pristine_langchain, "_convert_message_to_dict")
        mod = _reload_factory()
        assert mod.reasoning_patch_failures() == ("outbound replay patch",)

    def test_record_distinguishes_inbound_from_outbound(self, pristine_langchain):
        """Which side failed changes the impact, so the record must say which."""
        delattr(pristine_langchain, "_convert_dict_to_message")
        mod = _reload_factory()
        assert mod.reasoning_patch_failures() == ("inbound patch (non-streaming)",)

    def test_multiple_failures_are_all_recorded(self, pristine_langchain):
        delattr(pristine_langchain, "_convert_dict_to_message")
        delattr(pristine_langchain, "_convert_message_to_dict")
        mod = _reload_factory()
        assert len(mod.reasoning_patch_failures()) == 2

    def test_summary_warning_reports_the_count(self, pristine_langchain, caplog):
        delattr(pristine_langchain, "_convert_message_to_dict")
        with caplog.at_level("WARNING"):
            _reload_factory()
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "1 of 3 failed" in text


class TestSignatureChangeIsTolerated:
    def test_outbound_patch_works_without_the_api_parameter(self, pristine_langchain):
        """``api`` only exists in newer versions; losing it must not break us."""
        real = pristine_langchain._convert_message_to_dict

        def older_signature(message):
            return real(message)

        pristine_langchain._convert_message_to_dict = older_signature
        mod = _reload_factory()
        assert mod.reasoning_patch_failures() == ()

        msg = AIMessage(
            content="",
            additional_kwargs={
                "reasoning_content": "意图Y",
                mod._reasoning_model_key(settings.model_name): True,
            },
        )
        d = pristine_langchain._convert_message_to_dict(msg)
        assert d["reasoning_content"] == "意图Y"

    def test_outbound_patch_still_honours_a_positional_api(self, pristine_langchain):
        """Forwarding must not lose a positionally-passed ``api``."""
        mod = _reload_factory()
        msg = AIMessage(
            content="",
            additional_kwargs={
                "reasoning_content": "意图Z",
                mod._reasoning_model_key(settings.model_name): True,
            },
        )
        d = pristine_langchain._convert_message_to_dict(msg, "responses")
        assert "reasoning_content" not in d
