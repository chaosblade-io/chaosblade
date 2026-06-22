"""Test fixtures for tests/test_tools/.

Ensures tests that assert kubeconfig-mode command shapes are isolated
from the user's ~/.blade-ai/config.json (which may set kubewiz mode).
"""

import pytest


@pytest.fixture(autouse=True)
def _force_kubeconfig_mode(monkeypatch):
    """Force kubeconfig connection mode for deterministic command assertions.

    Without this, tests break when the developer's config.json sets
    kube_connection_mode=kubewiz (higher priority than env vars in
    pydantic-settings source chain).
    """
    from chaos_agent.config.settings import settings as _s
    monkeypatch.setattr(_s, "kube_connection_mode", "kubeconfig")
