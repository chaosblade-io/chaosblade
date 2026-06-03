"""FastAPI dependencies for route handlers."""

# Singleton references set during app startup
_skill_registry = None
_agents = None


def get_skill_registry():
    """Get the global skill registry instance."""
    return _skill_registry


def get_agents():
    """Get the global compiled agent instances."""
    return _agents
