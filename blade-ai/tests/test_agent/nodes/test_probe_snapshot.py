"""Probe snapshot pipeline tests (tier1-speedup).

Covers the dual-source harvester in ``intent_confirm``, the
``[FAULT INTENT]`` tail-section renderer in ``agent_loop``, the
approved-handoff wiring, and — unit 1.6b — the verification that the
EXISTING ledger re-injection chain already carries intent-time facts
into the planner's system prompt. No new passthrough code was built for
that link; these tests pin it so a future refactor cannot silently
break the intent→plan fact flow.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.execute.agent_loop import (
    _ledger_tail_for_planning,
    _render_probe_snapshot_section,
)
from chaos_agent.agent.nodes.planning import intent_confirm as ic_mod
from chaos_agent.agent.nodes.planning.handoff_strip import (
    CONTEXT_ANCHOR_FLAG,
    select_strip_targets,
)
from chaos_agent.agent.nodes.planning.intent_confirm import (
    _harvest_probe_snapshot,
    intent_confirm,
)
from chaos_agent.agent.state import IntentState
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.tools.progress import update_progress


def _spec(**overrides) -> FaultSpec:
    base = dict(
        namespace="cms-demo",
        scope="pod",
        fault_target="cpu",
        fault_action="fullload",
        names=("drill-target",),
        params={"process": "nginx"},
        duration_seconds=600,
    )
    base.update(overrides)
    return FaultSpec(**base)


def _self_record(facts: list) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "update_progress",
        "args": {"state_update": {"established_facts": facts}},
        "id": "up-1",
    }])


def _probe_call(call_id: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "kubectl_read", "args": args, "id": call_id,
    }])


# ── Primary source: the model's own update_progress records ────────────
# Fixture facts carry the spec's target token ("drill-target"): the
# cross-intent guard drops a whole token-less batch as previous-intent
# residue (see TestCrossIntentStalenessGuard at the bottom of this file).


class TestHarvestPrimarySource:
    def test_self_recorded_facts_keep_model_wording(self):
        msgs = [_self_record([
            "target pod drill-target is Running on node worker-1",
        ])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert snap is not None
        entry = snap["facts"][0]
        assert entry["fact"] == "target pod drill-target is Running on node worker-1"
        assert entry["source_tool"] == "update_progress"
        assert entry["probed_at"] == "2026-08-26T10:00:00+08:00"

    def test_last_update_progress_call_defines_view(self):
        """The ledger state layer is shallow-overwrite; the snapshot must
        agree with the ledger's FINAL view, not merge superseded facts."""
        msgs = [
            _self_record(["wrong A", "dropped B"]),
            AIMessage(content="", tool_calls=[{
                "name": "update_progress",
                "args": {"state_update": {"established_facts": ["drill-target corrected A"]}},
                "id": "up-2",
            }]),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert [f["fact"] for f in snap["facts"]] == ["drill-target corrected A"]

    def test_log_only_call_is_skipped_earlier_facts_still_harvested(self):
        msgs = [
            _self_record(["drill-target fact from round 1"]),
            AIMessage(content="", tool_calls=[{
                "name": "update_progress",
                "args": {"log_append": [{"event": "m", "status": "observed"}]},
                "id": "up-2",
            }]),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert snap["facts"][0]["fact"] == "drill-target fact from round 1"

    def test_json_string_state_update_tolerated(self):
        """Models do pass the inner dict as a JSON string — the same
        mis-formatting ``progress._coerce_json_arg`` guards against."""
        msgs = [AIMessage(content="", tool_calls=[{
            "name": "update_progress",
            "args": {"state_update": "{\"established_facts\": [\"drill-target json-fact\"]}"},
            "id": "up-1",
        }])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert snap["facts"][0]["fact"] == "drill-target json-fact"


# ── Fallback source: deterministic rows from read-only tool results ────


class TestHarvestFallback:
    def test_fallback_fills_when_model_forgot(self):
        msgs = [
            _probe_call("t1", {"verb": "describe", "pod": "drill-target"}),
            ToolMessage(
                content="Name: drill-target\nRestart Policy: Always",
                tool_call_id="t1", name="kubectl_read",
            ),
            _probe_call("t2", {"verb": "get"}),
            ToolMessage(
                content="NAME READY STATUS\ndrill-target 1/1 Running",
                tool_call_id="t2", name="kubectl_read",
            ),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        facts = [f["fact"] for f in snap["facts"]]
        assert any("Restart Policy: Always" in f for f in facts)
        assert any(f.startswith("drill-target 1/1") or f.startswith("Name: drill-target")
                   for f in facts)
        assert all(f["source_tool"] == "kubectl_read" for f in snap["facts"])

    def test_fallback_scans_host_read_too(self):
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "host_read", "args": {"cmd": "systemctl status nginx"},
                "id": "h1",
            }]),
            ToolMessage(
                content="Active: active (running) nginx",
                tool_call_id="h1", name="host_read",
            ),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert any(f["source_tool"] == "host_read" for f in snap["facts"])

    def test_ps_row_with_colon_suffix_matched(self):
        """Real ``ps aux`` output renders ``nginx: master`` — the param
        value must match through word boundaries, not bare token equality."""
        msgs = [
            _probe_call("t3", {"verb": "exec", "pod": "drill-target",
                               "command": "ps aux"}),
            ToolMessage(
                content="USER PID %CPU COMMAND\nroot 1 0.1 nginx: master",
                tool_call_id="t3", name="kubectl_read",
            ),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert any("nginx: master" in f["fact"] for f in snap["facts"])

    def test_restart_policy_requires_target_context(self):
        """A describe of an UNRELATED pod must not contribute its
        restartPolicy as a fact about our target."""
        msgs = [
            _probe_call("t1", {"verb": "describe", "pod": "other-pod"}),
            ToolMessage(content="Restart Policy: Never",
                        tool_call_id="t1", name="kubectl_read"),
        ]
        assert _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00") is None

    def test_prefix_name_not_matched(self):
        msgs = [
            _probe_call("t1", {"verb": "get"}),
            ToolMessage(content="drill-target-2 1/1 Running",
                        tool_call_id="t1", name="kubectl_read"),
        ]
        assert _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00") is None

    def test_blade_tools_not_scanned(self):
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "blade_status", "args": {}, "id": "b1",
            }]),
            ToolMessage(content="drill-target experiment running",
                        tool_call_id="b1", name="blade_status"),
        ]
        assert _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00") is None


