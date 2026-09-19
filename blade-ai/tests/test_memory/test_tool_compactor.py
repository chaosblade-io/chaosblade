"""Tests for tool output two-stage truncation."""

import json
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from chaos_agent.memory.tool_compactor import (
    CLEARED_MARKER,
    _is_cache_artifact_name,
    ToolResultCompactor,
    build_truncation_notice,
    is_ai_message,
    is_tool_message,
    maybe_time_based_microcompact,
    smart_strip_k8s_json,
    truncate_json_at_boundary,
    truncate_text,
)


# ---------------------------------------------------------------------------
# Original tests (preserved)
# ---------------------------------------------------------------------------


class TestIsToolMessage:
    """Test tool message detection."""

    def test_tool_message(self):
        msg = MagicMock()
        msg.type = "tool"
        assert is_tool_message(msg) is True

    def test_non_tool_message(self):
        msg = MagicMock()
        msg.type = "human"
        assert is_tool_message(msg) is False

    def test_no_type_attribute(self):
        msg = MagicMock(spec=[])  # No attributes
        assert is_tool_message(msg) is False


class TestTruncateText:
    """Test text truncation."""

    def test_short_text_unchanged(self):
        text = "hello world"
        assert truncate_text(text, 1000) == text

    def test_long_text_truncated(self):
        text = "a" * 5000
        result = truncate_text(text, 1000)
        assert len(result.encode("utf-8")) <= 1000

    def test_preserves_valid_utf8(self):
        text = "你好世界" * 1000
        result = truncate_text(text, 100)
        # Should not raise on encode
        result.encode("utf-8")


class TestToolResultCompactor:
    """Test two-stage truncation logic."""

    def test_small_content_not_truncated(self):
        msg = MagicMock()
        msg.type = "tool"
        msg.content = "small output"

        compactor = ToolResultCompactor()
        result = compactor.compact([msg])
        assert result[0].content == "small output"

    def test_old_tool_output_low_limit(self):
        """Older tool outputs (not in last 5) get 1KB limit."""
        compactor = ToolResultCompactor()

        # Create 7 tool messages with large content (KEEP_RECENT_N=5)
        msgs = []
        for i in range(7):
            msg = MagicMock()
            msg.type = "tool"
            msg.content = "x" * 5000  # 5KB each
            msgs.append(msg)

        result = compactor.compact(msgs)
        # First 2 (old) should be truncated, last 5 (recent) kept
        for i in range(2):
            assert "TRUNCATED" in result[i].content or len(result[i].content) < 5000

    def test_batch_of_four_stays_intact(self):
        """A typical parallel batch (4 calls) survives the window whole.

        #13-R regression guard: the pre-fix 3-slot window demoted the
        oldest result of the very batch that just returned — a 9785B
        pods enumeration was demoted 78s after arrival, and the
        notice-directed cache re-read was demoted 0.3s after THAT.
        """
        compactor = ToolResultCompactor()

        msgs = [MagicMock(type="tool", content="x" * 5000) for _ in range(4)]
        result = compactor.compact(msgs)
        for i in range(4):
            assert "TRUNCATED" not in result[i].content

    def test_recent_tool_output_high_limit(self):
        """Last 3 tool outputs get 100KB limit."""
        compactor = ToolResultCompactor()

        msg = MagicMock()
        msg.type = "tool"
        msg.content = "y" * 1000  # Well under 100KB

        result = compactor.compact([msg])
        assert "TRUNCATED" not in result[0].content

    def test_cache_to_disk(self, tmp_path):
        """Oversized output should be cached to disk."""
        compactor = ToolResultCompactor(cache_dir=tmp_path / "cache")

        msgs = [MagicMock(type="tool", content="a" * 10000) for _ in range(7)]
        result = compactor.compact(msgs)  # must not raise
        # The 2 oldest messages exceed the 1KB historical cap
        # (KEEP_RECENT_N=5) and land on disk as signature-named
        # artifacts; the 5 recent ones stay in-memory only.
        artifacts = list((tmp_path / "cache").glob("*.txt"))
        assert len(artifacts) == 2
        assert all(_is_cache_artifact_name(p.name) for p in artifacts)
        assert len(result) == 7  # no message lost

    def test_non_string_content_skipped(self):
        msg = MagicMock()
        msg.type = "tool"
        msg.content = 12345  # Not a string

        compactor = ToolResultCompactor()
        result = compactor.compact([msg])
        assert result[0].content == 12345  # Unchanged

    def test_no_tool_messages_unchanged(self):
        msgs = [MagicMock(type="human", content="hello")]
        compactor = ToolResultCompactor()
        result = compactor.compact(msgs)
        assert result == msgs


# ---------------------------------------------------------------------------
# New tests: Time-based MicroCompact (Migration Point 11)
# ---------------------------------------------------------------------------


class TestIsAIMessage:
    """Test AI message detection."""

    def test_ai_message(self):
        msg = MagicMock()
        msg.type = "ai"
        assert is_ai_message(msg) is True

    def test_non_ai_message(self):
        msg = MagicMock()
        msg.type = "human"
        assert is_ai_message(msg) is False


