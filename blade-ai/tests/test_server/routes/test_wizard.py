"""Tests for /api/v1/wizard endpoints.

Covers:
  - GET /wizard/model-presets returns the curated 5-model list.
  - POST /validate/url shape rules (empty, http(s) prefix).
  - POST /validate/api-key error paths (missing key / base, openai
    SDK absent — we don't hit a real LLM in tests).
  - POST /validate/kubeconfig path existence + context discovery shape.
  - POST /save whitelist gating + empty-skip semantics.

These are pure shape tests — the wizard validators themselves carry
unit tests at the module level for the live-call branches.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    """Minimal app with just the wizard router mounted."""
    # ConfigStore writes to ~/.blade-ai by default; redirect to tmp so
    # the /save tests don't clobber the developer's real config file.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BLADE_AI_CONFIG_DIR", str(tmp_path / ".blade-ai"))

    from chaos_agent.server.routes import wizard_router
    from chaos_agent.server.routes import wizard as _wizard  # noqa: F401

    app = FastAPI()
    app.include_router(wizard_router)

    # Lightweight request-id middleware to mirror prod (wizard handlers
    # read ``req.state.request_id`` for the envelope).
    @app.middleware("http")
    async def add_request_id(request, call_next):
        request.state.request_id = "test"
        return await call_next(request)

    return TestClient(app)


# ── model-presets ──────────────────────────────────────────────────────


def test_model_presets_returns_five_recommended(test_client):
    resp = test_client.get("/api/v1/wizard/model-presets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    presets = body["data"]["presets"]
    assert isinstance(presets, list)
    assert len(presets) == 5
    ids = [p["id"] for p in presets]
    # The wizard's recommended-radio order — first item is the default.
    assert ids == [
        "qwen3-max-preview",
        "deepseek-v4-pro",
        "glm-5.1",
        "qwen3.6-plus",
        "claude-opus-4-7",
    ]
    # Each preset has the four required keys for the TS radio renderer.
    for p in presets:
        assert set(p.keys()) >= {"id", "label", "vendor", "hint"}


# ── needs-setup ────────────────────────────────────────────────────────


def test_needs_setup_true_when_no_config_file(test_client, tmp_path, monkeypatch):
    """Empty home → no config.json → wizard should fire on all 3 fields."""
    monkeypatch.delenv("BLADE_AI_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BLADE_AI_MODEL_NAME", raising=False)
    monkeypatch.delenv("BLADE_AI_API_BASE_URL", raising=False)
    resp = test_client.get("/api/v1/wizard/needs-setup")
    body = resp.json()
    assert body["data"]["needs_setup"] is True
    # All 3 fields should be flagged missing.
    assert set(body["data"]["missing"]) == {
        "llm_api_key",
        "model_name",
        "api_base_url",
    }


def test_needs_setup_true_when_only_api_key_filled(
    test_client, tmp_path, monkeypatch,
):
    """Settings has defaults for model/url but those don't count —
    the wizard should still fire because the user hasn't *picked*."""
    cfg_dir = tmp_path / ".blade-ai"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        '{"llm_api_key": "sk-test"}', encoding="utf-8",
    )
    monkeypatch.delenv("BLADE_AI_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BLADE_AI_MODEL_NAME", raising=False)
    monkeypatch.delenv("BLADE_AI_API_BASE_URL", raising=False)
    resp = test_client.get("/api/v1/wizard/needs-setup")
    body = resp.json()
    assert body["data"]["needs_setup"] is True
    assert set(body["data"]["missing"]) == {"model_name", "api_base_url"}


def test_needs_setup_false_when_all_three_filled_in_file(
    test_client, tmp_path, monkeypatch,
):
    cfg_dir = tmp_path / ".blade-ai"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        '{"llm_api_key": "sk-test", "model_name": "qwen3-max-preview", '
        '"api_base_url": "https://api.example.com/v1"}',
        encoding="utf-8",
    )
    monkeypatch.delenv("BLADE_AI_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BLADE_AI_MODEL_NAME", raising=False)
    monkeypatch.delenv("BLADE_AI_API_BASE_URL", raising=False)
    resp = test_client.get("/api/v1/wizard/needs-setup")
    body = resp.json()
    assert body["data"]["needs_setup"] is False
    assert body["data"]["missing"] == []


