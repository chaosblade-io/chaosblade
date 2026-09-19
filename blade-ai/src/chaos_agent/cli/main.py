"""CLI entry point: Typer app with all commands registered."""

import os
import shutil
import sys
import warnings
from pathlib import Path

# Suppress Pydantic V1 deprecation warning on Python 3.14+
# (langchain_core still uses pydantic.v1 compat layer internally)
warnings.filterwarnings("ignore", message="Core Pydantic V1 functionality", category=UserWarning)

import typer

from chaos_agent.cli.commands.capabilities_cmd import capabilities_sync
from chaos_agent.cli.commands.config import config_command
from chaos_agent.cli.commands.confirm import confirm_command
from chaos_agent.cli.commands.inject import inject_command
from chaos_agent.cli.commands.list_cmd import list_command
from chaos_agent.cli.commands.metric import metric_command
from chaos_agent.cli.commands.recover import recover_command
from chaos_agent.cli.commands.serve import serve_command
from chaos_agent.cli.commands.uninstall import uninstall_command
from chaos_agent.cli.commands.update import update_command
from chaos_agent.cli.commands.version import version_command
from chaos_agent.cli.commands.web import web_command


# TyperGroup subclass that enables -h as an alias for --help
class HelpAliasGroup(typer.core.TyperGroup):
    """Click group that recognizes -h alongside --help."""


