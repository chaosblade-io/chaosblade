"""Snapshot equivalence tests for the V2 Layer-2 context assembly (profile slots).

The golden fixture ``_golden_layer2_context.json`` was captured from the
pre-refactor ``_build_first_iteration_context`` / ``_build_baseline_tool_messages``
across a fault matrix. These tests assert the profile-slot-backed assembly
reproduces those bytes exactly, guarding against silent LLM-prompt drift when the
7 fault-specific fragments moved into ``_verification_profiles`` slots.
"""

import json
from pathlib import Path

import pytest

from chaos_agent.agent.nodes.verify._verifier_messages import (
    _build_baseline_tool_messages,
    _build_first_iteration_context,
)
from chaos_agent.agent.result.verdict import Layer1Result

_GOLDEN = Path(__file__).parent / "_golden_layer2_context.json"

_BASELINE = {
    "captured_at": "2026-05-09T10:00:00",
    "source": "registry",
    "success_count": 1,
    "total_count": 1,
    "observations": [
        {
            "exit_code": 0,
            "description": "df -h",
            "command": "kubectl exec ... df -h",
            "stdout": "Filesystem Size Used Avail Use% Mounted on\n/dev/vda3 40G 6G 34G 16% /host\n/dev/vdb 20G 8G 12G 42% /",
        }
    ],
}


def _state(*, scope, target, action, parsed, params=None, baseline=None,
           skill_case="", post_fill=None, post_burn=None, evidence=None):
    st = {
        "target": {
            "namespace": "default",
            "names": ["test-node"],
            "labels": {},
            "resource_type": scope,
        },
        "blade_scope": scope,
        "blade_target": target,
        "blade_action": action,
        "blade_parsed_flags": parsed,
        "params": params or {},
        "skill_case_content": skill_case,
    }
    if baseline is not None:
        st["baseline_data"] = baseline
    if post_fill is not None:
        st["disk_fill_post_check"] = post_fill
    if post_burn is not None:
        st["disk_burn_post_check"] = post_burn
    if evidence is not None:
        st["evidence_snapshot"] = evidence
    return st


def _layer1(**kw):
    base = dict(status="passed", affected_count=1, raw_output="Success", details="")
    base.update(kw)
    return Layer1Result(**base)


# (label, state, layer1, blade_uid, skill_name, kubeconfig, tool_pod, conv_hint)
_MATRIX = [
    ("node-disk-fill-imagefs-small-toolpod-baseline",
     _state(scope="node", target="disk", action="fill",
            parsed={"path": "/var/lib/docker", "size": "1000"}, baseline=_BASELINE),
     _layer1(), "uid-1", "node-disk-fill", "/kc", "tool-pod-a", ""),
    ("node-disk-fill-nodefs-large-notoolpod",
     _state(scope="node", target="disk", action="fill",
            parsed={"path": "/var/log", "size": "10000"}),
     _layer1(), "uid-2", "node-disk-fill", "/kc", None, ""),
    ("node-disk-fill-unknownpath-toolpod",
     _state(scope="node", target="disk", action="fill",
            parsed={"path": "/weird/place", "size": "6000"}),
     _layer1(), "uid-3", "node-disk-fill", "/kc", "tool-pod-c", ""),
    ("pod-disk-fill-small",
     _state(scope="pod", target="disk", action="fill",
            parsed={"path": "/tmp", "size": "500"}, baseline=_BASELINE),
     _layer1(), "uid-4", "pod-disk-fill", "/kc", None, ""),
    ("node-disk-burn-toolpod",
     _state(scope="node", target="disk", action="burn", parsed={"size": "1024"},
            skill_case="## 注入验证\n1. check dd\n",
            post_burn={"burn_io_detected": True, "active_partitions": [
                {"name": "/dev/vdb", "write_throughput_mb_s": 120}],
                "target_pod": "tp", "node": "n1", "scope": "node"}),
     _layer1(), "uid-5", "node-disk-burn", "/kc", "tool-pod-e", ""),
    ("pod-disk-burn",
     _state(scope="pod", target="disk", action="burn", parsed={},
            skill_case="## 注入验证\n1. check dd\n"),
     _layer1(), "uid-6", "pod-disk-burn", "/kc", None, ""),
    ("pod-mem-load",
     _state(scope="pod", target="mem", action="load", parsed={"percent": "80"},
            skill_case="## 注入验证\n1. check mem\n",
            evidence={"ps aux": {"rc": 0, "stdout": "root 1 java"}}),
     _layer1(), "uid-7", "pod-mem-load", "/kc", None, ""),
    ("pod-process-kill",
     _state(scope="pod", target="process", action="kill", parsed={},
            skill_case="## 注入验证\n1. check proc\n"),
     _layer1(), "uid-8", "pod-process-kill", "/kc", None, ""),
    ("pod-network-dns",
     _state(scope="pod", target="network", action="dns", parsed={}),
     _layer1(), "uid-9", "pod-network-dns", "/kc", None, ""),
    ("pod-cpu-load",
     _state(scope="pod", target="cpu", action="load", parsed={"percent": "90"}),
     _layer1(), "uid-10", "pod-cpu-load", "/kc", None, ""),
    ("node-disk-fill-selfdestruct-skipped",
     _state(scope="node", target="disk", action="fill",
            parsed={"path": "/tmp", "size": "3000"}),
     _layer1(status="skipped", details="node is self-destructive and NotReady"),
     "uid-11", "node-disk-fill", "/kc", "tool-pod-k", ""),
    ("node-disk-fill-postcheck-found",
     _state(scope="node", target="disk", action="fill",
            parsed={"path": "/var/lib/docker", "size": "8000"},
            post_fill={"fill_file_found": True, "target_pod": "tp",
                       "ls_output": "chaos_filldisk.log.dat", "df_output": "vdb 90%"}),
     _layer1(), "uid-12", "node-disk-fill", "/kc", "tool-pod-l", ""),
]

