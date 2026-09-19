"""Task B: experiment-id provenance lives in the provider layer.

The tool screener's destroy gate consults a carrier-neutral registry aggregate
(``FaultProviderRegistry.created_experiment_ids``); each backend scans its OWN
create results and claims its OWN durable record. These tests pin the
per-backend evidence semantics and the aggregate union.
"""

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider
from chaos_agent.agent.providers.chaosblade.python_provider import (
    ChaosbladePythonProvider,
)
from chaos_agent.agent.providers.host_shell.provider import HostShellProvider
from chaos_agent.agent.providers.k8s_native.provider import K8sNativeProvider

OWN_UID = "a1b2c3d4e5f60718"
# Round-19 N3: the failed-create ``UID:`` wording anchor now composes the
# single-source hex16 shape — a dashed placeholder would be (correctly)
# refused as K8s-object vocabulary, so the fixture carries the lowercase
# hex16 the anchor's producer (cli.py) actually re-wraps.
FAILED_UID = "deadbeef00000010"
PY_UID = "f00dface12345678"


def _create_result(uid: str) -> ToolMessage:
    return ToolMessage(
        content=f'{{"code": 200, "success": true, "result": "{uid}"}}',
        name="blade_create",
        tool_call_id="tc-create",
    )


def _failed_create_result(uid: str) -> ToolMessage:
    return ToolMessage(
        content=f'{{"code": 500, "success": false, "error": "UID: {uid} CRD stuck"}}',
        name="blade_create",
        tool_call_id="tc-create-fail",
    )


def _py_create_result(uid: str) -> ToolMessage:
    return ToolMessage(
        content=f'{{"code": 200, "success": true, "result": "{uid}"}}',
        name="blade_python_create",
        tool_call_id="tc-py-create",
    )


class TestChaosbladeProvenance:
    def test_collects_own_create_results_and_failed_crds(self):
        uids = ChaosbladeProvider().created_experiment_ids(
            [_create_result(OWN_UID), _failed_create_result(FAILED_UID)], {}
        )
        assert uids == {OWN_UID, FAILED_UID}

    def test_claims_durable_record_when_attribution_is_its_own(self):
        uids = ChaosbladeProvider().created_experiment_ids(
            [], {"experiment_uid": f" {OWN_UID} "}
        )
        assert uids == {OWN_UID}

    def test_durable_record_not_claimed_for_python_agent(self):
        # python_agent owns the durable experiment_uid via its own provider.
        uids = ChaosbladeProvider().created_experiment_ids(
            [], {"experiment_uid": OWN_UID, "injection_method": "python_agent"}
        )
        assert uids == set()

    def test_ignores_other_tools_results(self):
        msg = ToolMessage(content="irrelevant", name="kubectl", tool_call_id="t")
        assert ChaosbladeProvider().created_experiment_ids([msg], {}) == set()


class TestPythonAgentProvenance:
    def test_collects_own_tool_results(self):
        uids = ChaosbladePythonProvider().created_experiment_ids(
            [_py_create_result(PY_UID)], {}
        )
        assert uids == {PY_UID}

    def test_ignores_os_carrier_create_results(self):
        # blade_create evidence belongs to the ChaosBlade provider.
        assert ChaosbladePythonProvider().created_experiment_ids(
            [_create_result(OWN_UID)], {}
        ) == set()

    def test_claims_durable_record_only_for_python_agent(self):
        provider = ChaosbladePythonProvider()
        assert provider.created_experiment_ids(
            [], {"experiment_uid": PY_UID, "injection_method": "python_agent"}
        ) == {PY_UID}
        assert provider.created_experiment_ids(
            [], {"experiment_uid": PY_UID}
        ) == set()


class TestUidLessProvenance:
    def test_native_carriers_prove_nothing(self):
        assert K8sNativeProvider().created_experiment_ids(
            [_create_result(OWN_UID)], {"experiment_uid": OWN_UID}
        ) == set()
        assert HostShellProvider().created_experiment_ids(
            [_create_result(OWN_UID)], {"experiment_uid": OWN_UID}
        ) == set()


class TestRegistryProvenanceAggregate:
    def test_unions_across_backends(self):
        uids = FaultProviderRegistry.created_experiment_ids(
            [_create_result(OWN_UID), _py_create_result(PY_UID)],
            {"injection_method": "python_agent", "experiment_uid": PY_UID},
        )
        assert {OWN_UID, PY_UID} <= uids

    def test_screener_facade_matches_registry(self):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _experiment_uids_created_by_current_task,
        )

        msgs = [_create_result(OWN_UID), _failed_create_result(FAILED_UID)]
        state = {"experiment_uid": OWN_UID}
        assert _experiment_uids_created_by_current_task(msgs, state) == (
            FaultProviderRegistry.created_experiment_ids(msgs, state)
        )

    def test_none_state_is_tolerated(self):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _experiment_uids_created_by_current_task,
        )

        assert _experiment_uids_created_by_current_task([_create_result(OWN_UID)]) == {
            OWN_UID
        }


