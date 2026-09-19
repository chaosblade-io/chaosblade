"""Tests for chaos_agent.utils.message_integrity.

Pins the Layer-1 enforcement point of the tool_call/ToolMessage pairing
invariant (openspec: llm-message-pairing-integrity): ``sanitize_tool_pairing``
for the send-side orphan gate, ``synthetic_pairs_intact`` +
``drop_messages_with_tool_call_ids`` for the dedup half that decides whether a
synthetic pair set must be rebuilt, and ``apply_synthetic_pair_gate`` for the
gate both nodes share. Every scenario maps to a spec scenario of the same name.

The cross-module half of the gate contract — that each node's declared id set
matches what its builder actually emits, and that both nodes really call the
shared gate with their own set and phase — lives in
``tests/test_agent/nodes/test_synthetic_pair_gate_contract.py``, because it has
to import the node modules.
"""

from __future__ import annotations

import logging

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    ToolMessageChunk,
)

from chaos_agent.utils.message_integrity import (
    PAIR_DAMAGED,
    PAIR_INTACT,
    PAIR_REORDERED,
    _CHUNK_TYPE_ALIASES,
    _msg_type,
    answer_dangling_tool_calls,
    apply_synthetic_pair_gate,
    diagnose_synthetic_pairs,
    drop_messages_with_tool_call_ids,
    log_pair_rebuild,
    sanitize_tool_pairing,
    synthetic_pairs_intact,
)

MODULE_LOGGER = "chaos_agent.utils.message_integrity"


def _ai(tool_call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "kubectl_read",
            "args": {"command": "get pods"},
            "id": tool_call_id,
            "type": "tool_call",
        }],
    )


def _tool(tool_call_id: str = "call_1", content: str = "result") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


# ---------------------------------------------------------------------------
# Orphan drop + WARNING
# ---------------------------------------------------------------------------


class TestOrphanDrop:
    def test_orphan_tool_message_dropped_with_warning(self, caplog):
        """Spec: 孤儿 ToolMessage 被丢弃并告警."""
        msgs = [SystemMessage(content="sys"), _tool("call_missing", "kubectl top output")]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)
        assert out == [msgs[0]]
        text = caplog.text
        assert "call_missing" in text
        assert "kubectl top output" in text  # content preview
        assert "1 orphan" in text  # drop count

    def test_multiple_orphans_counted(self, caplog):
        msgs = [_tool("a"), _tool("b"), HumanMessage(content="hi")]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)
        assert out == [msgs[2]]
        assert "2 orphan" in caplog.text

    def test_content_preview_truncated_at_200_chars(self, caplog):
        msgs = [_tool("gone", "x" * 500)]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            sanitize_tool_pairing(msgs)
        assert "x" * 200 + "..." in caplog.text
        assert "x" * 201 not in caplog.text

    def test_emptying_the_whole_list_logs_error_naming_the_real_cause(self, caplog):
        """When every message is an orphan the request still goes out and the
        provider answers with a 400 about an empty message array — a symptom
        that points at the provider. The ERROR line is what names the actual
        fault, so it has to exist and carry the dropped ids.
        """
        msgs = [_tool("gone_a", "result a"), _tool("gone_b", "result b")]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)

        assert out == []
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, "exactly one ERROR, at the emptied-list point"
        text = errors[0].getMessage()
        assert "emptied the ENTIRE message list" in text
        assert "gone_a" in text and "gone_b" in text
        # It must state where the fault actually is, or the log reads as
        # another provider complaint.
        assert "not a provider fault" in text or "real fault is local" in text

    def test_partial_drop_does_not_raise_the_error(self, caplog):
        """The ERROR is specific to a fully emptied list — a normal orphan
        drop keeps the WARNING-only profile it always had."""
        msgs = [_tool("gone", "orphan"), HumanMessage(content="still here")]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)

        assert out == [msgs[1]]
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]
        assert "emptied the ENTIRE message list" not in caplog.text

    def test_caller_supplied_empty_list_is_not_our_fault(self, caplog):
        """An empty list from the caller is passed through untouched — the
        gate reports what IT dropped, and it dropped nothing here."""
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            assert sanitize_tool_pairing([]) == []
        assert caplog.text == ""


# ---------------------------------------------------------------------------
# Legal pairings — the three id homes
# ---------------------------------------------------------------------------


class TestLegalPairingNotDropped:
    def test_pair_via_tool_calls(self):
        msgs = [_ai("c1"), _tool("c1")]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_pair_via_invalid_tool_calls_only(self):
        """Spec: 截断流工具的 call id 只存在 invalid_tool_calls."""
        caller = AIMessage(content="")
        # langchain normalises invalid entries to dicts with an id.
        caller.invalid_tool_calls = [{
            "name": "kubectl_read",
            "args": "get po",  # truncated JSON — that's why it's invalid
            "id": "c_trunc",
            "error": "Malformed args.",
        }]
        msgs = [caller, _tool("c_trunc")]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_pair_via_additional_kwargs_raw_form(self):
        caller = AIMessage(content="")
        caller.additional_kwargs["tool_calls"] = [{
            "id": "c_raw",
            "type": "function",
            "function": {"name": "kubectl_read", "arguments": "{}"},
        }]
        msgs = [caller, _tool("c_raw")]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_multiple_tool_messages_sharing_one_caller(self):
        """Parallel tool calls: one AI, several ToolMessages, distinct ids."""
        ai = AIMessage(content="", tool_calls=[
            {"name": "a", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "b", "args": {}, "id": "c2", "type": "tool_call"},
        ])
        msgs = [ai, _tool("c1"), _tool("c2")]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_caller_appearing_after_tool_message_is_repaired_not_dropped(self, caplog):
        """Two-pass scan: collection completes before any orphan decision, so
        scan order within the list cannot cause a misjudgment — the result is
        NOT dropped as an orphan even though its caller has not been seen yet.

        It IS moved, though. Matching is only half the protocol rule; the
        caller must also PRECEDE the result. This assertion used to be
        ``sanitize_tool_pairing(msgs) is msgs``, i.e. the reversed pair shipped
        untouched — which is exactly what a strict provider rejects.
        """
        tool, caller = _tool("c1"), _ai("c1")
        msgs = [tool, caller]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)
        assert out == [caller, tool], "moved after its caller, not dropped"
        assert "orphan" not in caplog.text
        assert "moved 1 ToolMessage" in caplog.text
        assert "c1" in caplog.text


# ---------------------------------------------------------------------------
# Empty / None ids never forge a pairing
# ---------------------------------------------------------------------------


class TestFalsyIdRules:
    def test_none_caller_id_does_not_pair_with_empty_tool_id(self, caplog):
        """Spec: 空/None id 不参与配对 — no None==None false pairing."""
        caller = AIMessage(content="")
        caller.invalid_tool_calls = [{
            "name": "broken", "args": "", "id": None, "error": "Malformed.",
        }]
        orphan = ToolMessage(content="stray", tool_call_id="")
        msgs = [caller, orphan]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)
        assert out == [caller]  # the empty-id ToolMessage is dropped
        assert "empty tool_call_id" in caplog.text

    def test_empty_id_tool_message_dropped_even_with_clean_history(self, caplog):
        msgs = [HumanMessage(content="hi"), ToolMessage(content="x", tool_call_id="")]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)
        assert out == [msgs[0]]
        assert "empty tool_call_id" in caplog.text


# ---------------------------------------------------------------------------
# Zero-copy happy path + input tolerance
# ---------------------------------------------------------------------------


