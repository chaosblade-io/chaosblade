"""Tests for l4-intent-handoff-parity: attach_intent_handoff bridging.

Function-level coverage for the L4 invoke-side handoff — reading the
intent graph checkpoint, reusing the single-source extraction function
and residue guards, and degrading to cold-start on every failure shape.
Graph-level wiring (execution → adapter) is covered separately in
test_intent_handoff_execution.py.
"""

from types import SimpleNamespace

from langchain_core.messages import SystemMessage

from chaos_agent.l4 import adapter as _adapter_mod
from chaos_agent.l4.schemas import L4TestTask

_attach = _adapter_mod.attach_intent_handoff
_to_initial_state = _adapter_mod.test_task_to_initial_state


def _valid_payload(**overrides):
    payload = {
        "fault_intent": {
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
            "names": ["drill-target"],
            "labels": {"app": "myapp"},
            "params": {"cpu-percent": "80"},
            "duration_seconds": 300,
        },
        "kubeconfig": "/home/user/.kube/config",
    }
    payload.update(overrides)
    return payload


def _task(**payload_overrides):
    return L4TestTask(
        task_id="t-bridge-001",
        intent="inject pod cpu fault",
        payload=_valid_payload(**payload_overrides),
    )


class _FakeIntentGraph:
    """Minimal intent_graph stand-in: returns canned state or raises."""

    def __init__(self, values=None, error=None):
        self._values = values
        self._error = error

    async def aget_state(self, config):
        if self._error is not None:
            raise self._error
        if self._values is None:
            return None
        return SimpleNamespace(values=self._values)


class _FakePool:
    def __init__(self, graph):
        self.intent_graph = graph


def _checkpoint_values(
    *,
    facts=None,
    established=None,
    handoff_summary="",
):
    values = {"confirmed_intent": "inject"}
    if facts is not None:
        values["probe_snapshot"] = {
            "facts": [
                {"fact": f, "source_tool": "kubectl_read", "probed_at": "2026-08-27T10:00:00+00:00"}
                for f in facts
            ]
        }
    if established is not None:
        values["progress_ledger"] = {
            "state": {"established_facts": established},
        }
    if handoff_summary:
        values["handoff_summary"] = handoff_summary
    return values


