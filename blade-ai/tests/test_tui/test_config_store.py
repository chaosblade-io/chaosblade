"""Tests for ConfigStore."""

import json
import os

import pytest

from chaos_agent.tui.config_store import ConfigStore


@pytest.fixture
def config_dir(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "llm_api_key": "sk-test-1234567890abcdef",
        "model_name": "qwen3.6-max-preview",
        "confirmation_required": True,
        "llm_max_retries": 3,
    }))
    return str(tmp_path)


class TestConfigStoreRead:
    def test_read_all(self, config_dir):
        store = ConfigStore(os.path.join(config_dir, "config.json"))
        data = store.read_all()
        assert data["model_name"] == "qwen3.6-max-preview"
        assert data["llm_api_key"] == "sk-test-1234567890abcdef"

    def test_read_nonexistent(self, tmp_path):
        store = ConfigStore(str(tmp_path / "missing.json"))
        data = store.read_all()
        assert data == {}

    def test_read_corrupted_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid json")
        store = ConfigStore(str(bad))
        assert store.read_all() == {}


class TestConfigStoreWrite:
    def test_set_single_key(self, config_dir):
        store = ConfigStore(os.path.join(config_dir, "config.json"))
        store.set("model_name", "deepseek-chat")
        data = store.read_all()
        assert data["model_name"] == "deepseek-chat"

    def test_set_bool_coercion(self, config_dir):
        store = ConfigStore(os.path.join(config_dir, "config.json"))
        store.set("confirmation_required", "false")
        data = store.read_all()
        assert data["confirmation_required"] is False

    def test_set_int_coercion(self, config_dir):
        store = ConfigStore(os.path.join(config_dir, "config.json"))
        store.set("llm_max_retries", "5")
        data = store.read_all()
        assert data["llm_max_retries"] == 5

    def test_set_many(self, config_dir):
        store = ConfigStore(os.path.join(config_dir, "config.json"))
        store.set_many({"model_name": "test-model", "llm_max_retries": 10})
        data = store.read_all()
        assert data["model_name"] == "test-model"
        assert data["llm_max_retries"] == 10

    def test_atomic_write(self, config_dir):
        p = os.path.join(config_dir, "config.json")
        store = ConfigStore(p)
        store.set("model_name", "atomic-test")
        assert not os.path.exists(p + ".tmp")
        data = store.read_all()
        assert data["model_name"] == "atomic-test"