# ── Merge discipline and caps ───────────────────────────────────────────
# Same fixture convention as above: facts carry the spec's target token
# so the cross-intent guard keeps the batch.


class TestHarvestMergeAndCaps:
    def test_self_recorded_wins_on_collision(self):
        """Same information from both sources → keep the model's wording,
        do not spend two snapshot slots on one fact."""
        msgs = [
            _self_record(["target pod drill-target is Running on node worker-1"]),
            _probe_call("t1", {"verb": "get"}),
            ToolMessage(content="drill-target 1/1 Running 0 5m",
                        tool_call_id="t1", name="kubectl_read"),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert len(snap["facts"]) == 1
        assert snap["facts"][0]["source_tool"] == "update_progress"

    def test_cap_twelve_drops_oldest(self):
        msgs = [_self_record([f"drill-target fact-{i} established" for i in range(15)])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert len(snap["facts"]) == 12
        # Head of the model's list is the oldest — dropped first, mirroring
        # the ledger's keep-tail rolling.
        assert snap["facts"][0]["fact"] == "drill-target fact-3 established"
        assert snap["facts"][-1]["fact"] == "drill-target fact-14 established"

    def test_fact_clipped_to_200_chars(self):
        # Token at the head survives the clip; the guard checks the batch
        # BEFORE clipping, so the batch passes and the stored fact is clipped.
        msgs = [_self_record(["drill-target " + "x" * 500])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert len(snap["facts"][0]["fact"]) == 200

    def test_any_failure_degrades_to_none(self):
        class Bomb:
            @property
            def names(self):
                raise RuntimeError("boom")

        assert _harvest_probe_snapshot([], Bomb()) is None

    def test_empty_history_returns_none(self):
        assert _harvest_probe_snapshot([], _spec()) is None


# ── Approved-handoff wiring ─────────────────────────────────────────────


class TestApprovedHandoffWiring:
    async def _noop(self, *args, **kwargs):
        return None
    def _long_dialogue_state(self) -> dict:
        """Longer than the trim window (4) so the harvest must happen
        BEFORE the trim drops the early clarification turns."""
        return {
            "task_id": "t-probe-1",
            "fault_spec": _spec().to_dict(),
            "intent_confidence": 0.9,
            "dialogue_round": 3,
            "messages": [
                HumanMessage(content="inject cpu fault", id="m1"),
                _self_record(["target pod drill-target is Running on node worker-1"]),
                AIMessage(content="", tool_calls=[{
                    "name": "kubectl_read", "args": {"verb": "get"}, "id": "k1",
                }], id="m2"),
                ToolMessage(content="drill-target 1/1 Running",
                            tool_call_id="k1", name="kubectl_read", id="m3"),
                AIMessage(content="probing the target…", id="m4"),
                HumanMessage(content="confirmed", id="m5"),
            ],
        }

    async def test_approved_writes_snapshot_harvested_before_trim(self, monkeypatch):
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "approved")
        monkeypatch.setattr(ic_mod, "_revive_task_row", self._noop)
        delta = await intent_confirm(self._long_dialogue_state())
        # The trim DID fire (remove list non-empty) yet the snapshot still
        # holds the early-turn fact → harvest ran on the pre-trim history.
        assert any(getattr(m, "id", None) for m in delta["messages"])
        snap = delta["probe_snapshot"]
        assert snap is not None
        facts = [f["fact"] for f in snap["facts"]]
        assert any("Running on node worker-1" in f for f in facts)

    async def test_rejected_does_not_write_snapshot(self, monkeypatch):
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "rejected")
        monkeypatch.setattr(ic_mod, "_cancel_task_row", self._noop)
        delta = await intent_confirm(self._long_dialogue_state())
        assert delta == {"confirmed_intent": None}

    async def test_stale_snapshot_overwritten_on_new_handoff(self, monkeypatch):
        """The field is durable; a second fault in the same session must
        not inherit the first fault's snapshot."""
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "approved")
        monkeypatch.setattr(ic_mod, "_revive_task_row", self._noop)
        state = self._long_dialogue_state()
        state["probe_snapshot"] = {"facts": [{
            "fact": "stale from previous fault",
            "source_tool": "update_progress",
            "probed_at": "2026-08-26T08:00:00+08:00",
        }]}
        state["messages"] = [HumanMessage(content="second fault, no probes")]
        delta = await intent_confirm(state)
        assert delta["probe_snapshot"] is None

    async def test_dry_run_writes_snapshot_too(self, monkeypatch):
        """Dry-Run mirrors the approved pipeline; its plan preview needs
        the same evidence."""
        monkeypatch.setattr(ic_mod, "_revive_task_row", self._noop)
        state = self._long_dialogue_state()
        state["dry_run"] = True
        delta = await intent_confirm(state)
        assert "probe_snapshot" in delta
        assert delta["probe_snapshot"] is not None


