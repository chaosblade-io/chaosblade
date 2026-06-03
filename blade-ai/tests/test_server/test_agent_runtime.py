"""Tests for ``chaos_agent.server.agent_runtime.maybe_rebuild_agents``.

Three behaviours to lock:
  · Short-circuit when the changed key set doesn't touch any LLM-
    bound key — must NOT call ``create_agent``.
  · Successful rebuild — swap ``app.state.agents`` + checkpointer
    alias, return None.
  · Failed rebuild — leave existing agents untouched, return the
    error string for the route to surface.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.server.agent_runtime import (
    LLM_BOUND_KEYS,
    maybe_rebuild_agents,
)


@pytest.fixture
def fake_app():
    """Bare app stand-in — ``maybe_rebuild_agents`` only reaches into
    ``app.state``, so a MagicMock with a real ``state`` is enough."""
    app = MagicMock()
    app.state.skill_registry = MagicMock(name="registry")
    app.state.agents = {"inject": "OLD", "checkpointer": "OLD_CP"}
    app.state.checkpointer = "OLD_CP"
    return app


@pytest.mark.asyncio
async def test_short_circuits_when_no_llm_bound_key_changed(fake_app):
    """timeout_kubectl is not LLM-bound → helper must NOT call
    create_agent and must return None."""
    with patch(
        "chaos_agent.agent.factory.create_agent", new_callable=AsyncMock
    ) as create:
        err = await maybe_rebuild_agents(fake_app, ["timeout_kubectl"])
    assert err is None
    create.assert_not_called()
    # State stays untouched.
    assert fake_app.state.agents == {"inject": "OLD", "checkpointer": "OLD_CP"}
    assert fake_app.state.checkpointer == "OLD_CP"


@pytest.mark.asyncio
async def test_short_circuits_on_empty_iterable(fake_app):
    """Empty changed_keys → no work, no error."""
    with patch(
        "chaos_agent.agent.factory.create_agent", new_callable=AsyncMock
    ) as create:
        err = await maybe_rebuild_agents(fake_app, [])
    assert err is None
    create.assert_not_called()


@pytest.mark.asyncio
async def test_rebuilds_and_swaps_state_on_llm_bound_change(fake_app):
    """model_name IS LLM-bound → helper rebuilds + swaps state."""
    new_agents = {"inject": "NEW", "checkpointer": "NEW_CP"}
    with patch(
        "chaos_agent.agent.factory.create_agent",
        new_callable=AsyncMock,
        return_value=new_agents,
    ) as create:
        err = await maybe_rebuild_agents(fake_app, ["model_name"])
    assert err is None
    # E9 — rebuild forwards the existing mcp_manager so MCP tools
    # don't get silently dropped on wizard /save / model swap.
    create.assert_awaited_once_with(
        fake_app.state.skill_registry,
        mcp_manager=fake_app.state.mcp_manager,
    )
    assert fake_app.state.agents == new_agents
    # Checkpointer alias must sync — turn / sessions routes read it
    # directly without going through agents dict.
    assert fake_app.state.checkpointer == "NEW_CP"


@pytest.mark.asyncio
async def test_returns_error_string_on_rebuild_failure(fake_app):
    """create_agent raises → helper logs + returns the error string,
    leaves existing app.state.agents untouched so /turn keeps working
    against the previous (stale but functional) agents."""
    with patch(
        "chaos_agent.agent.factory.create_agent",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        err = await maybe_rebuild_agents(fake_app, ["llm_api_key"])
    assert err is not None
    assert "RuntimeError" in err
    assert "boom" in err
    # Old state preserved.
    assert fake_app.state.agents == {"inject": "OLD", "checkpointer": "OLD_CP"}
    assert fake_app.state.checkpointer == "OLD_CP"


@pytest.mark.asyncio
async def test_skips_gracefully_when_registry_missing():
    """Bare app without skill_registry on state — defensive path for
    tests that spin up a partial app. Must return a clear message
    rather than crash."""
    app = MagicMock()
    # Set state to a spec'd object so getattr returns the default
    # for an unknown attr instead of MagicMock's auto-created child.
    class _State:
        pass

    app.state = _State()
    with patch(
        "chaos_agent.agent.factory.create_agent", new_callable=AsyncMock
    ) as create:
        err = await maybe_rebuild_agents(app, ["model_name"])
    assert err is not None
    assert "skill_registry" in err
    create.assert_not_called()


@pytest.mark.asyncio
async def test_partial_overlap_triggers_rebuild(fake_app):
    """Changed keys include one LLM-bound + several non-bound → rebuild
    fires once (no per-key fan-out)."""
    new_agents = {"inject": "NEW"}
    with patch(
        "chaos_agent.agent.factory.create_agent",
        new_callable=AsyncMock,
        return_value=new_agents,
    ) as create:
        err = await maybe_rebuild_agents(
            fake_app,
            ["timeout_kubectl", "model_name", "log_level"],
        )
    assert err is None
    create.assert_awaited_once()
    assert fake_app.state.agents == new_agents


def test_llm_bound_keys_set_is_immutable():
    """Locked set — every consumer (config route, model route, wizard)
    relies on this being the single source of truth. A future addition
    must be deliberate."""
    assert isinstance(LLM_BOUND_KEYS, frozenset)
    # Sanity: the 3 keys the wizard writes are all in here, otherwise
    # the rebuild after wizard /save would silently skip.
    assert {"model_name", "api_base_url", "llm_api_key"} <= LLM_BOUND_KEYS


def test_llm_bound_keys_are_subset_of_cold_keys():
    """Invariant: every LLM-bound key MUST also be in ConfigStore's
    ``_COLD_KEYS``. Reasoning:

    Cold = "settings.reload() can't make the change take effect on
    the running process". A key that's hot from ConfigStore's view
    (settings.reload covers it) but flagged here as LLM-bound would
    trigger a rebuild on every write — wasteful, since reload alone
    sufficed. Worse, an LLM-bound key that ISN'T cold means our
    routing layer treats it as "needs rebuild" while ConfigStore
    silently swears it doesn't — confusing for future maintainers.

    This test fires when someone adds an LLM-bound key here but
    forgets to also classify it cold in config_store.py. It
    catches the drift in ONE direction (the direction that breaks
    the user contract); the other direction (key is cold but not
    LLM-bound) is just suboptimal, not broken.
    """
    from chaos_agent.tui.config_store import _COLD_KEYS

    missing = LLM_BOUND_KEYS - _COLD_KEYS
    assert not missing, (
        f"LLM_BOUND_KEYS includes {sorted(missing)} which are NOT in "
        f"ConfigStore._COLD_KEYS — settings.reload() would silently "
        f"cover them and the agent rebuild we do here would be a waste."
    )
