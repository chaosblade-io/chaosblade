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
        from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig

        state = {
            "kubeconfig": "/path/from/state",
            "params": {"kubeconfig": "/path/from/params"},
        }
        assert _resolve_kubeconfig(state) == "/path/from/state"

    def test_params_kubeconfig_second_priority(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig

        state = {
            "kubeconfig": "",
            "params": {"kubeconfig": "/path/from/params"},
        }
        assert _resolve_kubeconfig(state) == "/path/from/params"

    def test_settings_fallback(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig

        state = {"kubeconfig": "", "params": {}}
        # Should fall back to settings.kubeconfig_path (may be empty in test env)
        result = _resolve_kubeconfig(state)
        assert isinstance(result, str)

    def test_empty_state(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig

        state = {}
        result = _resolve_kubeconfig(state)
        assert isinstance(result, str)


class TestInjectKubeconfigIntoToolCalls:
    """Tests for inject_kubeconfig_into_tool_calls."""

    def test_inject_into_kubectl_missing_kubeconfig(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "top", "v_args": "pod -n cms-demo"},
            "id": "call-1",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_inject_into_blade_missing_kubeconfig(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "blade_status",
            "args": {"uid": "abc123"},
            "id": "call-2",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_do_not_override_existing_kubeconfig(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

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
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

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
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

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
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([])
        # Should not raise any error
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")

    def test_skip_when_tool_calls_none(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = AIMessage(content="final text", id="test-id")
        # tool_calls is None for a text-only response
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")

    def test_mixed_tool_calls_only_inject_kubectl_blade(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

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
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "describe", "v_args": "pod my-pod -n default"},
            "id": "call-7",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_inject_into_kubectl_get(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "get", "v_args": "pods -n cms-demo checkout-pod -o json"},
            "id": "call-8",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_inject_into_kubectl_exec(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

        msg = _make_ai_message([{
            "name": "kubectl",
            "args": {"subcommand": "exec", "v_args": "my-pod -n default -- top -bn1"},
            "id": "call-9",
            "type": "tool_call",
        }])
        inject_kubeconfig_into_tool_calls(msg, "/path/to/kubeconfig")
        assert msg.tool_calls[0]["args"]["kubeconfig"] == "/path/to/kubeconfig"

    def test_multiple_kubectl_calls_all_injected(self):
        from chaos_agent.agent.nodes._kubeconfig_inject import inject_kubeconfig_into_tool_calls

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