# ── [FAULT INTENT] tail-section rendering ───────────────────────────────


class TestSnapshotRendering:
    def _snap(self, probed_at: str = "2026-08-26T09:00:00+08:00") -> dict:
        return {"facts": [{
            "fact": "target pod drill-target is Running on node worker-1",
            "source_tool": "update_progress",
            "probed_at": probed_at,
        }]}

    def test_section_header_and_same_source_claim(self):
        text = "\n".join(_render_probe_snapshot_section(self._snap()))
        assert "### Environment facts from intent dialogue" in text
        assert "Same source as the progress" in text
        assert "authoritative record" in text

    def test_age_rendered_as_fact_form(self):
        text = "\n".join(_render_probe_snapshot_section(self._snap()))
        assert "probed ~" in text
        assert "via update_progress" in text
        # Age is anchored to PLANNING START, not "now": the anchored message
        # is frozen after the first planning round, so a "~2m ago" tense
        # would rot as the message survives into verify/recover.
        assert "before planning" in text
        assert " ago" not in text
        # No verdict vocabulary — judgement is the reader's, never the
        # renderer's.
        low = text.lower()
        assert "stale" not in low
        assert "expired" not in low

    def test_bad_timestamp_degrades_to_no_age(self):
        text = "\n".join(_render_probe_snapshot_section(self._snap("not-a-time")))
        assert "(via update_progress)" in text
        assert "probed ~" not in text

    def test_empty_snapshot_renders_no_section(self):
        assert _render_probe_snapshot_section(None) == []
        assert _render_probe_snapshot_section({}) == []
        assert _render_probe_snapshot_section({"facts": []}) == []
        assert _render_probe_snapshot_section({"facts": [None, {}, {"fact": ""}]}) == []

    def test_old_checkpoint_without_snapshot_renders_no_section(self):
        """Checkpoints written before this change carry no probe_snapshot
        key — state reads None, rendering must be a no-op."""
        assert _render_probe_snapshot_section(None) == []

    def test_consumption_rules_present_and_threshold_free(self):
        """Unit 2.1/2.2 — the consumption rules pinned at the section tail:
        age-is-not-a-verdict, self-adjudicated re-verification (both the
        plan-breaking and the cheap-unsure cases), no indiscriminate
        re-probing, and runtime-wins-on-contradiction. Wording aligns with
        preplan_probe's "hints, not verdicts" discipline."""
        text = "\n".join(_render_probe_snapshot_section(self._snap()))
        # (1) age is a fact, not a verdict — older ⇒ likelier drift,
        #     but never proof of invalidity
        assert "age alone never proves a fact wrong" in text
        # (2) re-verification is the reader's call, by load-bearing degree
        #     and re-check cost
        assert "Whether to re-verify" in text and "is your call per fact" in text
        assert "fall apart without" in text and "deserves a re-check" in text
        assert "cheap to re-check" in text and "unsure about it should be re-checked" in text
        # (3) no indiscriminate re-probing of accepted facts
        assert "do NOT re-probe accepted facts indiscriminately" in text
        # (4) contradiction → trust runtime observation
        assert "trust your observation" in text
        # hints-not-verdicts stance
        assert "hints, not verdicts" in text
        # No threshold vocabulary anywhere in the section
        low = text.lower()
        for banned in ("stale", "expired", "older than", "max age", "refresh required"):
            assert banned not in low, banned


