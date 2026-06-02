"""Tests for the side-effect detection framework."""

from chaos_agent.agent.nodes._side_effect_detectors import (
    ContainerRestartDetector,
    CrashLoopDetector,
    DependencyErrorDetector,
    DetectionContext,
    EndpointRemovalDetector,
    EndpointSnapshot,
    EvictedPodDetector,
    HPAScaleDetector,
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
            "app-pod-1": PodSnapshot(
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
            "app-pod-1": PodSnapshot(
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


class TestEvictedPodDetector:
    def test_detects_new_eviction(self):
        detector = EvictedPodDetector()
        before = _make_snapshot(pods={
            "app-pod-1": PodSnapshot(
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
            "app-pod-1": PodSnapshot(
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
            "sidecar-1": PodSnapshot(
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
            "worker-1": PodSnapshot(
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
            "app-pod-1": PodSnapshot(
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
