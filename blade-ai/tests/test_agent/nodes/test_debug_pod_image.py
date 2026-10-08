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

The chain retries over IMAGES, so it may only absorb image-shaped failures:
see the W-67-1 section at the bottom for the classification pins.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.execute._debug_pod import (
    CARRIER_IMAGE_EXHAUSTED,
    CARRIER_PARSE_FAILURE,
    CARRIER_REQUEST_REJECTED,
    CARRIER_RETRYABLE,
    CARRIER_TARGET_MISSING,
    _classify_debug_create_failure,
    _resolve_debug_pod_images,
    create_and_wait_debug_pod,
    create_and_wait_debug_pod_with_reason,
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


# ── W-67-1: the retry dimension must match the failure dimension ────────────
#
# ``create_and_wait_debug_pod`` retries over IMAGE candidates. Case #67 fed it
# a pod name where a node name belonged, so the API answered
# ``Error from server (NotFound): nodes "<POD>" not found`` and the loop
# rotated every candidate — ten identical rejections, ten mutation requests,
# and a caller that could not tell "no image worked" from "that node does not
# exist". These pins lock the classification, not the case: the strings below
# are kubectl's standard rendering of apimachinery StatusReasons, so the
# behaviour they assert holds for any resource kind and any target name.


@pytest.mark.parametrize("stderr,expected", [
    # Canonical API-status renderings.
    ('Error from server (NotFound): nodes "n1" not found',
     CARRIER_TARGET_MISSING),
    ('Error from server (NotFound): pods "victim-0" not found',
     CARRIER_TARGET_MISSING),
    ('Error from server (Forbidden): pods is forbidden: user "u" cannot '
     'create resource "pods" in API group "" in the namespace "default"',
     CARRIER_REQUEST_REJECTED),
    ('Error from server (Unauthorized): Unauthorized',
     CARRIER_REQUEST_REJECTED),
    ('Error from server (MethodNotSupported): delete not supported',
     CARRIER_REQUEST_REJECTED),
    # Prefix stripped by a wrapping transport — same fact, bare shape.
    ('nodes "n1" not found', CARRIER_TARGET_MISSING),
    # Image-shaped complaints stay retryable: swapping the candidate IS the
    # cure, whatever prefix they arrive under.
    ('pull access denied for img-a, repository does not exist',
     CARRIER_RETRYABLE),
    ('failed to resolve reference "registry.io/lib/busybox:latest": '
     'manifest unknown', CARRIER_RETRYABLE),
    ('Error from server (Invalid): spec.containers[0].image: Invalid value',
     CARRIER_RETRYABLE),
    # Structural evidence outranks keywords: an object NAME that merely
    # contains an image vocabulary word must not mask a missing target.
    ('Error from server (NotFound): nodes "image-cache-0" not found',
     CARRIER_TARGET_MISSING),
    ('Error from server (NotFound): nodes "registry-pool-1" not found',
     CARRIER_TARGET_MISSING),
    # Transport / connectivity hiccups and unrecognized shapes stay in the
    # historic retryable bucket — the fix may only ever STOP useless retries.
    ('Unable to connect to the server: connection refused',
     CARRIER_RETRYABLE),
    ('etcdserver: request timed out', CARRIER_RETRYABLE),
    ('nope', CARRIER_RETRYABLE),
    ('', CARRIER_RETRYABLE),
])
def test_create_failure_classification(stderr, expected):
    """The classifier decides whether another --image value could help."""
    assert _classify_debug_create_failure(stderr) == expected


async def _attempted_images(create_stderr, create_exit=1, discovered="img-a,img-b"):
    """Run the chain against a fixed create-call rejection; return the images
    it actually attempted."""
    attempted = []

    async def fake_xport(cmd, *_a, **_kw):
        argv = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "debug" in argv and "node/" in argv:
            image = next(a for a in argv.split() if a.startswith("--image="))
            attempted.append(image.split("=", 1)[1])
            return _result(exit_code=create_exit, stderr=create_stderr)
        return _result(exit_code=1, stderr="unexpected")

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(discovered=discovered):
        carrier, reason = await create_and_wait_debug_pod_with_reason(
            "n1", "/kc", "t", namespace="default",
        )
    return attempted, carrier, reason


@pytest.mark.asyncio
async def test_missing_node_stops_the_chain_after_one_attempt():
    """The exact W-67-1 shape: one rejection, not one per candidate."""
    attempted, carrier, reason = await _attempted_images(
        'Error from server (NotFound): nodes "drill-sts-pvc-target-0" '
        'not found',
    )
    assert carrier is None
    assert reason == CARRIER_TARGET_MISSING
    assert attempted == ["img-a"]


@pytest.mark.asyncio
async def test_forbidden_caller_stops_the_chain_after_one_attempt():
    """RBAC is image-independent too — and unlike a missing target it leaves
    the node real, which is why the reason is distinct."""
    attempted, carrier, reason = await _attempted_images(
        'Error from server (Forbidden): pods is forbidden',
    )
    assert carrier is None
    assert reason == CARRIER_REQUEST_REJECTED
    assert attempted == ["img-a"]


@pytest.mark.asyncio
async def test_image_shaped_rejection_still_rotates_every_candidate():
    """Non-degradation pin: a rejection that names the image must keep the
    chain alive — that is the only failure the chain can cure."""
    attempted, carrier, reason = await _attempted_images(
        'pull access denied for img-a, manifest unknown',
    )
    assert carrier is None
    assert reason == CARRIER_IMAGE_EXHAUSTED
    # img-a, img-b, then the busybox tail.
    assert attempted == ["img-a", "img-b", "busybox"]


@pytest.mark.asyncio
async def test_unclassified_rejection_keeps_the_historic_rotation():
    """Unknown stderr keeps rotating (the pre-fix behaviour), so this change
    can never turn a survivable failure into a dead end."""
    attempted, carrier, reason = await _attempted_images("nope")
    assert carrier is None
    assert reason == CARRIER_IMAGE_EXHAUSTED
    assert attempted == ["img-a", "img-b", "busybox"]


@pytest.mark.asyncio
async def test_object_name_containing_an_image_word_is_still_a_missing_target():
    """Keyword-only matching would misread ``nodes "image-cache-0" not found``
    as an image problem and burn the whole chain. Structural evidence wins."""
    attempted, _carrier, reason = await _attempted_images(
        'Error from server (NotFound): nodes "image-cache-0" not found',
    )
    assert reason == CARRIER_TARGET_MISSING
    assert attempted == ["img-a"]


@pytest.mark.asyncio
async def test_empty_node_name_never_reaches_the_api_server():
    """An empty target is definitionally missing — do not spend a mutation
    request discovering that, and do not let a caller fall back on ""."""
    calls = []

    async def fake_xport(cmd, *_a, **_kw):
        calls.append(cmd)
        return _result()

    with patch(_XPORT, new=AsyncMock(side_effect=fake_xport)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(discovered="img-a,img-b"):
        carrier, reason = await create_and_wait_debug_pod_with_reason(
            "", "/kc", "t", namespace="default",
        )
    assert carrier is None
    assert reason == CARRIER_TARGET_MISSING
    assert calls == []


@pytest.mark.asyncio
async def test_carrier_only_wrapper_contract_is_unchanged():
    """The public wrapper still returns a bare carrier tuple, so every
    consumer that does not need the reason keeps its old shape."""
    rejected = _result(
        exit_code=1,
        stderr='Error from server (NotFound): nodes "n1" not found',
    )
    with patch(_XPORT, new=AsyncMock(return_value=rejected)), \
            patch(_SLEEP, new=AsyncMock()), \
            _cfg(discovered="img-a"):
        carrier = await create_and_wait_debug_pod(
            "n1", "/kc", "t", namespace="default")
    assert carrier is None


# ── W-67-1(b): a fallback may not re-ask what the API just answered ────────


@pytest.mark.parametrize("reason,fallback_expected", [
    (CARRIER_TARGET_MISSING, False),
    (CARRIER_REQUEST_REJECTED, True),
    (CARRIER_IMAGE_EXHAUSTED, True),
    (CARRIER_PARSE_FAILURE, True),
    (CARRIER_RETRYABLE, True),
])
@pytest.mark.asyncio
async def test_tool_pod_fallback_runs_unless_the_node_itself_is_missing(
    reason, fallback_expected,
):
    """The tool-pod fallback filters candidates by ``spec.nodeName ==
    node_name``. When the node does not exist that filter can never match, so
    the fallback is provably futile — and worse, its ABSENT answer would dress
    up one unresolved name as two independent confirmations. Every other
    reason leaves the node real, so the fallback stays available."""
    from chaos_agent.agent.nodes.baseline import _executors

    commands = [{
        "description": "host iostat",
        "command": "kubectl exec {debug_pod} -- iostat -xd 1",
        "mode": "debug_two_step",
        "_node_name": "ghost-node",
        # Skip the per-command loop: this pin is about the carrier decision.
        "_unresolved": True,
        "_extractors": [],
    }]

    create = AsyncMock(return_value=(None, reason))
    discover = AsyncMock(return_value=None)
    with patch.object(_executors, "_create_and_wait_debug_pod_with_reason",
                      new=create), \
            patch.object(_executors, "discover_tool_pod_on_node",
                         new=discover):
        await _executors._execute_observations(commands, "/kc", "t-fb")

    create.assert_awaited_once_with("ghost-node", "/kc", "t-fb")
    assert discover.await_count == (1 if fallback_expected else 0)