class TestAttachIntentHandoff:
    async def test_bridge_success_all_three_fields(self):
        """Checkpoint evidence names this task's target → all three fields
        land: snapshot + ledger into state, summary as seed SystemMessage."""
        values = _checkpoint_values(
            facts=["pod drill-target running on node-1"],
            established=["drill-target has 3 replicas"],
            handoff_summary=(
                "[Intent Clarification Summary]\nDialogue rounds: 2\n"
                "Confirmed intent: inject\n"
                "Fault: pod-cpu-fullload → pod/cpu/fullload @ cms-demo"
            ),
        )
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-session-1")

        assert out["probe_snapshot"]["facts"][0]["fact"] == (
            "pod drill-target running on node-1"
        )
        assert out["progress_ledger"]["state"]["established_facts"] == [
            "drill-target has 3 replicas"
        ]
        assert out["messages"] and isinstance(out["messages"][0], SystemMessage)
        assert out["messages"][0].content.startswith("[Intent Clarification Summary]")
        # Seed message is PREPENDED; existing messages (L4 starts empty) follow.
        assert out["messages"][1:] == []

    async def test_skip_without_prefix(self):
        """Non-chaos- prefix / empty thread id → no bridge, state untouched."""
        pool = _FakePool(_FakeIntentGraph(_checkpoint_values(facts=["drill-target"])))
        for thread_id in ("", "task-abc-123", None):
            initial = _to_initial_state(_task())
            out = await _attach(initial, pool, thread_id)
            assert out is initial
            assert "probe_snapshot" not in out
            assert "progress_ledger" not in out
            assert out["messages"] == []

    async def test_missing_checkpoint_degrades(self):
        """No checkpoint for the thread → cold-start state kept."""
        pool = _FakePool(_FakeIntentGraph(values=None))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-never-clarked")

        assert out is initial
        assert "probe_snapshot" not in out

    async def test_empty_checkpoint_values_degrades(self):
        """Checkpoint exists but carries no evidence → empty bridge, i.e.
        byte-identical cold start (pre-change behaviour)."""
        pool = _FakePool(_FakeIntentGraph(_checkpoint_values()))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-empty-1")

        assert "probe_snapshot" not in out
        assert "progress_ledger" not in out
        assert out["messages"] == []

    async def test_aget_state_error_degrades(self):
        """aget_state raising (PG hiccup etc.) → cold-start state kept,
        exception never escapes to the execute flow."""
        pool = _FakePool(_FakeIntentGraph(error=RuntimeError("pg hiccup")))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-flaky-1")

        assert out is initial
        assert "probe_snapshot" not in out

    async def test_residue_dropped_snapshot_and_ledger_independent(self):
        """Residue guard: snapshot from a previous intent about a different
        target is dropped; the on-target ledger survives — the two fields
        are judged independently, never all-or-nothing."""
        values = _checkpoint_values(
            facts=["other-app pod running on node-9"],
            established=["drill-target restartPolicy is Always"],
            handoff_summary=(
                "[Intent Clarification Summary]\nDialogue rounds: 1\n"
                "Confirmed intent: inject\n"
                "Fault: pod-cpu-fullload → pod/cpu/fullload @ cms-demo"
            ),
        )
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-stale-1")

        assert "probe_snapshot" not in out
        assert out["progress_ledger"]["state"]["established_facts"] == [
            "drill-target restartPolicy is Always"
        ]
        # handoff_summary survives independently of the residue verdicts.
        assert out["messages"] and out["messages"][0].content.startswith(
            "[Intent Clarification Summary]"
        )

    async def test_residue_both_fields_dropped(self):
        """Both fields about a different target → both dropped, summary
        still bridged (it names the operation boundary, not the target)."""
        values = _checkpoint_values(
            facts=["other-app pod running"],
            established=["other-app has 2 replicas"],
        )
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-stale-2")

        assert "probe_snapshot" not in out
        assert "progress_ledger" not in out

    async def test_summary_identity_mismatch_dropped(self):
        """Identity guard: the checkpoint's approved intent (different
        identity quadruple) must not seed this task's pipeline with a
        stale summary. The decoupled platform timing makes "approve A,
        then direct-inject B in the same session" reachable — B's payload
        is authoritative, so A's summary is stale intent identity, not
        conversation context. Snapshot/ledger keep their own independent
        word-boundary verdicts regardless."""
        values = _checkpoint_values(
            facts=["drill-target running on node-1"],
            established=["drill-target has 3 replicas"],
            handoff_summary=(
                "[Intent Clarification Summary]\nDialogue rounds: 1\n"
                "Confirmed intent: inject\n"
                "Fault: pod-mem-burn → pod/mem/burn @ other-ns"
            ),
        )
        values["fault_spec"] = {
            "namespace": "other-ns",
            "scope": "pod",
            "fault_target": "mem",
            "fault_action": "burn",
        }
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-stale-3")

        # Summary dropped: the approved intent it describes (pod/mem/burn
        # @ other-ns) is not the task being executed (pod/cpu/fullload @
        # cms-demo).
        assert out["messages"] == []
        # Independent verdicts unchanged: on-target evidence still lands.
        assert out["probe_snapshot"]["facts"][0]["fact"] == (
            "drill-target running on node-1"
        )
        assert out["progress_ledger"]["state"]["established_facts"] == [
            "drill-target has 3 replicas"
        ]

    async def test_summary_identity_match_kept(self):
        """Same identity quadruple → summary kept (normal three-step flow
        AND the same-intent re-inject flow). params differences must NOT
        matter: the platform legitimately injects defaults (timeout) into
        the payload after approval, so params are excluded from the
        identity judgement."""
        values = _checkpoint_values(
            handoff_summary=(
                "[Intent Clarification Summary]\nDialogue rounds: 2\n"
                "Confirmed intent: inject\n"
                "Fault: pod-cpu-fullload → pod/cpu/fullload @ cms-demo"
            ),
        )
        # Checkpoint spec WITHOUT the platform-injected timeout param —
        # identity quadruple still matches the task spec.
        values["fault_spec"] = {
            "namespace": "cms-demo",
            "scope": "pod",
            "fault_target": "cpu",
            "fault_action": "fullload",
            "params": {"cpu-percent": "80"},
        }
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-fresh-1")

        assert out["messages"] and isinstance(out["messages"][0], SystemMessage)
        assert "pod/cpu/fullload @ cms-demo" in out["messages"][0].content

    async def test_summary_spec_reset_same_intent_kept(self):
        """Spec was reset by a later clarify turn (interaction.py reset
        dicts clear fault_spec but NOT handoff_summary), and the summary
        still names this task's namespace → word-boundary fallback keeps
        it. This is the same-intent re-inject-after-clarify flow: the
        speed-up must not degrade just because a clarify turn ran."""
        values = _checkpoint_values(
            handoff_summary=(
                "[Intent Clarification Summary]\nDialogue rounds: 2\n"
                "Confirmed intent: inject\n"
                "Fault: pod-cpu-fullload → pod/cpu/fullload @ cms-demo"
            ),
        )
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-fresh-2")

        assert out["messages"] and isinstance(out["messages"][0], SystemMessage)
        assert out["messages"][0].content.startswith("[Intent Clarification Summary]")

    async def test_summary_word_boundary_fallback_spec_reset(self):
        """S4: approve A → clarify turn for B (spec reset) → direct-inject
        B with a DIFFERENT namespace. The checkpoint-side spec is gone, so
        the identity quadruple cannot run — the word-boundary fallback
        judges the summary alone: it names none of the task's tokens
        (summary never carries names; only the namespace token can hit)
        → stale summary dropped, no seed message."""
        values = _checkpoint_values(
            handoff_summary=(
                "[Intent Clarification Summary]\nDialogue rounds: 1\n"
                "Confirmed intent: inject\n"
                "Fault: pod-mem-burn → pod/mem/burn @ other-ns"
            ),
        )
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-stale-4")

        assert out["messages"] == []

    async def test_summary_same_ns_residual_window(self):
        """S6 (accepted residue): approve A → clarify turn (spec reset) →
        direct-inject B in the SAME namespace but different target. Both
        guards are blind here — the quadruple has no checkpoint spec and
        the word-boundary fallback hits on the shared namespace token.
        Locked as ACCEPTED: three coincidences must stack (approval,
        clarify reset, direct inject) and the residue is one SystemMessage
        seed whose payload contradicts the authoritative task-side
        fault_spec. Guard misjudgements degrade to cold-start, never
        introduce wrong evidence."""
        values = _checkpoint_values(
            handoff_summary=(
                "[Intent Clarification Summary]\nDialogue rounds: 1\n"
                "Confirmed intent: inject\n"
                "Fault: pod-mem-burn → pod/mem/burn @ cms-demo"
            ),
        )
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-window-1")

        # Locked behaviour: summary kept (ns token "cms-demo" hits).
        assert out["messages"] and "pod/mem/burn @ cms-demo" in out["messages"][0].content

    async def test_summary_truncated_no_identity_line_dropped(self):
        """A truncated/legacy summary without the identity line carries no
        judgeable signal — the word-boundary fallback drops it when the
        task has tokens. Real summaries always carry the identity line
        (``_build_handoff_summary`` fixed format); this locks the defensive
        direction: unjudgeable + no token hit → cold start, never a bare
        seed. (With EMPTY tokens — cluster-scoped faults without names
        — the fallback skips and the summary survives: fail-open floor.)"""
        values = _checkpoint_values(
            handoff_summary="[Intent Clarification Summary]\nDialogue rounds: 1",
        )
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-trunc-1")

        assert out["messages"] == []

    async def test_checkpoint_payload_not_mutated(self):
        """The bridge must not leak references back into the checkpoint —
        mutating the bridged snapshot must not touch the source values."""
        values = _checkpoint_values(facts=["drill-target running"])
        pool = _FakePool(_FakeIntentGraph(values))
        initial = _to_initial_state(_task())

        out = await _attach(initial, pool, "chaos-isolated-1")
        out["probe_snapshot"]["facts"][0]["fact"] = "MUTATED"

        assert values["probe_snapshot"]["facts"][0]["fact"] == "drill-target running"