def _inline_create_pair(
    uid: str, v_args: str = None, subcommand: str = "exec"
) -> list:
    """AIMessage kubectl-exec blade-create tool_call + its paired receipt."""
    if v_args is None:
        v_args = (
            "toolpod -n chaosblade -- blade create k8s pod-cpu fullload "
            "--names nginx-1 --timeout 300"
        )
    return [
        AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": subcommand, "v_args": v_args},
            "id": "tc-inline",
            "type": "tool_call",
        }]),
        ToolMessage(
            content=f'{{"code": 200, "success": true, "result": "{uid}"}}',
            name="kubectl",
            tool_call_id="tc-inline",
        ),
    ]


class TestInlineCreateProvenance:
    """Round-14 G1: birth-ledger channel parity for inline create receipts.

    The provenance scan's message side used to see ONLY host ``blade_create``
    ToolMessages — an inline-delivered experiment was invisible to the destroy
    whitelist. The main chain hid this behind the durable birth registry, but
    the hydration fallback (legacy checkpoints / DB-only recovery) has no such
    cover: the LLM's own destroy of its own inline experiment was refused with
    the receipt sitting in the visible history.
    """

    def test_inline_create_receipt_hydrates_whitelist(self):
        uids = ChaosbladeProvider().created_experiment_ids(
            _inline_create_pair(OWN_UID), {}
        )
        assert uids == {OWN_UID}

    def test_inline_destroy_passes_gate_in_hydration_scene(self):
        # The G1b end-to-end: no durable record at all, receipt in visible
        # history — the provenance gate must ALLOW the task's own cleanup.
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _screen_destroy_uid_provenance,
        )
        from chaos_agent.agent.providers.chaosblade.provider import (
            classify_inline_blade,
        )

        msgs = _inline_create_pair(OWN_UID)
        et = classify_inline_blade(
            ["blade", "destroy", OWN_UID], "x",
            fallback_ns="chaosblade", fallback_pod="toolpod",
        )
        decision = _screen_destroy_uid_provenance(
            et.blade_destroy_uid, et, msgs, {},
        )
        assert str(decision.verdict).endswith("ALLOW")

    def test_unpaired_receipt_counts_nothing(self):
        # A kubectl ToolMessage whose owning call cannot be resolved must
        # not license provenance (fail-closed — mirrors the attribution
        # scan's discipline).
        msgs = _inline_create_pair(OWN_UID)[1:]  # receipt without the call
        assert ChaosbladeProvider().created_experiment_ids(msgs, {}) == set()

    def test_get_json_output_not_a_create_receipt(self):
        # task-51193464 shape: a ``get -o json`` output embeds metadata.uid
        # shaped like an experiment UID — paired-call gate + vocabulary keep
        # it out of the birth registry.
        msgs = _inline_create_pair(
            OWN_UID, v_args="nginx-1 -n demo -o json",
        )
        assert ChaosbladeProvider().created_experiment_ids(msgs, {}) == set()

    def test_non_exec_subcommand_not_a_create_delivery(self):
        # The paired-call gate keys on subcommand='exec' (plus the blade+create
        # vocabulary); a describe call embedding blade-create WORDS is not a
        # delivery — regardless of what its v_args text mentions.
        msgs = _inline_create_pair(
            OWN_UID,
            v_args=(
                "describe pod nginx-1 -n demo -- blade create k8s "
                "pod-cpu fullload"
            ),
            subcommand="describe",
        )
        assert ChaosbladeProvider().created_experiment_ids(msgs, {}) == set()

    def test_failed_inline_create_still_owed_cleanup(self):
        # Terminal create failures: the CRD may exist — the UID joins the
        # whitelist exactly like the host face's failed-create treatment.
        # Round-16 dialect correction: the exec channel sees the blade
        # CLI's RAW failure JSON (top-level ``"uid": "<hex16>"`` key —
        # the shape cli.py itself mines for the host face), NOT the host
        # face's ``UID: ...`` wrapper wording the r14 anchor pinned here.
        inline_failed_uid = "f00dcafe12345678"
        pair = _inline_create_pair(inline_failed_uid)
        pair[1] = ToolMessage(
            content=(
                f'{{"code": 500, "success": false, '
                f'"error": "create experiment failed: rpc error: timeout", '
                f'"uid": "{inline_failed_uid}"}}'
            ),
            name="kubectl",
            tool_call_id="tc-inline",
        )
        uids = ChaosbladeProvider().created_experiment_ids(pair, {})
        assert inline_failed_uid in uids

    def test_python_agent_does_not_claim_inline_receipts(self):
        # Inline blade delivery is the OS carrier's domain; the python-agent
        # provider stays out of it (channel vocabulary belongs to blade).
        assert ChaosbladePythonProvider().created_experiment_ids(
            _inline_create_pair(PY_UID), {}
        ) == set()

    def test_registry_aggregate_includes_inline_face(self):
        assert FaultProviderRegistry.created_experiment_ids(
            _inline_create_pair(OWN_UID), {}
        ) == {OWN_UID}
