"""Unit tests for the onboarding wizard.

These tests exercise the wizard's pure helpers and the WizardApp state
machine directly (skip-logic, advance/back, save/cancel side effects).
The full prompt_toolkit Application loop is not booted because it
requires a real TTY; we instead drive the state-machine methods.
"""

from __future__ import annotations

import pytest

from chaos_agent.tui.config_store import ConfigStore
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.renderers import onboarding


@pytest.fixture
def store(tmp_path):
    return ConfigStore(str(tmp_path / "config.json"))


@pytest.fixture
def ctx(store):
    return onboarding.WizardCtx(config_store=store, edit_mode=False)


# ── Smart defaults ─────────────────────────────────────────────────


def test_default_api_key_prefers_dashscope(monkeypatch, ctx):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash-xyz")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BLADE_AI_LLM_API_KEY", raising=False)
    assert onboarding._default_api_key(ctx) == "sk-dash-xyz"


def test_default_model_dashscope_when_dashscope_env(monkeypatch, ctx):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert onboarding._default_model(ctx).startswith("qwen")


def test_default_model_openai_when_openai_env(monkeypatch, ctx):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    assert onboarding._default_model(ctx) == "gpt-4o"


def test_default_api_url_follows_chosen_model(ctx):
    ctx.updates["model_name"] = "gpt-4o"
    assert "openai.com" in onboarding._default_api_url(ctx)
    ctx.updates["model_name"] = "qwen3-max"
    assert "dashscope" in onboarding._default_api_url(ctx)


def test_default_kubeconfig_uses_home_when_present(monkeypatch, tmp_path, ctx):
    fake_home_kube = tmp_path / "home.kube"
    fake_home_kube.write_text("apiVersion: v1\n")
    # Force ~/.kube/config resolution to hit our fake file
    monkeypatch.setenv("KUBECONFIG", str(fake_home_kube))
    assert onboarding._default_kubeconfig(ctx) == str(fake_home_kube)


# ── Edit-mode prefill ──────────────────────────────────────────────


def test_edit_mode_prefills_from_settings_snapshot(store):
    ctx = onboarding.WizardCtx(
        config_store=store,
        edit_mode=True,
        snapshot={
            "llm_api_key": "sk-existing-key",
            "model_name": "qwen3-max",
            "api_base_url": "https://x.example/v1",
            "kubeconfig_path": "/etc/kube/conf",
            "kube_context": "prod",
            "confirmation_required": False,
        },
    )
    assert onboarding._default_api_key(ctx) == "sk-existing-key"
    assert onboarding._default_model(ctx) == "qwen3-max"
    assert onboarding._default_api_url(ctx) == "https://x.example/v1"
    assert onboarding._default_kubeconfig(ctx) == "/etc/kube/conf"
    assert onboarding._default_permission(ctx) is False


# ── URL validator ──────────────────────────────────────────────────


async def test_validate_api_url_rejects_blank(ctx):
    r = await onboarding._validate_api_url("", ctx)
    assert r.status == "error" and r.block is True


async def test_validate_api_url_rejects_no_scheme(ctx):
    r = await onboarding._validate_api_url("example.com/v1", ctx)
    assert r.status == "error" and r.block is True


async def test_validate_api_url_accepts_https(ctx):
    r = await onboarding._validate_api_url("https://api.openai.com/v1", ctx)
    assert r.status == "ok"


# ── Kubeconfig validator ───────────────────────────────────────────


async def test_validate_kubeconfig_warns_when_missing_file(ctx):
    r = await onboarding._validate_kubeconfig("/nonexistent/path", ctx)
    assert r.status == "warn" and r.block is False


async def test_validate_kubeconfig_warns_when_blank(ctx):
    r = await onboarding._validate_kubeconfig("", ctx)
    assert r.status == "warn" and r.block is False


# ── API key live validation ────────────────────────────────────────


class _FakeAuthError(Exception):
    pass


class _FakeModelsBad:
    async def list(self):
        raise _FakeAuthError("401 Unauthorized: Invalid API key")


class _FakeClientBad:
    def __init__(self, **_):
        self.models = _FakeModelsBad()


class _FakeModelsOk:
    async def list(self):
        return {"data": []}


class _FakeClientOk:
    def __init__(self, **_):
        self.models = _FakeModelsOk()


