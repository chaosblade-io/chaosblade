"""Blade AI TUI — async REPL loop using prompt_toolkit + Rich.

`run_tui()` is the synchronous entry point preserved for `cli/main.py`.
"""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from chaos_agent.tui import strings
from chaos_agent.tui.config_store import ConfigStore
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.controllers.commands import CommandDispatcher
from chaos_agent.tui.controllers.conversation import ConversationController
from chaos_agent.tui.controllers.task_tracker import TaskTracker
from chaos_agent.tui.events import InterruptRequired
from chaos_agent.tui.intent import IntentRouter, IntentType
from chaos_agent.tui.key_bindings import (
    CYCLE_DISPLAY_MODE_SENTINEL,
    CYCLE_MODE_SENTINEL,
)
from chaos_agent.tui.prompt import make_session
from chaos_agent.tui.renderers import Renderer
from chaos_agent.tui.renderers import goodbye as goodbye_renderer
from chaos_agent.tui.renderers import onboarding as onboarding_renderer
from chaos_agent.tui.renderers import preflight as preflight_renderer
from chaos_agent.tui.renderers import welcome as welcome_renderer
from chaos_agent.tui.state import SessionState

logger = logging.getLogger(__name__)


def _first_locator_label(state: SessionState) -> str:
    """Return the earliest allocated locator label, or "" if none.

    Used to drive the PR-E9 "first locator" JIT hint. Experiments come
    before tools because the user is more likely to /show E1 than T1
    immediately after their first injection.
    """
    locators = getattr(state, "locators", None)
    if locators is None:
        return ""
    experiments = locators.list_experiments()
    if experiments:
        return experiments[0].locator
    tools = locators.list_tools()
    if tools:
        return tools[0].locator
    return ""


async def _handle_interrupt(
    event: InterruptRequired,
    *,
    interrupt_handler,
    console: ChaosConsole,
) -> None:
    """Render a confirm or question card via the interrupt handler.

    This is a fallback for the event-based path. In the new multi-invocation
    architecture, interrupts are handled inline by interrupt_callback in the
    stream generator. This handler is kept as a safety net for edge cases
    (e.g., resume without callback).
    """
    # Delegate to the self-contained interrupt handler which renders UI
    # and returns the answer. The answer is logged but not "resolved" anywhere
    # because in the new architecture the callback path handles resumption.
    answer = await interrupt_handler.handle_interrupt(event.interrupt_info)
    logger.debug(f"Fallback interrupt handler resolved with: {answer}")


