"""Tests for _kubeconfig_inject shared utility module."""

from langchain_core.messages import AIMessage


def _make_ai_message(tool_calls: list[dict]) -> AIMessage:
    """Helper: create an AIMessage with the given tool_calls."""
    return AIMessage(
        content="",
        id="test-msg-id",
        tool_calls=tool_calls,
    )


class TestResolveKubeconfig:
    """Tests for _resolve_kubeconfig."""

    def test_state_kubeconfig_first_priority(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig

        state = {
            "kubeconfig": "/path/from/state",
            "params": {"kubeconfig": "/path/from/params"},
        }
        assert _resolve_kubeconfig(state) == "/path/from/state"

    def test_params_kubeconfig_second_priority(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        # Now params live on FaultSpec, not state.params — the fallback
        # path checks spec.params.kubeconfig.
        spec = FaultSpec(params={"kubeconfig": "/path/from/params"})
        state = {
            "kubeconfig": "",
            "fault_spec": spec.to_dict(),
        }
        assert _resolve_kubeconfig(state) == "/path/from/params"

    def test_settings_fallback(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig

        state = {"kubeconfig": "", "params": {}}
        # Should fall back to settings.kubeconfig_path (may be empty in test env)
        result = _resolve_kubeconfig(state)
        assert isinstance(result, str)

    def test_empty_state(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig

        state = {}
        result = _resolve_kubeconfig(state)
        assert isinstance(result, str)


class TestInjectKubeconfigIntoToolCalls:
    """Tests for inject_kubeconfig_into_tool_calls."""

    def test_inject_into_kubectl_missing_kubeconfig(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "top", "v_args": "pod -n cms-demo"},
            "id": "call-1",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_inject_into_blade_missing_kubeconfig(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "blade_status",
            "args": {"uid": "abc123"},
            "id": "call-2",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_do_not_override_existing_kubeconfig(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "get", "v_args": "pods", "kubeconfig": "/other/path"},
            "id": "call-3",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        # Should NOT override the explicit value
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/other/path"

    def test_skip_non_kubectl_blade_tools(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "web_search",
            "args": {"query": "kubectl top pod"},
            "id": "call-4",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        # web_search should NOT have kubeconfig injected
        assert "kubeconfig" not in msg.tool_calls[0]["args"]

    def test_skip_when_kubeconfig_empty(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "top", "v_args": "pod"},
            "id": "call-5",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "")
        # Should not inject empty kubeconfig
        assert "kubeconfig" not in msg.tool_calls[0]["args"]

    def test_skip_when_no_tool_calls(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([])
        # Should not raise any error
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")

    def test_skip_when_tool_calls_none(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = AIMessage(content="final text", id="test-id")
        # tool_calls is None for a text-only response
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")

    def test_mixed_tool_calls_only_inject_kubectl_blade(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([
            {
                "name": "kubectl",
                "args": {"subcommand": "get", "v_args": "pods -n ns"},
                "id": "call-6a",
                "type": "tool_call",
            },
            {
                "name": "web_search",
                "args": {"query": "cpu stress"},
                "id": "call-6b",
                "type": "tool_call",
            },
            {
                "name": "blade_status",
                "args": {"uid": "uid123"},
                "id": "call-6c",
                "type": "tool_call",
            },
        ])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"
        assert "kubeconfig" not in msg.tool_calls[1]["args"]
        assert msg.tool_calls[2]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_inject_into_kubectl_describe(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "describe", "v_args": "pod my-pod -n default"},
            "id": "call-7",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_inject_into_kubectl_get(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "get", "v_args": "pods -n cms-demo checkout-pod -o json"},
            "id": "call-8",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_inject_into_kubectl_exec(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "exec", "v_args": "my-pod -n default -- top -bn1"},
            "id": "call-9",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_multiple_kubectl_calls_all_injected(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([
            {
                "name": "kubectl",
                "args": {"subcommand": "top", "v_args": "pod -n ns"},
                "id": "call-10a",
                "type": "tool_call",
            },
            {
                "name": "kubectl",
                "args": {"subcommand": "get", "v_args": "pod -n ns -o json"},
                "id": "call-10b",
                "type": "tool_call",
            },
        ])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"
        assert msg.tool_calls[1]["args"]["kubeconfig"] == "/path/to/kubeconfig"


class TestInjectTaskIdIntoToolCalls:
    def test_overrides_llm_supplied_task_id_for_execution_tool(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import (
            inject_task_id_into_tool_calls,
        )

        msg = _make_ai_message([{
            "name": "blade_create",
            "args": {"scope": "node", "task_id": "llm-plan-id"},
            "id": "call-task-1",
            "type": "tool_call",
        }])

        inject_task_id_into_tool_calls(msg, "task-from-state")

        assert msg.tool_calls[0]["args"]["task_id"] == "task-from-state"

    def test_does_not_add_task_id_to_tools_without_that_argument(self):
        from chaos_agent.agent.nodes.execute._kubeconfig_inject import (
            inject_task_id_into_tool_calls,
        )

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "get", "v_args": "nodes"},
            "id": "call-task-2",
            "type": "tool_call",
        }])

        inject_task_id_into_tool_calls(msg, "task-from-state")

        assert "task_id" not in msg.tool_calls[0]["args"]


class TestResolveTransportTargetSeam:
    """Tests for the ``resolve_transport_target`` seam (D5).

    The seam is the single authoritative entry point for obtaining a
    ``TransportTarget`` from *state*; its canonical home is
    ``chaos_agent.transports.registry`` (re-exported through
    ``_kubeconfig_inject`` for execute-side callers).  It must carry the same
    guard intent that ``sync_kubewiz_runtime`` used to enforce, plus the D1
    hydration contract:

    * A **stale** ``kubewiz_cluster_uuid`` / ``kubewiz_profile`` lingering in
      state MUST NOT hijack a session whose *config* resolves to a non-kubewiz
      channel — the seam drops those state params before field-based inference.
    * When config *does* resolve to a kubewiz channel, a state kubewiz override
      is honoured (per-session connection fact).
    * An unset ``ssh_port`` in state is ``None`` and falls back to the
      config-authoritative port — the exact regression (B3) that the old
      truthy ``22`` placeholder masked.
    """

    def test_stale_state_kubewiz_stripped_when_config_not_kubewiz(self, monkeypatch):
        """Non-kubewiz config + stale state kubewiz params → params dropped."""
        from chaos_agent.config.settings import settings as _settings
        from chaos_agent.transports import registry as reg

        monkeypatch.setattr(reg, "is_kubewiz_channel", lambda *a, **k: False)
        monkeypatch.setattr(_settings, "kubewiz_cluster_uuid", "")
        monkeypatch.setattr(_settings, "kubewiz_profile", "")

        state = {"kubewiz_cluster_uuid": "stale-uuid", "kubewiz_profile": "stale-profile"}
        target = reg.resolve_transport_target(state)

        # Stripped → falls back to the (empty) config value, no hijack.
        assert target.kubewiz_cluster_uuid == ""
        assert target.kubewiz_profile == ""

    def test_state_kubewiz_preserved_when_config_is_kubewiz(self, monkeypatch):
        """Kubewiz config → state kubewiz override survives the seam."""
        from chaos_agent.transports import registry as reg

        monkeypatch.setattr(reg, "is_kubewiz_channel", lambda *a, **k: True)

        state = {"kubewiz_cluster_uuid": "new-uuid", "kubewiz_profile": "new-profile"}
        target = reg.resolve_transport_target(state)

        assert target.kubewiz_cluster_uuid == "new-uuid"
        assert target.kubewiz_profile == "new-profile"

    def test_unset_state_port_falls_back_to_settings(self, monkeypatch):
        """B3: state ssh_port=None + settings 2222 → seam resolves 2222."""
        from chaos_agent.config.settings import settings as _settings
        from chaos_agent.transports import registry as reg

        monkeypatch.setattr(reg, "is_kubewiz_channel", lambda *a, **k: False)
        monkeypatch.setattr(_settings, "ssh_port", 2222)

        target = reg.resolve_transport_target({"ssh_port": None})

        assert target.ssh_port == 2222

    def test_explicit_state_port_overrides_settings(self, monkeypatch):
        """An explicit state port is a per-session override and wins."""
        from chaos_agent.config.settings import settings as _settings
        from chaos_agent.transports import registry as reg

        monkeypatch.setattr(reg, "is_kubewiz_channel", lambda *a, **k: False)
        monkeypatch.setattr(_settings, "ssh_port", 2222)

        target = reg.resolve_transport_target({"ssh_port": 2200})

        assert target.ssh_port == 2200

    def test_reexport_is_canonical_seam(self):
        """The execute-side re-export IS the transports-registry seam (one impl)."""
        from chaos_agent.agent.nodes.execute import _kubeconfig_inject as mod
        from chaos_agent.transports import registry as reg

        assert mod.resolve_transport_target is reg.resolve_transport_target

    def test_empty_state_does_not_crash(self):
        """Intent ③ ported from the retired ``TestSyncKubewizRuntime``: an
        empty (or ``None``) state resolves to the settings-authoritative
        target without raising — the seam's ``dict(state or {})`` guard."""
        from chaos_agent.transports import registry as reg

        # Neither shape may raise; both yield a TransportTarget.
        assert isinstance(reg.resolve_transport_target({}), reg.TransportTarget)
        assert isinstance(reg.resolve_transport_target(None), reg.TransportTarget)


# The six cluster tools whose signature declares ``kubeconfig``. This is the
# exact set the runtime injector targets (provider-union
# ``kubeconfig_scoped_tool_names``): kubectl/kubectl_read from the k8s-native
# provider plus the four chaosblade CLI tools.
def _kubeconfig_scoped_tools() -> dict:
    from chaos_agent.agent.providers.chaosblade.cli import (
        blade_create,
        blade_destroy,
        blade_query_k8s,
        blade_status,
    )
    from chaos_agent.tools.kubectl_cli import kubectl, kubectl_read

    return {
        "kubectl": kubectl,
        "kubectl_read": kubectl_read,
        "blade_create": blade_create,
        "blade_destroy": blade_destroy,
        "blade_status": blade_status,
        "blade_query_k8s": blade_query_k8s,
    }


class TestKubeconfigSchemaInvisibleToLLM:
    """kubeconfig is a runtime-owned connection fact, NOT an LLM planning
    parameter — the same treatment K7 gives --context/--cluster.

    Root cause this pins shut: kubeconfig used to be an ordinary tool arg
    exposed in the LLM-facing schema (and prompts told the model to retype it
    on every cluster call). The model hand-copied the path token-by-token and
    occasionally typo'd it (``jiangzelin`` -> ``jiangzerin``); the injector
    only fills an EMPTY kubeconfig and never corrects a wrong one, so the bad
    path reached kubectl and forced exit-1 detours.

    Marking the parameter ``InjectedToolArg`` hides it from
    ``convert_to_openai_tool`` — the model can no longer name it, so it can no
    longer typo it — while the runtime injector still supplies the authoritative
    value. These assertions pin that the schema stays closed (anti-regression):
    if anyone drops the ``InjectedToolArg`` annotation, the parameter reappears
    in the schema and this test flips red.
    """

    def test_kubeconfig_absent_from_every_llm_facing_schema(self):
        from langchain_core.utils.function_calling import convert_to_openai_tool

        for name, tool in _kubeconfig_scoped_tools().items():
            schema = convert_to_openai_tool(tool)
            props = schema["function"]["parameters"].get("properties", {})
            assert "kubeconfig" not in props, (
                f"{name} re-exposes kubeconfig to the LLM — the InjectedToolArg "
                "annotation was dropped, reopening the typo surface"
            )

    def test_provider_union_still_covers_every_scoped_tool(self):
        """Hiding the arg from the schema must NOT un-target the runtime
        injector: the provider union still names all six, so injection keeps
        supplying the authoritative kubeconfig at dispatch time."""
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        union = FaultProviderRegistry.union_tool_names("kubeconfig_scoped_tool_names")
        assert set(_kubeconfig_scoped_tools()) <= union