app = typer.Typer(
    name="blade-ai",
    help="Chaos Engineering Agent - Kubernetes fault injection via CLI",
    no_args_is_help=False,
    cls=HelpAliasGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Chaos Engineering Agent - interactive TUI or CLI commands."""
    if ctx.invoked_subcommand is None:
        _launch_default_tui()


def _launch_default_tui(extra_args: list[str] | None = None) -> None:
    """Hand the terminal over to the TS (Ink) TUI — the only TUI.

    ``extra_args`` is appended verbatim to the TUI's argv — used by
    ``blade-ai resume -i <sid>`` to pass ``--resume <sid>`` (parsed by
    the TS side's ``parseResumeArgv``).

    The legacy Python TUI was removed; the TS TUI is the sole
    interactive surface. Failure modes are all fail-loud:

      1. ``BLADE_AI_TUI=legacy`` → rejected with an explanatory error.
         Kept as an explicit check (instead of falling through) so
         users with the old env var set get a clear migration note
         rather than a confusing generic failure.
      2. TS bundle not found or Node.js missing → print an actionable
         error and exit 1. There is no fallback TUI anymore.
      3. ``BLADE_AI_TUI=ts`` remains a legal no-op value (equivalent
         to the default behaviour; kept for CI / smoke compatibility).

    We hand off via ``os.execvp`` rather than ``subprocess.run`` so the
    user's terminal cleanly belongs to the new process — no Python
    parent in the ``ps`` tree, signals route directly, exit code is the
    TS process's own.
    """
    pref = (os.environ.get("BLADE_AI_TUI") or "").strip().lower()
    if pref == "legacy":
        sys.stderr.write(
            "blade-ai: BLADE_AI_TUI=legacy is no longer supported — "
            "the Python TUI has been removed.\n"
            "  Unset the variable (or set BLADE_AI_TUI=ts) and run blade-ai again.\n"
        )
        sys.exit(1)

    bundle, reason = _resolve_ts_bundle()
    if bundle is None:
        if reason == "node_missing":
            sys.stderr.write(
                "blade-ai: the TS TUI bundle (cli.js) is present but Node.js is not installed.\n"
                "  The TS TUI requires Node.js >= 22 to run.\n"
                "  Install Node.js: https://nodejs.org/\n"
            )
        else:
            sys.stderr.write(
                "blade-ai: the TS TUI bundle was not found.\n"
                "  install: npm install -g @blade-ai/tui\n"
                "  or build from source: npm --prefix tui run build\n"
            )
        sys.exit(1)

    # PyInstaller --onedir / --onefile sets ``sys.frozen``. In that mode
    # there is NO external Python interpreter on the user's PATH (the
    # whole point of curl-bash distribution), so the TS TUI's default
    # ``spawn("python", [...])`` fails with ENOENT. Hand it our own
    # bundled binary path; ``server-process.ts`` reads this env var and
    # invokes ``<bin> __embedded_server__ ...`` to start the FastAPI
    # server instead of ``python -m chaos_agent.server.app ...``.
    if getattr(sys, "frozen", False):
        os.environ["BLADE_AI_SERVER_BIN"] = sys.executable
        # PyInstaller prepends _internal/ to LD_LIBRARY_PATH with its
        # old libstdc++ (manylinux2014). Node needs GLIBCXX_3.4.30+
        # which that old lib doesn't have. Replace LD_LIBRARY_PATH
        # with vendor/node/lib/ (ships Node's own libstdc++) so Node
        # loads the right version regardless of what the system has.
        _node_lib = Path(sys._MEIPASS) / "vendor" / "node" / "lib"
        if _node_lib.is_dir():
            os.environ["LD_LIBRARY_PATH"] = str(_node_lib)
        else:
            _orig_ldpath = os.environ.get("LD_LIBRARY_PATH_ORIG")
            if _orig_ldpath is not None:
                os.environ["LD_LIBRARY_PATH"] = _orig_ldpath
            elif "LD_LIBRARY_PATH" in os.environ:
                del os.environ["LD_LIBRARY_PATH"]

    argv, exec_path = bundle
    if extra_args:
        argv = [*argv, *extra_args]
    try:
        os.execvp(exec_path, argv)
    except OSError as err:
        # exec failure is rare (PATH lied, perms broken). No fallback
        # TUI exists anymore — fail loud with the underlying error.
        sys.stderr.write(f"blade-ai: TS TUI launch failed ({err})\n")
        sys.exit(1)


# Sentinel reasons returned alongside None from _resolve_ts_bundle to
# distinguish "bundle not found" from "bundle found but Node missing".
_REASON_NOT_FOUND = "not_found"
_REASON_NODE_MISSING = "node_missing"


def _resolve_ts_bundle() -> tuple[tuple[list[str], str] | None, str]:
    """Locate an executable form of the TS bundle.

    Returns ``((argv, exec_path), reason)`` where the first element is
    suitable for ``os.execvp``, or ``(None, reason)`` if the bundle
    can't be launched. ``reason`` is one of:
      - ``"not_found"`` — no cli.js exists in any expected location.
      - ``"node_missing"`` — cli.js was found but Node.js is absent.
      - ``"ok"`` — bundle resolved successfully.

    Search order:
      1. ``BLADE_AI_TUI_BIN`` env — explicit override for tests / dev
         workflows pointing at a custom build.
      2. PyInstaller frozen-bundle path (``sys._MEIPASS`` set) — for
         curl-bash users who installed via the standalone binary.
      3. Wheel-embedded asset at ``<chaos_agent>/_tui_assets/cli.js`` —
         the bundle force-included into the wheel by hatch (see
         pyproject.toml). Wheel users hit this path; editable installs
         skip it because force-include only fires during wheel build.
      4. ``blade-ai-tui`` shim in ``PATH`` — the npm-installed binary.
      5. ``<repo>/tui/dist/cli.js`` walked up from this file — the
         in-tree dev build (``npm --prefix tui run build``).

    Wheel-embedded check runs BEFORE the PATH shim because a user who
    `pip install`-ed blade-ai gets a coherent installation; we shouldn't
    silently prefer a stale `npm install -g` shim with a different
    version. A ``.js`` candidate requires Node in ``PATH``; a binary
    candidate is execed directly.
    """
    # Track whether we found a .js file but couldn't exec it (no Node).
    found_js_but_no_node = False

    override = os.environ.get("BLADE_AI_TUI_BIN")
    if override:
        form, needs_node = _exec_form(Path(override))
        if form is not None:
            return form, "ok"
        if needs_node:
            found_js_but_no_node = True

    # PyInstaller onedir/onefile mode sets ``sys._MEIPASS`` to the data
    # root. In practice, PyInstaller (>=6) also rewrites ``__file__`` to
    # live under that root, so the next branch (wheel-embedded via
    # parents[1]) would also resolve correctly — but checking _MEIPASS
    # first is an explicit, version-independent contract.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        frozen = Path(meipass) / "chaos_agent" / "_tui_assets" / "cli.js"
        if frozen.is_file():
            form, needs_node = _exec_form(frozen)
            if form is not None:
                return form, "ok"
            if needs_node:
                found_js_but_no_node = True

    here = Path(__file__).resolve()

    embedded = here.parents[1] / "_tui_assets" / "cli.js"
    if embedded.is_file():
        form, needs_node = _exec_form(embedded)
        if form is not None:
            return form, "ok"
        if needs_node:
            found_js_but_no_node = True

    shim = shutil.which("blade-ai-tui")
    if shim:
        return ([shim], shim), "ok"

    for parent in here.parents:
        candidate = parent / "tui" / "dist" / "cli.js"
        if candidate.is_file():
            form, needs_node = _exec_form(candidate)
            if form is not None:
                return form, "ok"
            if needs_node:
                found_js_but_no_node = True
            break
        # Don't escape past the repo — once we hit the FS root, give up.
        if parent == parent.parent:
            break

    reason = _REASON_NODE_MISSING if found_js_but_no_node else _REASON_NOT_FOUND
    return None, reason


def _find_bundled_node() -> str | None:
    """Return the path to the bundled Node.js binary, if present.

    PyInstaller bundles vendor/node/node into the data root. Check
    there first; this makes the standalone binary self-contained.
    Dev-tree fallback stops at the project root (pyproject.toml).
    """
    node_name = "node.exe" if sys.platform.startswith("win") else "node"

    # PyInstaller frozen mode
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "vendor" / "node" / node_name
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)

    # Dev-tree: walk up from this file, stop at project root
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "vendor" / "node" / node_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        if (parent / "pyproject.toml").is_file():
            break

    return None


def _exec_form(candidate: Path) -> tuple[tuple[list[str], str] | None, bool]:
    """Pack a path into the (argv, exec_path) shape ``execvp`` wants.

    Returns ``(result, needs_node)`` where ``needs_node`` is True when
    the candidate is a .js file but Node.js was not found anywhere
    (neither bundled nor in PATH).
    """
    if not candidate.exists():
        return None, False
    if candidate.suffix == ".js":
        # Prefer bundled node (PyInstaller / vendor), then PATH.
        node = _find_bundled_node() or shutil.which("node")
        if node is None:
            return None, True
        return ([node, str(candidate)], node), False
    if os.access(candidate, os.X_OK):
        return ([str(candidate)], str(candidate)), False
    return None, False


app.command(name="config", help="Manage configuration (mode, API keys, etc.)")(config_command)
# Machine-readable gate for scripts/CI — exit 0 iff all 3 required
# fields (llm_api_key / model_name / api_base_url) resolve to non-empty
# values via Settings. The interactive counterpart lives in the TS
# TUI's in-Ink wizard (HTTP /api/v1/wizard/*); the old standalone
# ``config-wizard`` command shipped with the removed Python TUI.
from chaos_agent.cli.commands.config_check import config_check_command  # noqa: E402
app.command(name="config-check", help="Exit 0 if required config fields are set (machine-readable gate for scripts/CI)")(config_check_command)
app.command(name="inject", help="Inject a fault into a Kubernetes target")(inject_command)
app.command(name="recover", help="Recover a fault injection by task ID")(recover_command)
from chaos_agent.cli.commands.resume_cmd import resume_command  # noqa: E402
app.command(name="resume", help="Start the TUI taking over a previous session (-i <sid>; bare form lists sessions)")(resume_command)
app.command(name="metric", help="Query task status and execution metrics")(metric_command)
app.command(name="list", help="List supported fault capabilities")(list_command)
app.command(name="capabilities-sync", help="Sync: LLM derives commands from each skill case (slow, manual)")(capabilities_sync)
app.command(name="confirm", help="Confirm or reject a pending task")(confirm_command)
app.command(name="version", help="Show version information")(version_command)
app.command(name="update", help="Update blade-ai to the latest version")(update_command)
app.command(name="uninstall", help="Uninstall blade-ai from the system")(uninstall_command)
app.command(name="server", help="Start the blade-ai HTTP API server")(serve_command)
app.command(name="web", help="Start the Web UI locally and open it in a browser")(web_command)


# Hidden subcommand: started by the TS TUI in PyInstaller mode to host
# the embedded FastAPI server. Mirrors the ``python -m chaos_agent.server.app``
# argparse contract (``_cli`` in server/app.py) but routed through the
# bundled blade-ai binary so curl-bash installs don't need an external
# Python on PATH. Kept separate from the public ``server`` above because the
# TUI bridge needs different defaults (loopback host, OS-allocated port).
# The double underscores keep it visually distinct from user-facing
# commands; ``hidden=True`` keeps it out of ``--help``.
@app.command(
    name="__embedded_server__",
    hidden=True,
    help="Internal: launch embedded FastAPI server (TS TUI bridge).",
)
def _embedded_server_command(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(0, "--port"),
    ready_stdout: bool = typer.Option(False, "--ready-stdout"),
) -> None:
    from chaos_agent.server.app import run_server
    # embedded=True disables the server_token gate: this entry point is
    # only ever spawned by the TS TUI on loopback, and the TUI sends no
    # Authorization header (a configured token would 401-block it).
    run_server(host=host, port=port, ready_stdout=ready_stdout, embedded=True)


if __name__ == "__main__":
    app()