def _configure_tui_logging() -> None:
    """Route all logging to a rotating file so log output cannot break the TUI.

    Default Python logging writes WARNING+ to stderr, which prompt_toolkit
    cannot patch — those writes corrupt the redrawn prompt frame. We replace
    the root handlers with a single file handler and silence stderr entirely.
    Idempotent: safe to call multiple times.
    """
    from chaos_agent.config.settings import settings

    root = logging.getLogger()
    if getattr(root, "_chaos_tui_logging_configured", False):
        return

    log_dir = settings.resolved_memory_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return  # If we can't create the dir, fall back to default behavior.

    log_path = log_dir / "tui.log"
    try:
        handler = RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    except Exception:
        return

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Drop any existing handlers (Typer/Click may have installed a stderr one).
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)

    level_name = (settings.log_level or "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    # Silence noisy third-party loggers that would otherwise spam the file.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root._chaos_tui_logging_configured = True  # type: ignore[attr-defined]


async def _install_operator(action: str, renderer: Renderer) -> None:
    from chaos_agent.tui.controllers.commands import (
        _install_operator_helm,
        _install_operator_kubectl,
    )

    if action == "install_helm":
        await _install_operator_helm(renderer)
    elif action == "install_kubectl":
        await _install_operator_kubectl(renderer)
    elif action == "skip":
        renderer.system("Operator installation skipped. Use /doctor later to retry.")


async def run_tui_async() -> None:
    _configure_tui_logging()
    console = ChaosConsole()
    state = SessionState()
    config_store = ConfigStore()

    from chaos_agent.config.settings import settings

    # PR-E1 — every dispatched TUIEvent is appended to a per-task JSONL
    # under <memory_dir>/recordings/. The recorder is best-effort; it
    # disables itself on disk error rather than breaking the TUI.
    from chaos_agent.tui.recording import EventRecorder
    recorder = EventRecorder(settings.resolved_memory_dir)
    renderer = Renderer(console, state=state, recorder=recorder)

    # PR-E9 — JIT learning hints. Engine observes turns / errors / mode
    # cycles and emits one-shot tips via ``renderer.system``.
    from chaos_agent.tui.hints import JITHintEngine
    hint_engine = JITHintEngine(state)

    def _maybe_emit(hint: Optional[str]) -> None:
        if hint:
            renderer.system(hint)

    # First-time setup. Trigger condition is the *minimum field set* the
    # intent-recognition node needs to construct an LLM client (see
    # `agent/factory.py:make_llm` — model_name, llm_api_key, api_base_url
    # are all required). If any one is missing we relaunch the wizard on
    # every startup; silently bypassing it would just leave the user
    # stuck at the first message.
    missing = [
        name for name, val in (
            ("llm_api_key", settings.llm_api_key),
            ("model_name", settings.model_name),
            ("api_base_url", settings.api_base_url),
        )
        if not (val or "").strip()
    ]
    if not missing:
        state.set_config_complete(True)
    else:
        logger.info("onboarding wizard triggered; missing fields: %s", missing)
        saved = await onboarding_renderer.run(console, config_store)
        if not saved:
            state.onboarding_skipped = True
        state.set_config_complete(saved)

    # Sync header from settings
    if settings.kube_context:
        state.cluster_name = settings.kube_context

    # Initialize TUI session store and register global so SessionStore can
    # opportunistically index tasks under this TUI session.
    from chaos_agent.memory.tui_session_store import (
        TuiSessionStore,
        set_global_tui_session_store,
    )
    tui_session_store = TuiSessionStore(settings.resolved_memory_dir / "sessions")
    set_global_tui_session_store(tui_session_store)
    tui_session_store.create(
        state.tui_session_id,
        cluster_name=state.cluster_name,
        namespace=state.namespace,
    )

    # Initialize runner (lazy heavy import)
    try:
        from chaos_agent.cli.runner import AgentRunner
        runner = AgentRunner()
        runner._tui_session_store = tui_session_store  # wire session store for dialogue routing
    except Exception as e:
        renderer.error(f"Failed to initialize agent runner: {e}")
        return

    conversation = ConversationController(state=state, runner=runner, renderer=renderer, console=console)
    commands = CommandDispatcher(
        state=state,
        conversation=conversation,
        config_store=config_store,
        renderer=renderer,
        runner=runner,
    )
    task_tracker = TaskTracker(state=state, runner=runner, renderer=renderer)

    # Wire interrupt handling (fallback for non-callback paths, e.g., resume)
    async def _on_interrupt(event: InterruptRequired) -> None:
        await _handle_interrupt(
            event,
            interrupt_handler=conversation.interrupt_handler,
            console=console,
        )

    renderer.set_interrupt_handler(_on_interrupt)
    renderer.set_task_done_handler(lambda _: task_tracker.mark_injection_done())

    # One-shot startup output
    welcome_renderer.print_card(console, state)
    console.print("")
    if state.onboarding_skipped and not state.config_complete:
        renderer.system(strings.WIZARD_SKIPPED_BANNER)

    # Preflight
    try:
        with patch_stdout(raw=True):
            results, action = await preflight_renderer.run_and_render(console)
        if action:
            await _install_operator(action, renderer)
    except Exception as e:
        logger.warning(f"Preflight failed: {e}")

    # Crash recovery
    try:
        await task_tracker.recover_interrupted_tasks(conversation)
    except Exception as e:
        logger.warning(f"Crash recovery scan failed: {e}")

    # Pre-initialize agent runner (loads skills, creates graphs) to avoid
    # delay on the first user message.
    try:
        await runner.initialize()
    except Exception as e:
        logger.warning(f"Agent runner pre-initialization failed (will retry on first use): {e}")

    # Build dynamic /<skill-name> commands from the loaded registry.
    try:
        commands.refresh_dynamic_commands()
    except Exception as e:
        logger.warning(f"Failed to register dynamic skill commands: {e}")

    # Build prompt session
    prompt_session: PromptSession = make_session(commands.registry, state)
    intent_router = IntentRouter()

    # Main REPL
    try:
        while True:
            try:
                with patch_stdout(raw=True):
                    text = await prompt_session.prompt_async()
            except (EOFError, KeyboardInterrupt):
                break

            text = (text or "").strip()
            if not text:
                continue

            # Sentinel for Shift+Tab → cycle permission mode
            if text == CYCLE_MODE_SENTINEL:
                new_mode = state.cycle_permission_mode()
                renderer.system(f"Permission mode: {new_mode.value}")
                continue

            # Sentinel for Ctrl-G → cycle display-density mode (PR-D1)
            if text == CYCLE_DISPLAY_MODE_SENTINEL:
                new_display = state.cycle_display_mode()
                label = strings.DISPLAY_MODE_LABELS.get(
                    new_display.value, new_display.value
                )
                renderer.system(
                    f"信息密度: {label} ({new_display.value})"
                )
                _maybe_emit(hint_engine.on_display_mode_cycled())
                continue

            state.message_count += 1
            # The prompt_toolkit rule + "❯ <text>" already lands in scrollback
            # as the user echo; rendering it again here just duplicates the line.

            intent = intent_router.classify(text)
            if intent == IntentType.SLASH_COMMAND:
                if text.lstrip().lower().startswith("/recover"):
                    state.recovery_count += 1
                try:
                    await commands.dispatch(text)
                except Exception as e:
                    renderer.error(f"Command failed: {e}")
                if commands.exit_requested:
                    break
                continue

            if intent == IntentType.EXIT:
                break

            # Agent input — could be chat / Q&A / recover-bridge / inject.
            # Counters only advance when the turn actually ran the inject
            # pipeline; conversation.last_turn_was_injection answers that
            # after handle_input returns.
            task_tracker.mark_injection_active()
            renderer.begin_task()
            injection_failed = False
            try:
                await conversation.handle_input(text)
            except SystemExit:
                break
            except KeyboardInterrupt:
                injection_failed = True
                renderer.system(strings.INJECTION_CANCELLED)
                try:
                    await conversation.cancel()
                except Exception:
                    pass
            except Exception as e:
                injection_failed = True
                logger.exception("Agent run failed")
                renderer.error(f"Error: {e}")
            finally:
                # On exception we always count it as an attempted injection
                # (the user committed to "do something" and it crashed);
                # on clean return we trust the controller's classification.
                # turn_failed covers the runner-emitted error path (baseline
                # failure / safety rejection) where no exception is raised.
                counted = injection_failed or conversation.last_turn_was_injection
                if counted:
                    state.injection_count += 1
                    if injection_failed or conversation.last_turn_failed:
                        state.injection_fail += 1
                    else:
                        state.injection_success += 1
                task_tracker.mark_injection_done()
                renderer.end_task()

                # PR-E9 — surface JIT hints AFTER the renderer's terminal
                # output for this turn (result panel / error message) so
                # the tip lands in the user's eye without breaking the
                # mid-turn rhythm. Order matters: error hint first (it's
                # the most urgent), locator next (concrete next action),
                # then either streak / injection bookkeeping.
                #
                # render_system already provides its own marginTop blank
                # line; placing a second console.print("") before the hint
                # would stack to 2 blank lines. Instead, we emit the
                # vertical-breath line AFTER the hint section, which
                # serves as: (1) marginBottom for the hint when one was
                # emitted, or (2) marginTop before the prompt when no
                # hint was emitted. Either way the spacing is exactly 1
                # blank line between adjacent blocks.
                if injection_failed or conversation.last_turn_failed:
                    _maybe_emit(hint_engine.on_first_error())
                if counted:
                    hint_engine.on_injection_turn()
                else:
                    _maybe_emit(hint_engine.on_chat_turn())
                first_loc = _first_locator_label(state)
                if first_loc:
                    _maybe_emit(hint_engine.on_first_locator(first_loc))
                # Vertical breath: 1 blank line between output and next
                # input. Placed after hints so it doesn't stack with
                # render_system's own marginTop.
                console.print("")
                try:
                    tui_session_store.update_stats(
                        state.tui_session_id,
                        {
                            "message_count": state.message_count,
                            "injection_count": state.injection_count,
                            "injection_success": state.injection_success,
                            "injection_fail": state.injection_fail,
                            "recovery_count": state.recovery_count,
                        },
                    )
                except Exception:
                    logger.debug("Failed to update TUI session stats", exc_info=True)
    finally:
        renderer.shutdown()
        try:
            await conversation.cleanup()
        except Exception:
            pass
        try:
            tui_session_store.update_env(
                state.tui_session_id,
                cluster_name=state.cluster_name,
                namespace=state.namespace,
            )
            tui_session_store.finalize(state.tui_session_id)
        except Exception:
            logger.debug("Failed to finalize TUI session", exc_info=True)
        try:
            goodbye_renderer.print_card(console, state)
        except Exception:
            pass


def run_tui() -> None:
    """Synchronous entry point preserved for cli/main.py:38."""
    try:
        asyncio.run(run_tui_async())
    except KeyboardInterrupt:
        pass
        pass