class TestZeroCopyAndTolerance:
    def test_clean_sequence_returns_same_object(self):
        """Spec: 干净序列零开销直通 — identity, not equality."""
        msgs = [SystemMessage(content="s"), _ai("c1"), _tool("c1")]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_no_tool_messages_returns_same_object(self):
        msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
        assert sanitize_tool_pairing(msgs) is msgs

    @pytest.mark.parametrize("empty", [[], None], ids=["empty_list", "none"])
    def test_empty_and_none_passthrough(self, empty):
        assert sanitize_tool_pairing(empty) is empty

    def test_non_message_elements_tolerated(self):
        """Strings/None mixed into the list must not crash the scan."""
        msgs = ["not a message", None, _tool("orphan")]
        out = sanitize_tool_pairing(msgs)
        assert out == ["not a message", None]

    def test_dict_shaped_messages_supported(self):
        """State-persisted messages can arrive as dicts."""
        caller = {"type": "ai", "content": "", "tool_calls": [{"id": "cd", "name": "t", "args": {}}]}
        paired = {"type": "tool", "content": "ok", "tool_call_id": "cd"}
        orphan = {"type": "tool", "content": "stray", "tool_call_id": "zz"}
        msgs = [caller, paired, orphan]
        out = sanitize_tool_pairing(msgs)
        assert out == [caller, paired]


# ---------------------------------------------------------------------------
# Synthetic pair audit — the DEDUP half of the invariant
# (spec: verifier 合成 baseline 对去重的配对语义)
# ---------------------------------------------------------------------------


REQUIRED = frozenset({"syn_a", "syn_b"})


def _pair(tc_id: str) -> list:
    """A well-formed synthetic [caller, result] pair."""
    return [_ai(tc_id), _tool(tc_id, f"result-{tc_id}")]


