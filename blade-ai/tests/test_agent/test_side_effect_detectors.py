"""Tests for the side-effect detection framework."""

import pytest

from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (
    ContainerRestartDetector,
    CrashLoopDetector,
    DependencyErrorDetector,
    DetectionContext,
    EndpointRemovalDetector,
    EndpointSnapshot,
    EvictedPodDetector,
    HostObserver,
    HPAScaleDetector,
    K8sObserver,
    OOMKilledSiblingDetector,
    PodSnapshot,
    PostInjectState,
    ProbeFailureDetector,
    SideEffectSnapshot,
    run_all_detectors,
)


def _make_ctx(**kwargs):
    defaults = {
        "namespace": "default",
        "target_names": ["app-pod-1"],
        "scope": "pod",
        "kubeconfig": "",
        "injection_start_time": "2026-05-26T10:00:00Z",
        "task_id": "test-task",
    }
    defaults.update(kwargs)
    return DetectionContext(**defaults)


def _make_snapshot(**kwargs):
    defaults = {
        "captured_at": "2026-05-26T09:59:00Z",
        "namespace": "default",
        "pods": {},
        "endpoints": {},
    }
    defaults.update(kwargs)
    return SideEffectSnapshot(**defaults)


class TestContainerRestartDetector:
    def test_detects_new_restart(self):
        detector = ContainerRestartDetector()
        before = _make_snapshot(pods={
            "default/app-pod-1": PodSnapshot(
                name="app-pod-1", namespace="default", phase="Running",
                restart_counts={"main": 2},
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 3,
                        "lastState": {"terminated": {
                            "reason": "OOMKilled",
                            "finishedAt": "2026-05-26T10:05:00Z",
                        }},
                        "state": {"running": {}},
                    }],
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert len(results) == 1
        assert results[0]["pod"] == "app-pod-1"
        assert results[0]["restart_delta"] == 1
        assert results[0]["reason"] == "OOMKilled"

    def test_ignores_pre_existing_restart(self):
        detector = ContainerRestartDetector()
        before = _make_snapshot(pods={
            "default/app-pod-1": PodSnapshot(
                name="app-pod-1", namespace="default", phase="Running",
                restart_counts={"main": 3},
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 3,
                        "lastState": {"terminated": {"reason": "OOMKilled", "finishedAt": "2026-05-26T09:50:00Z"}},
                        "state": {"running": {}},
                    }],
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert results == []

    def test_no_snapshot_uses_timestamp(self):
        detector = ContainerRestartDetector()
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 1,
                        "lastState": {"terminated": {
                            "reason": "Error",
                            "finishedAt": "2026-05-26T10:02:00Z",
                        }},
                        "state": {"running": {}},
                    }],
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(None, after, ctx)
        assert len(results) == 1


