"""Unit tests for the shared ``target_identity`` seam (W-83-5 root-fix).

The seam is the single authority for "fault target → the literal cluster object
a pod-level probe must address". These tests pin the invariant that killed
W-83-5: a workload name is NEVER substituted for a pod name — an unresolvable
target fails open to ``None`` (a visible absence), and only ``pod`` scope treats
``names[0]`` as literal.

The transport is injected via the ``executor=`` seam parameter (a fake), which
also exercises the dependency-injection mechanism baseline_capture relies on to
keep its own module-level patch target.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.target_identity import (
    POD_OWNER_SELECTOR_JSONPATH,
    discover_pod_selector,
    resolve_node_by_selector,
    resolve_node_of_pod,
    resolve_pod_by_selector,
    resolve_pods_by_selector,
    resolve_target_pod_name,
)


class _Result:
    """Minimal CommandResult stand-in (exit_code / stdout / stderr)."""

    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _fake_executor(*, selector_out=None, pods_out=None, discover_exit=0,
                   pods_exit=0, node_out=None, node_exit=0, raise_exc=False):
    """Build an injected executor that records commands and answers by shape.

    * ``get <kind> <name> -o jsonpath=<selector>`` → ``selector_out``
    * ``get pods -l <sel> -o jsonpath=...metadata.name`` → ``pods_out``
    * ``get pod <n> -o jsonpath={.spec.nodeName}`` → ``node_out``
    * ``get pods -l <sel> -o jsonpath={.items[0].spec.nodeName}`` → ``node_out``
    """
    calls: list[list[str]] = []

    async def _exec(cmd, target, timeout=0, task_id="", expect_profile=None,
                    **kwargs):
        if raise_exc:
            raise RuntimeError("boom")
        tokens = [str(t) for t in cmd]
        calls.append(tokens)
        joined = " ".join(tokens)
        # Node-axis reads are matched on their jsonpath BEFORE the generic
        # ``pods -l`` pod-name branch (a node-by-selector query is also a
        # ``pods -l`` query, so order matters).
        if "jsonpath={.spec.nodeName}" in joined:
            return _Result(exit_code=node_exit, stdout=node_out or "")
        if "jsonpath={.items[0].spec.nodeName}" in joined:
            return _Result(exit_code=node_exit, stdout=node_out or "")
        is_selector_discovery = (
            "jsonpath=" in joined and "matchLabels" in joined
            or ("jsonpath={.spec.selector}" in joined)
        )
        is_pods_query = "pods" in tokens and "-l" in tokens
        if is_pods_query:
            return _Result(exit_code=pods_exit, stdout=pods_out or "")
        if is_selector_discovery:
            return _Result(exit_code=discover_exit, stdout=selector_out or "")
        return _Result(exit_code=0, stdout="")

    _exec.calls = calls  # type: ignore[attr-defined]
    return _exec


# ---------------------------------------------------------------------------
# resolve_target_pod_name — the orchestrator
# ---------------------------------------------------------------------------


class TestResolveTargetPodName:
    @pytest.mark.asyncio
    async def test_pod_scope_returns_names_zero_without_query(self):
        """pod scope: names[0] IS the literal pod — zero transport calls."""
        ex = _fake_executor()
        pod = await resolve_target_pod_name(
            scope="pod", names=["my-pod-abc"], namespace="default",
            kubeconfig="", executor=ex,
        )
        assert pod == "my-pod-abc"
        assert ex.calls == []  # no query for a literal pod name

    @pytest.mark.asyncio
    async def test_workload_scope_with_explicit_selector_queries_once(self):
        """deployment + pre-discovered selector: ONE get-pods query, resolves."""
        ex = _fake_executor(pods_out="my-deploy-7d9-xyz\n")
        pod = await resolve_target_pod_name(
            scope="deployment", names=["my-deploy"], namespace="default",
            kubeconfig="", selector={"app": "my-app"}, executor=ex,
        )
        assert pod == "my-deploy-7d9-xyz"
        assert len(ex.calls) == 1
        joined = " ".join(ex.calls[0])
        assert "-l" in joined and "app=my-app" in joined
        assert "jsonpath={.items[0].metadata.name}" in joined

    @pytest.mark.asyncio
    async def test_workload_scope_uses_labels_when_no_selector(self):
        ex = _fake_executor(pods_out="from-labels-pod")
        pod = await resolve_target_pod_name(
            scope="statefulset", names=["sts"], namespace="ns",
            kubeconfig="", labels={"app": "from-labels"}, executor=ex,
        )
        assert pod == "from-labels-pod"
        assert "app=from-labels" in " ".join(ex.calls[0])

    @pytest.mark.asyncio
    async def test_discover_selector_true_resolves_self_contained(self):
        """W-83-5 core: a workload target with NO selector and NO labels (the
        side-effect node's situation — spec.labels is structurally empty under
        workload scope) resolves via selector DISCOVERY then a get-pods query."""
        ex = _fake_executor(
            selector_out='{"app": "drill-http-victim"}',
            pods_out="drill-http-victim-6b7-abcde",
        )
        pod = await resolve_target_pod_name(
            scope="deployment", names=["drill-http-victim"], namespace="default",
            kubeconfig="", discover_selector=True, executor=ex,
        )
        assert pod == "drill-http-victim-6b7-abcde"
        # two queries: discover selector (get deployment ... matchLabels), then
        # resolve pod (get pods -l app=...).
        assert len(ex.calls) == 2
        assert "matchLabels" in " ".join(ex.calls[0])
        assert "app=drill-http-victim" in " ".join(ex.calls[1])

    @pytest.mark.asyncio
    async def test_discover_selector_false_without_selector_returns_none(self):
        """baseline's contract (discover_selector=False): no selector/labels →
        None with ZERO queries — never a self-discovery, never a name guess."""
        ex = _fake_executor()
        pod = await resolve_target_pod_name(
            scope="deployment", names=["d"], namespace="ns", kubeconfig="",
            discover_selector=False, executor=ex,
        )
        assert pod is None
        assert ex.calls == []

    @pytest.mark.asyncio
    async def test_unresolvable_workload_never_returns_workload_name(self):
        """THE invariant: discovery yields a selector but no pod matches (empty
        get-pods output) → None. NEVER 'drill-http-victim' (the workload name)."""
        ex = _fake_executor(selector_out='{"app": "x"}', pods_out="")
        pod = await resolve_target_pod_name(
            scope="deployment", names=["drill-http-victim"], namespace="default",
            kubeconfig="", discover_selector=True, executor=ex,
        )
        assert pod is None

    @pytest.mark.asyncio
    async def test_discovery_finds_no_selector_returns_none(self):
        """Discovery returns empty (selectorless) → None, no get-pods issued."""
        ex = _fake_executor(selector_out="")
        pod = await resolve_target_pod_name(
            scope="deployment", names=["d"], namespace="ns", kubeconfig="",
            discover_selector=True, executor=ex,
        )
        assert pod is None
        assert len(ex.calls) == 1  # only the discovery query ran

    @pytest.mark.asyncio
    async def test_node_scope_returns_none(self):
        ex = _fake_executor()
        pod = await resolve_target_pod_name(
            scope="node", names=["node-1"], namespace="", kubeconfig="",
            discover_selector=True, executor=ex,
        )
        assert pod is None
        assert ex.calls == []

    @pytest.mark.asyncio
    async def test_non_k8s_profile_returns_none(self):
        ex = _fake_executor()
        pod = await resolve_target_pod_name(
            scope="pod", names=["p"], namespace="", kubeconfig="",
            profile="host", executor=ex,
        )
        assert pod is None

    @pytest.mark.asyncio
    async def test_query_exception_failopen_to_none(self):
        ex = _fake_executor(raise_exc=True)
        pod = await resolve_target_pod_name(
            scope="deployment", names=["d"], namespace="ns", kubeconfig="",
            selector={"a": "b"}, executor=ex,
        )
        assert pod is None


# ---------------------------------------------------------------------------
# resolve_pod_by_selector — the selector→pod primitive
# ---------------------------------------------------------------------------


class TestResolvePodBySelector:
    @pytest.mark.asyncio
    async def test_empty_selector_returns_none_without_query(self):
        ex = _fake_executor()
        assert await resolve_pod_by_selector(
            selector=None, namespace="ns", kubeconfig="", executor=ex,
        ) is None
        assert await resolve_pod_by_selector(
            selector={}, namespace="ns", kubeconfig="", executor=ex,
        ) is None
        assert ex.calls == []

    @pytest.mark.asyncio
    async def test_nonzero_exit_failopen_to_none(self):
        ex = _fake_executor(pods_out="ignored", pods_exit=1)
        assert await resolve_pod_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="", executor=ex,
        ) is None


# ---------------------------------------------------------------------------
# resolve_pods_by_selector — the SET twin (selector → every matching pod name)
# ---------------------------------------------------------------------------


class TestResolvePodsBySelector:
    """The victim-SET primitive both the planning ``tool_screener`` selector
    cross-shape probe and ``target_health._resolve_pod_names`` (the R69 site)
    delegate to. Pins: it returns EVERY name, fails open to ``()`` (never a
    workload name), and — the R69 invariant — NEVER launders a failed query's
    ``Error from server …`` text into pod-name tokens."""

    @pytest.mark.asyncio
    async def test_returns_every_name_via_items_star_jsonpath(self):
        ex = _fake_executor(pods_out="acct-6fb-qn2vr acct-6fb-xqhdd acct-6fb-zf458\n")
        names = await resolve_pods_by_selector(
            selector={"app": "accounting"}, namespace="cms-demo",
            kubeconfig="", executor=ex,
        )
        assert names == ("acct-6fb-qn2vr", "acct-6fb-xqhdd", "acct-6fb-zf458")
        assert len(ex.calls) == 1
        joined = " ".join(ex.calls[0])
        assert "-l" in joined and "app=accounting" in joined
        assert "-n cms-demo" in joined
        # The SET jsonpath — the exact shape the two consumers hand-rolled.
        assert "jsonpath={.items[*].metadata.name}" in joined

    @pytest.mark.asyncio
    async def test_empty_selector_returns_empty_without_query(self):
        ex = _fake_executor(pods_out="ignored")
        assert await resolve_pods_by_selector(
            selector=None, namespace="ns", kubeconfig="", executor=ex,
        ) == ()
        assert await resolve_pods_by_selector(
            selector={}, namespace="ns", kubeconfig="", executor=ex,
        ) == ()
        assert ex.calls == []

    @pytest.mark.asyncio
    async def test_no_match_empty_output_failopen_to_empty(self):
        ex = _fake_executor(pods_out="")
        assert await resolve_pods_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="", executor=ex,
        ) == ()

    @pytest.mark.asyncio
    async def test_nonzero_exit_failopen_to_empty(self):
        ex = _fake_executor(pods_out="ignored", pods_exit=1)
        assert await resolve_pods_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="", executor=ex,
        ) == ()

    @pytest.mark.asyncio
    async def test_exception_failopen_to_empty(self):
        ex = _fake_executor(raise_exc=True)
        assert await resolve_pods_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="", executor=ex,
        ) == ()

    @pytest.mark.asyncio
    async def test_r69_error_payload_never_becomes_pod_names(self):
        """THE R69 invariant on the SET axis: kubectl's real failure shape is a
        non-zero exit with ``Error from server …`` on the payload. Gating on
        ``exit_code`` means the split never runs — so ``()`` rather than
        ``("Error", "from", "server", …)``."""
        ex = _fake_executor(
            pods_out="Error from server (NotFound): pods not found", pods_exit=1,
        )
        names = await resolve_pods_by_selector(
            selector={"app": "gone"}, namespace="ns", kubeconfig="", executor=ex,
        )
        assert names == ()
        assert not any(tok.startswith("Error") for tok in names)

    @pytest.mark.asyncio
    async def test_field_selector_threaded_into_query(self):
        ex = _fake_executor(pods_out="running-pod")
        await resolve_pods_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="",
            field_selector="status.phase=Running", executor=ex,
        )
        assert "--field-selector status.phase=Running" in " ".join(ex.calls[0])

    @pytest.mark.asyncio
    async def test_source_forwarded_to_transport(self):
        """The screener attributes its probe via ``source``; the seam must
        forward it verbatim so transport-side accounting is unchanged."""
        seen: dict = {}

        async def _exec(cmd, target, timeout=0, task_id="", expect_profile=None,
                        **kwargs):
            seen.update(kwargs)
            return _Result(exit_code=0, stdout="pod-a")

        names = await resolve_pods_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="",
            source="selector-name-probe", executor=_exec,
        )
        assert names == ("pod-a",)
        assert seen.get("source") == "selector-name-probe"


# ---------------------------------------------------------------------------
# discover_pod_selector — the kind→selector primitive
# ---------------------------------------------------------------------------


class TestDiscoverPodSelector:
    @pytest.mark.asyncio
    async def test_deployment_selector_parsed(self):
        ex = _fake_executor(selector_out='{"app": "drill", "tier": "web"}')
        sel = await discover_pod_selector(
            scope="deployment", name="drill", namespace="ns", kubeconfig="",
            executor=ex,
        )
        assert sel == {"app": "drill", "tier": "web"}
        assert POD_OWNER_SELECTOR_JSONPATH["deployment"] in " ".join(ex.calls[0])

    @pytest.mark.asyncio
    async def test_service_uses_bare_selector_jsonpath(self):
        ex = _fake_executor(selector_out='{"app": "svc"}')
        sel = await discover_pod_selector(
            scope="service", name="svc", namespace="ns", kubeconfig="",
            executor=ex,
        )
        assert sel == {"app": "svc"}
        assert "jsonpath={.spec.selector}" in " ".join(ex.calls[0])

    @pytest.mark.asyncio
    async def test_non_workload_scope_returns_none(self):
        ex = _fake_executor()
        assert await discover_pod_selector(
            scope="pod", name="p", namespace="ns", kubeconfig="", executor=ex,
        ) is None
        assert ex.calls == []

    @pytest.mark.asyncio
    async def test_bad_json_failopen_to_none(self):
        ex = _fake_executor(selector_out="not-json")
        assert await discover_pod_selector(
            scope="deployment", name="d", namespace="ns", kubeconfig="",
            executor=ex,
        ) is None

    @pytest.mark.asyncio
    async def test_empty_values_dropped_and_all_empty_returns_none(self):
        ex = _fake_executor(selector_out='{"app": "", "x": null}')
        assert await discover_pod_selector(
            scope="deployment", name="d", namespace="ns", kubeconfig="",
            executor=ex,
        ) is None


# ---------------------------------------------------------------------------
# resolve_node_of_pod — the pod→node primitive (node axis, #83 audit)
# ---------------------------------------------------------------------------


class TestResolveNodeOfPod:
    @pytest.mark.asyncio
    async def test_resolves_node_via_spec_nodename(self):
        ex = _fake_executor(node_out="cn-shanghai.10.0.0.5\n")
        node = await resolve_node_of_pod(
            pod_name="victim-0", namespace="default", kubeconfig="", executor=ex,
        )
        assert node == "cn-shanghai.10.0.0.5"
        assert len(ex.calls) == 1
        joined = " ".join(ex.calls[0])
        assert "jsonpath={.spec.nodeName}" in joined
        assert "victim-0" in joined and "-n default" in joined

    @pytest.mark.asyncio
    async def test_empty_pod_name_returns_none_without_query(self):
        ex = _fake_executor(node_out="ignored")
        assert await resolve_node_of_pod(
            pod_name="", namespace="ns", kubeconfig="", executor=ex,
        ) is None
        assert ex.calls == []

    @pytest.mark.asyncio
    async def test_empty_output_failopen_to_none(self):
        """An unscheduled/terminating pod has no .spec.nodeName → empty stdout
        → None, never the pod name as a node substitute."""
        ex = _fake_executor(node_out="")
        assert await resolve_node_of_pod(
            pod_name="pending-pod", namespace="ns", kubeconfig="", executor=ex,
        ) is None

    @pytest.mark.asyncio
    async def test_nonzero_exit_failopen_to_none(self):
        ex = _fake_executor(node_out="ignored", node_exit=1)
        assert await resolve_node_of_pod(
            pod_name="gone", namespace="ns", kubeconfig="", executor=ex,
        ) is None

    @pytest.mark.asyncio
    async def test_exception_failopen_to_none(self):
        ex = _fake_executor(raise_exc=True)
        assert await resolve_node_of_pod(
            pod_name="p", namespace="ns", kubeconfig="", executor=ex,
        ) is None

    @pytest.mark.asyncio
    async def test_namespace_omitted_when_empty(self):
        ex = _fake_executor(node_out="node-a")
        await resolve_node_of_pod(
            pod_name="p", namespace="", kubeconfig="", executor=ex,
        )
        assert "-n" not in ex.calls[0]


# ---------------------------------------------------------------------------
# resolve_node_by_selector — the selector→items[0]'s-node primitive
# ---------------------------------------------------------------------------


class TestResolveNodeBySelector:
    @pytest.mark.asyncio
    async def test_resolves_first_pod_node(self):
        ex = _fake_executor(node_out="node-b\n")
        node = await resolve_node_by_selector(
            selector={"app": "victim"}, namespace="ns", kubeconfig="", executor=ex,
        )
        assert node == "node-b"
        joined = " ".join(ex.calls[0])
        assert "jsonpath={.items[0].spec.nodeName}" in joined
        assert "-l" in joined and "app=victim" in joined

    @pytest.mark.asyncio
    async def test_empty_selector_returns_none_without_query(self):
        ex = _fake_executor(node_out="ignored")
        assert await resolve_node_by_selector(
            selector=None, namespace="ns", kubeconfig="", executor=ex,
        ) is None
        assert await resolve_node_by_selector(
            selector={}, namespace="ns", kubeconfig="", executor=ex,
        ) is None
        assert ex.calls == []

    @pytest.mark.asyncio
    async def test_empty_output_failopen_to_none(self):
        ex = _fake_executor(node_out="")
        assert await resolve_node_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="", executor=ex,
        ) is None


# ---------------------------------------------------------------------------
# field_selector — the optional Running-phase filter (feasibility lane)
# ---------------------------------------------------------------------------


class TestFieldSelector:
    @pytest.mark.asyncio
    async def test_field_selector_added_to_pods_query(self):
        ex = _fake_executor(pods_out="running-pod")
        pod = await resolve_pod_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="",
            field_selector="status.phase=Running", executor=ex,
        )
        assert pod == "running-pod"
        joined = " ".join(ex.calls[0])
        assert "--field-selector status.phase=Running" in joined

    @pytest.mark.asyncio
    async def test_no_field_selector_by_default(self):
        """baseline / side-effect address the frozen target regardless of phase:
        the default (None) leaves the query byte-identical to before."""
        ex = _fake_executor(pods_out="any-pod")
        await resolve_pod_by_selector(
            selector={"app": "x"}, namespace="ns", kubeconfig="", executor=ex,
        )
        assert "--field-selector" not in " ".join(ex.calls[0])

    @pytest.mark.asyncio
    async def test_field_selector_threaded_through_orchestrator(self):
        ex = _fake_executor(pods_out="running-pod")
        await resolve_target_pod_name(
            scope="deployment", names=["d"], namespace="ns", kubeconfig="",
            selector={"app": "x"}, field_selector="status.phase=Running",
            executor=ex,
        )
        assert "--field-selector status.phase=Running" in " ".join(ex.calls[0])