class TestMaybeTimeBasedMicroCompact:
    """Test time-based micro-compact cleanup."""

    def _make_ai_msg(self, minutes_ago: float = 10.0) -> MagicMock:
        """Create an AI message with a timestamp minutes_ago."""
        msg = MagicMock()
        msg.type = "ai"
        ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        msg.additional_kwargs = {"timestamp": ts}
        return msg

    def _make_tool_msg(self, content: str = "kubectl output") -> MagicMock:
        """Create a tool result message."""
        msg = MagicMock()
        msg.type = "tool"
        msg.content = content
        return msg

    def _make_human_msg(self, content: str = "user request") -> MagicMock:
        msg = MagicMock()
        msg.type = "human"
        msg.content = content
        return msg

    def test_returns_none_when_no_ai_message(self):
        msgs = [self._make_human_msg()]
        result = maybe_time_based_microcompact(msgs)
        assert result is None

    def test_returns_none_when_gap_too_short(self):
        ai_msg = self._make_ai_msg(minutes_ago=1.0)  # Only 1 min ago
        msgs = [ai_msg, self._make_tool_msg()]
        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0)
        assert result is None

    def test_returns_none_when_few_tool_results(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        msgs = [ai_msg, self._make_tool_msg()]
        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=3)
        assert result is None  # Only 1 tool result, <= keep_recent

    def test_clears_old_tool_results(self):
        """Old tool results (beyond keep_recent) are replaced with CLEARED_MARKER."""
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        assert result is not None
        # First 3 should be cleared, last 2 kept
        for i in range(3):
            assert result[1 + i].content == CLEARED_MARKER  # +1 for ai_msg offset
        # Last 2 should be preserved
        assert result[-1].content == "output 4"
        assert result[-2].content == "output 3"

    def test_preserves_recent_tool_results(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=3)
        assert result is not None
        # Last 3 should be preserved
        for i in range(3):
            assert result[-(i + 1)].content != CLEARED_MARKER

    def test_non_tool_messages_unchanged(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        human_msg = self._make_human_msg("keep this")
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [human_msg, ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        assert result is not None
        # Human message should be unchanged
        assert result[0].content == "keep this"

    def test_already_cleared_not_double_cleared(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        # Mark one as already cleared
        tool_msgs[0].content = CLEARED_MARKER
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        # Should still work (returns modified list, but no new modification for already-cleared)
        assert result is not None

    def test_ai_timestamp_as_iso_string(self):
        """Test that ISO format string timestamps work."""
        ai_msg = MagicMock()
        ai_msg.type = "ai"
        ts = (datetime.now(timezone.utc) - timedelta(minutes=10.0)).isoformat()
        ai_msg.additional_kwargs = {"timestamp": ts}

        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        assert result is not None


class TestToolResultCompactorWithTimeMC:
    """Test ToolResultCompactor.compact() integrates time-based micro-compact."""

    def test_time_based_cleanup_before_truncation(self):
        """Time-based micro-compact runs first, then size truncation."""
        compactor = ToolResultCompactor()

        # Create AI message from 10 min ago
        ai_msg = MagicMock()
        ai_msg.type = "ai"
        ts = datetime.now(timezone.utc) - timedelta(minutes=10.0)
        ai_msg.additional_kwargs = {"timestamp": ts}
        ai_msg.content = "assistant response"

        # Create tool messages
        tool_msgs = []
        for i in range(5):
            msg = MagicMock()
            msg.type = "tool"
            msg.content = f"output {i}"
            tool_msgs.append(msg)

        msgs = [ai_msg] + tool_msgs
        result = compactor.compact(msgs)
        # Should have processed messages (time cleanup + truncation)
        assert len(result) == len(msgs)


class TestTruncationNotice:
    """Problem D: a compacted historical notice must steer the model away
    from acting destructively on output whose structure is no longer visible."""

    def test_historical_notice_warns_against_destructive_action(self):
        notice = build_truncation_notice(
            original_size=100_000, max_bytes=1024, is_recent=False,
            cache_path="/tmp/cache/out.txt",
        )
        assert "TRUNCATED" in notice
        assert "/tmp/cache/out.txt" in notice
        assert "NEVER execute a destructive or structural change" in notice
        # Command-agnostic narrowing directive (not the cache re-read —
        # a re-read returns the full original as a demotable tool
        # result; #13-R): the model derives the narrower re-run from
        # the producing command visible one message up.
        assert "re-run the command that produced it" in notice

    def test_recent_notice_keeps_strategy_hints(self):
        notice = build_truncation_notice(
            original_size=100_000, max_bytes=16 * 1024, is_recent=True,
            cache_path="/tmp/cache/out.txt",
        )
        assert "OUTPUT_TRUNCATED" in notice
        assert "field-selector" in notice

    # ── truncation-governance-consistency: three-field invariant ──
    # Both variants now carry the shared skeleton: marker + original
    # size + retrieval path (the historical variant gained the size
    # field when the construction was delegated to the shared module).

    def test_historical_notice_three_field_invariant(self):
        notice = build_truncation_notice(
            original_size=100_000, max_bytes=1024, is_recent=False,
            cache_path="/tmp/cache/out.txt",
        )
        assert "⚠️ TRUNCATED" in notice        # marker family member
        assert "100000 bytes" in notice        # honest original size
        assert "/tmp/cache/out.txt" in notice  # retrieval path

    def test_recent_notice_three_field_invariant(self):
        notice = build_truncation_notice(
            original_size=100_000, max_bytes=16 * 1024, is_recent=True,
            cache_path="/tmp/cache/out.txt",
        )
        assert "⚠️ OUTPUT_TRUNCATED" in notice
        assert "97KB" in notice                # size in the recent KB convention
        assert "Full output cached at: /tmp/cache/out.txt" in notice


class TestSmartStripStructuralValidity:
    """truncation-governance-consistency (TDD): the smart stripper must
    produce STRUCTURALLY valid output — K8s schema shapes and parseable
    JSON — and honor the budget contract. These assertions were written
    FIRST (red) against the two live bugs: the array-shape loss in
    _strip_item and the fake closing bracket in truncate_json_at_boundary."""

    @staticmethod
    def _pod(name: str, restarts: int = 3) -> dict:
        return {
            "kind": "Pod",
            "metadata": {"name": name, "namespace": "default"},
            "spec": {"nodeName": "node-a"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "restartCount": restarts,
                        "state": {"running": {"startedAt": "2026-01-01T00:00:00Z"}},
                        "image": "busybox:latest",
                    }
                ],
            },
        }

    @staticmethod
    def _podlist(pods) -> str:
        return json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "metadata": {}, "items": list(pods)},
            ensure_ascii=False,
        )

    def test_container_statuses_keeps_list_shape(self):
        """`containerStatuses[0].restartCount` field paths must produce a
        LIST in the stripped output (K8s schema) — the extraction side
        handles the index; the construction side must not flatten it to
        a dict. NOTE: multi-container pods keep only [0] (the whitelist
        indexes the first container only) — preserved field-selection
        policy, not a shape bug."""
        out = smart_strip_k8s_json(self._podlist([self._pod("a"), self._pod("b")]), 16 * 1024)
        assert out is not None
        data = json.loads(out)
        for item in data["items"]:
            cs = item["status"]["containerStatuses"]
            assert isinstance(cs, list), f"containerStatuses must be a list, got {type(cs).__name__}"
            assert cs[0]["restartCount"] == 3
            assert "state" in cs[0]
            assert "image" in cs[0]
        assert data["truncated"] is True

    def test_progressive_pop_stays_valid_json(self):
        """Popping items off the end to fit the budget must leave a
        parseable JSON at every step (remaining items keep full fields)."""
        pods = [self._pod(f"p-{i:03d}", restarts=i) for i in range(40)]
        out = smart_strip_k8s_json(self._podlist(pods), 1200)
        assert out is not None
        data = json.loads(out)  # parseable after popping
        assert 1 <= len(data["items"]) < 40   # items were actually popped
        assert data["truncated"] is True
        assert len(out.encode("utf-8")) <= 1200

    def test_single_item_over_budget_honors_budget_contract(self):
        """Budget contract floor: when ONE item's stripped serialization
        still exceeds the budget, fall back to an honest plain-text cut
        (result ≤ budget) instead of silently returning over-budget JSON.
        JSON-awareness is best-effort, not a hard guarantee."""
        giant = self._pod("x" * 900)
        giant["status"]["conditions"] = [
            {"type": f"T{i}", "status": "True", "message": "m" * 120}
            for i in range(20)
        ]
        out = smart_strip_k8s_json(self._podlist([giant]), 600)
        assert out is not None
        assert len(out.encode("utf-8")) <= 600  # budget contract honored


