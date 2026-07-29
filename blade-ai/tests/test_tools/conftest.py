"""Test fixtures for tests/test_tools/.

Ensures tests that assert kubeconfig-mode command shapes are isolated
from the user's ~/.blade-ai/config.json (which may set kubewiz mode).
"""

import pytest


@pytest.fixture(autouse=True)
def _force_kubeconfig_mode(monkeypatch):
    """Force kubeconfig connection mode for deterministic command assertions.

    kube_connection_mode is now an explicit channel override; leaving it
    empty ("") plus clearing the kubewiz selector fields makes field-based
    inference deterministically resolve to the kubeconfig channel, immune
    to the developer's config.json.
    """
    from chaos_agent.config.settings import settings as _s
    monkeypatch.setattr(_s, "kube_connection_mode", "")
    monkeypatch.setattr(_s, "kubewiz_cluster_uuid", "")