def test_needs_setup_false_when_env_provides_missing_field(
    test_client, tmp_path, monkeypatch,
):
    """ENV var fills the gap when config.json doesn't have the field."""
    cfg_dir = tmp_path / ".blade-ai"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        '{"llm_api_key": "sk-test", "model_name": "qwen3-max-preview"}',
        encoding="utf-8",
    )
    monkeypatch.delenv("BLADE_AI_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BLADE_AI_MODEL_NAME", raising=False)
    monkeypatch.setenv("BLADE_AI_API_BASE_URL", "https://from-env.example.com")
    resp = test_client.get("/api/v1/wizard/needs-setup")
    body = resp.json()
    assert body["data"]["needs_setup"] is False


def test_needs_setup_treats_empty_string_as_missing(
    test_client, tmp_path, monkeypatch,
):
    """``"api_base_url": ""`` in file ≡ field not set → wizard fires.
    Same fall-through-to-default semantic the settings source applies
    everywhere else."""
    cfg_dir = tmp_path / ".blade-ai"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        '{"llm_api_key": "sk-test", "model_name": "qwen3-max-preview", '
        '"api_base_url": ""}',
        encoding="utf-8",
    )
    monkeypatch.delenv("BLADE_AI_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BLADE_AI_MODEL_NAME", raising=False)
    monkeypatch.delenv("BLADE_AI_API_BASE_URL", raising=False)
    resp = test_client.get("/api/v1/wizard/needs-setup")
    body = resp.json()
    assert body["data"]["needs_setup"] is True
    assert body["data"]["missing"] == ["api_base_url"]


# ── validate/url ───────────────────────────────────────────────────────


