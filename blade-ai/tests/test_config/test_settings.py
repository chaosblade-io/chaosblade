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
        assert s.timeout_llm == 180
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