class TestContainerRestartNaturalCyclePeel:
    """B7 (cases #2/#17/#34): `sleep 7200` keep-alive targets restart on
    their natural ~2h cadence; when that renewal lands inside the run
    window the restart diff must NOT surface as a side effect. A
    successful lifecycle completion (exit 0 / reason "Completed") is a
    periodic workload renewing itself, not a fault — AUTO mode has no
    confirmation card where a reviewer would catch the false positive.
    """

    @staticmethod
    def _keepalive_after(exit_code=None, reason="Completed"):
        # The #2 incident's exact shape: baseline restartCount=4, post-state
        # restartCount=6 (delta=2), last termination successful and INSIDE
        # the window (finished_at 10:26 vs injection 10:00).
        terminated = {
            "reason": reason,
            "finishedAt": "2026-05-26T10:26:00Z",
        }
        if exit_code is not None:
            terminated["exitCode"] = exit_code
        return PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "drill-pvc-target"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 6,
                        "lastState": {"terminated": terminated},
                        "state": {"running": {}},
                    }],
                },
            }]},
        )

    @staticmethod
    def _keepalive_snapshot():
        return _make_snapshot(pods={
            "default/drill-pvc-target": PodSnapshot(
                name="drill-pvc-target", namespace="default", phase="Running",
                restart_counts={"main": 4},
            ),
        })

    def test_exit_zero_completion_not_reported(self):
        detector = ContainerRestartDetector()
        results = detector.detect(
            self._keepalive_snapshot(), self._keepalive_after(exit_code=0), _make_ctx()
        )
        assert results == []

    def test_completed_reason_without_exit_code_not_reported(self):
        # Some runtimes stamp only the reason; "Completed" is the standard
        # K8s wording for a zero exit — same peel, same evidence.
        detector = ContainerRestartDetector()
        results = detector.detect(
            self._keepalive_snapshot(), self._keepalive_after(), _make_ctx()
        )
        assert results == []

    def test_nonzero_exit_restart_still_reported(self):
        # A kill-shaped injection restart (Pod_进程被杀死: exit 137) must
        # keep surfacing — the peel discriminates by SUCCESS, never by
        # "it's a restart".
        detector = ContainerRestartDetector()
        results = detector.detect(
            self._keepalive_snapshot(),
            self._keepalive_after(exit_code=137, reason="Error"),
            _make_ctx(),
        )
        assert len(results) == 1
        assert results[0]["restart_delta"] == 2
        assert results[0]["reason"] == "Error"

    def test_unknown_exit_evidence_still_reported(self):
        # No exit code, no "Completed" reason: cannot be PROVEN a natural
        # completion, so the report stands (fail-closed — the peel never
        # silently swallows what it cannot classify).
        detector = ContainerRestartDetector()
        results = detector.detect(
            self._keepalive_snapshot(),
            self._keepalive_after(reason=""),
            _make_ctx(),
        )
        assert len(results) == 1

    def test_peel_is_per_container_not_per_pod(self):
        # Generality of the peel's GRANULARITY: one Pod, two containers —
        # main renewed naturally (exit 0, peeled) while its sidecar died
        # fault-shaped (exit 137, reported). A pod-level peel would have
        # swallowed the sidecar's real fault signal along with the main's
        # renewal.
        detector = ContainerRestartDetector()
        before = _make_snapshot(pods={
            "default/mixed-pod": PodSnapshot(
                name="mixed-pod", namespace="default", phase="Running",
                restart_counts={"main": 4, "sidecar": 0},
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "mixed-pod"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "main",
                            "restartCount": 6,
                            "lastState": {"terminated": {
                                "exitCode": 0,
                                "reason": "Completed",
                                "finishedAt": "2026-05-26T10:26:00Z",
                            }},
                            "state": {"running": {}},
                        },
                        {
                            "name": "sidecar",
                            "restartCount": 1,
                            "lastState": {"terminated": {
                                "exitCode": 137,
                                "reason": "Error",
                                "finishedAt": "2026-05-26T10:04:00Z",
                            }},
                            "state": {"running": {}},
                        },
                    ],
                },
            }]},
        )
        results = detector.detect(before, after, _make_ctx())
        assert len(results) == 1
        assert results[0]["container"] == "sidecar"
        assert results[0]["restart_delta"] == 1
        assert results[0]["reason"] == "Error"

    def test_peel_leaves_an_audit_log_trail(self, caplog):
        # "Distinguish, don't discard" is a design promise: the peeled
        # renewal must leave a logger.info trail naming the pod, the
        # container and the delta — the peel is observable after the fact,
        # not a silent drop. Pin it so a future refactor cannot delete the
        # trail without this test going red.
        import logging

        detector = ContainerRestartDetector()
        with caplog.at_level(logging.INFO, logger="chaos_agent.agent.nodes.side_effect._side_effect_detectors"):
            results = detector.detect(
                self._keepalive_snapshot(),
                self._keepalive_after(exit_code=0),
                _make_ctx(),
            )
        assert results == []
        assert "drill-pvc-target" in caplog.text
        assert "peeled as natural lifecycle completion" in caplog.text
        assert "restart_delta=2" in caplog.text

    def test_contradictory_nonzero_exit_beats_completed_reason(self):
        # Evidence hierarchy: exitCode is the K8s API's REQUIRED field, the
        # reason label is optional. When they CONTRADICT (non-zero exit +
        # "Completed" label — anomalous or hand-crafted payloads), the
        # stronger evidence wins and the report stands: a naive
        # ``exit_code == 0 or reason == "Completed"`` would fail-open here
        # and swallow a REAL fault hiding behind a stale label.
        detector = ContainerRestartDetector()
        results = detector.detect(
            self._keepalive_snapshot(),
            self._keepalive_after(exit_code=137, reason="Completed"),
            _make_ctx(),
        )
        assert len(results) == 1
        assert results[0]["restart_delta"] == 2
        assert results[0]["reason"] == "Completed"


class TestEvictedPodDetector:
    def test_detects_new_eviction(self):
        detector = EvictedPodDetector()
        before = _make_snapshot(pods={
            "default/app-pod-1": PodSnapshot(
                name="app-pod-1", namespace="default", phase="Running",
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {
                    "phase": "Failed",
                    "reason": "Evicted",
                    "message": "low on ephemeral-storage",
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert len(results) == 1
        assert results[0]["pod"] == "app-pod-1"
        assert results[0]["message"] == "low on ephemeral-storage"

    def test_ignores_pre_existing_eviction(self):
        detector = EvictedPodDetector()
        before = _make_snapshot(pods={
            "default/app-pod-1": PodSnapshot(
                name="app-pod-1", namespace="default", phase="Failed", evicted=True,
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {"phase": "Failed", "reason": "Evicted", "message": ""},
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert results == []


class TestOOMKilledSiblingDetector:
    def test_detects_sibling_oom(self):
        detector = OOMKilledSiblingDetector()
        before = _make_snapshot(pods={
            "default/sidecar-1": PodSnapshot(
                name="sidecar-1", namespace="default", phase="Running",
                oom_killed_containers=set(),
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "sidecar-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "envoy",
                        "restartCount": 1,
                        "lastState": {"terminated": {
                            "reason": "OOMKilled",
                            "finishedAt": "2026-05-26T10:03:00Z",
                        }},
                        "state": {"running": {}},
                    }],
                },
            }]},
        )
        ctx = _make_ctx(target_names=["app-pod-1"])
        results = detector.detect(before, after, ctx)
        assert len(results) == 1
        assert results[0]["pod"] == "sidecar-1"
        assert results[0]["container"] == "envoy"

    def test_excludes_target_pod(self):
        detector = OOMKilledSiblingDetector()
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 1,
                        "lastState": {"terminated": {"reason": "OOMKilled", "finishedAt": "2026-05-26T10:01:00Z"}},
                        "state": {"running": {}},
                    }],
                },
            }]},
        )
        ctx = _make_ctx(target_names=["app-pod-1"])
        results = detector.detect(None, after, ctx)
        assert results == []