# ── Unit 1.6b: the EXISTING ledger chain carries intent facts to plan ──


class TestLedgerPassthroughVerification:
    def test_intent_facts_reach_plan_system_prompt_via_existing_ledger_chain(self):
        """Full chain: the clarification-bound update_progress tool writes
        the ledger → ``_ledger_tail_for_planning`` renders those facts
        with the anti-re-derivation directive. This change added NO new
        passthrough code — these assertions pin the pre-existing chain.

        context-cache-prefix-stability Unit A (task 2.6): the planning ledger
        moved OUT of the FULL system head onto the message tail, so the helper
        now returns the TAIL rendering — supersedes marker + no-anchor directive
        + body. The no-anchor variant is what renders during planning (the
        anchor is not frozen until execute_loop), so its wording must survive the
        move.
        """
        # 1. The tool submits its ledger delta (InjectedState passed by hand)
        #    and the progress_ledger channel's reducer applies it — the same
        #    fold the real graph performs (Case #46 protocol: tools submit
        #    deltas, merge_ledger_channel applies them).
        from chaos_agent.agent.progress_ledger import merge_ledger_channel

        cmd = update_progress.func(
            state_update={"established_facts": [
                "target pod drill-target is Running on node worker-1",
            ]},
            state={},
            tool_call_id="t1",
        )
        ledger = merge_ledger_channel(
            None, cmd.update["progress_ledger"],
        )
        facts = (ledger.get("state") or {}).get("established_facts") or []
        assert any("drill-target" in f for f in facts)
        # 2. The planner's ledger TAIL renders those facts + the no-anchor
        #    directive + the D2 supersedes marker.
        section = _ledger_tail_for_planning({"progress_ledger": ledger})
        assert "drill-target" in section
        assert "do not re-derive what is already established" in section
        assert "supersedes" in section

    # "update_progress is bound in clarification" is pinned by the tool-set
    # baseline in test_factory.py; "same ledger semantics everywhere" holds
    # by construction — factory binds the single module-level tool object
    # (one import site, tools/progress.py) into every phase's static_base.