def test_validate_url_rejects_empty(test_client):
    resp = test_client.post("/api/v1/wizard/validate/url", json={"url": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "error"
    assert body["data"]["block"] is True


def test_validate_url_rejects_missing_scheme(test_client):
    resp = test_client.post(
        "/api/v1/wizard/validate/url", json={"url": "api.example.com"},
    )
    body = resp.json()
    assert body["data"]["status"] == "error"
    assert "http://" in body["data"]["message"]


def test_validate_url_accepts_https(test_client):
    resp = test_client.post(
        "/api/v1/wizard/validate/url",
        json={"url": "https://api.example.com/v1"},
    )
    body = resp.json()
    assert body["data"]["status"] == "ok"


def test_validate_url_rejects_non_string(test_client):
    resp = test_client.post("/api/v1/wizard/validate/url", json={"url": 42})
    body = resp.json()
    # Non-string payload is a 200 + fail-coded envelope, not 4xx.
    assert body["status"] == "fail"


# ── validate/api-key ───────────────────────────────────────────────────


def test_validate_api_key_missing_returns_error(test_client):
    resp = test_client.post(
        "/api/v1/wizard/validate/api-key",
        json={"api_key": "", "base_url": "https://api.example.com"},
    )
    body = resp.json()
    assert body["data"]["status"] == "error"
    assert body["data"]["block"] is True


def test_validate_api_key_missing_base_returns_error(test_client):
    resp = test_client.post(
        "/api/v1/wizard/validate/api-key",
        json={"api_key": "sk-anything", "base_url": ""},
    )
    body = resp.json()
    assert body["data"]["status"] == "error"


def test_validate_api_key_no_openai_sdk_degrades_to_warn(
    test_client, monkeypatch,
):
    # Make ``import openai`` fail to exercise the soft-degrade branch.
    import builtins
    real_import = builtins.__import__

    def _block_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("openai not installed (test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_openai)
    resp = test_client.post(
        "/api/v1/wizard/validate/api-key",
        json={
            "api_key": "sk-test",
            "base_url": "https://api.example.com/v1",
        },
    )
    body = resp.json()
    assert body["data"]["status"] == "warn"
    assert body["data"]["block"] is False


# ── validate/kubeconfig ────────────────────────────────────────────────


def test_validate_kubeconfig_empty_path_warns(test_client):
    resp = test_client.post(
        "/api/v1/wizard/validate/kubeconfig", json={"path": ""},
    )
    body = resp.json()
    assert body["data"]["status"] == "warn"
    assert body["data"]["block"] is False
    assert body["data"]["metadata"]["contexts"] == []


def test_validate_kubeconfig_nonexistent_path_warns(test_client):
    resp = test_client.post(
        "/api/v1/wizard/validate/kubeconfig",
        json={"path": "/tmp/definitely-not-here-xyz123.yaml"},
    )
    body = resp.json()
    assert body["data"]["status"] == "warn"
    assert "does not exist" in body["data"]["message"]


def test_validate_kubeconfig_real_file_returns_contexts_metadata(
    test_client, tmp_path,
):
    # A real file path — discovery will collapse to empty list because
    # `kubectl config get-contexts` returns nonzero on a non-kubeconfig
    # YAML, but the shape (metadata.contexts list) must be present.
    cfg = tmp_path / "kc.yaml"
    cfg.write_text("apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n")
    resp = test_client.post(
        "/api/v1/wizard/validate/kubeconfig", json={"path": str(cfg)},
    )
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert isinstance(body["data"]["metadata"]["contexts"], list)


# ── save ───────────────────────────────────────────────────────────────


def test_save_persists_whitelisted_keys(test_client, tmp_path):
    resp = test_client.post(
        "/api/v1/wizard/save",
        json={
            "config": {
                "model_name": "qwen3-max-preview",
                "api_base_url": "https://api.example.com/v1",
                "llm_api_key": "sk-test-12345",
                "kubeconfig_path": "/tmp/somepath",
                "confirmation_required": "true",
            },
        },
    )
    body = resp.json()
    assert body["status"] == "success"
    saved = body["data"]["saved_keys"]
    assert set(saved) == {
        "model_name",
        "api_base_url",
        "llm_api_key",
        "kubeconfig_path",
        "confirmation_required",
    }
    assert body["data"]["saved_path"].endswith("config.json")


def test_save_ignores_unknown_keys(test_client):
    resp = test_client.post(
        "/api/v1/wizard/save",
        json={
            "config": {
                "model_name": "qwen3-max-preview",
                "kube_context": "starops-test",
                "tasks_pg_dsn": "postgres://attacker",  # not in whitelist
                "evil_field": "boom",
            },
        },
    )
    body = resp.json()
    assert body["status"] == "success"
    saved = set(body["data"]["saved_keys"])
    assert "tasks_pg_dsn" not in saved
    assert "evil_field" not in saved
    assert "model_name" in saved


def test_save_skips_empty_values(test_client):
    resp = test_client.post(
        "/api/v1/wizard/save",
        json={
            "config": {
                "model_name": "qwen3-max-preview",
                "api_base_url": "",  # empty = leave unchanged
                "llm_api_key": None,  # None = leave unchanged
            },
        },
    )
    body = resp.json()
    saved = set(body["data"]["saved_keys"])
    assert saved == {"model_name"}


def test_save_rejects_non_dict_body(test_client):
    resp = test_client.post(
        "/api/v1/wizard/save", json={"config": "not a dict"},
    )
    body = resp.json()
    assert body["status"] == "fail"


# ── save: first-tier discoverability seeds ────────────────────────────


def _read_config(tmp_path) -> dict:
    return json.loads((tmp_path / ".blade-ai" / "config.json").read_text())


def test_save_seeds_first_tier_defaults(test_client, tmp_path):
    """A fresh save must materialize the curated first-tier knobs so
    users discover they exist (model_budgets, safety blacklist, ...)."""
    resp = test_client.post(
        "/api/v1/wizard/save",
        json={"config": {"model_name": "qwen3-max-preview"}},
    )
    body = resp.json()
    assert body["status"] == "success"

    seeded = set(body["data"]["seeded_keys"])
    assert seeded == {
        "model_budgets",
        "context_max_tokens",
        "context_compact_ratio",
        "llm_thinking_format",
        "safety_blacklist_namespaces",
        "experiment_timeout",
        "confirmation_required",
        "kube_connection_mode",
        "max_agent_loop",
    }

    cfg = _read_config(tmp_path)
    # Personalized entry: the user's own model, valued from the
    # built-in table (qwen3-max prefix → 256K), not a generic example.
    assert cfg["model_budgets"] == {
        "qwen3-max-preview": {"max_tokens": 262_144, "compact_ratio": 0.8},
    }
    assert "_model_budgets_example" not in cfg
    assert cfg["context_max_tokens"] == 100_000
    assert cfg["context_compact_ratio"] == 0.85
    assert cfg["llm_thinking_format"] == "auto"
    assert cfg["safety_blacklist_namespaces"] == ""
    assert cfg["experiment_timeout"] == 300
    assert cfg["confirmation_required"] is False
    assert cfg["kube_connection_mode"] == "kubeconfig"
    assert cfg["max_agent_loop"] == 100
    # Quality-guarantee toggle must NOT be user-discoverable.
    assert "llm_enable_thinking" not in cfg


def test_save_seeds_unknown_model_with_global_fallback(test_client, tmp_path):
    """Models absent from the built-in table still get a personalized
    entry — valued with the global fallback so users can tune it."""
    resp = test_client.post(
        "/api/v1/wizard/save",
        json={"config": {"model_name": "totally-unknown-llm"}},
    )
    assert resp.json()["status"] == "success"
    cfg = _read_config(tmp_path)
    assert cfg["model_budgets"] == {
        "totally-unknown-llm": {
            "max_tokens": 100_000,  # global fallback
            "compact_ratio": 0.85,
        },
    }


# ── boot-time backfill (old configs, wizard not firing) ───────────────


@pytest.fixture
def seed_env(tmp_path, monkeypatch):
    """Env isolation for direct backfill_seed_defaults() calls: same
    HOME / BLADE_AI_CONFIG_DIR redirect as test_client, plus the three
    essential env vars cleared so the file is the only source."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BLADE_AI_CONFIG_DIR", str(tmp_path / ".blade-ai"))
    for var in (
        "BLADE_AI_LLM_API_KEY",
        "BLADE_AI_MODEL_NAME",
        "BLADE_AI_API_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / ".blade-ai" / "config.json"


def _write_config(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


_OLD_CONFIG = {
    "llm_api_key": "sk-old",
    "model_name": "totally-unknown-llm",
    "api_base_url": "https://example.com/v1",
    "experiment_timeout": 900,  # user-tuned value that must survive
}


def test_backfill_seeds_old_config(seed_env):
    """Old install (essentials complete, no seed keys) gets the whole
    first tier backfilled; pre-existing keys are untouched."""
    from chaos_agent.server.routes.wizard import backfill_seed_defaults

    _write_config(seed_env, _OLD_CONFIG)
    seeded, materialized = backfill_seed_defaults()
    assert materialized == []
    assert set(seeded) == {
        "model_budgets",
        "context_max_tokens",
        "context_compact_ratio",
        "llm_thinking_format",
        "safety_blacklist_namespaces",
        "confirmation_required",
        "kube_connection_mode",
        "max_agent_loop",
    }  # experiment_timeout EXCLUDED — already present

    cfg = json.loads(seed_env.read_text())
    assert cfg["experiment_timeout"] == 900  # user value preserved
    assert cfg["llm_api_key"] == "sk-old"
    # Personalized entry valued with the global fallback (unknown model).
    assert cfg["model_budgets"] == {
        "totally-unknown-llm": {
            "max_tokens": 100_000,
            "compact_ratio": 0.85,
        },
    }
    assert cfg["context_max_tokens"] == 100_000
    assert "llm_enable_thinking" not in cfg


def test_backfill_materializes_env_values(seed_env, monkeypatch):
    """Env-configured user: essentials live ONLY in env vars, the file
    lacks them. Backfill must write the ENV values into the file —
    never defaults — and key model_budgets on the env-configured model."""
    from chaos_agent.server.routes.wizard import backfill_seed_defaults

    monkeypatch.setenv("BLADE_AI_LLM_API_KEY", "sk-from-env")
    monkeypatch.setenv("BLADE_AI_MODEL_NAME", "qwen3-max-preview")
    _write_config(seed_env, {"api_base_url": "https://example.com/v1"})

    seeded, materialized = backfill_seed_defaults()
    assert set(materialized) == {"llm_api_key", "model_name"}
    assert "model_budgets" in seeded

    cfg = json.loads(seed_env.read_text())
    # Env values materialized verbatim — defaults must NOT appear.
    assert cfg["llm_api_key"] == "sk-from-env"
    assert cfg["model_name"] == "qwen3-max-preview"
    assert cfg["api_base_url"] == "https://example.com/v1"  # untouched
    # Budget entry keys on the model now genuinely in the file
    # (qwen3-max prefix → 256K from the built-in table).
    assert cfg["model_budgets"] == {
        "qwen3-max-preview": {"max_tokens": 262_144, "compact_ratio": 0.8},
    }


def test_backfill_skips_when_essential_missing(seed_env):
    """Wizard-bound boot (an essential field missing) — /save owns the
    seed there; backfill must not race it."""
    from chaos_agent.server.routes.wizard import backfill_seed_defaults

    cfg = dict(_OLD_CONFIG)
    cfg["llm_api_key"] = ""  # empty value counts as missing
    _write_config(seed_env, cfg)
    assert backfill_seed_defaults() == ([], [])
    assert json.loads(seed_env.read_text()) == cfg  # zero changes


def test_backfill_skips_corrupt_file(seed_env):
    from chaos_agent.server.routes.wizard import backfill_seed_defaults

    seed_env.parent.mkdir(parents=True, exist_ok=True)
    seed_env.write_text('{"llm_api_key": broken', encoding="utf-8")
    assert backfill_seed_defaults() == ([], [])
    # The corrupt file is left EXACTLY as-is for the error page.
    assert seed_env.read_text() == '{"llm_api_key": broken'


def test_backfill_skips_when_no_config_file(seed_env):
    """Pure env-var users with NO file get no config.json fabricated."""
    from chaos_agent.server.routes.wizard import backfill_seed_defaults

    assert backfill_seed_defaults() == ([], [])
    assert not seed_env.exists()


def test_backfill_is_idempotent(seed_env):
    from chaos_agent.server.routes.wizard import backfill_seed_defaults

    _write_config(seed_env, _OLD_CONFIG)
    seeded, _ = backfill_seed_defaults()
    assert seeded  # first pass seeds
    assert backfill_seed_defaults() == ([], [])  # second: nothing left
    # Third pass after a user edit still touches nothing.
    cfg = json.loads(seed_env.read_text())
    cfg["context_max_tokens"] = 50_000
    _write_config(seed_env, cfg)
    assert backfill_seed_defaults() == ([], [])
    assert json.loads(seed_env.read_text())["context_max_tokens"] == 50_000


def test_seed_never_overwrites_existing_values(test_client, tmp_path):
    """Re-running the wizard must not clobber user-customized knobs."""
    cfg_dir = tmp_path / ".blade-ai"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({
        "context_max_tokens": 50_000,
        "model_budgets": {"my-llm": {"max_tokens": 4096, "compact_ratio": 0.9}},
        "safety_blacklist_namespaces": "kube-system,prod",
    }))

    resp = test_client.post(
        "/api/v1/wizard/save",
        json={"config": {"model_name": "qwen3-max-preview"}},
    )
    body = resp.json()
    assert body["status"] == "success"
    # Already-present keys are NOT re-seeded.
    seeded = set(body["data"]["seeded_keys"])
    assert "context_max_tokens" not in seeded
    assert "model_budgets" not in seeded
    assert "safety_blacklist_namespaces" not in seeded

    cfg = _read_config(tmp_path)
    assert cfg["context_max_tokens"] == 50_000
    assert cfg["model_budgets"] == {"my-llm": {"max_tokens": 4096, "compact_ratio": 0.9}}
    assert cfg["safety_blacklist_namespaces"] == "kube-system,prod"
    # Absent keys still get seeded.
    assert cfg["experiment_timeout"] == 300


def test_user_payload_wins_over_seed(test_client, tmp_path):
    """A wizard answer saved in the same call beats the seed default."""
    resp = test_client.post(
        "/api/v1/wizard/save",
        json={"config": {"confirmation_required": "true"}},
    )
    assert resp.json()["status"] == "success"
    cfg = _read_config(tmp_path)
    assert cfg["confirmation_required"] is True
    assert "confirmation_required" not in resp.json()["data"]["seeded_keys"]