class TestSyntheticPairsIntact:
    def test_both_pairs_well_formed(self):
        """Spec: 完整对在场——跳过重建."""
        msgs = [SystemMessage(content="s"), *_pair("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is True

    def test_missing_caller_is_not_intact(self):
        """Spec: caller 丢失只剩孤儿 ToolMessage——重建完整对."""
        msgs = [_tool("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_missing_result_is_not_intact(self):
        """A caller shipping with no answer is rejected just as firmly."""
        msgs = [_ai("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_one_damaged_id_fails_the_whole_gate(self):
        """Spec: metrics 对单独受损也触发重建 — 另一对完整不算通过."""
        msgs = [*_pair("syn_a"), _tool("syn_b")]  # syn_b lost its caller
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_duplicate_result_is_not_intact(self):
        """The one violation ``sanitize_tool_pairing`` structurally CANNOT see:
        both copies have a caller, so both look legal to the orphan scan."""
        msgs = [*_pair("syn_a"), _tool("syn_a", "second answer"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False
        assert sanitize_tool_pairing(msgs) is msgs  # send-side gate lets it pass

    def test_duplicate_caller_is_not_intact(self):
        msgs = [*_pair("syn_a"), _ai("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_reversed_order_is_not_intact(self):
        """Providers require the result to answer a PRECEDING call."""
        msgs = [_tool("syn_a"), _ai("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_absent_pairs_are_not_intact(self):
        """Spec: 双双缺席——正常注入."""
        assert synthetic_pairs_intact([HumanMessage(content="h")], REQUIRED) is False

    def test_unrelated_pairs_do_not_satisfy_the_gate(self):
        msgs = [*_pair("other_1"), *_pair("other_2")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_empty_required_ids_is_vacuously_intact(self):
        assert synthetic_pairs_intact([], frozenset()) is True

    def test_caller_id_in_invalid_tool_calls_counts(self):
        """Truncated-stream callers keep their id ONLY in invalid_tool_calls."""
        caller = AIMessage(content="")
        caller.invalid_tool_calls = [
            {"name": "t", "args": "", "id": "syn_a", "error": "Malformed args."},
        ]
        msgs = [caller, _tool("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is True

    def test_caller_id_in_additional_kwargs_counts(self):
        caller = AIMessage(content="", additional_kwargs={
            "tool_calls": [{
                "id": "syn_a",
                "type": "function",
                "function": {"name": "t", "arguments": "{}"},
            }],
        })
        msgs = [caller, _tool("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is True

    def test_one_caller_carrying_the_id_in_two_homes_counts_once(self):
        """Real provider responses keep the raw dict in ``additional_kwargs``
        AND the parsed entry in ``tool_calls`` — the same id, twice, in ONE
        message. Counting occurrences would read that as a duplicated caller
        and rebuild the pair set on every genuine turn."""
        caller = AIMessage(content="", additional_kwargs={
            "tool_calls": [{
                "id": "syn_a",
                "type": "function",
                "function": {"name": "t", "arguments": "{}"},
            }],
        })
        assert caller.tool_calls, "langchain should have parsed the raw kwargs"
        assert caller.tool_calls[0]["id"] == "syn_a"
        assert caller.additional_kwargs["tool_calls"][0]["id"] == "syn_a"
        msgs = [caller, _tool("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is True

    def test_two_caller_messages_sharing_an_id_are_a_duplicate(self):
        """Contrast with the above: two MESSAGES really are two callers."""
        msgs = [_ai("syn_a"), _ai("syn_a"), _tool("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_non_message_elements_tolerated(self):
        msgs = ["junk", None, *_pair("syn_a"), *_pair("syn_b")]
        assert synthetic_pairs_intact(msgs, REQUIRED) is True

    def test_dict_shaped_messages_supported(self):
        msgs = [
            {"type": "ai", "content": "", "tool_calls": [{"id": "syn_a"}]},
            {"type": "tool", "content": "ok", "tool_call_id": "syn_a"},
            {"type": "ai", "content": "", "tool_calls": [{"id": "syn_b"}]},
            {"type": "tool", "content": "ok", "tool_call_id": "syn_b"},
        ]
        assert synthetic_pairs_intact(msgs, REQUIRED) is True


class TestDropMessagesWithToolCallIds:
    def test_drops_both_halves_of_every_required_pair(self, caplog):
        other = HumanMessage(content="keep me")
        msgs = [other, *_pair("syn_a"), *_pair("syn_b")]
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = drop_messages_with_tool_call_ids(msgs, REQUIRED)
        assert out == [other]
        assert "4 stale synthetic" in caplog.text

    def test_the_drop_log_is_info_not_warning(self, caplog):
        """Dropping stale fragments is this helper doing its job, and once a
        reversed pair is pinned in state it repeats on EVERY turn. Severity
        belongs to the gate, which knows why the rebuild happened — so this
        line must stay below WARNING or a benign reversal alarms forever."""
        msgs = [*_pair("syn_a")]
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            drop_messages_with_tool_call_ids(msgs, {"syn_a"})
        assert caplog.records, "the drop is still logged, just quietly"
        assert all(r.levelno == logging.INFO for r in caplog.records)
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            caplog.clear()
            drop_messages_with_tool_call_ids([*_pair("syn_a")], {"syn_a"})
            assert caplog.text == ""

    def test_drops_lone_fragment(self):
        """The rebuild precondition: a surviving orphan must not ship beside
        its fresh replacement, or one tool_call gets answered twice."""
        msgs = [*_pair("syn_a"), _tool("syn_b", "orphan")]
        out = drop_messages_with_tool_call_ids(msgs, {"syn_b"})
        assert out == msgs[:2]

    def test_drops_caller_found_via_invalid_tool_calls(self):
        caller = AIMessage(content="")
        caller.invalid_tool_calls = [{"name": "t", "args": "", "id": "syn_a", "error": "x"}]
        keep = HumanMessage(content="h")
        assert drop_messages_with_tool_call_ids([caller, keep], {"syn_a"}) == [keep]

    def test_no_match_returns_same_object(self):
        msgs = [SystemMessage(content="s"), *_pair("real_call")]
        assert drop_messages_with_tool_call_ids(msgs, REQUIRED) is msgs

    @pytest.mark.parametrize("empty", [[], None], ids=["empty_list", "none"])
    def test_empty_passthrough(self, empty):
        assert drop_messages_with_tool_call_ids(empty, REQUIRED) is empty

    def test_empty_id_set_passthrough(self):
        msgs = [*_pair("syn_a")]
        assert drop_messages_with_tool_call_ids(msgs, frozenset()) is msgs

    def test_drop_then_rebuild_yields_an_intact_set(self):
        """Round trip: the two helpers are the rebuild, in that order."""
        damaged = [*_pair("syn_a"), _tool("syn_b", "orphan")]
        assert synthetic_pairs_intact(damaged, REQUIRED) is False
        rebuilt = drop_messages_with_tool_call_ids(damaged, REQUIRED)
        rebuilt = rebuilt + _pair("syn_a") + _pair("syn_b")
        assert synthetic_pairs_intact(rebuilt, REQUIRED) is True
        assert sanitize_tool_pairing(rebuilt) is rebuilt  # and nothing orphaned


# ---------------------------------------------------------------------------
# Chunk type normalisation
# ---------------------------------------------------------------------------


def _ai_chunk(tool_call_id: str = "call_1") -> AIMessageChunk:
    """An AGGREGATED stream chunk: ``.tool_calls`` is populated, which is the
    shape a chunk has once the stream finishes. Its ``.type`` literal is
    ``"AIMessageChunk"``, not ``"ai"`` — the whole reason for the alias table.
    """
    return AIMessageChunk(
        content="",
        tool_calls=[{
            "name": "kubectl_read",
            "args": {"command": "get pods"},
            "id": tool_call_id,
            "type": "tool_call",
        }],
    )


class TestChunkTypeNormalisation:
    """langchain's ``.type`` is class-derived, so chunk classes report
    ``"AIMessageChunk"`` / ``"ToolMessageChunk"`` instead of ``"ai"`` /
    ``"tool"``. Without normalising, a chunk caller's ids are never collected
    and its legitimate ToolMessage gets dropped as an orphan.
    """

    def test_chunk_literal_really_differs_from_base(self):
        """Precondition for every test below — if upstream ever aligns the
        literals, the alias table becomes dead code and this fails loudly."""
        assert _ai_chunk().type == "AIMessageChunk"
        assert AIMessage(content="").type == "ai"
        assert ToolMessageChunk(content="r", tool_call_id="c").type == "ToolMessageChunk"
        assert ToolMessage(content="r", tool_call_id="c").type == "tool"

    def test_msg_type_normalises_chunks_and_passes_bases_through(self):
        assert _msg_type(_ai_chunk()) == "ai"
        assert _msg_type(AIMessage(content="")) == "ai"
        assert _msg_type(ToolMessageChunk(content="r", tool_call_id="c")) == "tool"
        assert _msg_type(ToolMessage(content="r", tool_call_id="c")) == "tool"
        assert _msg_type(HumanMessage(content="h")) == "human"
        assert _msg_type(SystemMessage(content="s")) == "system"

    def test_msg_type_tolerates_non_message_junk(self):
        """The gate meets dicts, None and arbitrary objects in state."""
        assert _msg_type({"type": "AIMessageChunk"}) == "ai"
        assert _msg_type({"type": "tool"}) == "tool"
        assert _msg_type({}) == ""
        assert _msg_type(None) == ""
        assert _msg_type(object()) == ""
        assert _msg_type({"type": 42}) == ""

    def test_chunk_caller_answered_by_plain_tool_message_survives(self):
        """The misjudgement this guards: caller is a chunk, so its id used to
        be missed and the perfectly legal result was dropped as an orphan."""
        msgs = [_ai_chunk("call_1"), _tool("call_1", "paired result")]
        assert sanitize_tool_pairing(msgs) is msgs  # zero-copy ⇒ nothing dropped

    def test_chunk_result_paired_with_chunk_caller_survives(self):
        msgs = [
            _ai_chunk("call_1"),
            ToolMessageChunk(content="paired result", tool_call_id="call_1"),
        ]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_orphan_chunk_result_is_still_dropped(self, caplog):
        """Normalising must not weaken the gate — a chunk orphan is an orphan."""
        msgs = [
            _ai("call_1"),
            _tool("call_1"),
            ToolMessageChunk(content="orphan", tool_call_id="call_vanished"),
        ]
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            out = sanitize_tool_pairing(msgs)
        assert out == msgs[:2]
        assert "call_vanished" in caplog.text

    def test_pair_audit_sees_chunk_halves(self):
        """The dedup gate must accept a chunk-built pair as intact, or it
        rebuilds every turn against a state that is actually fine."""
        msgs = [
            _ai_chunk("syn_a"),
            ToolMessageChunk(content="a", tool_call_id="syn_a"),
            _ai("syn_b"),
            _tool("syn_b", "b"),
        ]
        assert synthetic_pairs_intact(msgs, REQUIRED) is True

    def test_pair_audit_still_rejects_reversed_chunk_pair(self):
        msgs = [
            ToolMessageChunk(content="a", tool_call_id="syn_a"),
            _ai_chunk("syn_a"),
            *_pair("syn_b"),
        ]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_pair_audit_still_rejects_duplicated_chunk_result(self):
        msgs = [
            _ai_chunk("syn_a"),
            ToolMessageChunk(content="a", tool_call_id="syn_a"),
            ToolMessageChunk(content="a again", tool_call_id="syn_a"),
            *_pair("syn_b"),
        ]
        assert synthetic_pairs_intact(msgs, REQUIRED) is False

    def test_drop_clears_chunk_fragments(self, caplog):
        msgs = [
            HumanMessage(content="keep"),
            _ai_chunk("syn_a"),
            ToolMessageChunk(content="a", tool_call_id="syn_a"),
        ]
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = drop_messages_with_tool_call_ids(msgs, {"syn_a"})
        assert out == [msgs[0]]
        assert "2 stale synthetic" in caplog.text

    def test_alias_table_covers_every_langchain_chunk_class(self):
        """Regression lock: enumerate langchain's chunk classes and require
        the table to map each one to its BASE class's literal. A new chunk
        class upstream, or a renamed literal, must fail here instead of
        silently reintroducing the orphan misjudgement.
        """
        import langchain_core.messages as m

        checked = []
        for name in sorted(dir(m)):
            cls = getattr(m, name)
            if not (isinstance(cls, type) and name.endswith("MessageChunk")):
                continue
            base = cls.__mro__[1]
            base_default = getattr(base.model_fields.get("type"), "default", None)
            if not isinstance(base_default, str):
                continue  # BaseMessageChunk — abstract, carries no literal
            checked.append(name)
            assert _CHUNK_TYPE_ALIASES.get(name) == base_default, (
                f"{name}.type must alias to {base}({base_default!r}); "
                f"table says {_CHUNK_TYPE_ALIASES.get(name)!r}"
            )
            # And the chunk really does report its own class name, otherwise
            # the alias would be unnecessary.
            assert cls.model_fields["type"].default == name

        assert len(checked) == len(_CHUNK_TYPE_ALIASES), (
            f"alias table and langchain disagree on the chunk set: "
            f"checked={checked}, table={sorted(_CHUNK_TYPE_ALIASES)}"
        )


# ---------------------------------------------------------------------------
# Precedence repair — the second half of the protocol rule
# ---------------------------------------------------------------------------


def _protocol_legal(msgs: list) -> bool:
    """The check a strict provider applies, written independently of the code
    under test: every tool result must answer a PRECEDING call.
    """
    asked: set = set()
    for m in msgs:
        if isinstance(m, AIMessage):
            asked.update(tc.get("id") for tc in (m.tool_calls or []))
        elif isinstance(m, ToolMessage):
            if m.tool_call_id not in asked:
                return False
    return True


class TestPrecedenceRepair:
    def test_legal_sequence_is_returned_unchanged(self):
        """Zero-copy survives the extra pass — this runs on every LLM call."""
        msgs = [SystemMessage(content="s"), *_pair("syn_a"), *_pair("syn_b")]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_reversed_pair_is_moved_after_its_caller(self):
        h = HumanMessage(content="h")
        caller, result = _ai("c1"), _tool("c1")
        assert sanitize_tool_pairing([result, h, caller]) == [h, caller, result]

    def test_several_premature_results_keep_their_relative_order(self):
        caller = AIMessage(content="", tool_calls=[
            {"name": "a", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "b", "args": {}, "id": "c2", "type": "tool_call"},
        ])
        r1, r2 = _tool("c1"), _tool("c2")
        assert sanitize_tool_pairing([r2, r1, caller]) == [caller, r2, r1]

    def test_only_the_premature_result_moves(self):
        """A result already behind its caller must not be shuffled — the
        repair fixes violations, it is not a canonical re-sort."""
        ok_caller, ok_result = _ai("c_ok"), _tool("c_ok")
        bad_result, bad_caller = _tool("c_bad"), _ai("c_bad")
        tail = HumanMessage(content="tail")
        msgs = [ok_caller, ok_result, bad_result, tail, bad_caller]
        out = sanitize_tool_pairing(msgs)
        assert out == [ok_caller, ok_result, tail, bad_caller, bad_result]

    def test_repair_is_idempotent(self):
        once = sanitize_tool_pairing([_tool("c1"), _ai("c1")])
        assert sanitize_tool_pairing(once) is once

    def test_caller_list_is_not_mutated_in_place(self):
        msgs = [_tool("c1"), _ai("c1")]
        snapshot = list(msgs)
        sanitize_tool_pairing(msgs)
        assert msgs == snapshot

    def test_orphan_drop_and_precedence_repair_compose(self):
        """One call can need both halves: drop the unmatched, move the matched
        but premature."""
        caller = _ai("c1")
        stray, premature = _tool("stray"), _tool("c1")
        out = sanitize_tool_pairing([stray, premature, caller])
        assert out == [caller, premature]

    def test_dict_shaped_reversed_pair_is_repaired(self):
        caller = {"type": "ai", "content": "", "tool_calls": [{"id": "cd"}]}
        result = {"type": "tool", "content": "ok", "tool_call_id": "cd"}
        assert sanitize_tool_pairing([result, caller]) == [caller, result]

    def test_chunk_shaped_reversed_pair_is_repaired(self):
        caller = _ai_chunk("c1")
        result = ToolMessageChunk(content="a", tool_call_id="c1")
        assert sanitize_tool_pairing([result, caller]) == [caller, result]

    def test_ungated_consumer_of_a_reversed_state_now_ships_legally(self):
        """The exposure this repair closes.

        A node with no synthetic-pair gate of its own (execute, recover) ships
        ``state["messages"]`` as it stands. When state holds a reversed pair,
        matching alone let it through: measured before the fix, Layer 1
        dropped NOTHING, the evidence survived, and the sequence was still
        illegal — a 400 on a strict provider, blamed on the provider.
        """
        state = [
            HumanMessage(content="h0"),
            _tool("syn_a", "BASELINE-EVIDENCE"),
            HumanMessage(content="ctx"),
            AIMessage(content="resp"),
            _ai("syn_a"),
        ]
        assert not _protocol_legal(state), "precondition: the input is illegal"
        out = sanitize_tool_pairing(state)
        assert len(out) == len(state), "nothing dropped — the evidence survives"
        assert any(getattr(m, "content", "") == "BASELINE-EVIDENCE" for m in out)
        assert _protocol_legal(out)


# ---------------------------------------------------------------------------
# Three-state diagnosis
# ---------------------------------------------------------------------------


class TestPairDiagnosis:
    def test_intact(self):
        msgs = [*_pair("syn_a"), *_pair("syn_b")]
        assert diagnose_synthetic_pairs(msgs, REQUIRED) == PAIR_INTACT

    def test_reversed_is_reordered_not_damaged(self):
        """Both halves present exactly once — only their order is wrong."""
        msgs = [_tool("syn_a"), _ai("syn_a"), *_pair("syn_b")]
        assert diagnose_synthetic_pairs(msgs, REQUIRED) == PAIR_REORDERED

    @pytest.mark.parametrize("msgs", [
        pytest.param([_tool("syn_a"), *_pair("syn_b")], id="caller_missing"),
        pytest.param([_ai("syn_a"), *_pair("syn_b")], id="result_missing"),
        pytest.param(
            [*_pair("syn_a"), _tool("syn_a", "again"), *_pair("syn_b")],
            id="result_duplicated",
        ),
        pytest.param(
            [*_pair("syn_a"), _ai("syn_a"), *_pair("syn_b")], id="caller_duplicated",
        ),
        pytest.param([HumanMessage(content="h")], id="both_absent"),
    ])
    def test_damaged_shapes(self, msgs):
        assert diagnose_synthetic_pairs(msgs, REQUIRED) == PAIR_DAMAGED

    def test_damaged_outranks_reordered(self):
        """One id reversed AND another missing must not report the benign
        state, or the gate logs INFO and nobody ever looks upstream."""
        msgs = [_tool("syn_a"), _ai("syn_a"), _tool("syn_b")]
        assert diagnose_synthetic_pairs(msgs, REQUIRED) == PAIR_DAMAGED

    def test_empty_required_set_is_intact(self):
        assert diagnose_synthetic_pairs([_tool("x")], frozenset()) == PAIR_INTACT

    def test_bool_wrapper_agrees_with_diagnosis_over_the_matrix(self):
        """Consistency lock: ``synthetic_pairs_intact`` must stay exactly
        ``diagnose(...) == PAIR_INTACT``. A gate using one and a test using
        the other would otherwise drift apart silently."""
        matrix = [
            [*_pair("syn_a"), *_pair("syn_b")],
            [_tool("syn_a"), _ai("syn_a"), *_pair("syn_b")],
            [_tool("syn_a"), *_pair("syn_b")],
            [*_pair("syn_a"), _tool("syn_a", "again"), *_pair("syn_b")],
            [HumanMessage(content="h")],
        ]
        for msgs in matrix:
            assert synthetic_pairs_intact(msgs, REQUIRED) is (
                diagnose_synthetic_pairs(msgs, REQUIRED) == PAIR_INTACT
            )


class TestLogPairRebuild:
    def test_damaged_logs_warning(self, caplog):
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            log_pair_rebuild(PAIR_DAMAGED, "verify", REQUIRED)
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "DAMAGED" in caplog.text
        assert "verify" in caplog.text and "syn_a" in caplog.text

    def test_reordered_logs_info_only(self, caplog):
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            log_pair_rebuild(PAIR_REORDERED, "recover_verify", REQUIRED)
        assert caplog.records, "the reversal is still recorded"
        assert all(r.levelno == logging.INFO for r in caplog.records)
        assert "REVERSED" in caplog.text and "recover_verify" in caplog.text

    def test_reordered_line_says_it_will_not_converge(self, caplog):
        """The point of the INFO line: whoever reads it must learn this
        repeats and is tolerated, instead of hunting a ghost every turn."""
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            log_pair_rebuild(PAIR_REORDERED, "verify", REQUIRED)
        assert "every turn" in caplog.text
        assert "CONTRACT BOUNDARY" in caplog.text

    def test_intact_logs_nothing(self, caplog):
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            log_pair_rebuild(PAIR_INTACT, "verify", REQUIRED)
        assert caplog.text == ""

    def test_a_rebuild_that_did_not_happen_is_not_claimed(self, caplog):
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            log_pair_rebuild(PAIR_DAMAGED, "verify", REQUIRED, rebuilt=False)
        assert "NO rebuild" in caplog.text
        assert "rebuilt the pair set" not in caplog.text


# ---------------------------------------------------------------------------
# The tolerated non-convergence, characterised against the real reducer
# ---------------------------------------------------------------------------


class TestReversedStateIsToleratedNotLeaked:
    """Locks the CONTRACT BOUNDARY written in the module docstring, using the
    real ``add_messages`` reducer instead of a hand-merged list.

    This is a CHARACTERISATION test: it asserts behaviour we deliberately do
    not fix (state stays reversed, the gate rebuilds every turn) next to the
    two properties that make that acceptable (nothing illegal ever ships, the
    synthetic set never grows). If someone later makes state converge the
    assertions below fail and this class must be rewritten — which is exactly
    what should happen to a documented trade-off.
    """

    CALLER_ID = "synthetic:verify:baseline:caller"
    RESULT_ID = "synthetic:verify:baseline:result"
    TC = "baseline_collector"

    def _fresh_pair(self) -> list:
        return [
            AIMessage(content="", id=self.CALLER_ID, tool_calls=[{
                "name": "baseline_collector", "args": {},
                "id": self.TC, "type": "tool_call",
            }]),
            ToolMessage(
                content="BASELINE-EVIDENCE", id=self.RESULT_ID,
                tool_call_id=self.TC,
            ),
        ]

    def _gate(self, msgs: list) -> tuple[list, str]:
        """What both node gates do, minus the baseline-content building."""
        if diagnose_synthetic_pairs(msgs, {self.TC}) == PAIR_INTACT:
            return list(msgs), "SKIP"
        rebuilt = drop_messages_with_tool_call_ids(list(msgs), {self.TC})
        rebuilt.extend(self._fresh_pair())
        return rebuilt, "REBUILD"

    def test_three_turns_from_a_damaged_state(self):
        from langgraph.graph.message import add_messages

        # Damage precondition: the caller was compacted away, the result
        # survived in the middle of state.
        state = [
            HumanMessage(content="h0", id="h0"),
            ToolMessage(
                content="BASELINE-EVIDENCE", id=self.RESULT_ID,
                tool_call_id=self.TC,
            ),
            HumanMessage(content="ctx", id="hm"),
            AIMessage(content="resp1", id="r1"),
        ]
        assert diagnose_synthetic_pairs(state, {self.TC}) == PAIR_DAMAGED

        gates = []
        for turn in range(3):
            local, gate = self._gate(state)
            gates.append(gate)
            shipped = sanitize_tool_pairing(list(local))
            assert _protocol_legal(shipped), f"turn {turn}: shipped an illegal sequence"
            assert any(
                getattr(m, "content", "") == "BASELINE-EVIDENCE" for m in shipped
            ), f"turn {turn}: baseline evidence vanished"

            update = [
                m for m in shipped
                if getattr(m, "id", None) in (self.CALLER_ID, self.RESULT_ID)
            ] + [AIMessage(content=f"resp{turn + 2}", id=f"r{turn + 2}")]
            state = add_messages(state, update)

            synthetic = [
                m for m in state
                if getattr(m, "id", None) in (self.CALLER_ID, self.RESULT_ID)
            ]
            assert len(synthetic) == 2, (
                f"turn {turn}: stable ids must keep exactly one copy per role, "
                f"got {len(synthetic)}"
            )

        assert gates == ["REBUILD"] * 3, "the gate does not converge — documented"
        positions = {
            m.id: i for i, m in enumerate(state)
            if getattr(m, "id", None) in (self.CALLER_ID, self.RESULT_ID)
        }
        assert positions[self.RESULT_ID] < positions[self.CALLER_ID], (
            "add_messages pins the surviving result to its old index while the "
            "rebuilt caller is appended, so state stays reversed"
        )
        assert diagnose_synthetic_pairs(state, {self.TC}) == PAIR_REORDERED


# ---------------------------------------------------------------------------
# The shared gate both nodes call
# ---------------------------------------------------------------------------


class _BuilderSpy:
    """A ``build_fresh`` stand-in that records whether it was asked at all.

    The intact path must NOT pay for the builder — on the verify side it
    formats the entire baseline evidence blob — so "was it called" is part of
    the contract, not an implementation detail.
    """

    def __init__(self, out):
        self.out = out
        self.calls = 0

    def __call__(self) -> list:
        self.calls += 1
        return list(self.out)


class TestApplySyntheticPairGate:
    def test_intact_returns_the_same_object_and_skips_the_builder(self):
        """Spec: 完整对在场——跳过重建. Identity, and the builder stays cold."""
        msgs = [SystemMessage(content="s"), *_pair("syn_a"), *_pair("syn_b")]
        spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
        out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert out is msgs, "the caller keeps appending to its own list"
        assert spy.calls == 0, "the intact path must not pay for the builder"

    def test_damaged_pair_is_cleared_then_rebuilt(self):
        """Spec: caller 丢失只剩孤儿 ToolMessage——重建完整对."""
        msgs = [HumanMessage(content="h"), _tool("syn_a", "orphan"), *_pair("syn_b")]
        spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
        out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert spy.calls == 1
        assert diagnose_synthetic_pairs(out, REQUIRED) == PAIR_INTACT
        assert [m for m in out if getattr(m, "content", "") == "orphan"] == [], \
            "the stale fragment must be cleared, not left beside its replacement"
        assert len([m for m in out if isinstance(m, ToolMessage)
                    and m.tool_call_id == "syn_a"]) == 1, \
            "one tool_call, exactly one answer"

    def test_reversed_pair_is_rebuilt_and_logged_at_info(self, caplog):
        """Spec: 反序记 INFO 而非 WARNING — through the gate, not just the logger."""
        msgs = [_tool("syn_a"), _ai("syn_a"), *_pair("syn_b")]
        assert diagnose_synthetic_pairs(msgs, REQUIRED) == PAIR_REORDERED
        spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="recover_verify")
        assert diagnose_synthetic_pairs(out, REQUIRED) == PAIR_INTACT
        assert caplog.records, "a rebuild is always logged"
        assert all(r.levelno == logging.INFO for r in caplog.records), \
            "a benign reversal must not alarm at WARNING on every turn"
        assert "recover_verify" in caplog.text, "the phase names the gate"

    def test_damaged_is_logged_at_warning_with_the_phase(self, caplog):
        msgs = [_tool("syn_a"), *_pair("syn_b")]
        spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "verify" in caplog.text
        assert "legal either way" in caplog.text, \
            "a COMPLETED rebuild is legal in both directions, so the qualifier " \
            "belongs to the unrepaired branch alone — pinning this side too, or " \
            "the conditional collapses untested in either direction"

    def test_zero_fragment_rebuild_is_first_injection_info_not_damaged(self, caplog):
        """Spec: 零片段 + builder 非空 = 周期首次注入，INFO 而非 WARNING.

        The cycle's first turn — cycle start, or the first turn after a
        replan seam removed the previous cycle's pair (change
        ``stale-baseline-pair-seam-cleanup``) — holds NO fragment, so a
        successful rebuild is not a repair after damage. The old code logged
        it through the DAMAGED vocabulary at WARNING, crying wolf on every
        healthy task's first verify turn while pointing at memory compaction.
        The damaged-with-fragment side stays WARNING — pinned by
        ``test_damaged_is_logged_at_warning_with_the_phase`` next door.
        """
        msgs = [HumanMessage(content="h"), _ai("real_call")]
        spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert spy.calls == 1
        assert diagnose_synthetic_pairs(out, REQUIRED) == PAIR_INTACT
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
            "the cycle's first injection must not alarm"
        assert "first time" in caplog.text
        assert "memory compaction" not in caplog.text, \
            "nothing was lost — the compaction pointer would misdirect triage"

    def test_builder_returning_nothing_does_not_claim_a_rebuild(self, caplog):
        """Spec: 未真正重建时不得声称重建.

        Reachable in production, not only in a guard: every builder returns []
        when no observation carries usable stdout, and the node only enters the
        gate when ``success_count > 0``. The two predicates disagree about what
        "usable" means — ``_is_observation_success`` counts an observation with
        ``exit_code == 0`` and EMPTY stdout as a success (it rejects only a
        non-zero exit or a kubectl error marker in the text), while both
        builders skip an observation with no stdout — so an output-less but
        successful baseline lands exactly here. (An all-non-zero-exit baseline
        cannot: those observations never reach ``success_count`` in the first
        place, so the node's precondition keeps the gate out.)

        This case holds a FRAGMENT (an orphan plus an intact pair), so it is
        real damage the empty builder cannot repair and stays a WARNING. The
        fragment-free turn is the benign one — see the next test.
        """
        msgs = [_tool("syn_a", "orphan"), *_pair("syn_b")]
        spy = _BuilderSpy([])
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert spy.calls == 1
        assert out == msgs, "nothing to rebuild with, so nothing was touched"
        assert "NO rebuild" in caplog.text
        assert "rebuilt the pair set" not in caplog.text

    def test_empty_builder_without_fragments_is_info_not_a_false_alarm(self, caplog):
        """Spec: 无残片且无证据可注入——不是损坏，不得每 turn 告警.

        The turn the previous test's WARNING used to cover as well: state holds
        NO fragment of the pair set and the builder legitimately has nothing to
        inject, so nothing was ever injected and nothing was lost. Reporting
        DAMAGED here is a false alarm with a compounding cost — ``rebuilt=False``
        changes nothing, so the next turn diagnoses identically and the WARNING
        repeats on EVERY iteration without converging, while its "look upstream
        — memory compaction" pointer sends the reader hunting for a loss that
        never happened. INFO keeps it discoverable and names the real source.
        """
        msgs = [HumanMessage(content="h"), _ai("real_call")]
        spy = _BuilderSpy([])
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert spy.calls == 1
        assert out == msgs, "no fragment to clear, no evidence to add"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
            "a benign empty-injection turn must not alarm"
        assert "No synthetic tool pair injected" in caplog.text
        assert "baseline_capture" in caplog.text, \
            "triage must be pointed at the observations, not at compaction"
        assert "NO rebuild" not in caplog.text, \
            "no damage was diagnosed, so the rebuild vocabulary does not apply"

    def test_orphan_result_alone_is_a_fragment_the_empty_builder_cannot_excuse(self, caplog):
        """Spec: 仅剩孤儿 ToolMessage + builder 空——仍是损坏，必须 WARNING.

        Pins the TOOL half of ``_count_pair_fragments`` on its own. The mixed
        case two tests up holds a complete pair beside the orphan, so either
        half of the discriminator still counts a fragment and killing the tool
        branch leaves everything green — measured, not assumed: deleting those
        three lines kept 117 passing. A sequence holding ONLY the orphan result
        has nothing else to count, so that branch has to work.
        """
        msgs = [HumanMessage(content="h"), _tool("syn_a", "orphan")]
        spy = _BuilderSpy([])
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert spy.calls == 1
        assert out == msgs, "no evidence to rebuild with, so nothing was touched"
        assert any(r.levelno == logging.WARNING for r in caplog.records), \
            "a surviving orphan result is real damage even when the builder is empty"
        assert "NO rebuild" in caplog.text
        assert "No synthetic tool pair injected" not in caplog.text, \
            "something WAS injected once — its caller is what went missing"
        assert "legal either way" not in caplog.text, \
            "unrepaired damage must not claim a legality no layer enforced"

    def test_unanswered_caller_alone_is_a_fragment_and_the_gate_does_not_cause_it(self, caplog):
        """Spec: 仅剩未应答 caller + builder 空——WARNING，且闸门不制造新违规.

        Pins the AI half of ``_count_pair_fragments``: killing that branch also
        left 117 passing, because no empty-builder case held a caller-only
        fragment.

        This is the shape that a strict provider rejects outright — a tool_call
        with no answer — and the gate cannot repair it here, so what is
        asserted is the property the Non-Goal section below actually promises:
        the gate never CAUSES the violation it declines to detect. The caller
        arrived in ``state["messages"]`` and leaves exactly as it came, with no
        second unanswered call added beside it. Asserting instead that the
        shipped sequence has no unanswered caller would contradict that
        Non-Goal (``sanitize_tool_pairing`` is one-directional by design), so
        the blindness stays pinned where it is documented rather than being
        silently assumed away here.
        """
        msgs = [HumanMessage(content="h"), _ai("syn_a")]
        spy = _BuilderSpy([])
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="recover_verify")
        assert spy.calls == 1
        assert any(r.levelno == logging.WARNING for r in caplog.records), \
            "a caller with no answer is real damage even when the builder is empty"
        assert "NO rebuild" in caplog.text
        assert out == msgs, "nothing to rebuild with, so the fragments are passed through"
        assert _unanswered_call_ids(out) == _unanswered_call_ids(msgs) == ["syn_a"], \
            "the gate declines to clear this caller, and must not add another"
        assert "No synthetic tool pair injected" not in caplog.text, \
            "a fragment survived, so this is damage rather than an absent pair set"
        assert "legal either way" not in caplog.text, \
            "unrepaired damage must not claim a legality no layer enforced"
        assert "unanswered caller" in caplog.text, \
            "the reader must be told which fragment nothing downstream will clear"

    def test_absent_pairs_are_injected_without_needing_a_drop(self):
        """Spec: 双双缺席——正常注入. The drop finds nothing and must still work."""
        msgs = [HumanMessage(content="h")]
        spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
        out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert out == [msgs[0], *_pair("syn_a"), *_pair("syn_b")]
        assert diagnose_synthetic_pairs(out, REQUIRED) == PAIR_INTACT

    def test_the_callers_list_is_not_mutated(self):
        """The caller's object is a copy of ``state["messages"]``; the gate
        must not reach back into it, or a node that reuses the list for
        something else would see the rebuild it did not ask for."""
        msgs = [_tool("syn_a", "orphan"), *_pair("syn_b")]
        before = list(msgs)
        spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
        out = apply_synthetic_pair_gate(msgs, REQUIRED, spy, phase="verify")
        assert msgs == before, "input untouched"
        assert out is not msgs

    def test_gate_output_is_clean_for_the_send_side(self):
        """Composition: the two gates are layers, not alternatives. Whatever
        the pair gate ships, ``sanitize_tool_pairing`` must have nothing left
        to drop or move — asserted by identity, which covers both halves."""
        for damaged in (
            [_tool("syn_a", "orphan"), *_pair("syn_b")],
            [_tool("syn_a"), _ai("syn_a"), *_pair("syn_b")],
            [*_pair("syn_a"), _tool("syn_a", "twice"), *_pair("syn_b")],
            [HumanMessage(content="h")],
        ):
            spy = _BuilderSpy([*_pair("syn_a"), *_pair("syn_b")])
            out = apply_synthetic_pair_gate(damaged, REQUIRED, spy, phase="verify")
            assert sanitize_tool_pairing(out) is out, f"not clean: {damaged}"
            assert _protocol_legal(out)



# ---------------------------------------------------------------------------
# ``sanitize_tool_pairing`` is one-directional. The module's REVERSE half —
# answering a caller that has no result — is the sibling
# ``answer_dangling_tool_calls``, tested in its own class below.
# ---------------------------------------------------------------------------


def _unanswered_call_ids(msgs: list) -> list:
    """Tool calls with no answer, written independently of the code under test.

    The mirror image of ``_protocol_legal``: that one asks "does every result
    have a preceding call", this asks "does every call have a result".
    ``sanitize_tool_pairing`` enforces only the former; the module answers the
    latter through the sibling ``answer_dangling_tool_calls``. These tests pin
    the forward gate's blindness to callers — a deliberate split, not an
    oversight — so the reverse half has to be proven by its own tests below
    rather than assumed away here.
    """
    asked: list = []
    answered: set = set()
    for m in msgs:
        if isinstance(m, AIMessage):
            asked.extend(tc.get("id") for tc in (m.tool_calls or []))
        elif isinstance(m, ToolMessage):
            answered.add(m.tool_call_id)
    return [tc_id for tc_id in asked if tc_id not in answered]


class TestSanitizeToolPairingIsOneDirectional:
    """Spec: ``sanitize_tool_pairing`` 只做正向净化, 对未应答 caller 保持盲区.

    A CHARACTERISATION class. It pins that the FORWARD gate is blind to a
    caller with no answer, next to the one property that makes that safe — the
    gate never CAUSES the violation it declines to detect. The blindness is
    deliberate and stays: the module answers dangling callers through the
    SIBLING ``answer_dangling_tool_calls``, which the send path runs right
    AFTER this gate (``resilient_llm._sanitize_input``). These tests call the
    gate ALONE, so they stay green. If someone makes ``sanitize_tool_pairing``
    ITSELF answer or strip callers, these fail, and the split between the two
    functions has to be re-justified.
    """

    def test_unanswered_caller_passes_through_untouched(self):
        """Spec: 未应答 caller 被钉为收口层的已知盲区.

        Identity, not just equality: the gate does not even build a list, so
        there is no code path in it that could see the missing answer.
        """
        msgs = [HumanMessage(content="inject cpu"), _ai("c1")]
        assert _unanswered_call_ids(msgs) == ["c1"], "precondition: it IS unanswered"
        assert sanitize_tool_pairing(msgs) is msgs

    def test_unanswered_caller_beside_a_legal_pair_is_still_untouched(self):
        """The blind spot is not narrowed by there being well-formed traffic
        next to it — the gate decides per ToolMessage, never per caller."""
        msgs = [HumanMessage(content="h"), _ai("c1"), _ai("c2"), _tool("c2")]
        assert _unanswered_call_ids(msgs) == ["c1"]
        assert sanitize_tool_pairing(msgs) is msgs

    def test_multi_call_message_with_one_answer_keeps_the_gap(self):
        """One AIMessage asking twice, answered once. The unanswered half is
        the reachable shape of ``handle_truncated_response`` skipping a call
        whose id came back empty."""
        caller = AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "kubectl_read", "args": {}, "id": "c2", "type": "tool_call"},
        ])
        msgs = [caller, _tool("c1")]
        assert _unanswered_call_ids(msgs) == ["c2"]
        assert sanitize_tool_pairing(msgs) is msgs

    @pytest.mark.parametrize("msgs", [
        [_ai("c1"), _tool("c1"), _tool("zz", "stray")],
        [_ai("c1"), _tool("zz", "stray")],
        [_tool("zz", "stray"), _ai("c1"), _tool("c1")],
    ], ids=["answered_pair_plus_orphan", "unanswered_caller_plus_orphan", "orphan_first"])
    def test_dropping_an_orphan_never_creates_an_unanswered_caller(self, msgs):
        """Spec: 丢弃孤儿不得制造新的未应答 caller.

        The half of the Non-Goal that is a real obligation rather than a
        declared blind spot. It holds by definition — an orphan's
        ``tool_call_id`` matches no caller, so the message being dropped was
        never anyone's answer — but "by definition" is exactly the kind of
        claim that silently stops being true when a second id source is added
        to the caller scan.
        """
        before = set(_unanswered_call_ids(msgs))
        out = sanitize_tool_pairing(list(msgs))
        after = set(_unanswered_call_ids(out))
        assert after - before == set(), "the drop un-answered a call"
        assert before <= after, "and it must not answer one either"

    def test_the_repair_never_creates_an_unanswered_caller_either(self):
        """Same obligation on the MOVING half: relocating a premature result
        must not leave some other caller without its answer."""
        msgs = [_tool("c1", "early"), _ai("c1"), _ai("c2"), _tool("c2")]
        assert _unanswered_call_ids(msgs) == []
        out = sanitize_tool_pairing(msgs)
        assert out is not msgs, "precondition: the repair really ran"
        assert _unanswered_call_ids(out) == []
        assert _protocol_legal(out)


# ---------------------------------------------------------------------------
# The REVERSE half — answer_dangling_tool_calls
# (spec: tool-call-answer-completeness — every caller reaching a provider is
#  answered with an honest non-execution placeholder, never a fabricated one)
# ---------------------------------------------------------------------------


def _recording_factory(seen: list):
    """Stand-in for ``resilient_llm._not_executed_answer``: records each id it
    is asked to answer and returns a real ToolMessage, so the output stays
    checkable by ``_unanswered_call_ids`` / ``_protocol_legal``.
    """
    def factory(tool_call_id: str) -> ToolMessage:
        seen.append(tool_call_id)
        return ToolMessage(content=f"not executed {tool_call_id}", tool_call_id=tool_call_id)
    return factory


class TestAnswerDanglingToolCalls:
    """Spec: 反向配对由 ``answer_dangling_tool_calls`` 强制(发送前兜底).

    The sibling of ``sanitize_tool_pairing`` and the answer to the blindness
    the characterisation class above pins. The send path runs it right after the
    forward gate (``resilient_llm._sanitize_input``), so a caller left dangling
    by the execute-loop short-circuit is answered before it can reach a strict
    provider.
    """

    def test_dangling_caller_is_answered_and_the_pair_becomes_legal(self):
        """The simplest reachable shape: the model asked, the loop short-circuited,
        no result was produced, and nothing follows the caller. The net inserts
        one honest answer right after it — which, with the caller last, is the
        end."""
        msgs = [HumanMessage(content="inject cpu"), _ai("c1")]
        assert _unanswered_call_ids(msgs) == ["c1"], "precondition: it IS dangling"
        seen: list = []
        out = answer_dangling_tool_calls(msgs, _recording_factory(seen))
        assert seen == ["c1"], "the factory is asked exactly for the dangling id"
        assert _unanswered_call_ids(out) == [], "no caller is left dangling"
        assert _protocol_legal(out), "and the forward rule still holds"
        assert out[:2] == msgs, "the caller is untouched; the answer follows it"
        assert isinstance(out[2], ToolMessage) and out[2].tool_call_id == "c1"
        assert len(out) == len(msgs) + 1

    def test_answer_is_inserted_after_caller_when_other_messages_follow(self):
        """THE regression the placement fix exists for — the real verifier send
        shape. The dangling caller is last in ``state``, but the verifier /
        recover-verifier append their synthetic baseline pair and a Human
        instruction AFTER it before sending (``_build_layer2_messages``). The
        answer MUST land right after its caller, NOT at the very end: an
        end-append leaves the caller immediately followed by another assistant
        AND strands its tool response past a Human message — a shape a strict
        provider rejects just as it rejects the dangling caller itself.
        """
        msgs = [
            HumanMessage(content="inject cpu"),
            _ai("call_1"),                        # dangling: loop short-circuited
            _ai("baseline"), _tool("baseline"),   # verifier's synthetic pair
            HumanMessage(content="verify now"),   # verifier instruction (last)
        ]
        assert _unanswered_call_ids(msgs) == ["call_1"], "precondition"
        out = answer_dangling_tool_calls(msgs, _recording_factory([]))
        # out == [Human, AI(call_1), Tool(call_1), AI(baseline), Tool(baseline), Human]
        assert out[1] is msgs[1], "caller untouched, still at index 1"
        assert isinstance(out[2], ToolMessage) and out[2].tool_call_id == "call_1", \
            "answer inserted IMMEDIATELY after its caller"
        assert out[3] is msgs[2] and out[4] is msgs[3], "baseline pair kept intact, in order"
        assert out[5] is msgs[4] and isinstance(out[-1], HumanMessage), \
            "the Human instruction is still LAST — the answer was NOT appended past it"
        assert _unanswered_call_ids(out) == []
        assert _protocol_legal(out)

    def test_all_answered_sequence_is_returned_untouched_zero_copy(self):
        """The 99% case: a provider-accepted conversation has every caller
        answered, so the net is a no-op that neither copies nor fabricates."""
        msgs = [HumanMessage(content="h"), _ai("c1"), _tool("c1")]
        seen: list = []
        assert answer_dangling_tool_calls(msgs, _recording_factory(seen)) is msgs
        assert seen == [], "the factory is never called on a complete sequence"

    def test_sequence_with_no_tool_calls_is_returned_untouched(self):
        msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
        assert answer_dangling_tool_calls(msgs, _recording_factory([])) is msgs

    def test_multi_call_message_answers_only_the_unanswered_half(self):
        """One AIMessage asking twice, answered once — the shape
        ``handle_truncated_response`` leaves when a call's id came back empty.
        Only the missing half is filled; the real result is never duplicated."""
        caller = AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "kubectl_read", "args": {}, "id": "c2", "type": "tool_call"},
        ])
        msgs = [caller, _tool("c1", "real result")]
        seen: list = []
        out = answer_dangling_tool_calls(msgs, _recording_factory(seen))
        assert seen == ["c2"], "only the unanswered call is filled"
        assert _unanswered_call_ids(out) == []
        assert _protocol_legal(out)
        assert out[1] is msgs[1] and out[1].content == "real result", \
            "the genuine answer survives untouched; the placeholder is appended"

    def test_multi_call_turn_short_circuited_wholesale_answers_in_original_order(self):
        """THE primary reachable shape for parallel tool calls: one AIMessage
        asks for SEVERAL tools, the execute loop short-circuits (budget /
        wall-clock / error) before phase2_tools runs, so ALL of them dangle. The
        answers MUST come out in the order the model asked — NOT sorted by id —
        so a strict provider that pairs tool responses positionally with the
        caller's tool_calls array stays satisfied, and so the net matches
        execute_loop._answer_replan_tool_calls (which preserves order too). Ids
        are chosen so alphabetical (c1<c2<c3) differs from emission (c3,c1,c2):
        a sorted implementation would wrongly answer c1,c2,c3.
        """
        caller = AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "args": {}, "id": "c3", "type": "tool_call"},
            {"name": "kubectl_read", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "kubectl_read", "args": {}, "id": "c2", "type": "tool_call"},
        ])
        msgs = [HumanMessage(content="h"), caller]
        assert sorted(_unanswered_call_ids(msgs)) == ["c1", "c2", "c3"], \
            "precondition: all three dangle"
        seen: list = []
        out = answer_dangling_tool_calls(msgs, _recording_factory(seen))
        assert seen == ["c3", "c1", "c2"], \
            "answered in the caller's ORIGINAL tool_call order, not sorted by id"
        assert _unanswered_call_ids(out) == []
        assert _protocol_legal(out)
        assert out[1] is caller, "caller untouched, still at index 1"
        assert [m.tool_call_id for m in out[2:]] == ["c3", "c1", "c2"], \
            "the three answers follow the caller adjacently, in emission order"
        assert len(out) == len(msgs) + 3

    def test_multi_call_with_middle_sibling_answered_fills_the_rest_in_order(self):
        """A multi-call caller whose MIDDLE sibling already has a real result:
        the net skips the answered one and fills the rest, still in the caller's
        original tool_call order, keeping the real result in place."""
        caller = AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "args": {}, "id": "c3", "type": "tool_call"},
            {"name": "kubectl_read", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "kubectl_read", "args": {}, "id": "c2", "type": "tool_call"},
        ])
        real_c1 = _tool("c1", "real result")
        msgs = [caller, real_c1]
        seen: list = []
        out = answer_dangling_tool_calls(msgs, _recording_factory(seen))
        assert seen == ["c3", "c2"], \
            "c1 is already answered; c3 and c2 filled in original order"
        assert _unanswered_call_ids(out) == []
        # out == [caller, Tool(c1 real), Tool(c3), Tool(c2)]
        assert out[1] is real_c1, "the real middle sibling keeps its place"
        assert [m.tool_call_id for m in out[2:]] == ["c3", "c2"]
        assert len(out) == len(msgs) + 2

    def test_second_pass_is_idempotent(self):
        """Re-sending the same conversation (retry, next turn) must not stack a
        second answer on the same call — the first answer now satisfies it."""
        msgs = [HumanMessage(content="h"), _ai("c1")]
        first = answer_dangling_tool_calls(msgs, _recording_factory([]))
        seen2: list = []
        second = answer_dangling_tool_calls(first, _recording_factory(seen2))
        assert second is first, "the answered sequence needs nothing more"
        assert seen2 == [], "no second answer is fabricated"

    def test_each_dangling_answer_is_inserted_right_after_its_own_caller(self):
        """Several dangling callers in one sequence: each answer lands directly
        after the caller that asked it, NOT batched at the end — batching would
        leave every caller immediately followed by another assistant, the exact
        shape the verifier path produces and a strict provider rejects.
        Determinism now comes from message position (a stable id upstream stays
        stable through the net); within ONE multi-call message the missing
        answers are sorted (see the half-answered test above)."""
        msgs = [HumanMessage(content="h"), _ai("c9"), _ai("c2"), _ai("c5")]
        seen: list = []
        out = answer_dangling_tool_calls(msgs, _recording_factory(seen))
        assert seen == ["c9", "c2", "c5"], "answered caller-by-caller in message order"
        assert _unanswered_call_ids(out) == []
        assert _protocol_legal(out)
        # out == [Human, AI(c9), Tool(c9), AI(c2), Tool(c2), AI(c5), Tool(c5)]
        assert out[0] is msgs[0]
        for out_idx, src_idx, tc_id in ((1, 1, "c9"), (3, 2, "c2"), (5, 3, "c5")):
            assert out[out_idx] is msgs[src_idx], "caller kept by identity"
            assert out[out_idx].tool_calls[0]["id"] == tc_id
            assert isinstance(out[out_idx + 1], ToolMessage)
            assert out[out_idx + 1].tool_call_id == tc_id, "answer adjacent to its caller"

    def test_non_list_input_passes_through(self):
        """A plain string prompt (some call sites) is not a message list; the
        net returns it as-is rather than raising."""
        assert answer_dangling_tool_calls("just a prompt", _recording_factory([])) == "just a prompt"

    def test_composes_with_the_forward_gate_into_a_fully_legal_sequence(self):
        """End to end at the send path: a sequence that is BOTH reversed (a
        premature result) AND dangling (an unanswered caller) is made fully
        legal by the two siblings in order — forward gate first, then the
        reverse net. This is exactly ``resilient_llm._sanitize_input``."""
        msgs = [_tool("c1", "early"), _ai("c1"), _ai("c2")]
        assert not _protocol_legal(msgs), "precondition: c1's result precedes it"
        assert _unanswered_call_ids(msgs) == ["c2"], "precondition: c2 dangles"
        sanitized = sanitize_tool_pairing(msgs)
        out = answer_dangling_tool_calls(sanitized, _recording_factory([]))
        assert _protocol_legal(out), "forward rule satisfied"
        assert _unanswered_call_ids(out) == [], "reverse rule satisfied"
