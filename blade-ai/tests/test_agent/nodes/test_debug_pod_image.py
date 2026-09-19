"""Image candidate chain + fast-fail readiness for framework debug pods.

Restricted-network clusters (VPC without docker.io egress) cannot pull the
historic hard-coded ``busybox``: the debug pod lands in ImagePullBackOff,
the 60s readiness wait burns out, and the whole baseline round is wasted
(#23/#28 three-sample forensics). Discovery order is alphabetical and the
probe does not verify toolchains, so ``_resolve_debug_pod_images`` must
expose an ordered candidate chain (explicit > all discovered > busybox)
and ``create_and_wait_debug_pod`` must abandon a doomed candidate in
seconds (deterministic failure reasons / restartCount on the sleep-only
skeleton) and try the next one.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.execute._debug_pod import (
    _resolve_debug_pod_images,
    create_and_wait_debug_pod,
    wait_for_debug_pod_ready,
)

_SETTINGS = "chaos_agent.agent.nodes.execute._debug_pod.settings"
_XPORT = "chaos_agent.agent.nodes.execute._debug_pod.execute_via_transport"
_SLEEP = "chaos_agent.agent.nodes.execute._debug_pod.asyncio.sleep"


def _cfg(explicit="", discovered=""):
    return patch(
        _SETTINGS,
        SimpleNamespace(
            debug_pod_image=explicit,
            recovery_carrier_discovered_images=discovered,
            timeout_kubectl=10,
            timeout_kubectl_exec=30,
        ),
    )


def _result(exit_code=0, stderr="", stdout=""):
    return SimpleNamespace(exit_code=exit_code, stderr=stderr, stdout=stdout)


# ── _resolve_debug_pod_images: ordered candidate chain ──────────────────────


def test_explicit_config_is_a_single_candidate_chain():
    with _cfg(explicit="my-registry/custom:v1", discovered="img-a,img-b"):
        assert _resolve_debug_pod_images() == ["my-registry/custom:v1"]


def test_discovered_candidates_keep_order_with_busybox_last():
    # Alphabetical discovery puts node-cached DaemonSet images first; busybox
    # (unpullable on restricted networks but the historic default) stays
    # last as the open-egress fallback.
    with _cfg(discovered="img-npd,img-csi,img-terway"):
        assert _resolve_debug_pod_images() == ["img-npd", "img-csi", "img-terway", "busybox"]


def test_duplicate_and_blank_entries_are_deduped():
    with _cfg(discovered="img-a, img-b,,img-a"):
        assert _resolve_debug_pod_images() == ["img-a", "img-b", "busybox"]


def test_empty_everything_falls_back_to_busybox_only():
    with _cfg():
        assert _resolve_debug_pod_images() == ["busybox"]


# ── wait_for_debug_pod_ready: deterministic failures return immediately ─────


@pytest.mark.asyncio
async def test_crashloop_reason_fails_fast_without_long_wait():
    calls = []

    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        calls.append(argv)
        if "jsonpath" in argv:
            return _result(stdout="false|0|CrashLoopBackOff")
        return _result(exit_code=1, stderr="timeout")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()):
        assert await wait_for_debug_pod_ready("p1", "/kc", "t") is False
    # One fast-fail probe in Phase A — no kubectl wait, no more polling.
    assert len([c for c in calls if "jsonpath" in c]) == 1
    assert not any(" wait " in f" {c} " for c in calls)
    # Regression anchor (task inject-43173315): the probe MUST address the
    # pod as `pod/<name>` — a bare `node-debugger-<node>-<suffix>` first
    # token is parsed by kubectl as a resource type and the probe returns
    # "server doesn't have a resource type" for every poll, silently
    # disabling fast-fail (Phase B's prefixed wait then saves the run).
    probe = next(c for c in calls if "jsonpath" in c)
    assert " pod/p1 " in f" {probe} "


@pytest.mark.asyncio
async def test_image_pull_backoff_fails_fast():
    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "jsonpath" in argv:
            return _result(stdout="false|0|ImagePullBackOff")
        return _result(exit_code=1, stderr="timeout")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()):
        assert await wait_for_debug_pod_ready("p1", "/kc", "t") is False


@pytest.mark.asyncio
async def test_restart_count_means_dead_sleep_skeleton():
    # The skeleton is ``sleep N`` — it never exits on its own, so any restart
    # proves the container died at startup (entrypoint crash).
    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "jsonpath" in argv:
            return _result(stdout="false|2|")
        return _result(exit_code=1, stderr="timeout")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()):
        assert await wait_for_debug_pod_ready("p1", "/kc", "t") is False


@pytest.mark.asyncio
async def test_ready_probe_returns_true():
    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "jsonpath" in argv:
            return _result(stdout="true|0|")
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()):
        assert await wait_for_debug_pod_ready("p1", "/kc", "t") is True


@pytest.mark.asyncio
async def test_transient_pending_falls_through_to_long_wait_then_false():
    calls = []

    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        calls.append(argv)
        if "jsonpath" in argv:
            return _result(stdout="false|0|ContainerCreating")
        if "--for=condition=Ready" in argv:
            return _result(exit_code=1, stderr="timed out")
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()):
        assert await wait_for_debug_pod_ready("p1", "/kc", "t") is False
    # Phase A exhausted (4 probes), Phase B long wait ran, Phase C probed once.
    probes = [c for c in calls if "jsonpath" in c]
    waits = [c for c in calls if "--for=condition=Ready" in c]
    assert len(probes) == 5
    assert len(waits) == 1


# ── create_and_wait_debug_pod: candidate retry chain ────────────────────────


@pytest.mark.asyncio
async def test_doomed_first_candidate_is_cleaned_up_and_next_wins():
    """The real restricted-network shape: alphabetical discovery ranks a Go
    single-binary image first (cannot host ``-- sleep 3600``), the proven
    image ranks later. Creation succeeds for both — readiness does not."""
    created_images = []
    deleted_pods = []

    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "debug" in argv and "node/n1" in argv:
            image = next(a for a in argv.split() if a.startswith("--image="))
            created_images.append(image.split("=", 1)[1])
            if image.endswith("img-npd"):
                return _result(
                    stdout="Creating debugging pod node-debugger-n1-aaa "
                           "with container debugger on node n1.")
            return _result(
                stdout="Creating debugging pod node-debugger-n1-bbb "
                       "with container debugger on node n1.")
        if "jsonpath" in argv:
            if "node-debugger-n1-aaa" in argv:
                return _result(stdout="false|0|CrashLoopBackOff")
            return _result(stdout="true|0|")
        if "delete" in argv:
            deleted_pods.append(argv)
            return _result()
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(discovered="img-npd,img-terway"):
        result = await create_and_wait_debug_pod("n1", "/kc", "t", namespace="default")

    assert result == ("node-debugger-n1-bbb", "default")
    assert created_images == ["img-npd", "img-terway"]
    # The doomed candidate's pod was force-deleted before trying the next.
    assert any("node-debugger-n1-aaa" in p for p in deleted_pods)


@pytest.mark.asyncio
async def test_all_candidates_dead_returns_none():
    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "debug" in argv and "node/n1" in argv:
            return _result(
                stdout="Creating debugging pod node-debugger-n1-aaa "
                       "with container debugger on node n1.")
        if "jsonpath" in argv:
            return _result(stdout="false|0|ImagePullBackOff")
        if "delete" in argv:
            return _result()
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(discovered="img-a,img-b"):
        # Two discovered candidates + busybox tail = 3 attempts, all doomed.
        assert await create_and_wait_debug_pod("n1", "/kc", "t", namespace="default") is None


@pytest.mark.asyncio
async def test_create_exit_nonzero_skips_to_next_candidate():
    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "debug" in argv and "node/n1" in argv:
            if "--image=img-a" in argv:
                return _result(exit_code=1, stderr="nope")
            return _result(
                stdout="Creating debugging pod node-debugger-n1-bbb "
                       "with container debugger on node n1.")
        if "jsonpath" in argv:
            return _result(stdout="true|0|")
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(discovered="img-a,img-b"):
        result = await create_and_wait_debug_pod("n1", "/kc", "t", namespace="default")
    assert result == ("node-debugger-n1-bbb", "default")


@pytest.mark.asyncio
async def test_parse_failure_stops_the_chain_no_leak_amplification():
    """Parse failure is image-independent (kubectl output shape): retrying
    other candidates would leak one unparseable — hence uncleanable — pod
    per candidate. The chain must bail out after the FIRST such failure."""
    created_images = []

    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "debug" in argv and "node/n1" in argv:
            image = next(a for a in argv.split() if a.startswith("--image="))
            created_images.append(image.split("=", 1)[1])
            # exit 0 but unparseable output — the parse-failure shape.
            return _result(stdout="pod created (weird format)")
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(discovered="img-a,img-b"):
        assert await create_and_wait_debug_pod("n1", "/kc", "t", namespace="default") is None
    # One attempt only — no candidate retry on an image-independent failure.
    assert created_images == ["img-a"]


@pytest.mark.asyncio
async def test_explicit_config_tries_only_that_image():
    created_images = []

    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "debug" in argv and "node/n1" in argv:
            image = next(a for a in argv.split() if a.startswith("--image="))
            created_images.append(image)
            return _result(
                stdout="Creating debugging pod node-debugger-n1-aaa "
                       "with container debugger on node n1.")
        if "jsonpath" in argv:
            return _result(stdout="false|0|CrashLoopBackOff")
        if "delete" in argv:
            return _result()
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(explicit="my-registry/custom:v1", discovered="img-a,img-b"):
        assert await create_and_wait_debug_pod("n1", "/kc", "t", namespace="default") is None
    # Explicit config is honoured exactly — no silent fallback candidates.
    assert created_images == ["--image=my-registry/custom:v1"]


# ── B49: sysadmin profile on every created debug pod ───────────────────────


@pytest.mark.asyncio
async def test_created_debug_pods_carry_sysadmin_profile():
    """B49 (case #33): a bare debug container cannot run host-level read-only
    probes — ``chroot /host iptables`` fails "Permission denied (you must be
    root)" without CAP_NET_ADMIN, and nsenter into host namespaces needs
    CAP_SYS_ADMIN. The executor-phase LLM path already creates sysadmin pods;
    the framework path must match: one "debug pod" concept, one capability
    level. Probe CONTENT is still gated by the readonly classifier."""
    creation_cmds = []

    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "debug" in argv and "node/n1" in argv:
            creation_cmds.append(argv)
            return _result(
                stdout="Creating debugging pod node-debugger-n1-aaa "
                       "with container debugger on node n1.")
        if "jsonpath" in argv:
            return _result(stdout="true|0|")
        if "delete" in argv:
            return _result()
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(explicit="my-registry/custom:v1"):
        result = await create_and_wait_debug_pod("n1", "/kc", "t", namespace="default")

    assert result == ("node-debugger-n1-aaa", "default")
    assert creation_cmds, "creation command must have been issued"
    for argv in creation_cmds:
        assert "--profile=sysadmin" in argv.split(), (
            "framework-created debug pods must be privileged (B49): " + argv)
        # profile sits between image and the -- separator (shape pin).
        parts = argv.split()
        assert parts.index("--profile=sysadmin") < parts.index("--")


def test_b49_debug_pod_creation_argv_passes_the_real_guard():
    """B49 self-review: the argv-construction test above MOCKS the transport,
    so it pins the shape but not the admission. ``create_and_wait_debug_pod``
    dispatches through ``execute_via_transport`` → ``guard_gateway`` — a guard
    rejection there is caught as a per-image warning and the candidate chain
    quietly exhausts into ``return None``: baseline capture degrades with NO
    failing test (the silent path). This pin routes the EXACT creation argv
    through the REAL gateway so any future guard tightening around kubectl
    debug flag shapes fails HERE, loudly, instead of in the field."""
    from chaos_agent.tools.guard_gateway import get_guard_gateway

    cmd = [
        "kubectl", "debug", "node/n1", "-n", "default",
        "--image=busybox", "--profile=sysadmin", "--", "sleep", "3600",
    ]
    feedback = get_guard_gateway().check_command(cmd)
    assert feedback.allowed, (
        "the framework debug-pod creation argv must stay guard-admissible "
        "(B49); a rejection degrades baseline capture silently (warning + "
        "candidate exhaustion + return None). If the guard was tightened "
        "deliberately, update the creation shape in _debug_pod.py together "
        "with this pin: " + feedback.render_for_llm()[:200]
    )