class TestCrashLoopDetector:
    def test_detects_new_crash_loop(self):
        detector = CrashLoopDetector()
        before = _make_snapshot(pods={
            "default/worker-1": PodSnapshot(
                name="worker-1", namespace="default", phase="Running",
                restart_counts={"main": 0}, crash_loop_containers=set(),
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "worker-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 3,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        "lastState": {},
                    }],
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert len(results) == 1
        assert results[0]["restart_delta"] == 3

    def test_crashloop_over_completed_laststate_not_reported(self):
        """B7 sibling: CrashLoopBackOff rendered on top of a SUCCESSFUL last
        termination is the back-off view of a periodic workload renewing
        itself (the #2 incident's ``CrashLoop: 1`` half), not a crash — a
        container that exited zero did not fail."""
        detector = CrashLoopDetector()
        before = _make_snapshot(pods={
            "default/drill-pvc-target": PodSnapshot(
                name="drill-pvc-target", namespace="default", phase="Running",
                restart_counts={"main": 4}, crash_loop_containers=set(),
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "drill-pvc-target"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 6,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        "lastState": {"terminated": {
                            "exitCode": 0,
                            "reason": "Completed",
                            "finishedAt": "2026-05-26T10:26:00Z",
                        }},
                    }],
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert results == []

    def test_crashloop_over_nonzero_exit_still_reported(self):
        """A real crash loop exits non-zero (Pod_进程被杀死 exit 137;
        Node_网络故障_节点端口占用 bind-failure exit 1) — those keep
        surfacing, the peel discriminates by SUCCESS."""
        detector = CrashLoopDetector()
        before = _make_snapshot(pods={
            "default/worker-1": PodSnapshot(
                name="worker-1", namespace="default", phase="Running",
                restart_counts={"main": 0}, crash_loop_containers=set(),
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "worker-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 3,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        "lastState": {"terminated": {
                            "exitCode": 137,
                            "reason": "Error",
                            "finishedAt": "2026-05-26T10:05:00Z",
                        }},
                    }],
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert len(results) == 1
        assert results[0]["restart_delta"] == 3

    def test_crashloop_contradictory_nonzero_exit_beats_completed_reason(self):
        """Same evidence hierarchy as the container_restarts peel: a
        non-zero exitCode outranks a contradicting "Completed" label, so
        the report stands even though the label alone would have peeled."""
        detector = CrashLoopDetector()
        before = _make_snapshot(pods={
            "default/worker-1": PodSnapshot(
                name="worker-1", namespace="default", phase="Running",
                restart_counts={"main": 0}, crash_loop_containers=set(),
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "worker-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 3,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        "lastState": {"terminated": {
                            "exitCode": 137,
                            "reason": "Completed",
                            "finishedAt": "2026-05-26T10:05:00Z",
                        }},
                    }],
                },
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert len(results) == 1
        assert results[0]["restart_delta"] == 3


class TestEndpointRemovalDetector:
    def test_detects_endpoint_drop(self):
        detector = EndpointRemovalDetector()
        before = _make_snapshot(endpoints={
            "api-svc": EndpointSnapshot(service="api-svc", ready_count=3),
        })
        after = PostInjectState(
            endpoints_json={"items": [{
                "metadata": {"name": "api-svc"},
                "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}],
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(before, after, ctx)
        assert len(results) == 1
        assert results[0]["service"] == "api-svc"
        assert results[0]["before"] == 3
        assert results[0]["after"] == 1

    def test_no_snapshot_returns_empty(self):
        detector = EndpointRemovalDetector()
        after = PostInjectState(endpoints_json={"items": []})
        ctx = _make_ctx()
        results = detector.detect(None, after, ctx)
        assert results == []


class TestHPAScaleDetector:
    def test_detects_rescale_event(self):
        detector = HPAScaleDetector()
        after = PostInjectState(
            events_json={"items": [{
                "reason": "SuccessfulRescale",
                "lastTimestamp": "2026-05-26T10:05:00Z",
                "involvedObject": {"name": "api-hpa"},
                "message": "New size: 5; reason: cpu resource utilization (percentage of request) above target; old size: 2; from 2 to 5",
            }]},
        )
        ctx = _make_ctx()
        results = detector.detect(None, after, ctx)
        assert len(results) == 1
        assert results[0]["hpa"] == "api-hpa"
        assert results[0]["old_replicas"] == 2
        assert results[0]["new_replicas"] == 5


class TestProbeFailureDetector:
    def test_detects_probe_failure_on_non_target(self):
        detector = ProbeFailureDetector()
        after = PostInjectState(
            events_json={"items": [{
                "reason": "Unhealthy",
                "lastTimestamp": "2026-05-26T10:03:00Z",
                "involvedObject": {"name": "gateway-pod"},
                "message": "Readiness probe failed: connection refused",
            }]},
        )
        ctx = _make_ctx(target_names=["app-pod-1"])
        results = detector.detect(None, after, ctx)
        assert len(results) == 1
        assert results[0]["pod"] == "gateway-pod"
        assert results[0]["probe_type"] == "Readiness"

    def test_excludes_target_pod(self):
        detector = ProbeFailureDetector()
        after = PostInjectState(
            events_json={"items": [{
                "reason": "Unhealthy",
                "lastTimestamp": "2026-05-26T10:03:00Z",
                "involvedObject": {"name": "app-pod-1"},
                "message": "Liveness probe failed",
            }]},
        )
        ctx = _make_ctx(target_names=["app-pod-1"])
        results = detector.detect(None, after, ctx)
        assert results == []


class TestDependencyErrorDetector:
    def test_detects_5xx_in_logs(self):
        detector = DependencyErrorDetector()
        after = PostInjectState(
            target_logs=(
                "2026-05-26T10:01:00Z INFO normal request\n"
                "2026-05-26T10:02:00Z ERROR connection refused to upstream\n"
                "2026-05-26T10:02:01Z ERROR connection refused to upstream\n"
                "2026-05-26T10:03:00Z ERROR HTTP 503 from service-b\n"
            ),
        )
        ctx = _make_ctx()
        results = detector.detect(None, after, ctx)
        patterns = {r["pattern"] for r in results}
        assert "connection refused" in patterns
        assert "503" in patterns

    def test_no_logs_returns_empty(self):
        detector = DependencyErrorDetector()
        after = PostInjectState(target_logs="")
        ctx = _make_ctx()
        results = detector.detect(None, after, ctx)
        assert results == []


class TestRunAllDetectors:
    def test_merges_multiple_detectors(self):
        before = _make_snapshot(pods={
            "default/app-pod-1": PodSnapshot(
                name="app-pod-1", namespace="default", phase="Running",
                restart_counts={"main": 0},
            ),
        })
        after = PostInjectState(
            pods_json={"items": [
                {
                    "metadata": {"name": "app-pod-1"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{
                            "name": "main",
                            "restartCount": 1,
                            "lastState": {"terminated": {"reason": "OOMKilled", "finishedAt": "2026-05-26T10:05:00Z"}},
                            "state": {"running": {}},
                        }],
                    },
                },
                {
                    "metadata": {"name": "other-pod"},
                    "status": {
                        "phase": "Failed",
                        "reason": "Evicted",
                        "message": "low on disk",
                        "containerStatuses": [],
                    },
                },
            ]},
            events_json={"items": []},
            endpoints_json={"items": []},
            target_logs="",
        )
        ctx = _make_ctx()
        results = run_all_detectors(before, after, ctx)
        assert "container_restarts" in results
        assert "evicted_pods" in results

    def test_empty_state_returns_empty(self):
        after = PostInjectState(
            pods_json={"items": []},
            events_json={"items": []},
            endpoints_json={"items": []},
        )
        ctx = _make_ctx()
        results = run_all_detectors(None, after, ctx)
        assert results == {}

    def test_peeled_renewal_leaves_no_key_for_consumers(self):
        # The consumer-side invariant of the B7 peel: an EMPTY detector
        # result must not enter the dict at all ("container_restarts" key
        # absent, not present-as-empty-list). Downstream consumers branch
        # on key presence / truthiness — verification["side_effects"].get(
        # "container_restarts", False) in the layer2_layer1_conflict gap,
        # side_effects.get("container_restarts") in infer_phase's
        # side-effect confirmation, state["side_effects"] in recover L1 —
        # so a peeled renewal is invisible to every one of them, which is
        # exactly the structural fix AUTO mode needs (no confirmation card
        # to catch the false positive).
        before = _make_snapshot(pods={
            "default/drill-pvc-target": PodSnapshot(
                name="drill-pvc-target", namespace="default", phase="Running",
                restart_counts={"main": 4},
            ),
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "drill-pvc-target"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 6,
                        "lastState": {"terminated": {
                            "exitCode": 0,
                            "reason": "Completed",
                            "finishedAt": "2026-05-26T10:26:00Z",
                        }},
                        "state": {"running": {}},
                    }],
                },
            }]},
            events_json={"items": []},
            endpoints_json={"items": []},
        )
        results = run_all_detectors(before, after, _make_ctx())
        assert "container_restarts" not in results
        assert "crash_loop_pods" not in results