_BASELINE_MATRIX = [
    ("bl-disk-fill-imagefs", _BASELINE, "disk", "fill", {"path": "/var/lib/docker", "size": "1000"}),
    ("bl-disk-fill-nodefs", _BASELINE, "disk", "fill", {"path": "/var/log", "size": "1000"}),
    ("bl-disk-fill-unknown", _BASELINE, "disk", "fill", {"path": "/weird", "size": "1000"}),
    ("bl-disk-burn", _BASELINE, "disk", "burn", {}),
    ("bl-mem", _BASELINE, "mem", "load", {}),
    ("bl-network-dns", _BASELINE, "network", "dns", {}),
]


def _golden_map():
    with open(_GOLDEN, encoding="utf-8") as fh:
        golden = json.load(fh)
    return {(e["kind"], e["label"]): e["out"] for e in golden}


@pytest.fixture(autouse=True)
def _force_non_kubewiz(monkeypatch):
    # The Layer-2 context embeds a kubeconfig line whose value depends on the
    # transport channel (``is_kubewiz_channel``). Pin it deterministically so
    # the golden snapshot is not sensitive to ambient transport state.
    monkeypatch.setattr(
        "chaos_agent.transports.is_kubewiz_channel", lambda: False
    )


class TestLayer2ContextSnapshot:
    """Byte-for-byte equivalence against the frozen pre-refactor assembly."""

    @pytest.mark.parametrize("row", _MATRIX, ids=[r[0] for r in _MATRIX])
    def test_first_iteration_context_matches_golden(self, row):
        label, st, l1, uid, skill, kc, tp, ch = row
        got = _build_first_iteration_context(st, l1, uid, skill, kc, tp, ch)
        assert got == _golden_map()[("context", label)]

    @pytest.mark.parametrize("row", _BASELINE_MATRIX, ids=[r[0] for r in _BASELINE_MATRIX])
    def test_baseline_tool_messages_match_golden(self, row):
        label, bl, tgt, act, parsed = row
        msgs = _build_baseline_tool_messages(bl, tgt, act, blade_parsed=parsed)
        ser = [{"type": type(m).__name__, "content": m.content} for m in msgs]
        assert ser == _golden_map()[("baseline", label)]

    def test_golden_has_no_hardcoded_knowledge_fragments(self):
        # The 7 fault-specific knowledge fragments were removed from code and now
        # live in the skill case / knowledge docs. The assembled context must no
        # longer emit them; it points at the knowledge doc instead.
        gmap = _golden_map()
        joined = "\n".join(v for (k, _), v in gmap.items() if k == "context")
        for gone in ("CRITICAL path semantics", "Disk-fill specific",
                     "PRIMARY VERIFICATION: Fill File Check", "Disk Fill Size Warning",
                     "Transient fault rules", "Container Restart Detection"):
            assert gone not in joined
        assert "fault-verification-strategies.md" in joined  # doc pointer present
        # baseline partition note also removed from the baseline entries
        bl_joined = json.dumps([v for (k, _), v in gmap.items() if k == "baseline"])
        assert "Baseline Partition Target" not in bl_joined
