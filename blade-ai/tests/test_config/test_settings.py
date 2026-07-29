"""Tests for pydantic-settings configuration."""

import pytest


class TestSettingsSshPortValidation:
    """ssh_port must be a valid TCP port at config-load time."""

    @pytest.mark.parametrize("port", [1, 22, 2222, 65535])
    def test_valid_ssh_port_accepted(self, port):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test", ssh_port=port)
        assert s.ssh_port == port

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999])
    def test_out_of_range_ssh_port_raises(self, port):
        from chaos_agent.config.settings import Settings

        with pytest.raises(ValueError):
            Settings(llm_api_key="test", ssh_port=port)


class TestSettingsDefaults:
    """Test default values for Settings."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch, tmp_path):
        # Point the config file at a nonexistent path so these default
        # assertions never read the developer's real ~/.blade-ai/config.json
        # (which may set e.g. confirmation_required=false and flip an
        # otherwise-default assertion). Mirrors the per-test isolation
        # TestSettingsPriority already applies.
        monkeypatch.setattr(
            "chaos_agent.config.settings._CONFIG_FILE",
            tmp_path / "nonexistent.json",
        )

    def test_default_model_name(self):
        from chaos_agent.config.settings import Settings

        # Explicitly pass model_name to avoid .env file interference
        s = Settings(llm_api_key="test", model_name="glm-5.1")
        assert s.model_name == "glm-5.1"

    def test_default_server_port(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.server_port == 8089

    def test_default_server_host(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.server_host == "0.0.0.0"

    def test_default_blade_path(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        # blade_path defaults to empty string; _resolve_blade_path() auto-detects
        assert s.blade_path == ""

    def test_default_kubectl_path(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.kubectl_path == "kubectl"

    def test_default_timeouts(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.timeout_blade == 60
        assert s.timeout_kubectl == 60
        assert s.timeout_kubectl_exec == 180
        # LLM timeout split into connect (fast-fail on bad URL/DNS) vs
        # read (generous so thinking models aren't cut off mid-inference).
        assert s.llm_connect_timeout == 10
        assert s.llm_read_timeout == 600
        assert s.timeout_default == 60

    def test_default_loop_limits(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        # Defaults were bumped during agent tuning — this test pins
        # them so a future quiet change has to update the assertion
        # alongside settings.py rather than silently drifting.
        assert s.max_agent_loop == 100
        assert s.max_execute_loop == 100
        assert s.recursion_limit == 500

    def test_default_retry_config(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.retry_max_retries == 2
        assert s.retry_base_delay == 1.0
        assert s.retry_max_delay == 30.0
        assert s.retry_exponential_base == 2.0
        assert s.retry_jitter is True

    def test_default_confirmation_required(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.confirmation_required is True


class TestBlacklistNamespaces:
    """Test blacklist_namespaces property parsing."""

    def test_default_blacklist(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.blacklist_namespaces == []

    def test_single_namespace(self):
        from chaos_agent.config.settings import Settings

        s = Settings(
            llm_api_key="test",
            safety_blacklist_namespaces="kube-system",
        )
        assert s.blacklist_namespaces == ["kube-system"]

    def test_empty_string(self):
        from chaos_agent.config.settings import Settings

        s = Settings(
            llm_api_key="test",
            safety_blacklist_namespaces="",
        )
        assert s.blacklist_namespaces == []

    def test_whitespace_handling(self):
        from chaos_agent.config.settings import Settings

        s = Settings(
            llm_api_key="test",
            safety_blacklist_namespaces=" ns1 , ns2 , ns3 ",
        )
        assert s.blacklist_namespaces == ["ns1", "ns2", "ns3"]

    def test_trailing_commas(self):
        from chaos_agent.config.settings import Settings

        s = Settings(
            llm_api_key="test",
            safety_blacklist_namespaces="ns1,ns2,",
        )
        assert s.blacklist_namespaces == ["ns1", "ns2"]


class TestSettingsPriority:
    """Test configuration priority: config.json > env vars > defaults."""

    def test_env_used_when_no_config_json(self, monkeypatch, tmp_path):
        """When config.json has no value for a key, env var takes effect."""
        from chaos_agent.config.settings import Settings

        # Temporarily point config to a non-existent file
        monkeypatch.setattr("chaos_agent.config.settings._CONFIG_FILE", tmp_path / "nonexistent.json")
        monkeypatch.setenv("BLADE_AI_MODEL_NAME", "qwen-max")
        s = Settings(llm_api_key="test")
        assert s.model_name == "qwen-max"

    def test_config_json_overrides_env_var(self, monkeypatch, tmp_path):
        """When config.json has a value, it takes priority over env var."""
        from chaos_agent.config.settings import Settings

        config_file = tmp_path / "test_config.json"
        config_file.write_text('{"model_name": "from-config"}', encoding="utf-8")
        monkeypatch.setattr("chaos_agent.config.settings._CONFIG_FILE", config_file)
        monkeypatch.setenv("BLADE_AI_MODEL_NAME", "from-env")
        s = Settings(llm_api_key="test")
        assert s.model_name == "from-config"

    def test_default_used_when_no_config_no_env(self, monkeypatch, tmp_path):
        """When neither config.json nor env var provides a value, code default is used."""
        from chaos_agent.config.settings import Settings

        monkeypatch.setattr("chaos_agent.config.settings._CONFIG_FILE", tmp_path / "nonexistent.json")
        # Don't set env var, so default should be used
        s = Settings(llm_api_key="test")
        assert s.server_port == 8089  # code default

    def test_env_prefix(self):
        from chaos_agent.config.settings import Settings

        assert Settings.model_config["env_prefix"] == "BLADE_AI_"


class TestEmptyStringFallback:
    """Empty string in config.json must NOT shadow ENV / code defaults.

    Regression test for the bug where setting ``"api_base_url": ""`` in
    config.json caused the LLM client to build with an empty base URL —
    LangChain's ChatOpenAI silently accepts it but every subsequent
    request hangs / 401s with no clear error surface for the user.
    The fix treats empty / whitespace-only strings as 'unset' so the
    next source in the priority chain (env, then default) provides the
    real value.
    """

    def test_empty_string_in_config_falls_back_to_env(self, monkeypatch, tmp_path):
        from chaos_agent.config.settings import Settings

        config_file = tmp_path / "test_config.json"
        config_file.write_text(
            '{"api_base_url": "", "llm_api_key": "test"}', encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.config.settings._CONFIG_FILE", config_file,
        )
        monkeypatch.setenv("BLADE_AI_API_BASE_URL", "https://env.example.com/v1")

        s = Settings()
        assert s.api_base_url == "https://env.example.com/v1"

    def test_empty_string_in_config_falls_back_to_default(
        self, monkeypatch, tmp_path,
    ):
        from chaos_agent.config.settings import Settings

        config_file = tmp_path / "test_config.json"
        config_file.write_text(
            '{"api_base_url": "", "model_name": "", "llm_api_key": "test"}',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.config.settings._CONFIG_FILE", config_file,
        )
        monkeypatch.delenv("BLADE_AI_API_BASE_URL", raising=False)
        monkeypatch.delenv("BLADE_AI_MODEL_NAME", raising=False)

        s = Settings()
        assert s.api_base_url == (
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        assert s.model_name == "qwen3.7-plus"

    def test_whitespace_only_string_in_config_treated_as_unset(
        self, monkeypatch, tmp_path,
    ):
        from chaos_agent.config.settings import Settings

        config_file = tmp_path / "test_config.json"
        config_file.write_text(
            '{"model_name": "   \\t", "llm_api_key": "test"}',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.config.settings._CONFIG_FILE", config_file,
        )
        monkeypatch.delenv("BLADE_AI_MODEL_NAME", raising=False)

        s = Settings()
        assert s.model_name == "qwen3.7-plus"

    def test_explicit_non_empty_string_in_config_still_overrides_env(
        self, monkeypatch, tmp_path,
    ):
        """The fix must not break the canonical 'config > env > default' priority."""
        from chaos_agent.config.settings import Settings

        config_file = tmp_path / "test_config.json"
        config_file.write_text(
            '{"api_base_url": "https://from-config.example.com", "llm_api_key": "test"}',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.config.settings._CONFIG_FILE", config_file,
        )
        monkeypatch.setenv("BLADE_AI_API_BASE_URL", "https://from-env.example.com")

        s = Settings()
        assert s.api_base_url == "https://from-config.example.com"


class TestResolveContextBudget:
    """v7 M2 — per-model context budget resolver."""

    def test_anthropic_opus_uses_200k_window(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test", model_name="claude-opus-4-7")
        assert s.resolve_context_budget("claude-opus-4-7") == (200_000, 0.85)

    def test_anthropic_haiku_uses_higher_ratio(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test", model_name="claude-haiku-4-5")
        # Haiku 是更便宜更快的模型，允许更晚才触发压缩
        assert s.resolve_context_budget("claude-haiku-4-5") == (200_000, 0.90)

    def test_qwen_longest_prefix_wins(self):
        from chaos_agent.config.settings import Settings

        # 同时匹配 "qwen3.6-max" 和 "qwen3"，最长前缀（更精确）胜出
        s = Settings(llm_api_key="test", model_name="qwen3.6-max-preview")
        assert s.resolve_context_budget("qwen3.6-max-preview") == (262_144, 0.80)

    def test_qwen37_generation_1m_window(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        # 3.7 全系 1M；注意 "qwen3.7" 必须胜过 "qwen-plus"(32K)
        assert s.resolve_context_budget("qwen3.7-max") == (1_000_000, 0.80)
        assert s.resolve_context_budget("qwen3.7-plus") == (1_000_000, 0.80)
        assert s.resolve_context_budget("qwen3.7-flash") == (1_000_000, 0.80)
        # 旧代际不受影响
        assert s.resolve_context_budget("qwen3.6-max-x") == (262_144, 0.80)
        assert s.resolve_context_budget("qwen-plus-2024") == (32_768, 0.80)

    def test_qwen_coder_variant_windows(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        # coder-plus/flash 为 1M；coder-next 等其余 coder 落到 256k 下界
        assert s.resolve_context_budget("qwen3-coder-plus") == (1_000_000, 0.80)
        assert s.resolve_context_budget("qwen3-coder-flash") == (1_000_000, 0.80)
        assert s.resolve_context_budget("qwen3-coder-next") == (262_144, 0.80)

    def test_qwen_plus_smaller_window(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.resolve_context_budget("qwen-plus-2024") == (32_768, 0.80)

    def test_openai_gpt5_and_gpt41_prefixes(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.resolve_context_budget("gpt-5.2") == (400_000, 0.80)
        # "gpt-4.1" 比 "gpt-4" 更长，精确前缀胜出（1M 窗口）
        assert s.resolve_context_budget("gpt-4.1-mini") == (1_047_576, 0.80)
        assert s.resolve_context_budget("gpt-4o-2024") == (128_000, 0.85)
        assert s.resolve_context_budget("o3-mini") == (200_000, 0.85)

    def test_deepseek_endpoint_prefix_beats_generic(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        # 官方端点名（V4 起 1M）优先于保守的泛化 "deepseek" 64K 兜底
        assert s.resolve_context_budget("deepseek-chat") == (1_000_000, 0.80)
        assert s.resolve_context_budget("deepseek-reasoner") == (1_000_000, 0.80)
        assert s.resolve_context_budget("deepseek-v4") == (1_000_000, 0.80)
        assert s.resolve_context_budget("deepseek-v2") == (64_000, 0.80)

    def test_anthropic_1m_generation_beats_legacy_prefix(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        # 4.6 起 1M GA；4.5 及更早代际回落到泛化前缀的 200K
        assert s.resolve_context_budget("claude-opus-4-6") == (1_000_000, 0.80)
        assert s.resolve_context_budget("claude-sonnet-4-6") == (1_000_000, 0.80)
        assert s.resolve_context_budget("claude-sonnet-5") == (1_000_000, 0.80)
        assert s.resolve_context_budget("claude-opus-4-5") == (200_000, 0.85)

    def test_new_vendor_entries(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.resolve_context_budget("gemini-2.5-flash") == (1_048_576, 0.80)
        assert s.resolve_context_budget("glm-5.2") == (1_000_000, 0.80)
        assert s.resolve_context_budget("glm-5") == (200_000, 0.85)
        assert s.resolve_context_budget("glm-4.6") == (200_000, 0.85)
        assert s.resolve_context_budget("glm-4-plus") == (128_000, 0.85)
        assert s.resolve_context_budget("kimi-k3") == (1_048_576, 0.80)
        assert s.resolve_context_budget("kimi-k2-thinking") == (262_144, 0.80)
        assert s.resolve_context_budget("grok-4-fast") == (2_000_000, 0.80)
        assert s.resolve_context_budget("grok-4-0709") == (256_000, 0.80)

    def test_case_insensitive_match(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.resolve_context_budget("Claude-Opus-4-7") == (200_000, 0.85)
        assert s.resolve_context_budget("CLAUDE-OPUS-4-7") == (200_000, 0.85)

    def test_unknown_model_falls_back_to_global(self):
        from chaos_agent.config.settings import Settings

        s = Settings(
            llm_api_key="test",
            context_max_tokens=99_999,
            context_compact_ratio=0.5,
        )
        assert s.resolve_context_budget("some-unknown-vendor-model") == (99_999, 0.5)

    def test_empty_model_falls_back_to_global(self):
        from chaos_agent.config.settings import Settings

        s = Settings(
            llm_api_key="test",
            model_name="",
            context_max_tokens=77_777,
            context_compact_ratio=0.6,
        )
        assert s.resolve_context_budget("") == (77_777, 0.6)
        assert s.resolve_context_budget(None) == (77_777, 0.6)

    def test_uses_settings_model_name_when_arg_omitted(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test", model_name="claude-haiku-4-5")
        assert s.resolve_context_budget() == (200_000, 0.90)

    def test_user_override_takes_precedence_over_builtin(self):
        from chaos_agent.config.settings import Settings

        # 用户给 claude-opus 设了更小的窗口（如代理裁剪过的版本）
        s = Settings(
            llm_api_key="test",
            model_budgets={
                "claude-opus": {"max_tokens": 100_000, "compact_ratio": 0.7},
            },
        )
        assert s.resolve_context_budget("claude-opus-4-7") == (100_000, 0.7)

    def test_user_can_add_unknown_model(self):
        from chaos_agent.config.settings import Settings

        s = Settings(
            llm_api_key="test",
            model_budgets={
                "my-private-llm": {"max_tokens": 50_000, "compact_ratio": 0.75},
            },
        )
        # 用户新增条目生效；built-in 仍兜底未匹配模型
        assert s.resolve_context_budget("my-private-llm-v1") == (50_000, 0.75)
        assert s.resolve_context_budget("claude-opus-4-7") == (200_000, 0.85)

    def test_malformed_user_entry_falls_through_to_builtin(self):
        from chaos_agent.config.settings import Settings

        # 用户填的 claude-opus 缺 max_tokens 字段 → 跳过用户层，
        # 用 built-in 的 claude-opus 条目
        s = Settings(
            llm_api_key="test",
            model_budgets={"claude-opus": {"compact_ratio": 0.5}},
        )
        assert s.resolve_context_budget("claude-opus-4-7") == (200_000, 0.85)

    def test_fallthrough_to_global_emits_warning(self, caplog):
        import logging

        from chaos_agent.config.settings import (
            Settings,
            _WARNED_FALLBACK_MODELS,
        )

        _WARNED_FALLBACK_MODELS.clear()
        s = Settings(llm_api_key="test")

        with caplog.at_level(logging.WARNING, logger="chaos_agent.config.settings"):
            mt, cr = s.resolve_context_budget("totally-unknown-vendor-model")

        assert (mt, cr) == (s.context_max_tokens, s.context_compact_ratio)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "totally-unknown-vendor-model" in warnings[0].message
        assert "model_budgets" in warnings[0].message

    def test_fallthrough_warning_dedupes_per_model(self, caplog):
        import logging

        from chaos_agent.config.settings import (
            Settings,
            _WARNED_FALLBACK_MODELS,
        )

        _WARNED_FALLBACK_MODELS.clear()
        s = Settings(llm_api_key="test")

        with caplog.at_level(logging.WARNING, logger="chaos_agent.config.settings"):
            # Same unknown model called twice — only one WARNING expected.
            s.resolve_context_budget("mystery-model-x")
            s.resolve_context_budget("mystery-model-x")
            # Different unknown model — should get its own WARNING.
            s.resolve_context_budget("another-mystery")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        assert any("mystery-model-x" in w.message for w in warnings)
        assert any("another-mystery" in w.message for w in warnings)

    def test_known_model_does_not_warn(self, caplog):
        import logging

        from chaos_agent.config.settings import (
            Settings,
            _WARNED_FALLBACK_MODELS,
        )

        _WARNED_FALLBACK_MODELS.clear()
        s = Settings(llm_api_key="test")

        with caplog.at_level(logging.WARNING, logger="chaos_agent.config.settings"):
            s.resolve_context_budget("claude-opus-4-7")
            s.resolve_context_budget("qwen3.6-max-preview")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_reload_clears_warning_dedup(self, caplog):
        import logging

        from chaos_agent.config.settings import (
            Settings,
            _WARNED_FALLBACK_MODELS,
        )

        _WARNED_FALLBACK_MODELS.clear()
        s = Settings(llm_api_key="test")

        with caplog.at_level(logging.WARNING, logger="chaos_agent.config.settings"):
            s.resolve_context_budget("reload-test-model")
            s.reload()
            # After reload, the same unknown model should warn again
            # (the operator may have just edited config to fix it).
            s.resolve_context_budget("reload-test-model")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2


class TestKubeConnectionModeValidator:
    """Validate the explicit channel override at config-load time."""

    def test_empty_mode_ok(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test", kube_connection_mode="")
        assert s.kube_connection_mode == ""

    def test_valid_modes_ok(self):
        from chaos_agent.config.settings import Settings

        for mode in ("kubeconfig", "kubewiz_k8s", "kubewiz_host", "ssh"):
            s = Settings(llm_api_key="test", kube_connection_mode=mode)
            assert s.kube_connection_mode == mode

    def test_invalid_mode_raises(self):
        import pytest
        from pydantic import ValidationError
        from chaos_agent.config.settings import Settings

        with pytest.raises(ValidationError, match="kube_connection_mode"):
            Settings(llm_api_key="test", kube_connection_mode="aaa")

    def test_legacy_kubewiz_value_rejected(self):
        """The old literal 'kubewiz' is no longer a valid value."""
        import pytest
        from pydantic import ValidationError
        from chaos_agent.config.settings import Settings

        with pytest.raises(ValidationError):
            Settings(llm_api_key="test", kube_connection_mode="kubewiz")


class TestSettingsSshStrictHostKeyChecking:
    """ssh_strict_host_key_checking must be one of accept-new/yes/no."""

    def test_valid_values_ok(self):
        from chaos_agent.config.settings import Settings

        for v in ("accept-new", "yes", "no"):
            s = Settings(llm_api_key="test", ssh_strict_host_key_checking=v)
            assert s.ssh_strict_host_key_checking == v

    def test_default_is_accept_new(self):
        from chaos_agent.config.settings import Settings

        assert Settings(llm_api_key="test").ssh_strict_host_key_checking == "accept-new"

    def test_invalid_value_raises(self):
        import pytest
        from pydantic import ValidationError
        from chaos_agent.config.settings import Settings

        with pytest.raises(ValidationError):
            Settings(llm_api_key="test", ssh_strict_host_key_checking="true")