class TestSnapshotSerialization:
    def test_round_trip(self):
        snapshot = _make_snapshot(
            pods={
                "p1": PodSnapshot(
                    name="p1", namespace="ns", phase="Running",
                    restart_counts={"c": 2},
                    oom_killed_containers={"c"},
                    crash_loop_containers=set(),
                ),
            },
            endpoints={
                "svc1": EndpointSnapshot(service="svc1", ready_count=5),
            },
        )
        d = snapshot.to_dict()
        restored = SideEffectSnapshot.from_dict(d)
        assert restored.pods["p1"].name == "p1"
        assert restored.pods["p1"].restart_counts == {"c": 2}
        assert "c" in restored.pods["p1"].oom_killed_containers
        assert restored.endpoints["svc1"].ready_count == 5


# ---------------------------------------------------------------------------
# Host profile: two-axis dispatch + host detectors + parse helpers
# ---------------------------------------------------------------------------

from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (  # noqa: E402
    DmesgOOMDetector,
    FilesystemFullDetector,
    HostPostInjectState,
    HostSnapshot,
    ProcessDeathDetector,
    ServiceDownDetector,
    _is_kernel_thread,
    _parse_df_mounts,
    _parse_process_comms,
    _parse_systemctl_services,
    iter_all_detectors,
    resolve_observer,
)


def _host_before(**kwargs) -> SideEffectSnapshot:
    host = HostSnapshot(**kwargs)
    return SideEffectSnapshot(captured_at="t0", namespace="", host=host)


def _host_after(**kwargs) -> PostInjectState:
    return PostInjectState(captured_at="t1", host=HostPostInjectState(**kwargs))


class TestHostObserverParsers:
    def test_parse_process_comms(self):
        out = "systemd\nsshd\nnginx\n\n  bash  \n"
        assert _parse_process_comms(out) == {"systemd", "sshd", "nginx", "bash"}
        assert _parse_process_comms(None) == set()

    def test_parse_process_comms_filters_kernel_threads(self):
        # Kernel threads churn on their own and must never reach the snapshot,
        # otherwise ProcessDeathDetector reports them as false "deaths".
        out = (
            "systemd\nsshd\nnginx\n"
            "kworker/0:1\nkworker/u16:3\nksoftirqd/0\nmigration/0\n"
            "rcu_sched\nwatchdog/0\nkthreadd\nkswapd0\n"
        )
        assert _parse_process_comms(out) == {"systemd", "sshd", "nginx"}

    def test_is_kernel_thread(self):
        for name in ("kworker/0:1", "kworker/u16:3", "ksoftirqd/2",
                     "migration/0", "rcu_sched", "watchdog/1", "kthreadd",
                     "kswapd0", "kcompactd0"):
            assert _is_kernel_thread(name) is True, name
        # ``watchdogd`` is a real USER-SPACE daemon (distinct from the kernel
        # ``watchdog/N`` softlockup threads) and must NOT be filtered.
        for name in ("systemd", "sshd", "nginx", "stress-ng", "python",
                     "watchdogd"):
            assert _is_kernel_thread(name) is False, name

    def test_parse_df_mounts(self):
        out = (
            "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
            "/dev/sda1         10000000 9600000    400000      96% /\n"
            "/dev/sdb1          5000000 1000000   4000000      20% /data\n"
        )
        mounts = _parse_df_mounts(out)
        assert mounts == {"/": 96, "/data": 20}

    def test_parse_systemctl_services(self):
        out = (
            "nginx.service     loaded active   running Nginx\n"
            "redis.service     loaded failed   failed  Redis\n"
            "not-a-unit line\n"
        )
        svc = _parse_systemctl_services(out)
        assert svc == {"nginx.service": "active", "redis.service": "failed"}


