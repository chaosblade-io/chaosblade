"""Guards for thinking-channel intent continuity (reasoning_content replay).

A thinking model writes "why I'm doing this / what I already did" into
``reasoning_content`` and often leaves ``content`` empty. langchain-openai's
outbound conversion emits only content/role/tool_calls/name, so without the
replay patch the history degenerates into bare tool calls with no rationale and
the model re-derives its intent from the original input every turn.

Verified against DashScope (qwen3.7-plus): with the field replayed the model
follows its own recorded progress 3/4 runs; with no field 0/4; with the field
named ``thinking`` 0/4.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# Importing the factory applies the patches at import time.
import chaos_agent.agent.factory  # noqa: F401
from chaos_agent.agent.factory import _reasoning_model_key
from chaos_agent.config.settings import settings

from langchain_openai.chat_models.base import (
    _convert_dict_to_message,
    _convert_message_to_dict,
)

THINKING = "已读完 A 目录。进度：步骤1=完成，步骤2(读 QX9)=待办。下一步读 QX9。"
TOOL_CALLS = [{"name": "kubectl_read", "args": {"subcommand": "get"}, "id": "c1"}]


def _tagged(reasoning: str = THINKING, model: str | None = None) -> AIMessage:
    """AIMessage as the inbound patch would have produced it."""
    key = _reasoning_model_key(model or settings.model_name)
    return AIMessage(
        content="",
        additional_kwargs={"reasoning_content": reasoning, key: True},
        tool_calls=list(TOOL_CALLS),
    )


class TestReplayPositive:
    def test_reasoning_content_is_replayed(self):
        d = _convert_message_to_dict(_tagged())
        assert d["reasoning_content"] == THINKING

    def test_inbound_then_outbound_round_trip(self):
        """The shape DashScope actually returns survives a full round trip."""
        msg = _convert_dict_to_message({
            "role": "assistant",
            "content": "",
            "reasoning_content": THINKING,
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "kubectl_read", "arguments": "{}"},
            }],
        })
        assert msg.additional_kwargs["reasoning_content"] == THINKING
        assert msg.additional_kwargs[_reasoning_model_key(settings.model_name)] is True
        assert _convert_message_to_dict(msg)["reasoning_content"] == THINKING

    def test_replay_is_not_pruned_by_age(self):
        """Every turn keeps its thinking — no recency window.

        Pruning by turn count would re-create the very bug this fixes: in a
        multi-step drill the step breakdown lives in the EARLY turns, so
        dropping them sends the model back to re-deriving step 1.
        """
        first = _tagged("第 1 轮：拆解为 4 个步骤，先打标签")
        history = [SystemMessage(content="sys"), HumanMessage(content="task"), first]
        for i in range(9):
            history.append(ToolMessage(content=f"out{i}", tool_call_id="c1"))
            history.append(_tagged(f"第 {i + 2} 轮"))

        converted = [_convert_message_to_dict(m) for m in history]
        assert converted[2]["reasoning_content"] == "第 1 轮：拆解为 4 个步骤，先打标签"
        replayed = [c for c in converted if "reasoning_content" in c]
        assert len(replayed) == 10


class TestReplayNegative:
    def test_absent_key_is_not_injected(self):
        d = _convert_message_to_dict(AIMessage(content="plain"))
        assert "reasoning_content" not in d

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_blank_reasoning_is_not_injected(self, blank):
        """Empty thinking carries no intent; sending "" is unverified behaviour."""
        d = _convert_message_to_dict(_tagged(blank))
        assert "reasoning_content" not in d

    def test_non_assistant_role_is_not_injected(self):
        msg = HumanMessage(
            content="hi",
            additional_kwargs={
                "reasoning_content": THINKING,
                _reasoning_model_key(settings.model_name): True,
            },
        )
        assert "reasoning_content" not in _convert_message_to_dict(msg)

    def test_responses_api_is_not_injected(self):
        """Only the Chat Completions shape is verified to accept the field."""
        d = _convert_message_to_dict(_tagged(), api="responses")
        assert "reasoning_content" not in d

    def test_non_string_reasoning_is_ignored(self):
        msg = AIMessage(
            content="",
            additional_kwargs={
                "reasoning_content": {"unexpected": "shape"},
                _reasoning_model_key(settings.model_name): True,
            },
        )
        assert "reasoning_content" not in _convert_message_to_dict(msg)


class TestModelProvenanceGuard:
    def test_other_model_is_not_replayed(self):
        """Checkpointer-restored thinking from a different model must not leak."""
        d = _convert_message_to_dict(_tagged(model="some-other-model"))
        assert "reasoning_content" not in d

    def test_missing_provenance_is_not_replayed(self):
        """Legacy messages carry no provenance key → treated as a mismatch."""
        msg = AIMessage(content="", additional_kwargs={"reasoning_content": THINKING})
        assert "reasoning_content" not in _convert_message_to_dict(msg)

    def test_provenance_key_survives_chunk_merge(self):
        """Streaming merges additional_kwargs by CONCATENATING string values.

        The model identity therefore lives in the KEY with a constant ``True``
        value; a plain ``{"_reasoning_model": "qwen"}`` would accumulate into
        "qwenqwenqwen..." across chunks and never match again.
        """
        from langchain_core.messages import AIMessageChunk

        key = _reasoning_model_key(settings.model_name)
        a = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "思考A", key: True})
        b = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "思考B", key: True})
        merged = a + b + b
        assert merged.additional_kwargs[key] is True
        assert merged.additional_kwargs["reasoning_content"] == "思考A思考B思考B"


class TestReplayBound:
    def test_oversized_reasoning_keeps_the_tail(self, monkeypatch):
        """The cap is an anti-runaway safety bound, and it keeps the conclusion.

        A thinking trace ends with what it decided, so truncation must drop the
        head, not the tail.
        """
        monkeypatch.setattr(settings, "reasoning_replay_max_chars", 50)
        reasoning = "噪声" * 200 + "结论：下一步读 QX9"
        replayed = _convert_message_to_dict(_tagged(reasoning))["reasoning_content"]
        assert len(replayed) == 50
        assert replayed.endswith("结论：下一步读 QX9")

    def test_under_limit_is_untouched(self, monkeypatch):
        monkeypatch.setattr(settings, "reasoning_replay_max_chars", 8000)
        assert _convert_message_to_dict(_tagged())["reasoning_content"] == THINKING

    def test_zero_limit_disables_truncation(self, monkeypatch):
        monkeypatch.setattr(settings, "reasoning_replay_max_chars", 0)
        long_reasoning = "推理" * 5000
        d = _convert_message_to_dict(_tagged(long_reasoning))
        assert d["reasoning_content"] == long_reasoning