async def test_validate_api_key_blocks_on_401(ctx, monkeypatch):
    fake_openai = type("M", (), {"AsyncOpenAI": _FakeClientBad})
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)
    r = await onboarding._validate_api_key("sk-bad", ctx)
    assert r.status == "error" and r.block is True


async def test_validate_api_key_passes_on_ok(ctx, monkeypatch):
    fake_openai = type("M", (), {"AsyncOpenAI": _FakeClientOk})
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)
    r = await onboarding._validate_api_key("sk-good", ctx)
    assert r.status == "ok" and r.block is False


async def test_validate_api_key_blocks_on_blank(ctx):
    r = await onboarding._validate_api_key("", ctx)
    assert r.status == "error" and r.block is True


# ── Step list & skip-if ─────────────────────────────────────────────


def test_kube_context_step_skipped_when_no_contexts(ctx):
    steps = onboarding._build_steps()
    kc_step = next(s for s in steps if s.key == "kube_context")
    ctx.discovered_contexts = []
    assert kc_step.skip_if(ctx) is True


def test_kube_context_step_skipped_when_single_context(ctx):
    steps = onboarding._build_steps()
    kc_step = next(s for s in steps if s.key == "kube_context")
    ctx.discovered_contexts = ["minikube"]
    assert kc_step.skip_if(ctx) is True


def test_kube_context_step_runs_when_multiple_contexts(ctx):
    steps = onboarding._build_steps()
    kc_step = next(s for s in steps if s.key == "kube_context")
    ctx.discovered_contexts = ["dev", "staging", "prod"]
    assert kc_step.skip_if(ctx) is False


def test_step_list_has_summary_at_end():
    steps = onboarding._build_steps()
    assert steps[-1].kind == onboarding.StepKind.SUMMARY


def test_step_list_count():
    # 1 welcome + 6 config steps + 1 summary
    assert len(onboarding._build_steps()) == 8


# ── WizardApp state machine ────────────────────────────────────────


def _new_app(store) -> onboarding.WizardApp:
    ctx = onboarding.WizardCtx(config_store=store, edit_mode=False)
    return onboarding.WizardApp(ChaosConsole(), ctx, onboarding._build_steps())


def test_cancel_does_not_persist_marker(store):
    """Cancel must NOT persist a skip marker — the wizard re-prompts on
    next launch when essential config is still missing."""
    app = _new_app(store)
    app._cancel()
    assert app.cancelled is True
    assert app.saved is False
    assert "onboarding_skipped_at" not in store.read_all()


def test_goto_next_skips_kube_context_when_no_discovery(store):
    app = _new_app(store)
    # Land on kubeconfig step
    for _ in range(4):
        app._goto_next()
    assert app.steps[app.idx].key == "kubeconfig_path"
    # No contexts discovered → next() should jump past the kube_context step
    app._goto_next()
    assert app.steps[app.idx].key == "confirmation_required"


def test_summary_e_jumps_back_to_first_editable(store):
    app = _new_app(store)
    # Move directly to summary
    summary_idx = next(
        i for i, s in enumerate(app.steps) if s.kind == onboarding.StepKind.SUMMARY
    )
    app._enter_step(summary_idx)
    # Find the first non-static, non-skipped step manually (mirror handler logic)
    for i, s in enumerate(app.steps):
        if s.kind not in (
            onboarding.StepKind.ENTER,
            onboarding.StepKind.SUMMARY,
        ) and not (s.skip_if and s.skip_if(app.ctx)):
            app._enter_step(i)
            break
    # After reorder: model → api_url → api_key, the first editable step
    # following the welcome screen is the model picker.
    assert app.steps[app.idx].key == "model_name"


def test_radio_move_wraps(store):
    app = _new_app(store)
    # Enter the model picker (key=model_name)
    model_idx = next(i for i, s in enumerate(app.steps) if s.key == "model_name")
    app._enter_step(model_idx)
    n = len(app.steps[model_idx].radio_options_fn(app.ctx))
    start = app.radio_idx
    for _ in range(n):
        app._radio_move(1)
    assert app.radio_idx == start


def test_mask_secret_short():
    assert onboarding._mask_secret("abc") == "•••"


def test_mask_secret_long():
    s = "sk-1234567890abcdefghij"
    out = onboarding._mask_secret(s)
    assert out.startswith("sk-1") and out.endswith("ghij")
    assert "•" in out