class TestSmartStripK8sSignatureGate:
    """issue #1347 + its silent sibling: ``items`` presence is NOT K8s
    ownership. The gate requires the APIServer list signature (top-level
    ``kind`` ending in ``List``) and all-dict items; anything else returns
    None so the caller falls back to conservative truncation. Misjudging
    "not K8s" costs token efficiency; misjudging "is K8s" costs data or
    the session — the gate must fail closed."""

    def test_string_items_rejected_without_crash(self):
        """Issue #1347 minimal shape: a JSON dict whose ``items`` is a
        STRING array (Jaeger/Prometheus-style pagination). The old code
        ran ``items[0].get("kind")`` on a str and the AttributeError
        propagated uncaught through compact → hook → graph node,
        aborting the session. Must return None instead."""
        content = json.dumps(
            {"ok": True, "service": "jaeger", "total": 2,
             "items": ["GET /api/cart", "POST /api/cart"]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_dict_items_without_list_signature_rejected(self):
        """The silent-data-loss sibling: an ordinary paginated REST dict
        (dict items, no ``kind``) used to be stripped by
        _GENERIC_STRIP_FIELDS down to ``{}`` entries — data lost without
        a crash. Must return None (the generic fallback preserves the
        original bytes)."""
        content = json.dumps(
            {"total": 1,
             "items": [{"id": 1, "endpoint": "/api/cart", "hits": 42}]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_forged_signature_non_dict_items_rejected(self):
        """Fail-closed element check: a well-formed ``kind: PodList``
        signature with string items is contradictory input — reject the
        whole document rather than trust the signature past its items."""
        content = json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "items": ["not-a-pod"]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_mixed_items_rejected(self):
        """Mixed arrays (dict + str) are rejected as a whole — a partial
        strip of only the dict members would silently drop the rest."""
        content = json.dumps(
            {"kind": "PodList", "apiVersion": "v1",
             "items": [{"kind": "Pod", "metadata": {"name": "a"}}, "corrupt"]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_non_list_items_value_rejected(self):
        """Round-34 self-containment: a truthy NON-iterable ``items``
        value (scalar) under a forged List signature used to raise
        TypeError inside the all() iteration — the one remaining
        raise-shaped hole in the entry contract (non-K8s → None, for
        EVERY input shape)."""
        content = json.dumps({"kind": "PodList", "apiVersion": "v1", "items": 5})
        assert smart_strip_k8s_json(content, 1024) is None

    def test_dict_valued_items_rejected(self):
        """``items`` as a mapping (name → result envelopes built by
        in-repo probe collectors) is not a K8s list — reject to the
        fallback instead of iterating its keys as items."""
        content = json.dumps({"items": {"probe-a": {"status": "ok"}}})
        assert smart_strip_k8s_json(content, 1024) is None

    def test_legitimate_podlist_still_stripped(self):
        """Gate tightening must not over-reject: a genuine PodList
        (signature + dict items) keeps going through the smart strip.
        Full stripping behaviour is anchored by
        TestSmartStripStructuralValidity; this pins the minimal
        signature-only acceptance delta."""
        podlist = json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "metadata": {},
             "items": [{"kind": "Pod",
                        "metadata": {"name": "a", "namespace": "default"},
                        "spec": {"nodeName": "node-a"},
                        "status": {"phase": "Running"}}]}
        )
        out = smart_strip_k8s_json(podlist, 16 * 1024)
        assert out is not None
        data = json.loads(out)
        assert data["items"][0]["metadata"]["name"] == "a"


class TestSmartStripMalformedFieldValues:
    """Round-35 A1-A4: a signature-VALID list whose field VALUES have
    the wrong type (scalar spec/status/metadata, null/scalar envelope
    metadata) is a contradictory payload, not a legal K8s response —
    the fail-closed family of the entry gate. These shapes used to
    raise TypeError/AttributeError inside the strip body (behind the
    gate); pre-guard they were session-killers. Must return None so
    the honest fallback takes over."""

    def test_scalar_item_spec_rejected(self):
        content = json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "items": [{"spec": 5}]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_scalar_item_metadata_rejected(self):
        content = json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "items": [{"metadata": 5}]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_scalar_item_status_rejected(self):
        content = json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "items": [{"status": 5}]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_scalar_top_level_metadata_rejected(self):
        """A4: signature-valid + legal items, but the envelope's own
        metadata is a scalar — the body's metadata.pop used to crash."""
        pod = {"kind": "Pod", "metadata": {"name": "a"}}
        content = json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "metadata": 5, "items": [pod]}
        )
        assert smart_strip_k8s_json(content, 1024) is None

    def test_null_top_level_metadata_rejected(self):
        """A4 sibling: explicit null envelope metadata — ``None.pop`` in
        the body would crash the same way as a scalar."""
        pod = {"kind": "Pod", "metadata": {"name": "a"}}
        content = json.dumps(
            {"kind": "PodList", "apiVersion": "v1", "metadata": None, "items": [pod]}
        )
        assert smart_strip_k8s_json(content, 1024) is None


class TestParseBoundaryExceptionFamilies:
    """Round-37: the input-domain exception taxonomy is part of "not a
    usable K8s list / not reducible JSON" — deep nesting blows the json
    parser's recursion limit (RecursionError), and lone surrogates pass
    json.loads but break the strict .encode("utf-8") size checks
    (UnicodeEncodeError). Both public parse boundaries route the whole
    family to the conservative fallback instead of raising."""

    def test_deep_nested_json_rejected(self):
        content = ('{"kind":"PodList","pad":"' + "x" * 20000
                   + '","items":' + "[" * 2500 + "]" * 2500 + "}")
        assert smart_strip_k8s_json(content, 1024) is None

    def test_lone_surrogate_rejected(self):
        """json.loads ACCEPTS the \\ud800 escape; the body's strict
        .encode("utf-8") cannot. Reject to the fallback (whose head-cut
        keeps the original escape-sequence form) — never inject raw
        surrogate characters into message content.

        (The doubled backslash above is load-bearing on Python 3.14: a
        DOCSTRING constant carrying a real lone surrogate fails
        ``compile()`` with UnicodeEncodeError, while the same escape in
        the ordinary literals below stays legal.)
        """
        content = ('{"kind":"PodList","pad":"' + "x" * 20000
                   + '","items":[{"kind":"Pod","metadata":{"name":"\\ud800"}}]}')
        assert smart_strip_k8s_json(content, 16 * 1024) is None

    def test_deep_nested_json_plain_text_cut(self):
        content = ('{"pad":"' + "x" * 20000 + '","deep":'
                   + "[" * 2500 + "]" * 2500 + "}")
        out = truncate_json_at_boundary(content, 500)
        assert len(out.encode("utf-8", errors="replace")) <= 500

    def test_lone_surrogate_plain_text_cut(self):
        content = '{"pad":"' + "x" * 20000 + '", "name": "\\ud800"}'
        out = truncate_json_at_boundary(content, 500)
        assert len(out.encode("utf-8", errors="replace")) <= 500


class TestCacheWriteBestEffort:
    """Round-39: the disk cache sits on compact()'s MANDATORY path
    (the cache write happens BEFORE the wrapped truncation boundaries),
    so a strict cache write made compaction itself fragile: a raw lone
    surrogate in message content blew UnicodeEncodeError at
    write_text(encoding="utf-8"), and an unwritable cache_dir blew the
    OSError family — either aborting truncation wholesale (message
    never compacted, retried every turn, orphaned cache files per
    attempt). The cache is a retrieval artifact, not message content:
    it must be best-effort by the same contract the boundaries obey."""

    def test_raw_surrogate_cached_with_replacement(self, tmp_path):
        """Encoding family: the cache write replaces undecodable bytes
        instead of raising — compaction proceeds with an honest
        truncation AND a live retrieval path, and the surrogate is
        never re-injected into message content. The replacement is
        encode-replace's '?' — the same semantics truncate_text's
        head-cut applies to message content (consistency over
        prettiness: decode-replace's U+FFFD would diverge from the
        rest of the pipeline)."""
        raw = "stdout kept a raw byte [\ud800] then padding " + "x" * 20000
        compactor = ToolResultCompactor(cache_dir=tmp_path / "cache")
        msg = MagicMock(type="tool", content=raw)

        result = compactor.compact([msg], task_id="t")  # must not raise

        assert "TRUNCATED" in result[0].content
        assert "\ud800" not in result[0].content
        cached = list((tmp_path / "cache").glob("*.txt"))
        assert len(cached) == 1  # one artifact — not orphans per failed turn
        assert cached[0].read_text(encoding="utf-8") == raw.replace(
            "\ud800", "?"
        )
        assert str(cached[0]) in result[0].content  # retrieval path emitted

    def test_cache_io_failure_downgrades_to_no_retrieval_path(self, tmp_path):
        """IO family: an unwritable cache_dir must not abort truncation —
        the notice simply omits the retrieval path (a dead cache path
        advertised to the model would be worse than none)."""
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        compactor = ToolResultCompactor(cache_dir=blocker / "sub")
        msg = MagicMock(type="tool", content="y" * 20000)

        result = compactor.compact([msg], task_id="t")  # must not raise

        assert "TRUNCATED" in result[0].content
        assert (
            len(result[0].content.encode("utf-8", errors="replace"))
            <= 16 * 1024
        )
        assert "blocker" not in result[0].content

    def test_expired_cache_artifacts_evicted_on_write(self, tmp_path):
        """TTL sweep: the module docstring's "TTL of 3 days" promise
        implemented — signature-named artifacts older than CACHE_TTL_DAYS
        are swept when a new write lands; within-TTL artifacts are
        retained. Foreign files in the same directory are NEVER touched
        (round-42 O1: the sweep owns only what the compactor itself
        names, not whatever else shares the directory)."""
        compactor = ToolResultCompactor(cache_dir=tmp_path / "cache")
        compactor.cache_dir.mkdir(parents=True, exist_ok=True)
        # Signature-shaped names (uuid4().hex[:8].txt — what _cache_to_disk
        # actually writes); both aged, one past TTL, one within.
        stale = tmp_path / "cache" / "0000dead.txt"
        stale.write_text("ancient")
        fresh = tmp_path / "cache" / "0000beef.txt"
        fresh.write_text("recent")
        # Foreign tenants: extension twins and other caches that could
        # co-locate here — equally ancient, must survive the sweep.
        foreign_txt = tmp_path / "cache" / "notes.txt"
        foreign_txt.write_text("not ours")
        foreign_json = tmp_path / "cache" / "skill_catalog_cache.json"
        foreign_json.write_text("{}")
        ten_days_ago = time.time() - 10 * 86400
        for p in (stale, foreign_txt, foreign_json):
            os.utime(p, (ten_days_ago, ten_days_ago))
        # fresh keeps its write-time mtime (within TTL)

        msg = MagicMock(type="tool", content="z" * 20000)
        compactor.compact([msg], task_id="t")  # the write triggers the sweep

        assert not stale.exists()  # past TTL: evicted
        assert fresh.exists()  # within TTL: retained
        assert foreign_txt.exists()  # not our naming: untouched
        assert foreign_json.exists()  # not our naming: untouched
        remaining = list((tmp_path / "cache").glob("*.txt"))
        assert len(remaining) == 3  # fresh + foreign.txt + the new artifact

    def test_is_cache_artifact_name_boundaries(self):
        """The ownership predicate admits exactly the compactor's naming
        signature — a set-membership test outside the UID-shape
        legislation (filename signature, not a UID shape)."""
        assert _is_cache_artifact_name("0000dead.txt")  # the signature
        assert _is_cache_artifact_name("abcdef01.txt")  # all-hex letters
        for foreign in (
            "0000DEAD.txt",  # uppercase: uuid4().hex is lowercase
            "0000dea.txt",  # 7 hex chars
            "0000deadd.txt",  # 9 hex chars
            "0000dead.txtx",  # wrong suffix
            "0000dead.md",  # wrong suffix
            "0000.dead.txt",  # embedded dot: partition takes the first
            ".txt",  # empty stem
            "0000dead",  # no suffix
            "notes.txt",  # not hex at all
        ):
            assert not _is_cache_artifact_name(foreign), foreign

    def test_eviction_failure_never_blocks_the_write(
        self, tmp_path, monkeypatch, caplog
    ):
        """Eviction is best-effort by the same contract as the write: an
        unlink failure is a loud warning, never an abort — the new
        artifact still lands and compaction proceeds."""
        compactor = ToolResultCompactor(cache_dir=tmp_path / "cache")
        compactor.cache_dir.mkdir(parents=True, exist_ok=True)
        stale = tmp_path / "cache" / "0000dead.txt"  # signature-shaped
        stale.write_text("ancient")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(stale, (ten_days_ago, ten_days_ago))

        def _boom(self):
            raise OSError("disk says no")

        monkeypatch.setattr(Path, "unlink", _boom)

        msg = MagicMock(type="tool", content="z" * 20000)
        with caplog.at_level(
            "WARNING", logger="chaos_agent.memory.tool_compactor"
        ):
            result = compactor.compact([msg], task_id="t")  # must not raise

        assert "TRUNCATED" in result[0].content  # compaction proceeded
        new_artifacts = [
            p for p in (tmp_path / "cache").glob("*.txt") if p != stale
        ]
        assert len(new_artifacts) == 1  # the write itself succeeded
        assert any(  # the failure is observable, not silent
            "Failed to evict" in r.message for r in caplog.records
        )


class TestJsonBoundaryTruncationValidity:
    """TDD (red first): boundary truncation must never fabricate closing
    brackets — dict-shaped content yields either a parseable JSON with a
    live truncated marker, or an honest plain-text cut."""

    def test_dict_shaped_content_parseable_or_plain_text(self):
        """A dict-shaped single object (e.g. one giant Pod with
        managedFields) must NOT come out as `...{` + fabricated `]`."""
        pod = {
            "kind": "Pod",
            "metadata": {
                "name": "giant",
                "managedFields": [
                    {"manager": f"m{i}", "fieldsV1": {"data": "f" * 400}} for i in range(40)
                ],
            },
            "spec": {"nodeName": "node-a"},
            "status": {"phase": "Running"},
        }
        content = json.dumps(pod, ensure_ascii=False)
        assert len(content.encode("utf-8")) > 8000  # force the boundary path

        out = truncate_json_at_boundary(content, 2048)

        try:
            data = json.loads(out)
            # Parseable branch: the truncated marker must actually be set
            # (the dead-code insertion branch must be alive).
            assert data.get("truncated") is True
        except json.JSONDecodeError:
            # Honest plain-text branch: a head cut — acceptable, but no
            # fabricated structural closers may decorate it.
            assert not out.rstrip().endswith("]")

    def test_top_level_array_stays_valid(self):
        """Regression anchor: top-level array content was ALREADY legal
        under the old code — it must stay legal after the rewrite."""
        items = [{"metadata": {"name": f"p{i}"}, "status": {"phase": "Running"}} for i in range(60)]
        content = json.dumps(items, ensure_ascii=False)
        assert len(content.encode("utf-8")) > 1500

        out = truncate_json_at_boundary(content, 700)
        assert json.loads(out)  # still legal JSON

    def test_pseudo_json_gets_plain_text_cut(self):
        """Content that merely LOOKS like JSON (merged error output) must
        get an honest plain-text cut — never fabricated closers."""
        content = '{"half": "json", "stdout": ' + "x" * 3000  # unterminated
        out = truncate_json_at_boundary(content, 500)
        assert len(out.encode("utf-8")) <= 500
        # no fabricated closing structure appended
        assert not out.rstrip().endswith("]}")

    def test_single_giant_list_item_never_yields_empty_array(self):
        """A list whose ONLY item cannot fit the budget must fall back to
        the honest plain-text cut of the original — popping that item to
        satisfy the budget would return "[]" (2 bytes, in-budget, ZERO
        information: the model reads an empty list + notice as "the list
        was empty", losing even the shape of the giant item)."""
        content = json.dumps([{"data": "f" * 800}])
        out = truncate_json_at_boundary(content, 600)
        assert out != "[]"
        assert len(out.encode("utf-8")) <= 600
        # honest head cut keeps the item's shape visible
        assert out.startswith('[{"data"')

    def test_list_reduction_floor_keeps_at_least_one_item(self):
        """Multi-item reduction may drop trailing items but NEVER the
        last one (same floor as smart_strip's while-len>1 loop)."""
        items = json.dumps([{"i": 1, "pad": "p" * 300}, {"i": 2, "pad": "p" * 300}])
        out = truncate_json_at_boundary(items, 400)
        data = json.loads(out)
        if data != json.loads(items):  # some reduction happened
            assert isinstance(data, list) and len(data) >= 1

    def test_large_bare_array_arithmetic_sizing_stays_correct(self):
        """The O(n)-sizing path (replacing pop-and-re-dumps) must keep
        the pop loop's exact semantics on realistic sizes: valid JSON,
        within budget, longest fitting prefix kept, floor of >= 1 item.
        Correctness here is what the arithmetic must not sacrifice for
        the ~50x speedup (520ms -> ~10ms on a 2000-item array)."""
        items = [
            {"name": f"my-app-pod-with-long-name-{i:05d}", "ns": "prod-x"}
            for i in range(2000)
        ]
        content = json.dumps(items, ensure_ascii=False)
        out = truncate_json_at_boundary(content, 1024)
        data = json.loads(out)  # still legal JSON
        assert isinstance(data, list) and len(data) >= 1  # floor
        assert len(out.encode("utf-8")) <= 1024
        # longest fitting prefix: leading items preserved in order
        assert data[0]["name"] == "my-app-pod-with-long-name-00000"
        assert data[-1]["name"] == f"my-app-pod-with-long-name-{len(data)-1:05d}"

    def test_exactly_at_budget_passthrough(self):
        """len(out) == max_bytes is WITHIN budget (<=): no reduction, no
        pop — the array returns byte-identical. Guards the first
        whole-array check's boundary (a strict < would wrongly enter
        reduction; the arithmetic tail then compensates, so the
        load-bearing variant lives in the test below)."""
        content = json.dumps([{"a": 1}, {"b": 2}])  # exactly 20 bytes
        assert len(content.encode("utf-8")) == 20
        out = truncate_json_at_boundary(content, 20)
        assert out == content

    def test_prefix_exactly_fills_budget_keeps_last_fitting_item(self):
        """The arithmetic loop's boundary is `running > max_bytes`
        (strict): a prefix that EXACTLY fills the budget keeps its last
        fitting item — an off-by-one (`>=`) would break early and
        silently drop a whole item. Constructed so the whole-array
        check fails (30B > 20B) and the arithmetic path actually RUNS:
        [A, B] serializes to exactly 20B of the 30B input."""
        content = json.dumps([{"a": 1}, {"b": 2}, {"c": 3}])  # 30B total
        expected = json.dumps([{"a": 1}, {"b": 2}])            # exactly 20B
        assert len(content.encode("utf-8")) == 30
        assert len(expected.encode("utf-8")) == 20
        out = truncate_json_at_boundary(content, 20)
        assert out == expected

    def test_mixed_type_array_degrades_legally(self):
        """Non-dict items (ints/strings/nested lists/None/bools) survive
        _prune_bloat and the arithmetic sizing unchanged in kind — the
        JSON-aware path is not dict-only."""
        content = json.dumps([1, "two", {"three": 3}, [4], None, True])
        out = truncate_json_at_boundary(content, 15)
        assert len(out.encode("utf-8")) <= 15
        assert out == json.dumps([1, "two"])  # longest legal prefix

    def test_dict_truncated_marker_overrides_original_false(self):
        """A dict already carrying "truncated": false must not ride it
        along unchanged: bloat removal IS a truncation, and keeping
        false reports "nothing was lost" on reduced content."""
        content = json.dumps({
            "truncated": False,
            "managedFields": "x" * 800,
            "data": "ok",
        })
        out = truncate_json_at_boundary(content, 400)
        data = json.loads(out)
        assert data.get("truncated") is True


class TestSmartStripArithmeticEquivalence:
    """truncation-debt-cleanup (task 2.2): the arithmetic sizing in
    ``smart_strip_k8s_json`` must be BYTE-IDENTICAL to the old
    pop-and-re-dumps loop across randomized inputs — a performance
    rewrite must not change observable semantics.

    The reference below re-implements the OLD collection algorithm
    (pop one item, re-dump the whole document, repeat). The strip
    preprocessing (_detect_item_kind/_get_strip_fields/_strip_item) is
    deliberately SHARED with the code under test — the equivalence under
    test is the collection arithmetic, not the (unchanged) stripping.
    """

    @staticmethod
    def _reference_pop_loop(content: str, max_bytes: int):
        """Independent re-implementation of the pre-2.1 algorithm."""
        import chaos_agent.memory.tool_compactor as tc

        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or "items" not in data:
            return None
        items = data.get("items", [])
        if not items:
            return None
        first_kind = tc._detect_item_kind(items[0])
        strip_fields = tc._get_strip_fields(first_kind)
        stripped_items = [tc._strip_item(item, strip_fields) for item in items]
        stripped_data = dict(data)
        stripped_data["items"] = stripped_items
        stripped_data["truncated"] = True
        if "metadata" in stripped_data:
            stripped_data["metadata"].pop("annotations", None)
            stripped_data["metadata"].pop("managedFields", None)

        result = json.dumps(stripped_data, ensure_ascii=False)
        if len(result.encode("utf-8")) <= max_bytes:
            return result
        while len(stripped_items) > 1:
            stripped_items.pop()
            stripped_data["items"] = stripped_items
            result = json.dumps(stripped_data, ensure_ascii=False)
            if len(result.encode("utf-8")) <= max_bytes:
                break
        if len(result.encode("utf-8")) > max_bytes:
            return tc.truncate_text(content, max_bytes)
        return result

    @staticmethod
    def _random_podlist(rng: random.Random) -> str:
        """Randomized PodList: item count, field sizes, bloat, and nested
        shapes all vary so the corpus crosses every collection branch —
        no-truncation, item collection, and single-item overflow."""
        n = rng.randint(2, 200)
        items = []
        for i in range(n):
            pad = "x" * rng.randint(0, 80)
            items.append({
                "kind": "Pod",
                "metadata": {
                    "name": f"pod-{i}-{pad[:20]}",
                    "namespace": f"ns-{pad[:8]}" if pad else "default",
                    "labels": {"app": "a" * rng.randint(1, 10)},
                    "annotations": {
                        "kubectl.kubernetes.io/last-applied": "y" * rng.randint(0, 100),
                    },
                },
                "spec": {"nodeName": f"node-{i % 7}"},
                "status": {
                    "phase": rng.choice(["Running", "Pending", "Succeeded"]),
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": rng.randint(0, 9),
                        "ready": rng.choice([True, False]),
                    }],
                    "conditions": [
                        {"type": f"T{j}", "status": "True",
                         "message": "m" * rng.randint(0, 60)}
                        for j in range(rng.randint(0, 4))
                    ],
                },
            })
        doc = {
            "apiVersion": "v1",
            "kind": "PodList",
            "metadata": {
                "resourceVersion": "12345",
                "annotations": {"z": "z" * rng.randint(0, 50)},
            },
            "items": items,
        }
        return json.dumps(doc, ensure_ascii=False)

    def test_arithmetic_matches_pop_loop_byte_for_byte(self):
        """200+ randomized cases: arithmetic sizing vs pop-loop output
        must be byte-identical (the spec pins equivalence, not just
        similarity — different outputs would mean the rewrite changed
        WHICH items survive, a semantic drift invisible to size-only
        assertions)."""
        rng = random.Random(20260910)
        checked = 0
        branch_coverage = {"no_truncation": 0, "collection": 0, "fallback": 0}
        for case in range(240):
            content = self._random_podlist(rng)
            # Budget sampling is STRATIFIED: one case in five draws from
            # a dedicated small-budget stratum (30-260B), because a
            # stripped item runs ~100-300B — uniform sampling over the
            # full range almost never lands below a single item, and the
            # fallback branch would go unexercised (measured: 4/240
            # hits before stratification). The rest span the full range:
            # collection middle → below the stripped whole (no cut).
            if case % 5 == 0:
                budget = rng.randint(30, 260)
            else:
                budget = rng.randint(60, max(61, len(content)))
            ref = self._reference_pop_loop(content, budget)
            out = smart_strip_k8s_json(content, budget)
            assert out == ref, (
                f"case {case}: outputs diverge at budget={budget} "
                f"(ref {len(ref) if ref else None}B vs "
                f"out {len(out) if out else None}B)"
            )
            checked += 1
            # Branch bookkeeping: every branch must be exercised by the
            # corpus, else the equivalence is only proven on a subset.
            # A truncate_text fallback is a plain-text HEAD cut of the
            # original, which fails json.loads (the cut lands inside the
            # JSON body); a JSON-path result always parses.
            try:
                parsed = json.loads(out)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if parsed is None:
                branch_coverage["fallback"] += 1
            elif len(parsed["items"]) == len(json.loads(content)["items"]):
                branch_coverage["no_truncation"] += 1
            else:
                branch_coverage["collection"] += 1
        assert checked == 240
        assert branch_coverage["collection"] >= 50, (
            f"corpus too weak: {branch_coverage} — the collection branch "
            "must dominate the equivalence claim"
        )
        assert branch_coverage["fallback"] >= 5