class TestProcessDeathDetector:
    def test_sibling_process_gone(self):
        before = _host_before(processes={"nginx", "redis", "victim"})
        after = _host_after(processes={"nginx", "victim"})
        ctx = _make_ctx(target_names=["victim"], profile="host", target="process")
        results = ProcessDeathDetector().detect(before, after, ctx)
        assert results == [{"process": "redis"}]

    def test_target_death_excluded(self):
        before = _host_before(processes={"nginx", "victim"})
        after = _host_after(processes={"nginx"})
        ctx = _make_ctx(target_names=["victim"], profile="host", target="process")
        # victim is the target — its death is intended, not collateral.
        assert ProcessDeathDetector().detect(before, after, ctx) == []


class TestFilesystemFullDetector:
    def test_mount_crosses_threshold(self):
        before = _host_before(mounts={"/": 40, "/data": 50})
        after = _host_after(mounts={"/": 41, "/data": 97})
        ctx = _make_ctx(profile="host", target="disk")
        results = FilesystemFullDetector().detect(before, after, ctx)
        assert results == [{"mount": "/data", "use_percent": 97, "baseline_percent": 50}]

    def test_already_full_not_incremental(self):
        before = _host_before(mounts={"/data": 96})
        after = _host_after(mounts={"/data": 99})
        ctx = _make_ctx(profile="host", target="disk")
        assert FilesystemFullDetector().detect(before, after, ctx) == []


class TestDmesgOOMDetector:
    def test_new_oom_line_flagged(self):
        before = _host_before(dmesg_line_count=2)
        after = _host_after(dmesg_lines=[
            "old line 1", "old line 2",
            "Out of memory: Killed process 123 (python)",
        ])
        ctx = _make_ctx(profile="host", target="mem")
        results = DmesgOOMDetector().detect(before, after, ctx)
        assert len(results) == 1
        assert "Out of memory" in results[0]["line"]

    def test_pre_baseline_oom_ignored(self):
        before = _host_before(dmesg_line_count=1)
        after = _host_after(dmesg_lines=["oom-killer invoked earlier", "quiet"])
        ctx = _make_ctx(profile="host", target="mem")
        # the oom line is before the cursor → ignored
        assert DmesgOOMDetector().detect(before, after, ctx) == []


class TestServiceDownDetector:
    def test_active_service_goes_down(self):
        before = _host_before(services={"nginx.service": "active", "cron.service": "active"})
        after = _host_after(services={"nginx.service": "active", "cron.service": "failed"})
        ctx = _make_ctx(profile="host", target="process")
        results = ServiceDownDetector().detect(before, after, ctx)
        assert results == [{"service": "cron.service", "before": "active", "after": "failed"}]


class TestHostProfileDispatch:
    def test_run_all_detectors_host_profile(self):
        before = _host_before(
            processes={"nginx", "victim"},
            services={"cron.service": "active"},
            dmesg_line_count=0,
        )
        after = _host_after(
            processes={"victim"},
            services={"cron.service": "failed"},
            dmesg_lines=["Out of memory: Killed process 9 (java)"],
            mounts={},
        )
        ctx = _make_ctx(target_names=["victim"], profile="host", target="mem")
        results = run_all_detectors(before, after, ctx, profile="host")
        # mem target: DmesgOOM (mem) runs, ProcessDeath/ServiceDown (agnostic) run,
        # FilesystemFull (disk) is filtered out.
        assert "dmesg_oom" in results
        assert "process_deaths" in results
        assert "service_down" in results
        assert "filesystem_full" not in results

    def test_applies_to_targets_filters_disk_only(self):
        before = _host_before(mounts={"/data": 50})
        after = _host_after(mounts={"/data": 98})
        ctx = _make_ctx(profile="host", target="mem")
        # disk-only detector must not fire for a mem fault
        results = run_all_detectors(before, after, ctx, profile="host")
        assert "filesystem_full" not in results

    def test_k8s_detectors_not_run_for_host_profile(self):
        before = _host_before(processes={"a"})
        after = _host_after(processes={"a"})
        ctx = _make_ctx(profile="host", target="process")
        results = run_all_detectors(before, after, ctx, profile="host")
        # no k8s detector keys leak into host results
        assert "container_restarts" not in results
        assert "oom_killed_pods" not in results

    def test_iter_all_detectors_covers_both_profiles(self):
        keys = {d.key for d in iter_all_detectors()}
        assert {"container_restarts", "oom_killed_pods"} <= keys
        assert {"process_deaths", "filesystem_full", "dmesg_oom", "service_down"} <= keys

    def test_host_observer_registered(self):
        assert resolve_observer("host") is not None
        assert resolve_observer("k8s") is not None
        assert resolve_observer("unknown") is None