# ── Anchor survival: the section rides the [FAULT INTENT] message ──────


class TestAnchorSurvival:
    def test_snapshot_section_survives_handoff_strip(self):
        """The snapshot rides the CONTEXT_ANCHOR_FLAG message; strip
        selection keeps anchors regardless of content, so the section
        survives every later trim by construction."""
        anchor = HumanMessage(
            content=(
                "[FAULT INTENT — UNVERIFIED parameters from user dialogue]\n"
                "Fault type: pod_cpu_fullload\n"
                "\n### Environment facts from intent dialogue\n"
                "- target pod drill-target is Running (probed ~3m before planning, via update_progress)"
            ),
            additional_kwargs={CONTEXT_ANCHOR_FLAG: True},
        )
        noise = [AIMessage(content="probe turn"), HumanMessage(content="turn")]
        seq = [anchor, *noise]
        targets = select_strip_targets(seq, 0, len(seq))
        target_ids = {t.id for t in targets}
        assert anchor.id not in target_ids


# ── Unit 4.1: real-trace replay (sess_9d6b3bbf, task inject-8b757abb) ──

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "probe_snapshot_replay_sess_9d6b3bbf.json"


def _load_replay() -> tuple[list, FaultSpec]:
    """Convert the recorded TUI-session trace into LangChain messages the
    harvester reads, plus the FaultSpec the intent dialogue settled on."""
    data = json.loads(_FIXTURE.read_text())
    spec = FaultSpec(
        namespace="ark-system", scope="pod", fault_target="process",
        fault_action="kill", names=tuple(data["spec"]["names"]),
        params=data["spec"]["params"], duration_seconds=600,
    )
    messages = []
    for rec in data["messages"]:
        if rec["role"] == "ai":
            messages.append(AIMessage(content="", tool_calls=rec["tool_calls"]))
        else:
            messages.append(ToolMessage(
                content=rec["content"], tool_call_id=rec["tool_call_id"],
                name=rec["name"],
            ))
    return messages, spec


class TestRealTraceReplay:
    """Replays the REAL pre-change clarification dialogue of task
    inject-8b757abb (28.9 min end-to-end): the model probed the target —
    established identity / Running state / node / two-replica split — but
    the pre-change FaultSpec handoff carried none of it, so plan
    re-derived everything (12 min). The harvester must recover those
    facts from the recorded trace via the fallback path (this trace
    predates update_progress self-recording)."""

    def test_fallback_harvests_real_trace_target_facts(self):
        messages, spec = _load_replay()
        snap = _harvest_probe_snapshot(messages, spec, now="2026-08-26T10:00:00+08:00")
        assert snap is not None, "real trace must yield facts via fallback"
        facts = "\n".join(f["fact"] for f in snap["facts"])
        # Target pod identity + Running state + node — the facts whose loss
        # cost plan its 12-minute re-derivation.
        assert "kone-app-service-58c7db669c-mpwr6" in facts
        assert "Running" in facts
        # Every entry is fallback-sourced (this trace predates self-recording)
        assert all(f["source_tool"] == "kubectl_read" for f in snap["facts"])
        # No cross-pod leakage: the sibling replica row matches the name
        # prefix only via a whole-word hit on the pod name — it is a
        # DIFFERENT pod (…-v4dng) and its line must not be attributed to
        # the target name. (The label row names the deployment, a distinct
        # token — allowed.)
        target_pod = "kone-app-service-58c7db669c-mpwr6"
        sibling = "kone-app-service-58c7db669c-v4dng"
        for f in snap["facts"]:
            if sibling in f["fact"]:
                assert target_pod in f["fact"], f["fact"]

    def test_primary_source_would_have_captured_the_causal_insight(self):
        """The same trace WITH self-recording (the post-change behaviour):
        the model's own wording — including the causal insight a keyword
        extractor can never catch ("PID 1 is the app itself, so killing it
        restarts the container") — is preserved verbatim."""
        messages, spec = _load_replay()
        causal = (
            "PID 1 inside the target container is kone-app-service itself; "
            "killing it triggers a container restart, and restartPolicy "
            "Always brings it back"
        )
        messages.insert(len(messages) - 2, _self_record([
            "target pod kone-app-service-58c7db669c-mpwr6 is Running on node cn-shanghai-c",
            causal,
        ]))
        snap = _harvest_probe_snapshot(messages, spec, now="2026-08-26T10:00:00+08:00")
        facts = "\n".join(f["fact"] for f in snap["facts"])
        assert causal in facts  # verbatim model wording survives
        assert snap["facts"][0]["source_tool"] == "update_progress"
        # Collision dedup working as designed: the self-recorded facts
        # already name the target pod, the process, and restartPolicy, so
        # the fallback's rows for the same information are dropped instead
        # of spending snapshot budget twice (the pure-fallback capture of
        # those rows is pinned by the test above).
        assert not any(f["source_tool"] == "kubectl_read" for f in snap["facts"])


