"""CLI command: blade-ai server

Public counterpart to the ``blade-ai-server`` console script. Both call the
same ``run_server``, but only this one is reachable from the single
PyInstaller binary: curl-bash installs ship just ``blade-ai``, so without it
there is no user-facing way to host the API from a release artifact
(``blade-ai-server`` is a console_script, generated only by pip installs).

Distinct from the hidden ``__embedded_server__`` command in ``main.py``, which
the TS TUI spawns in frozen mode and which needs different defaults
(loopback host, OS-allocated port).
"""

import typer


def serve_command(
    host: str | None = typer.Option(
        None, "--host",
        help="Bind address (default: BLADE_AI_SERVER_HOST, 0.0.0.0)",
    ),
    port: int | None = typer.Option(
        None, "--port",
        help="Bind port; 0 lets the OS allocate one "
             "(default: BLADE_AI_SERVER_PORT, 8089)",
    ),
    ready_stdout: bool = typer.Option(
        False, "--ready-stdout",
        help="Print 'BLADE_AI_READY port=N' once startup completes — for "
             "scripts that must wait until the API is actually serving",
    ),
) -> None:
    """Start the blade-ai HTTP API server.

    ``host`` / ``port`` default to ``None`` so the existing
    ``BLADE_AI_SERVER_HOST`` / ``BLADE_AI_SERVER_PORT`` settings stay
    authoritative; a flag only overrides them when explicitly passed. This
    keeps ``blade-ai server`` and ``blade-ai-server`` behaviourally identical.
    """
    from chaos_agent.server.app import run_server

    run_server(host=host, port=port, ready_stdout=ready_stdout)