class TestObserverSummarize:
    """Observers own how a snapshot reads (``summarize``) and whether they can
    capture for a given spec (``can_capture``); se_snapshot drives its
    tracker/session detail and skip decision off these, so both are pinned here."""

    def test_k8s_can_capture_with_namespace(self):
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        assert K8sObserver().can_capture(FaultSpec(namespace="prod")) is True

    def test_k8s_can_capture_node_scope_without_namespace(self):
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        spec = FaultSpec(scope="node", names=("node-1",), namespace="")
        assert K8sObserver().can_capture(spec) is True

    def test_k8s_cannot_capture_without_namespace_or_node(self):
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        assert K8sObserver().can_capture(FaultSpec(namespace="")) is False

    def test_host_can_always_capture(self):
        assert HostObserver().can_capture(object()) is True

    def test_k8s_summarize_counts_pods_and_endpoints(self):
        snap = _make_snapshot(
            pods={"p1": None, "p2": None, "p3": None},
            endpoints={"svc-a": None, "svc-b": None},
        )
        phrase, metrics = K8sObserver().summarize(snap)
        assert phrase == "3 pods, 2 endpoints"
        assert metrics == {"pods": 3, "endpoints": 2}

    def test_host_summarize_counts_processes_and_mounts(self):
        host = HostSnapshot(
            processes={"sshd", "nginx"},
            mounts={"/": 40, "/data": 55, "/var": 12},
        )
        snap = _make_snapshot(namespace="", host=host)
        phrase, metrics = HostObserver().summarize(snap)
        assert phrase == "2 processes, 3 mounts"
        assert metrics == {"processes": 2, "mounts": 3}

    def test_host_summarize_handles_missing_host(self):
        snap = _make_snapshot(namespace="")  # host payload absent
        phrase, metrics = HostObserver().summarize(snap)
        assert phrase == "0 processes, 0 mounts"
        assert metrics == {"processes": 0, "mounts": 0}


class TestProfileScopedSummary:
    """The side-effect summary breakdown is scoped to the run's profile: only
    the detectors that actually ran (that profile's group) are listed, so a k8s
    run does not carry host-only categories stuck at ``: 0`` and vice versa."""

    def test_detectors_for_k8s_excludes_host_groups(self):
        from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (
            detectors_for,
        )

        keys = {d.key for d in detectors_for("k8s")}
        assert "container_restarts" in keys
        assert "process_deaths" not in keys  # host-only

    def test_detectors_for_host_excludes_k8s_groups(self):
        from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (
            detectors_for,
        )

        keys = {d.key for d in detectors_for("host")}
        assert "process_deaths" in keys
        assert "container_restarts" not in keys  # k8s-only

    def test_detectors_for_none_falls_back_to_k8s(self):
        from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (
            detectors_for,
        )

        assert [d.key for d in detectors_for(None)] == [d.key for d in detectors_for("k8s")]

    def test_k8s_summary_omits_host_labels(self):
        from chaos_agent.agent.state import _build_side_effects_summary

        summary = _build_side_effects_summary({"side_effects": {}}, "k8s")
        assert "ContainerRestarts" in summary
        assert "ProcessDeaths" not in summary  # host label must not leak in

    def test_host_summary_omits_k8s_labels(self):
        from chaos_agent.agent.state import _build_side_effects_summary

        summary = _build_side_effects_summary({"side_effects": {}}, "host")
        assert "ProcessDeaths" in summary
        assert "ContainerRestarts" not in summary  # k8s label must not leak in

    def test_summary_counts_only_run_profile_detections(self):
        from chaos_agent.agent.state import _build_side_effects_summary

        # A host detection present in the dict is still counted in the total,
        # but a k8s-profile breakdown lists only k8s categories.
        summary = _build_side_effects_summary(
            {"side_effects": {"container_restarts": [{"pod": "p1"}]}}, "k8s",
        )
        assert "1 collateral impact(s) detected" in summary
        assert "ContainerRestarts: 1" in summary
        assert "ProcessDeaths" not in summary