class TestIntentGraphChannelSurvival:
    """Graph-level regression anchor for the cross-graph bridge.

    The original tier1-speedup implementation shipped with the bridge
    silently broken: ``IntentState`` had no ``progress_ledger`` /
    ``probe_snapshot`` channels, and langgraph drops node/tool writes to
    unknown channels WITHOUT any error — so ``update_progress`` ran "fine"
    in the intent graph, the harvester's ``_commit_inject_handoff`` return
    was accepted, and every value vanished before the dispatch code could
    ever read it. Function-level tests never see this; only a real
    ``StateGraph(IntentState)`` execution does. These tests pin the
    channels' existence against future schema regressions.
    """

    async def test_update_progress_ledger_write_survives_intent_state(self):
        from langgraph.graph import StateGraph
        from langgraph.prebuilt import ToolNode

        graph = StateGraph(IntentState)
        graph.add_node("tools", ToolNode([update_progress]))
        graph.set_entry_point("tools")
        compiled = graph.compile()

        result = await compiled.ainvoke(
            {"messages": [_self_record(["target pod is Running"])]},
            {"recursion_limit": 5},
        )

        ledger = result.get("progress_ledger")
        assert ledger, "progress_ledger write vanished — IntentState channel missing again"
        facts = ((ledger.get("state") or {}).get("established_facts"))
        assert facts == ["target pod is Running"]

    async def test_probe_snapshot_channel_round_trips_intent_state(self):
        from langgraph.graph import StateGraph

        async def _node(state):
            return {"probe_snapshot": {"facts": [{"fact": "x", "source_tool": "kubectl_read"}]}}

        graph = StateGraph(IntentState)
        graph.add_node("writer", _node)
        graph.set_entry_point("writer")
        compiled = graph.compile()

        result = await compiled.ainvoke({"messages": []}, {"recursion_limit": 5})

        assert result.get("probe_snapshot") == {
            "facts": [{"fact": "x", "source_tool": "kubectl_read"}]
        }


# ── Cross-intent staleness: previous-intent records must not leak in ──