class TestSmartStripPerformance:
    """truncation-debt-cleanup (task 2.3): compactor runs synchronously
    before every LLM turn, so per-message stalls are user-visible. The
    pre-2.1 pop-and-re-dumps loop measured 20.8s (proposal fixture) /
    7.5s (2026-09-10 re-measure, this machine) for a 5000-pod PodList at
    the 16KB recent budget; the arithmetic sizing landed at 37ms."""

    def test_5000_pod_podlist_within_budget_and_time(self):
        item = {
            "kind": "Pod",
            "metadata": {
                "name": "pod-" + "x" * 40,
                "namespace": "default",
                "uid": "uid" * 8,
                "labels": {"app": "myapp", "pod-template-hash": "hash" * 4},
            },
            "spec": {"containers": [{"name": "main", "image": "registry/app:v1.2.3"}]},
            "status": {
                "phase": "Running",
                "podIP": "10.1.2.3",
                "containerStatuses": [
                    {"name": "main", "ready": True, "restartCount": 3}
                ],
            },
        }
        content = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "PodList",
                "metadata": {"resourceVersion": "123"},
                "items": [dict(item) for _ in range(5000)],
            },
            ensure_ascii=False,
        )
        assert len(content.encode("utf-8")) > 1536 * 1024  # ~1.9MB corpus

        t0 = time.perf_counter()
        out = smart_strip_k8s_json(content, 16 * 1024)
        elapsed = time.perf_counter() - t0

        assert out is not None
        # Spec budget: 500ms for a ~2.3MB corpus (13x headroom over the
        # measured 37ms; generous enough for CI machines, tight enough
        # that the O(n²) form — 7.5s+ — can never pass).
        assert elapsed < 0.5, f"smart_strip took {elapsed:.2f}s (budget 0.5s)"
        assert len(out.encode("utf-8")) <= 16 * 1024
        data = json.loads(out)  # still valid JSON
        assert len(data["items"]) >= 1