class TestPostInjectCaptureErrors:
    """B77/B81 family: a failed capture channel must stay distinguishable
    from a genuinely-empty one past the log layer (``capture_errors``)."""

    async def test_failed_channels_record_diagnosis(self, monkeypatch):
        from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as det
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0, task_id="",
                               source="", expect_profile=None, **kwargs):
            tokens = [str(t) for t in cmd]
            # pods query fails; events query succeeds but empty; endpoints
            # query returns unparseable json.
            if "pods" in tokens:
                return CommandResult(
                    exit_code=1, stdout="", stderr="forbidden",
                )
            if "endpoints" in tokens:
                return CommandResult(exit_code=0, stdout="not-json", stderr="")
            return CommandResult(exit_code=0, stdout="{}", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        state = await det.fetch_post_inject_state(
            "ns", "", "2026-01-01T00:00:00Z", ["web-0"],
        )
        assert state.pods_json == {}          # fail-closed payload
        assert "exit=1" in state.capture_errors["pods"]
        assert "bad json" in state.capture_errors["endpoints"]
        # A healthy empty channel stays OUT of capture_errors entirely.
        assert "events" not in state.capture_errors

    async def test_all_healthy_leaves_errors_empty(self, monkeypatch):
        from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as det
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0, task_id="",
                               source="", expect_profile=None, **kwargs):
            return CommandResult(exit_code=0, stdout="{}", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        state = await det.fetch_post_inject_state(
            "ns", "", "2026-01-01T00:00:00Z", ["web-0"],
        )
        assert state.capture_errors == {}


class TestUnreadableChannelGate:
    """B77 tri-state gate: a detector whose declared channel is Unreadable
    (``capture_errors``) must be SKIPPED — "not captured" is never adjudicated
    as "captured, found empty" (case #40: failed endpoints query reported
    EndpointRemovals 2→0 off an empty payload)."""

    def test_unreadable_endpoints_skips_endpoint_removal(self):
        # #40 replay: baseline has a live service, the post-inject endpoints
        # QUERY FAILED (payload {} + capture_errors) — the old code read the
        # empty dict as "0 ready endpoints" and reported a removal.
        before = _make_snapshot(endpoints={
            "svc-a": EndpointSnapshot(service="svc-a", ready_count=2),
        })
        after = PostInjectState(
            endpoints_json={},
            capture_errors={"endpoints": "exit=1: forbidden"},
        )
        results = run_all_detectors(before, after, _make_ctx())
        assert "endpoint_removals" not in results

    def test_true_empty_endpoints_still_reports_removal(self):
        # Empty leg: the query SUCCEEDED and the Endpoints list is genuinely
        # empty — the service really lost all its addresses. This is a REAL
        # finding and must still be reported (gating must not over-suppress).
        before = _make_snapshot(endpoints={
            "svc-a": EndpointSnapshot(service="svc-a", ready_count=2),
        })
        after = PostInjectState(endpoints_json={"items": []})
        results = run_all_detectors(before, after, _make_ctx())
        assert results.get("endpoint_removals") == [
            {"service": "svc-a", "before": 2, "after": 0},
        ]

    def test_value_endpoints_normal_diff(self):
        # Value leg: healthy capture, healthy diff (2 → 1 partial removal).
        before = _make_snapshot(endpoints={
            "svc-a": EndpointSnapshot(service="svc-a", ready_count=2),
        })
        after = PostInjectState(endpoints_json={"items": [{
            "metadata": {"name": "svc-a"},
            "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}],
        }]})
        results = run_all_detectors(before, after, _make_ctx())
        assert results.get("endpoint_removals") == [
            {"service": "svc-a", "before": 2, "after": 1},
        ]

    def test_unreadable_pods_skips_pods_detectors(self):
        # Symmetric hazard on the pods channel: a failed pods query must not
        # be diffed (restart deltas / evictions / crash loops all read the
        # empty items list as "nothing changed", which is a claim about the
        # cluster the capture cannot support).
        before = _make_snapshot(pods={
            "default/app-pod-1": PodSnapshot(
                name="app-pod-1", namespace="default", phase="Running",
                restart_counts={"main": 0},
            ),
        })
        after = PostInjectState(
            pods_json={},
            capture_errors={"pods": "exit=1: connection refused"},
        )
        results = run_all_detectors(before, after, _make_ctx())
        for key in ("container_restarts", "evicted_pods", "crash_loop_pods"):
            assert key not in results, key

    def test_unreadable_channels_helper_single_source(self):
        from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (
            unreadable_channels,
        )
        after = PostInjectState(capture_errors={"endpoints": "exit=1: x"})
        assert unreadable_channels(after) == {"endpoints": "exit=1: x"}
        # Empty/Value legs: healthy capture → no unreadable channels.
        assert unreadable_channels(PostInjectState()) == {}

    def test_host_ps_failure_gates_process_death(self):
        # Host symmetric #40: a failed `ps` yields an empty after-set and the
        # old code would report EVERY baseline process as dead.
        before = _host_before(processes={"nginx", "sshd"})
        after = PostInjectState(
            host=HostPostInjectState(processes=set()),
            capture_errors={"processes": "diag failed: ps -e -o comm= returned nothing"},
        )
        ctx = _make_ctx(profile="host", target="process")
        results = run_all_detectors(before, after, ctx, profile="host")
        assert "process_deaths" not in results

    def test_host_healthy_ps_reports_real_death(self):
        # Empty leg must NOT suppress a genuine finding: ps succeeded, the
        # process is really gone.
        before = _host_before(processes={"nginx", "sshd"})
        after = _host_after(processes={"nginx"})
        ctx = _make_ctx(profile="host", target="process")
        results = run_all_detectors(before, after, ctx, profile="host")
        assert results.get("process_deaths") == [{"process": "sshd"}]


class TestHostObserverCaptureErrors:
    """HostObserver must mark a None diag output as Unreadable (tri-state),
    not as an empty host."""

    async def test_failed_diags_recorded_as_unreadable(self, monkeypatch):
        from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as det

        async def fake_diag(command, timeout=10):
            # ps and dmesg fail; df and systemctl succeed.
            if command[:2] == ["ps", "-e"]:
                return None
            if command[0] == "dmesg":
                return None
            if command[0] == "df":
                return "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1 1 1 1% /\n"
            return "nginx.service loaded active running Nginx\n"

        monkeypatch.setattr(det, "_run_host_diag", fake_diag)

        class _Spec:
            namespace = ""
            scope = "host"
            names = ()
            fault_target = "process"

        state = await det.HostObserver().fetch_post_inject_state(
            _Spec(), "", "2026-01-01T00:00:00Z",
        )
        assert "processes" in state.capture_errors
        assert "dmesg" in state.capture_errors
        assert "mounts" not in state.capture_errors
        assert "services" not in state.capture_errors

    async def test_healthy_diags_leave_errors_empty(self, monkeypatch):
        from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as det

        async def fake_diag(command, timeout=10):
            return "some output\n"

        monkeypatch.setattr(det, "_run_host_diag", fake_diag)

        class _Spec:
            namespace = ""
            scope = "host"
            names = ()
            fault_target = "process"

        state = await det.HostObserver().fetch_post_inject_state(
            _Spec(), "", "2026-01-01T00:00:00Z",
        )
        assert state.capture_errors == {}


# ---------------------------------------------------------------------------
# F2 symmetry: BEFORE-side tri-state (baseline capture_errors + gating)
# ---------------------------------------------------------------------------


class TestBeforeSideTriState:
    """F2: the baseline snapshot carries the same Unreadable leg as after.

    A channel that failed at baseline must gate the detectors reading it —
    in the OPPOSITE failure direction from an after-side miss: a failed
    before-pods query reads as baseline_rc=0 (ContainerRestart would flag
    every pre-existing restart), a failed before-endpoints reads as an
    empty baseline (EndpointRemoval would miss every removal).
    """

    def test_before_pods_error_gates_restart_detector(self):
        """B85/F2 headline case: before-pods Unreadable → a pod whose
        restartCount ROSE from an unknown baseline is NOT adjudicated —
        the container_restarts key must stay absent."""
        before = _make_snapshot(pods={}, capture_errors={
            "pods": "exit=1: connection refused",
        })
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 5,
                        "state": {"running": {}},
                    }],
                },
            }]},
            events_json={"items": []},
            endpoints_json={"items": []},
        )
        results = run_all_detectors(before, after, _make_ctx())
        assert "container_restarts" not in results

    def test_before_endpoints_error_gates_removal_detector(self):
        before = _make_snapshot(
            endpoints={}, capture_errors={"endpoints": "bad json: x"},
        )
        after = PostInjectState(
            pods_json={"items": []},
            events_json={"items": []},
            endpoints_json={"items": []},
        )
        results = run_all_detectors(before, after, _make_ctx())
        assert "endpoint_removals" not in results

    def test_before_and_after_errors_both_gate(self):
        """Same channel failed on BOTH sides — still one skip, and the
        warning names both sources (dedup, no double listing)."""
        before = _make_snapshot(capture_errors={"pods": "exit=1: before"})
        after = PostInjectState(
            pods_json={}, events_json={}, endpoints_json={},
            capture_errors={"pods": "exit=1: after"},
        )
        results = run_all_detectors(before, after, _make_ctx())
        assert "container_restarts" not in results

    def test_unaffected_detector_still_runs(self):
        """A failing before-endpoints channel must not gate detectors that
        read OTHER channels (pods-reading restarts keep running)."""
        before = _make_snapshot(
            pods={"default/app-pod-1": PodSnapshot(
                name="app-pod-1", namespace="default", phase="Running",
                restart_counts={"main": 0},
            )},
            endpoints={},
            capture_errors={"endpoints": "exit=1: refused"},
        )
        after = PostInjectState(
            pods_json={"items": [{
                "metadata": {"name": "app-pod-1"},
                "status": {
                    "containerStatuses": [{
                        "name": "main",
                        "restartCount": 1,
                        "lastState": {"terminated": {
                            "reason": "OOMKilled",
                            "finishedAt": "2026-05-26T10:05:00Z",
                        }},
                        "state": {"running": {}},
                    }],
                },
            }]},
            events_json={"items": []},
            endpoints_json={"items": []},
        )
        results = run_all_detectors(before, after, _make_ctx())
        assert "container_restarts" in results
        assert "endpoint_removals" not in results

    def test_snapshot_round_trip_carries_capture_errors(self):
        snapshot = _make_snapshot(capture_errors={"pods": "exit=1: boom"})
        restored = SideEffectSnapshot.from_dict(snapshot.to_dict())
        assert restored.capture_errors == {"pods": "exit=1: boom"}

    def test_legacy_snapshot_dict_without_errors_stays_empty(self):
        """Pre-F2 persisted snapshots (no capture_errors key) deserialize
        to an empty dict — legacy baselines keep their old semantics."""
        d = _make_snapshot().to_dict()
        del d["capture_errors"]
        restored = SideEffectSnapshot.from_dict(d)
        assert restored.capture_errors == {}