class TestCrossIntentStalenessGuard:
    """The intent graph keeps its messages across rejected/abandoned intents
    (a continued conversation iterates on established context — deliberate
    product behaviour), so the LAST update_progress record may belong to a
    PREVIOUS intent about a DIFFERENT target. The harvester must drop such a
    batch: the snapshot section claims "Established while clarifying the
    intent", and residue would carry a fabricated probed_at for the wrong
    target. Relevance tokens: this intent's names / param values / namespace.
    """

    def test_previous_intent_record_for_other_target_dropped(self):
        # Intent A (rejected) recorded facts about pod-other; intent B's spec
        # targets drill-target. None of B's tokens appear in the batch →
        # the whole batch is dropped and (with no relevant probe rows) the
        # snapshot is empty.
        msgs = [_self_record([
            "target pod pod-other is Running on node worker-9",
        ])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert snap is None

    def test_same_target_refine_record_kept(self):
        # Rejected-then-refined intent about the SAME target: the record
        # still names this intent's target — keep it. Conversation
        # continuity is exactly why messages survive rejections.
        msgs = [_self_record([
            "target pod drill-target is Running on node worker-1",
        ])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        facts = "\n".join(f["fact"] for f in snap["facts"])
        assert "drill-target is Running" in facts

    def test_causal_insight_via_param_value_kept(self):
        # A causal insight phrased with the process name (= a param value of
        # THIS intent) stays: relevance tokens include param values, and the
        # whole-BATCH check means insight-only batches naming the process
        # survive verbatim.
        msgs = [_self_record([
            "killing nginx triggers a container restart, restartPolicy Always brings it back",
        ])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        facts = "\n".join(f["fact"] for f in snap["facts"])
        assert "restartPolicy Always" in facts

    def test_environment_fact_via_namespace_kept(self):
        # Environment-level fact naming the namespace survives: same-session
        # namespace continuity is legitimate evidence for this intent too.
        msgs = [_self_record(["namespace cms-demo has 3 worker nodes"])]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert snap is not None

    def test_empty_token_set_skips_guard(self):
        # A spec with no names/params/namespace cannot discriminate — keep
        # the record rather than dropping everything on no evidence.
        msgs = [_self_record(["cluster has 3 nodes"])]
        snap = _harvest_probe_snapshot(
            msgs, _spec(names=(), params={}, namespace=""),
            now="2026-08-26T10:00:00+08:00",
        )
        assert snap is not None

    def test_fallback_immune_to_previous_intent_probe_rows(self):
        """The fallback cannot re-import what the primary-source guard
        dropped: its row filter is spec-token driven (current names /
        param values only), so a rejected intent's probe ToolMessages —
        which name the OLD target in both rows and call args — contribute
        nothing. Resource rows, ``Name:`` lines, and Restart Policy lines
        are each gated on the CURRENT spec's tokens, so the whole
        previous-intent evidence class is filtered out by construction.
        (Pinned in review round 8: the guard story would be incomplete if
        only the primary source were protected.)
        """
        msgs = [
            _probe_call("t1", {"verb": "describe", "pod": "pod-other"}),
            ToolMessage(
                content="Name: pod-other\nRestart Policy: Always",
                tool_call_id="t1", name="kubectl_read",
            ),
            _probe_call("t2", {"verb": "get"}),
            ToolMessage(
                content="NAME READY STATUS\npod-other 1/1 Running",
                tool_call_id="t2", name="kubectl_read",
            ),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert snap is None

    def test_fallback_shared_namespace_rows_still_filtered(self):
        """Hardest case: old and new targets live in the SAME namespace, so
        a ``kubectl get pods`` table lists BOTH. Only rows naming the
        current target survive — the old target's row is dropped even
        though it came from a tool call issued in this same session."""
        msgs = [
            _probe_call("t1", {"verb": "get"}),
            ToolMessage(
                content=(
                    "NAME READY STATUS\n"
                    "pod-other 1/1 Running\n"
                    "drill-target 1/1 Running"
                ),
                tool_call_id="t1", name="kubectl_read",
            ),
        ]
        snap = _harvest_probe_snapshot(msgs, _spec(), now="2026-08-26T10:00:00+08:00")
        assert snap is not None
        facts = "\n".join(f["fact"] for f in snap["facts"])
        assert "drill-target 1/1 Running" in facts
        assert "pod-other" not in facts


# ── Lifecycle policy: the snapshot must not leak across boundaries ──────


class TestSnapshotLifecyclePolicy:
    """``probe_snapshot`` is durable within one operation (checkpoint
    resume keeps it) but resets at the batch and recover boundaries
    (``state_lifecycle``), mirroring ``progress_ledger``: the next batch
    fault and any recover operate on different evidence and must not
    inherit stale intent-time facts. This also pins the graph-level
    consequence the batch dispatch path relies on: batch_setup runs
    EVERY fault (including the first) through this reset, so the server
    batch path deliberately does not pass the snapshot at dispatch —
    it would be cleared before any plan could read it.
    """

    def test_probe_snapshot_resets_on_batch_fault(self):
        from chaos_agent.agent.state_mgmt.state_lifecycle import per_fault_reset_state

        reset = per_fault_reset_state()
        assert reset.get("probe_snapshot") is None
        assert reset.get("progress_ledger") is None  # same-type neighbour

    def test_probe_snapshot_resets_on_recover(self):
        from chaos_agent.agent.state_mgmt.state_lifecycle import recover_reset_state

        reset = recover_reset_state()
        assert reset.get("probe_snapshot") is None
        assert reset.get("progress_ledger") is None

    def test_probe_snapshot_is_durable_for_checkpoint_resume(self):
        from chaos_agent.agent.state_mgmt.state_lifecycle import (
            STATE_DURABLE_FACT_FIELDS,
        )

        assert "probe_snapshot" in STATE_DURABLE_FACT_FIELDS
        assert "progress_ledger" in STATE_DURABLE_FACT_FIELDS


# ── Ledger handoff: same cross-intent guard as the snapshot ─────────────


class TestLedgerHandoffGuard:
    """The ledger bridge needs the same cross-intent guard as the snapshot
    primary source (review round 7): the ledger's ``established_facts`` are
    the LAST update_progress batch — when that batch belongs to a PREVIOUS,
    rejected intent about a different target (the intent graph keeps its
    messages and ledger across rejections so a continued conversation can
    iterate), handing the ledger off verbatim would render previous-target
    facts in the new plan's system prompt under "do not re-derive what is
    already established", and the seed-anchor-and-preserve branch would
    then carry them through execute/verify/recover.
    """

    async def _noop(self, *args, **kwargs):
        return None

    def _state(self, ledger) -> dict:
        return {
            "task_id": "t-ledger-guard",
            "fault_spec": _spec().to_dict(),
            "intent_confidence": 0.9,
            "dialogue_round": 1,
            "progress_ledger": ledger,
            "messages": [HumanMessage(content="inject cpu fault", id="m1")],
        }

    async def _approve(self, state, monkeypatch):
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "approved")
        monkeypatch.setattr(ic_mod, "_revive_task_row", self._noop)
        return await intent_confirm(state)

    async def test_previous_intent_ledger_residue_dropped(self, monkeypatch):
        # Intent A (rejected) recorded facts about pod-other; intent B's spec
        # targets drill-target. The ledger's last batch is A's residue → the
        # handoff must carry None, not the previous-target facts.
        ledger = {
            "state": {"established_facts": [
                "target pod pod-other is Running on node worker-9",
            ]},
            "log": [{"event": "probed pod-other", "status": "observed"}],
        }
        delta = await self._approve(self._state(ledger), monkeypatch)
        assert delta["progress_ledger"] is None
        # The snapshot guard agrees: no primary facts for the wrong target.
        assert delta["probe_snapshot"] is None

    async def test_same_target_ledger_kept(self, monkeypatch):
        # Rejected-then-refined intent about the SAME target: the ledger
        # still names this intent's target — keep it verbatim. Conversation
        # continuity is exactly why the ledger survives rejections.
        ledger = {
            "state": {"established_facts": [
                "target pod drill-target is Running on node worker-1",
            ]},
            "log": [{"event": "probed target", "status": "observed"}],
        }
        delta = await self._approve(self._state(ledger), monkeypatch)
        assert delta["progress_ledger"] == ledger

    async def test_absent_ledger_hands_off_none(self, monkeypatch):
        # Direct path (no intent-time recording): no ledger at all. The
        # handoff must not invent one.
        state = self._state(None)
        delta = await self._approve(state, monkeypatch)
        assert delta["progress_ledger"] is None

    async def test_log_only_ledger_kept(self, monkeypatch):
        # A ledger whose facts list is empty (log-only calls never touched
        # established_facts): the guard cannot discriminate on an empty
        # batch, so the ledger passes — log entries are session event
        # history, not "do not re-derive" facts.
        ledger = {
            "state": {},
            "log": [{"event": "probed something", "status": "observed"}],
        }
        delta = await self._approve(self._state(ledger), monkeypatch)
        assert delta["progress_ledger"] == ledger
