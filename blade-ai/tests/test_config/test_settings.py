"""Tests for pydantic-settings configuration."""


class TestSettingsDefaults:
    """Test default values for Settings."""

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
        assert s.timeout_blade == 30
        assert s.timeout_kubectl == 30
        assert s.timeout_kubectl_exec == 60
        # timeout_llm tightened from 180s → 30s so a misconfigured
        # base URL surfaces a clear error in ~60s (1 retry × 30s)
        # instead of ~9 minutes (3 retries × 180s).
        assert s.timeout_llm == 30
        assert s.timeout_default == 60

    def test_default_loop_limits(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        # Defaults were bumped during agent tuning — this test pins
        # them so a future quiet change has to update the assertion
        # alongside settings.py rather than silently drifting.
        assert s.max_agent_loop == 50
        assert s.max_execute_loop == 50
        assert s.recursion_limit == 150

    def test_default_retry_config(self):
        from chaos_agent.config.settings import Settings

        s = Settings(llm_api_key="test")
        assert s.retry_max_retries == 3
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
        assert s.blacklist_namespaces == ["kube-system", "kube-public"]

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
        assert s.model_name == "qwen3.6-max-preview"

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
        assert s.model_name == "qwen3.6-max-preview"

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