class TestBeforeCaptureRecordsErrors:
    """The baseline capture paths record failures instead of fake-empty."""

    @pytest.mark.asyncio
    async def test_k8s_capture_records_pods_failure(self, monkeypatch):
        from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as det

        class _FakeResult:
            exit_code = 1
            stdout = ""
            stderr = "Error from server: forbidden"

        async def fake_exec(cmd, target, **kwargs):
            return _FakeResult()

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_exec,
        )
        snap = await det.capture_snapshot("default", "/kc", task_id="t1")
        assert snap is not None
        assert "pods" in snap.capture_errors
        assert "forbidden" in snap.capture_errors["pods"]

    @pytest.mark.asyncio
    async def test_k8s_capture_healthy_leaves_errors_empty(self, monkeypatch):
        from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as det

        class _FakeResult:
            exit_code = 0
            stdout = '{"items": []}'
            stderr = ""

        async def fake_exec(cmd, target, **kwargs):
            return _FakeResult()

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_exec,
        )
        snap = await det.capture_snapshot("default", "/kc", task_id="t1")
        assert snap is not None
        assert snap.capture_errors == {}

    @pytest.mark.asyncio
    async def test_host_baseline_records_failed_diags(self, monkeypatch):
        from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as det

        async def fake_diag(command, timeout=10):
            return None if command[:2] == ["ps", "-e"] else "ok\n"

        monkeypatch.setattr(det, "_run_host_diag", fake_diag)

        class _Spec:
            namespace = ""
            scope = "host"
            names = ()
            fault_target = "process"

        snap = await det.HostObserver().capture_base_snapshot(_Spec(), "")
        assert snap is not None
        assert "processes" in snap.capture_errors
        assert "mounts" not in snap.capture_errors
        # and the failure gates the matching host detector:
        after = det.PostInjectState(
            host=det.HostPostInjectState(processes=set()),
        )
        results = run_all_detectors(
            snap, after, _make_ctx(profile="host", target="process"),
        )
        assert "process_deaths" not in results
