"""Standalone entry point for the first-time setup wizard.

Exposes the same 8-step ``onboarding_renderer.run()`` flow that the
legacy Python TUI launches inline, but as an independently-callable
Typer command. The TS TUI launcher in ``cli.tsx`` shells out to this
when ``llm_api_key`` is unset on first start, so end-users get the
same wizard regardless of which TUI front-end they boot.

Exit codes:
    0  user completed and saved
    2  user pressed Esc / cancelled — caller may continue with limited UX
    1  unexpected error during wizard run
"""

from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def config_wizard_command() -> None:
    """Run the configuration wizard standalone.

    Intentionally creates fresh ``ChaosConsole`` and ``ConfigStore``
    instances rather than borrowing the TUI app's globals — this
    command runs OUTSIDE any TUI session (the caller is either the
    user typing ``blade-ai config-wizard`` or the TS TUI launcher
    spawning us before its own server is up).
    """
    try:
        from chaos_agent.tui.config_store import ConfigStore
        from chaos_agent.tui.console import ChaosConsole
        from chaos_agent.tui.renderers import onboarding as onboarding_renderer
    except ImportError as e:
        # prompt_toolkit / rich might be missing in a truly minimal
        # install — fail loudly so the caller can decide what to do.
        sys.stderr.write(f"blade-ai: cannot import wizard dependencies: {e}\n")
        sys.exit(1)

    console = ChaosConsole()
    config_store = ConfigStore()

    try:
        saved = asyncio.run(onboarding_renderer.run(console, config_store))
    except KeyboardInterrupt:
        # Ctrl+C inside the wizard. Treat like a clean cancel so the
        # caller (TS TUI) can decide whether to retry or skip.
        sys.exit(2)
    except Exception as e:
        logger.exception("Wizard crashed")
        sys.stderr.write(f"blade-ai: wizard failed: {e}\n")
        sys.exit(1)

    sys.exit(0 if saved else 2)
