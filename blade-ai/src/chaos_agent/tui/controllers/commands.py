"""CommandDispatcher — slash command registration and dispatch."""

from __future__ import annotations

import logging
import os

from chaos_agent.tui import strings
from chaos_agent.tui.commands import SlashCommandRegistry
from chaos_agent.tui.config_store import ConfigStore
from chaos_agent.tui.state import DisplayMode, SessionState

logger = logging.getLogger(__name__)


def _env_or_dash(var: str) -> str:
    val = os.environ.get(var)
    return val if val else "—"


class CommandDispatcher:
    """Slash command registration and dispatch.

    Dependencies are injected at construction; command handlers
    access them via closure, not a god object.
    """

    def __init__(
        self,
        state: SessionState,
        conversation,  # ConversationController
        config_store: ConfigStore,
        renderer,
        runner=None,
    ) -> None:
        self._state = state
        self._conversation = conversation
        self._config_store = config_store
        self._renderer = renderer
        self._runner = runner
        self._registry = SlashCommandRegistry()
        self._exit_requested = False
        self._register_all()

    def attach_runner(self, runner) -> None:
        """Wire the runner after construction (lazy initialization path)."""
        self._runner = runner

    @property
    def registry(self) -> SlashCommandRegistry:
        return self._registry

    @property
    def exit_requested(self) -> bool:
        return self._exit_requested

    def _register_all(self) -> None:
        # General
        self._registry.register(
            "/help", strings.CMD_HELP_DESC, self._cmd_help, "", group="general"
        )
        self._registry.register(
            "/clear", strings.CMD_CLEAR_DESC, self._cmd_clear, "", group="general"
        )
        self._registry.register(
            "/exit", strings.CMD_EXIT_DESC, self._cmd_exit, "", group="general"
        )
        self._registry.register(
            "/doctor", strings.CMD_DOCTOR_DESC, self._cmd_doctor, "", group="general"
        )
        self._registry.register(
            "/config",
            strings.CMD_CONFIG_DESC,
            self._cmd_config,
            "list | get <k> | set <k> <v> | unset <k> | path",
            group="general",
        )
        self._registry.register_subcommand(
            "/config", "list", "列出当前配置", self._cmd_config_list
        )
        self._registry.register_subcommand(
            "/config", "get", "查看单个配置项", self._cmd_config_get, "<key>"
        )
        self._registry.register_subcommand(
            "/config", "set", "写入并重新加载配置", self._cmd_config_set, "<key> <value>"
        )
        self._registry.register_subcommand(
            "/config", "unset", "删除一个配置项", self._cmd_config_unset, "<key>"
        )
        self._registry.register_subcommand(
            "/config", "path", "打印 config.json 路径", self._cmd_config_path
        )
        self._registry.register_subcommand(
            "/config",
            "wizard",
            "重新打开配置向导（预填当前值）",
            self._cmd_config_wizard,
        )

        self._registry.register(
            "/model",
            strings.CMD_MODEL_DESC,
            self._cmd_model,
            "list | set <name>",
            group="general",
        )
        self._registry.register_subcommand(
            "/model", "list", "列出当前模型与候选", self._cmd_model_list
        )
        self._registry.register_subcommand(
            "/model", "set", "切换 LLM 模型", self._cmd_model_set, "<name>"
        )

        self._registry.register(
            "/compact",
            strings.CMD_COMPACT_DESC,
            self._cmd_compact,
            "",
            group="general",
        )

        # /mode — display-density (PR-D1 §17.1). Each density gets its own
        # subcommand so the slash menu surfaces them as discrete rows; bare
        # ``/mode`` cycles through them.
        self._registry.register(
            "/mode",
            strings.CMD_MODE_DESC,
            self._cmd_mode,
            "[calm|working|dense]",
            group="general",
        )
        self._registry.register_subcommand(
            "/mode",
            "calm",
            strings.DISPLAY_MODE_DESCRIPTIONS["calm"],
            self._cmd_mode_calm,
        )
        self._registry.register_subcommand(
            "/mode",
            "working",
            strings.DISPLAY_MODE_DESCRIPTIONS["working"],
            self._cmd_mode_working,
        )
        self._registry.register_subcommand(
            "/mode",
            "dense",
            strings.DISPLAY_MODE_DESCRIPTIONS["dense"],
            self._cmd_mode_dense,
        )

        self._registry.register(
            "/memory",
            strings.CMD_MEMORY_DESC,
            self._cmd_memory,
            "show | clear | path",
            group="general",
        )
        self._registry.register_subcommand(
            "/memory", "show", "查看会话记忆概要", self._cmd_memory_show
        )
        self._registry.register_subcommand(
            "/memory", "clear", "清理当前 thread 与会话快照", self._cmd_memory_clear
        )
        self._registry.register_subcommand(
            "/memory", "path", "打印 memory_dir", self._cmd_memory_path
        )

        # Business
        self._registry.register(
            "/plan",
            strings.CMD_PLAN_DESC,
            self._cmd_plan,
            "<NL>",
            group="business",
        )
        self._registry.register(
            "/run",
            strings.CMD_RUN_DESC,
            self._cmd_run,
            "[NL]",
            group="business",
        )
        self._registry.register(
            "/recover",
            strings.CMD_RECOVER_DESC,
            self._cmd_recover,
            "<task_id|latest|list>",
            group="business",
        )
        self._registry.register(
            "/review",
            strings.CMD_REVIEW_DESC,
            self._cmd_review,
            "[task_id]",
            group="business",
        )
        self._registry.register(
            "/tasks",
            strings.CMD_TASKS_DESC,
            self._cmd_tasks,
            "[active|failed|all]",
            group="business",
        )
        self._registry.register(
            "/experiments",
            strings.CMD_EXPERIMENTS_DESC,
            self._cmd_experiments,
            "",
            group="business",
        )
        # Locator triad — re-render / re-print / re-issue an [E#] / [T#].
        self._registry.register(
            "/show", strings.CMD_SHOW_DESC, self._cmd_show,
            "<E#|T#>", group="business",
        )
        self._registry.register(
            "/copy", strings.CMD_COPY_DESC, self._cmd_copy,
            "<E#|T#>", group="business",
        )
        self._registry.register(
            "/rerun", strings.CMD_RERUN_DESC, self._cmd_rerun,
            "<E#>", group="business",
        )
        self._registry.register(
            "/expand", strings.CMD_EXPAND_DESC, self._cmd_expand,
            "<T#>", group="business",
        )
        # PR-E3 — recording playback.
        self._registry.register(
            "/replay", strings.CMD_REPLAY_DESC, self._cmd_replay,
            "<task_id> [--instant] [--speed N]", group="business",
        )
        self._registry.register(
            "/recordings", strings.CMD_RECORDINGS_DESC, self._cmd_recordings,
            "[list|export <task_id> <path>]", group="business",
        )
        # /recover gains a `list` subcommand for crash-recovery discovery.
        self._registry.register_subcommand(
            "/recover",
            "list",
            "列出当前所有等待恢复的任务",
            self._cmd_recover_list,
        )

        # Skills
        self._registry.register(
            "/skills",
            strings.CMD_SKILLS_DESC,
            self._cmd_skills,
            "list | show <name> | reload | install <src> | enable <name> | disable <name> | path",
            group="skills",
        )
        self._registry.register_subcommand(
            "/skills", "list", strings.CMD_SKILLS_LIST_DESC, self._cmd_skills_list
        )
        self._registry.register_subcommand(
            "/skills",
            "show",
            strings.CMD_SKILLS_SHOW_DESC,
            self._cmd_skills_show,
            "<name>",
        )
        self._registry.register_subcommand(
            "/skills", "reload", strings.CMD_SKILLS_RELOAD_DESC, self._cmd_skills_reload
        )
        self._registry.register_subcommand(
            "/skills",
            "install",
            strings.CMD_SKILLS_INSTALL_DESC,
            self._cmd_skills_install,
            "<git-url|path>",
        )
        self._registry.register_subcommand(
            "/skills",
            "enable",
            strings.CMD_SKILLS_ENABLE_DESC,
            self._cmd_skills_enable,
            "<name>",
        )
        self._registry.register_subcommand(
            "/skills",
            "disable",
            strings.CMD_SKILLS_DISABLE_DESC,
            self._cmd_skills_disable,
            "<name>",
        )
        self._registry.register_subcommand(
            "/skills", "path", strings.CMD_SKILLS_PATH_DESC, self._cmd_skills_path
        )

        # Deprecated aliases — keep working but nudge users toward the new
        # names. Hidden from /help and the slash menu (still callable).
        self._registry.register(
            "/inject",
            "(已弃用，请使用 /run)",
            self._cmd_inject_deprecated,
            "<描述>",
            group="business",
            hidden=True,
        )
        self._registry.register(
            "/status",
            "(已弃用，请使用 /review)",
            self._cmd_status_deprecated,
            "",
            group="business",
            hidden=True,
        )

    def list_commands(self) -> list[tuple[str, str]]:
        return [(cmd.name, cmd.description) for cmd in self._registry.list_commands()]

    # Commands safe to invoke while a task is streaming. Tuples of
    # ``(root, sub)`` — ``sub`` is "" for root-only matches. Anything
    # outside this set prints a "wait or Ctrl+C" notice and returns.
    _STREAM_SAFE: frozenset[tuple[str, str]] = frozenset({
        ("/exit", ""),
        ("/help", ""),
        ("/tasks", ""),
        ("/review", ""),
        ("/experiments", ""),
        ("/recover", "list"),
        ("/config", "list"),
        ("/skills", "list"),
        ("/skills", "show"),
        ("/memory", "show"),
        # /mode is a UI knob — toggling density mid-stream should not be
        # blocked. Dropping to calm mid-run is exactly when a user wants it.
        ("/mode", ""),
        ("/mode", "calm"),
        ("/mode", "working"),
        ("/mode", "dense"),
        # Locator inspection is read-only — safe to inspect mid-stream.
        # /rerun is *not* listed: re-issuing an experiment while another
        # is streaming would race the conversation thread.
        ("/show", ""),
        ("/copy", ""),
    })

    async def dispatch(self, command_text: str) -> None:
        root, sub, args = self._registry.parse_command(command_text)
        cmd = self._registry.get(root)

        if cmd is None:
            self._renderer.system(
                f"Unknown command: {root}. Type /help for available commands."
            )
            return

        # Streaming gate — block destructive/blocking commands while a
        # task is mid-flight. Read-only inspection commands are allowed.
        if self._state.is_streaming and (root, sub) not in self._STREAM_SAFE:
            logger.info("slash %s %s blocked (streaming)", root, sub)
            self._renderer.system(
                "请等待当前任务结束或 Ctrl+C 中断后再使用此命令"
            )
            return

        # Audit trail.
        logger.info("slash %s %s args=%r", root, sub, args)

        if sub and cmd.subcommands:
            sub_cmd = cmd.subcommands.get(sub)
            if sub_cmd is not None:
                await sub_cmd.handler(args)
                return

        await cmd.handler(args)

    # ── Command handlers ──────────────────────────────────────────

    async def _cmd_help(self, args: str = "") -> None:
        from chaos_agent.tui.renderers import help_panel

        # Live blocks (streaming/thinking/tool/timeline) must release the
        # console before printing a panel, otherwise Rich layers them.
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        help_panel.print_panel(self._renderer.console, self._registry)

    async def _cmd_clear(self, args: str = "") -> None:
        self._renderer.console.clear()
        # End the multi-invocation conversation so the next user message
        # opens a fresh thread; reset the visible token counters and the
        # slash menu's transient state.
        end = getattr(self._conversation, "end_conversation", None)
        if callable(end):
            end()
        self._state.token_count_input = 0
        self._state.token_count_output = 0
        self._state._notify("token_count")
        self._state.slash_selected_index = 0
        self._state.slash_menu_signature = ""

    async def _cmd_exit(self, args: str = "") -> None:
        self._exit_requested = True

    async def _cmd_plan(self, args: str = "") -> None:
        text = (args or "").strip()
        if not text:
            self._renderer.system(
                "Usage: /plan <自然语言描述>\n"
                "  仅生成 Dry-Run 预览；后续 /plan 可继续修改，/run 落地执行。"
            )
            return
        # Dry-Run mode forces a fresh thread so the preview lifecycle is clean.
        await self._conversation.handle_input(text, dry_run=True)

    async def _cmd_run(self, args: str = "") -> None:
        text = (args or "").strip()
        if text:
            # /run <NL> always starts a fresh thread, even if a Dry-Run
            # conversation is currently open — otherwise we'd silently
            # continue a thread whose checkpoint still has dry_run=True
            # and the new injection would never actually execute.
            self._conversation.end_conversation()
            await self._conversation.handle_input(text)
            return
        # No-arg /run: lift the active Dry-Run thread.
        if not self._conversation.in_conversation:
            self._renderer.system(
                "/run 未提供参数，且当前没有进行中的 Dry-Run 会话。\n"
                "请先 /plan <描述> 或直接 /run <描述> 起新任务。"
            )
            return
        is_dry = await self._conversation.is_dry_run_thread()
        if not is_dry:
            self._renderer.system(
                "当前会话不是 Dry-Run，无法直接落地。请使用 /run <描述> 起新任务。"
            )
            return
        await self._conversation.lift_and_run()

    async def _cmd_review(self, args: str = "") -> None:
        if self._runner is None:
            self._renderer.system(strings.SESSION_NOT_INITIALIZED)
            return
        task_id = (args or "").strip() or self._state.active_task_id or self._conversation.last_task_id
        if not task_id:
            # No explicit id: fall back to the most recent task in the store.
            envelope = await self._safe_metric()
            data = (envelope or {}).get("data") or {}
            tasks = data.get("tasks") or []
            if not tasks:
                self._renderer.system(strings.NO_ACTIVE_TASK)
                return
            task_id = tasks[0].get("task_id", "")
            if not task_id:
                self._renderer.system(strings.NO_ACTIVE_TASK)
                return
        envelope = await self._safe_metric(task_id)
        from chaos_agent.tui.renderers import review_panel
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        review_panel.render_review(self._renderer.console, envelope)

    async def _cmd_tasks(self, args: str = "") -> None:
        if self._runner is None:
            self._renderer.system(strings.SESSION_NOT_INITIALIZED)
            return
        from chaos_agent.tui.renderers import tasks_table
        flt = tasks_table.parse_filter(args)
        envelope = await self._safe_metric()
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        tasks_table.render_tasks(self._renderer.console, envelope, filter_=flt)

    async def _cmd_experiments(self, args: str = "") -> None:
        if self._runner is None:
            self._renderer.system(strings.SESSION_NOT_INITIALIZED)
            return
        self._renderer.system("正在加载故障实验目录（首次会调用 LLM 生成）...")
        try:
            envelope = await self._runner.list_skills()
        except Exception as e:
            logger.exception("list_skills failed")
            self._renderer.system(f"加载故障实验失败: {e}")
            return
        from chaos_agent.tui.renderers import experiments_table
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        experiments_table.render_experiments(self._renderer.console, envelope)

    # ── Locator commands (PR-D4) ──────────────────────────────────────

    async def _cmd_show(self, args: str = "") -> None:
        """Re-render the snapshot for an [E#] / [T#] locator."""
        loc = (args or "").strip()
        if not loc:
            self._renderer.system("用法: /show <locator>，例如 /show E1")
            return
        record = self._state.locators.get(loc)
        if record is None:
            self._renderer.system(
                f"找不到 locator '{loc}'。/show 仅能查看本会话已出现的 [E#]/[T#]。"
            )
            return
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        if record.kind == "experiment":
            from chaos_agent.tui.renderers import experiment_card
            from chaos_agent.tui.state import DisplayMode
            fault_intent = record.payload.get("fault_intent") or {}
            # /show is an explicit user request to see the card. If the
            # session is in calm mode the card would otherwise be a no-op
            # (build_card returns None for calm). Force at least working
            # for this one render so the user gets what they asked for —
            # don't pass state so we don't mint a duplicate E#.
            mode = self._state.display_mode
            if mode == DisplayMode.CALM:
                mode = DisplayMode.WORKING
            experiment_card.render(
                self._renderer.console,
                fault_intent,
                display_mode=mode,
            )
        elif record.kind == "tool":
            self._renderer.system(
                f"[{record.locator}] {record.payload.get('tool_name', '')}\n"
                f"{record.payload.get('output', '')}"
            )
        else:
            self._renderer.system(f"[{record.locator}] {record.payload!r}")

    async def _cmd_copy(self, args: str = "") -> None:
        """Print a locator's payload as a copyable text block.

        We don't write to the system clipboard — that requires
        platform-specific tooling (pbcopy / xclip / pyperclip) that's
        unavailable in container/SSH environments where the TUI most
        often runs. Printing a plain-text block lets the user select +
        Cmd-C with the terminal's own copy gesture.
        """
        loc = (args or "").strip()
        if not loc:
            self._renderer.system("用法: /copy <locator>，例如 /copy T3")
            return
        record = self._state.locators.get(loc)
        if record is None:
            self._renderer.system(f"找不到 locator '{loc}'。")
            return
        if record.kind == "experiment":
            fault_intent = record.payload.get("fault_intent") or {}
            import json
            text = json.dumps(fault_intent, ensure_ascii=False, indent=2)
            self._renderer.system(
                f"# {record.locator} fault_intent (复制下方文本)\n{text}"
            )
        elif record.kind == "tool":
            tool = record.payload.get("tool_name", "")
            output = record.payload.get("output", "")
            self._renderer.system(
                f"# {record.locator} {tool} 输出 (复制下方文本)\n{output}"
            )
        else:
            self._renderer.system(f"{record.locator}: {record.payload!r}")

    async def _cmd_rerun(self, args: str = "") -> None:
        """Surface an experiment locator's original NL description for re-issue.

        We deliberately don't auto-execute: re-running a destructive
        experiment without an explicit user prompt is exactly the kind
        of foot-gun the intent_confirm gate exists to prevent. We print
        the prior ``user_description`` (or a synthesised summary) so the
        user can paste-and-edit on the next prompt.
        """
        loc = (args or "").strip()
        if not loc:
            self._renderer.system("用法: /rerun <E#>，例如 /rerun E1")
            return
        record = self._state.locators.get(loc)
        if record is None or record.kind != "experiment":
            self._renderer.system(
                f"找不到实验 locator '{loc}'。/rerun 仅能用于 [E#] 实验卡片。"
            )
            return
        fault_intent = record.payload.get("fault_intent") or {}
        description = fault_intent.get("user_description") or (
            f"在 {fault_intent.get('namespace', 'default')} 重新注入 "
            f"{fault_intent.get('target', 'pod')} {fault_intent.get('action', '')}"
        )
        self._renderer.system(
            f"[{record.locator}] 原始描述：{description}\n"
            "复制上述文本作为下一条输入即可重新发起；将再次经过意图确认。"
        )

    async def _cmd_expand(self, args: str = "") -> None:
        """Re-print the full cached output for a tool locator.

        Pairs with the inline two-line tool result hint, which prints
        ``· /expand <N> 查看全部 (<X> 行)`` after the truncated 70-char
        preview. The handler resolves the locator (accepting ``"1"``,
        ``"T1"``, or ``"T 1"`` for typo tolerance) and re-prints the
        raw cached output so the user can scroll back through it
        without re-running the tool.

        Why this is its own command rather than ``/show`` aliased:
        ``/show T1`` deliberately renders the *summarised* shape of a
        tool call (header + first line) so a long postmortem session
        doesn't spam the scrollback. ``/expand`` is the explicit "give
        me the whole thing" gesture — different intent, distinct
        command.
        """
        raw = (args or "").strip()
        if not raw:
            self._renderer.system(
                "用法: /expand <T#>，例如 /expand T1（可省略 T 写作 1）"
            )
            return

        # Normalise to the canonical T# form. Accept "1", "T1", "t1",
        # "T 1" (a stray space is the kind of typo the hint can produce
        # if a user tab-completes around it). Anything else falls
        # through to the explicit error below.
        normalised = raw.upper().replace(" ", "")
        if normalised.isdigit():
            normalised = f"T{normalised}"

        if not normalised.startswith("T") or not normalised[1:].isdigit():
            self._renderer.system(
                f"locator '{raw}' 无法识别。/expand 仅接受 T# 或纯数字（对应 T#）。"
                "如要查看实验卡片，请用 /show E#。"
            )
            return

        record = self._state.locators.get(normalised)
        if record is None:
            self._renderer.system(
                f"找不到 locator '{normalised}'。/expand 仅能展开本会话已出现的 [T#]。"
            )
            return
        if record.kind != "tool":
            # Cross-kind misroute: tell the user the right command for
            # what they actually have, rather than silently rendering
            # something unhelpful.
            self._renderer.system(
                f"[{record.locator}] 不是工具调用（kind={record.kind}）。"
                f"请用 /show {record.locator}。"
            )
            return

        output = (record.payload.get("output") or "").rstrip()
        tool_name = record.payload.get("tool_name") or "tool"
        elapsed = float(record.payload.get("elapsed") or 0.0)
        status = record.payload.get("status", "")

        if not output:
            self._renderer.system(
                f"[{record.locator}] {tool_name} 无缓存输出（可能是空响应或工具被中断）。"
            )
            return

        # Header line (icon + name + locator + meta) → body. We keep
        # the body rendering deliberately plain so that the user gets
        # the *exact* bytes the tool returned: log lines, kubectl
        # tabular text, JSON envelopes are all preserved verbatim.
        from rich.text import Text
        from chaos_agent.tui.theme import Colors, Icons

        # Drain any in-flight Live regions so the expanded body lands
        # in scrollback cleanly (same contract as the inline result
        # printer in tool_panel.py).
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()

        line_count = sum(1 for ln in output.splitlines() if ln.strip())
        glyph_style = (
            f"bold {Colors.ERROR}" if status == "error"
            else f"bold {Colors.SUCCESS}"
        )
        glyph = Icons.FAIL if status == "error" else Icons.MARKER

        header = Text()
        header.append(f" {glyph} ", style=glyph_style)
        header.append(tool_name, style="bold")
        header.append(f"  [{record.locator}]", style=Colors.DIM)
        meta = f"  ({elapsed:.1f}s · {line_count} 行)"
        header.append(meta, style=Colors.DIM)
        self._renderer.console.print(header)
        # Indent the body two columns to visually subordinate it under
        # the header, matching the ``⎿`` indent the inline two-line
        # form already uses for the preview line.
        for ln in output.splitlines():
            self._renderer.console.print(Text(f"  {ln}"))

    # ── Recording playback (PR-E3) ────────────────────────────────────

    async def _cmd_replay(self, args: str = "") -> None:
        """Re-dispatch a recorded task's events through the live renderer.

        Recordings live at ``<memory_dir>/recordings/<task_id>.jsonl``;
        we resolve the path, parse the events, and let the Replayer
        schedule them with original timing (clamped). The active
        recorder is muted for the duration so the replay doesn't tape
        itself.
        """
        from chaos_agent.config.settings import settings
        from chaos_agent.tui.replay import (
            Replayer,
            parse_replay_args,
            resolve_recording_path,
        )

        task_id, opts = parse_replay_args(args)
        if not task_id:
            self._renderer.system(
                "用法: /replay <task_id> [--instant] [--speed N]"
            )
            return
        path = resolve_recording_path(settings.resolved_memory_dir, task_id)
        if path is None:
            self._renderer.system(
                f"找不到录像 '{task_id}'。/recordings 列出本机已有录像。"
            )
            return
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        self._renderer.system(
            f"▶ 回放 {task_id}"
            + (" · instant" if opts.get("instant") else f" · speed {opts.get('speed', 1.0)}")
        )
        replayer = Replayer(
            self._renderer,
            speed=float(opts.get("speed", 1.0)),
            instant=bool(opts.get("instant", False)),
        )
        try:
            played = await replayer.replay(path)
        except Exception as e:
            logger.exception("replay failed")
            self._renderer.error(f"Replay failed: {e}")
            return
        self._renderer.system(f"■ 回放结束（{played} 条事件）")

    async def _cmd_recordings(self, args: str = "") -> None:
        """List or export recordings.

        Subcommands:
          (no args) | list           — show the most recent N tapes
          export <task_id> <path>    — copy the JSONL to ``path``

        Listing is the common case so we make it the default.
        """
        from chaos_agent.config.settings import settings
        from chaos_agent.tui.replay import (
            export_cast,
            format_meta_row,
            list_recordings,
            resolve_recording_path,
        )

        tokens = (args or "").split()
        sub = tokens[0] if tokens else "list"
        if sub == "list" or sub == "":
            metas = list_recordings(settings.resolved_memory_dir)
            if not metas:
                self._renderer.system("还没有任何录像。任意一次任务都会生成一份。")
                return
            lines = [format_meta_row(m) for m in metas]
            self._renderer.system(
                "可用录像（最新优先）：\n" + "\n".join(f"  {ln}" for ln in lines)
            )
            return
        if sub == "export":
            if len(tokens) < 3:
                self._renderer.system("用法: /recordings export <task_id> <out_path>")
                return
            task_id = tokens[1]
            from pathlib import Path
            out = Path(os.path.expanduser(tokens[2])).resolve()
            src = resolve_recording_path(settings.resolved_memory_dir, task_id)
            if src is None:
                self._renderer.system(f"找不到录像 '{task_id}'。")
                return
            try:
                size = export_cast(src, out)
            except FileExistsError:
                self._renderer.system(
                    f"导出目标已存在：{out}（拒绝覆盖；先移走或换路径）"
                )
                return
            except Exception as e:
                logger.exception("export_cast failed")
                self._renderer.system(f"导出失败：{e}")
                return
            self._renderer.system(f"已导出 {size} 字节到 {out}")
            return
        self._renderer.system(
            f"未知子命令 '{sub}'。用法: /recordings [list|export <task_id> <path>]"
        )

    async def _cmd_recover_list(self, args: str = "") -> None:
        if self._runner is None:
            self._renderer.system(strings.SESSION_NOT_INITIALIZED)
            return
        try:
            tasks = await self._runner.list_interrupted_tasks()
        except Exception as e:
            logger.exception("list_interrupted_tasks failed")
            self._renderer.system(f"查询中断任务失败: {e}")
            return
        if not tasks:
            self._renderer.system(strings.INTERRUPTED_TASKS_NONE)
            return
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        self._renderer.interrupted_tasks(tasks)

    async def _safe_metric(self, task_id: str = "") -> dict:
        try:
            return await self._runner.metric(task_id)
        except Exception as e:
            logger.exception("metric query failed")
            return {"code": 1, "message": str(e), "data": {}}

    async def _cmd_inject_deprecated(self, args: str = "") -> None:
        self._renderer.system("提示: /inject 已重命名为 /run。已为你转交。")
        await self._cmd_run(args)

    async def _cmd_status_deprecated(self, args: str = "") -> None:
        self._renderer.system("提示: /status 已重命名为 /review。已为你转交。")
        await self._cmd_review(args)

    async def _cmd_doctor(self, args: str = "") -> None:
        from chaos_agent.tui.renderers import preflight as preflight_renderer

        self._renderer.system("⏳ 正在检测环境...")
        results, action = await preflight_renderer.run_and_render(self._renderer.console)
        if action == "install_helm":
            await _install_operator_helm(self._renderer)
        elif action == "install_kubectl":
            await _install_operator_kubectl(self._renderer)

    async def _cmd_recover(self, args: str = "") -> None:
        if not args:
            self._renderer.system("Usage: /recover <task_id>\n  /recover latest - recover most recent task")
            return

        task_id = args.strip()
        if task_id == "latest":
            task_id = self._conversation.last_task_id
            if not task_id:
                self._renderer.system("No active or recent task to recover")
                return

        await self._conversation.recover_task(task_id)

    async def _cmd_config(self, args: str = "") -> None:
        # Plain ``/config`` (no sub) — print usage and the canonical path.
        self._renderer.system(
            "Usage:\n"
            "  /config list                — 查看当前配置\n"
            "  /config get <key>           — 查看单项\n"
            "  /config set <key> <value>   — 写入并重新加载\n"
            "  /config unset <key>         — 删除一项\n"
            "  /config path                — 打印 config.json 路径\n"
            "  /config wizard              — 打开配置向导（预填当前值）"
        )

    async def _cmd_config_list(self, args: str = "") -> None:
        display = self._config_store.get_display_dict()
        lines = [f"  {k}: {v}" for k, v in display.items()]
        self._renderer.system("Configuration:\n" + "\n".join(lines))

    async def _cmd_config_get(self, args: str = "") -> None:
        key = args.strip()
        if not key:
            self._renderer.system("Usage: /config get <key>")
            return
        value = self._config_store.get(key)
        if value is None:
            self._renderer.system(f"{key}: (未设置，使用默认值)")
        else:
            self._renderer.system(f"{key}: {value}")

    async def _cmd_config_set(self, args: str = "") -> None:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            self._renderer.system("Usage: /config set <key> <value>")
            return
        key, value = parts[0], parts[1]
        try:
            is_hot = self._config_store.set(key, value)
        except Exception as e:
            self._renderer.system(f"Failed to set config: {e}")
            return
        if is_hot:
            self._renderer.system(f"已写入 {key} = {value}（已热加载）")
        else:
            self._renderer.system(
                f"已写入 {key} = {value}\n注意: 该项需重启 TUI 后生效"
            )

    async def _cmd_config_unset(self, args: str = "") -> None:
        key = args.strip()
        if not key:
            self._renderer.system("Usage: /config unset <key>")
            return
        try:
            removed = self._config_store.unset(key)
        except Exception as e:
            self._renderer.system(f"Failed to unset config: {e}")
            return
        if removed:
            self._renderer.system(f"已删除 {key}")
        else:
            self._renderer.system(f"{key}: 不在 config.json 中")

    async def _cmd_config_path(self, args: str = "") -> None:
        self._renderer.system(self._config_store.path)

    async def _cmd_config_wizard(self, args: str = "") -> None:
        # Edit-mode wizard: prefilled with current settings.* values.
        # Live blocks must release the console first to avoid frame stacking.
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        from chaos_agent.tui.renderers import onboarding

        try:
            saved = await onboarding.run(
                self._renderer.console,
                self._config_store,
                edit_mode=True,
            )
        except Exception as e:
            logger.exception("/config wizard failed")
            self._renderer.system(f"向导异常: {e}")
            return
        if saved:
            self._renderer.system("配置已更新并热加载。")
        else:
            self._renderer.system("已取消向导，配置保持不变。")

    # ── /model ────────────────────────────────────────────────────

    async def _cmd_model(self, args: str = "") -> None:
        from chaos_agent.config.settings import settings as s
        self._renderer.system(
            f"当前模型: {s.model_name}\n"
            "Usage:\n"
            "  /model list           — 查看当前模型与候选\n"
            "  /model set <name>     — 切换模型并热加载"
        )

    async def _cmd_model_list(self, args: str = "") -> None:
        from chaos_agent.config.settings import settings as s
        # Known candidates — surfaced as hints, not enforced. Users may set
        # any model name supported by their api_base_url.
        candidates = [
            "qwen3.6-max-preview",
            "qwen3-max",
            "qwen3-30b",
            "qwen-plus",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "gpt-4o",
            "gpt-4o-mini",
        ]
        active = s.model_name
        lines = ["可用模型 (使用 /model set <name> 切换)："]
        for name in candidates:
            mark = "● " if name == active else "  "
            lines.append(f"  {mark}{name}")
        if active and active not in candidates:
            lines.append(f"  ● {active}  (当前)")
        lines.append("")
        lines.append(f"当前 base_url: {s.api_base_url}")
        self._renderer.system("\n".join(lines))

    async def _cmd_model_set(self, args: str = "") -> None:
        name = args.strip()
        if not name:
            self._renderer.system("Usage: /model set <name>")
            return
        try:
            self._config_store.set("model_name", name)
        except Exception as e:
            self._renderer.system(f"Failed to set model: {e}")
            return
        self._state.set_model_name(name)
        if self._conversation.in_conversation:
            self._renderer.system(
                f"模型已切换为 {name}（注意：当前会话仍使用旧模型，"
                "下一轮 /run 或 /clear 后生效）"
            )
        else:
            self._renderer.system(f"模型已切换为 {name}")

    # ── /mode ─────────────────────────────────────────────────────

    async def _cmd_mode(self, args: str = "") -> None:
        """Set or cycle the display-density mode (PR-D1 §17.1).

        Bare ``/mode`` walks calm → working → dense → calm. ``/mode <name>``
        is the explicit form (subcommand routing also lands here for
        unknown tokens, e.g. ``/mode foo`` falls through to the usage hint
        because the registry treats it as ``args``).
        """
        token = (args or "").strip().lower()
        if not token:
            new_mode = self._state.cycle_display_mode()
            self._announce_display_mode(new_mode)
            return
        try:
            new_mode = DisplayMode(token)
        except ValueError:
            valid = " / ".join(m.value for m in DisplayMode)
            self._renderer.system(
                f"未知的 mode: {token}\nUsage: /mode [{valid}]（不带参数则循环切换）"
            )
            return
        self._state.set_display_mode(new_mode)
        self._announce_display_mode(new_mode)

    async def _cmd_mode_calm(self, args: str = "") -> None:
        self._state.set_display_mode(DisplayMode.CALM)
        self._announce_display_mode(DisplayMode.CALM)

    async def _cmd_mode_working(self, args: str = "") -> None:
        self._state.set_display_mode(DisplayMode.WORKING)
        self._announce_display_mode(DisplayMode.WORKING)

    async def _cmd_mode_dense(self, args: str = "") -> None:
        self._state.set_display_mode(DisplayMode.DENSE)
        self._announce_display_mode(DisplayMode.DENSE)

    def _announce_display_mode(self, mode: DisplayMode) -> None:
        label = strings.DISPLAY_MODE_LABELS.get(mode.value, mode.value)
        desc = strings.DISPLAY_MODE_DESCRIPTIONS.get(mode.value, "")
        msg = f"信息密度: {label} ({mode.value})"
        if desc:
            msg = f"{msg}\n  {desc}"
        self._renderer.system(msg)

    # ── /compact ──────────────────────────────────────────────────

    async def _cmd_compact(self, args: str = "") -> None:
        if self._runner is None or not self._conversation.in_conversation:
            self._renderer.system(
                "当前没有活跃会话可压缩。开始一次 /run 后再执行 /compact。"
            )
            return
        thread_id = self._conversation.conversation_thread_id
        try:
            await self._compact_thread(thread_id)
        except Exception as e:
            logger.exception("compact failed")
            self._renderer.system(f"压缩失败: {e}")

    async def _compact_thread(self, thread_id: str) -> None:
        import asyncio
        from uuid import uuid4

        from chaos_agent.memory.tokens import count_tokens_messages
        from chaos_agent.config.settings import settings as s
        from chaos_agent.observability.status_tracker import (
            subscribe as _status_subscribe,
            unsubscribe as _status_unsubscribe,
        )

        agents = getattr(self._runner, "_agents", {}) or {}
        graph = agents.get("inject")
        if graph is None:
            self._renderer.system("Runner 未初始化 inject 图，无法压缩。")
            return

        # Unified path: drive the SAME PreReasoningHook the auto-trigger
        # uses, with force=True. Eliminates the dual-track risk where
        # manual /compact emitted a HumanMessage(summary) without the
        # "[Compressed History]" prefix, which the auto path then
        # re-compressed on the next turn (information dilution).
        hook = agents.get("pre_reason_hook")
        if hook is None:
            self._renderer.system(
                "PreReasoningHook 未初始化（启动时 LLM/Settings 缺失）。"
            )
            return

        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": s.recursion_limit}
        snapshot = await graph.aget_state(config)
        state_values = snapshot.values or {}
        messages = state_values.get("messages") or []
        # /compact reports raw token counts to the user — match the server
        # path semantics (sessions.py uses .count, not safe_count).
        before = count_tokens_messages(messages).count
        if before == 0:
            self._renderer.system("会话尚无消息可压缩。")
            return

        # Subscribe to a private tracker before invoking the hook so
        # we can render the same live progress the server-side SSE
        # gives the TS TUI. Without this the user sees a multi-second
        # silent pause while compact_memory's LLM call runs. The hook
        # emits to ``state["task_id"]``, so we override it with a
        # fresh id and subscribe to that — isolating us from any
        # ambient auto-compaction events on the long-running task id.
        compact_task_id = f"compact-{uuid4().hex[:12]}"
        queue = _status_subscribe(compact_task_id)

        async def _render_events() -> None:
            """Drain the tracker queue until cancelled. Each hook
            event becomes a system line so the user gets continuous
            feedback during the LLM call."""
            try:
                while True:
                    evt = await queue.get()
                    if getattr(evt, "source", "") != "memory_compression":
                        continue
                    phase = getattr(evt, "phase", "")
                    if phase == "started":
                        self._renderer.system("  LLM 摘要器运行中 …")
                    elif phase == "completed":
                        # Hook-level "completed" carries hook-side
                        # estimates; we print the final authoritative
                        # numbers below from graph state. Stay silent
                        # here to avoid duplicate messages.
                        pass
                    elif phase == "failed":
                        self._renderer.system(
                            f"  压缩失败: {getattr(evt, 'message', 'hook failure')}"
                        )
            except asyncio.CancelledError:
                pass

        render_task = asyncio.create_task(_render_events())
        try:
            state_for_hook = dict(state_values)
            state_for_hook["task_id"] = compact_task_id
            updates = await hook(state_for_hook, force=True)
        finally:
            render_task.cancel()
            try:
                await render_task
            except (asyncio.CancelledError, Exception):
                pass
            _status_unsubscribe(compact_task_id, queue)

        if not updates or "messages" not in updates:
            self._renderer.system(
                f"上下文 {before} tokens，无可压缩的历史消息。"
            )
            return

        await graph.aupdate_state(config, updates)

        # Recompute after by replaying through aget_state — the hook
        # may have emitted both RemoveMessages and the summary, and
        # the reducer's actual result is what the next turn will see.
        snapshot_after = await graph.aget_state(config)
        after = count_tokens_messages(
            (snapshot_after.values or {}).get("messages") or [],
        ).count
        if after >= before:
            self._renderer.system(
                f"上下文 {before} tokens → {after} tokens，未实际缩减。"
            )
            return

        saved = before - after
        pct = saved * 100 // before
        self._renderer.system(
            f"上下文已压缩（LLM 摘要）: {before} → {after} tokens "
            f"(节省 {saved} / {pct}%)"
        )

    # ── /memory ──────────────────────────────────────────────────

    async def _cmd_memory(self, args: str = "") -> None:
        self._renderer.system(
            "Usage:\n"
            "  /memory show     — 查看当前 TUI 会话与最近任务\n"
            "  /memory clear    — 清理当前 thread 与会话快照\n"
            "  /memory path     — 打印 memory_dir"
        )

    async def _cmd_memory_show(self, args: str = "") -> None:
        from chaos_agent.memory.tui_session_store import get_global_tui_session_store
        from chaos_agent.config.settings import settings as s

        store = get_global_tui_session_store()
        sid = self._state.tui_session_id
        if store is None:
            self._renderer.system("TUI session store 未初始化。")
            return
        data = store.read(sid) or {}
        task_ids = data.get("task_ids", [])[-3:]
        stats = data.get("stats", {})
        lines = [
            f"TUI session: {sid}",
            f"  cluster   : {data.get('cluster_name') or '(未设置)'}",
            f"  namespace : {data.get('namespace') or '(未设置)'}",
            f"  started_at: {data.get('started_at')}",
            f"  status    : {data.get('status', 'active')}",
            "",
            "最近任务 (最多 3 条):",
        ]
        if task_ids:
            for t in task_ids:
                lines.append(f"  - {t}")
        else:
            lines.append("  (无)")
        lines.append("")
        lines.append("统计:")
        for k, v in stats.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append(f"memory_dir: {s.resolved_memory_dir}")
        self._renderer.system("\n".join(lines))

    async def _cmd_memory_clear(self, args: str = "") -> None:
        from chaos_agent.memory.tui_session_store import get_global_tui_session_store
        store = get_global_tui_session_store()
        sid = self._state.tui_session_id

        # 1. End current conversation thread.
        end = getattr(self._conversation, "end_conversation", None)
        if callable(end):
            end()
        thread_id = getattr(self._conversation, "conversation_thread_id", "") or ""

        # 2. Drop the thread checkpoint, if a runner is attached.
        cleared_thread = False
        if thread_id and self._runner is not None:
            graph = getattr(self._runner, "_agents", {}).get("inject")
            if graph is not None:
                try:
                    from langchain_core.messages import RemoveMessage
                    cfg = {"configurable": {"thread_id": thread_id}}
                    snap = await graph.aget_state(cfg)
                    msgs = (snap.values or {}).get("messages") or []
                    removals = [RemoveMessage(id=m.id) for m in msgs if getattr(m, "id", None)]
                    if removals:
                        await graph.aupdate_state(cfg, {"messages": removals})
                    cleared_thread = True
                except Exception as e:
                    logger.warning("Failed to clear thread checkpoint: %s", e)

        # 3. Delete the current TUI session file.
        cleared_file = False
        if store is not None:
            try:
                file_path = store.session_dir / f"{sid}.json"
                if file_path.exists():
                    file_path.unlink()
                    cleared_file = True
            except Exception as e:
                logger.warning("Failed to delete session file: %s", e)

        # 4. Reset visible counters.
        self._state.token_count_input = 0
        self._state.token_count_output = 0
        self._state._notify("token_count")

        parts = []
        if cleared_thread:
            parts.append(f"thread {thread_id} 消息已清空")
        if cleared_file:
            parts.append(f"会话快照 {sid}.json 已删除")
        if not parts:
            parts.append("没有可清理的内容")
        self._renderer.system("已清理: " + "；".join(parts))

    async def _cmd_memory_path(self, args: str = "") -> None:
        from chaos_agent.config.settings import settings as s
        self._renderer.system(str(s.resolved_memory_dir))

    # ── /skills ───────────────────────────────────────────────────

    async def _cmd_skills(self, args: str = "") -> None:
        # Bare ``/skills`` — print usage, do not assume a default action.
        self._renderer.system(
            "Usage:\n"
            "  /skills list                    — 列出已加载技能\n"
            "  /skills show <name>             — 查看技能详情\n"
            "  /skills reload                  — 重扫技能目录\n"
            "  /skills install <git|path>      — 仅拷贝文件，不执行 setup\n"
            "  /skills enable <name>           — 启用先前禁用的技能\n"
            "  /skills disable <name>          — 禁用技能（保留文件）\n"
            "  /skills path                    — 打印技能目录解析结果"
        )

    def _require_registry(self) -> bool:
        if self._runner is None or getattr(self._runner, "_registry", None) is None:
            self._renderer.system(strings.SESSION_NOT_INITIALIZED)
            return False
        return True

    async def _cmd_skills_list(self, args: str = "") -> None:
        if not self._require_registry():
            return
        from chaos_agent.config.settings import settings as s
        from chaos_agent.skills.loader import get_skills_dir
        from chaos_agent.tui.renderers import skills_table

        registry = self._runner._registry
        blocked = self._collect_dynamic_conflicts(registry)
        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        skills_table.render_skills_list(
            self._renderer.console,
            registry=registry,
            skills_dir=get_skills_dir(),
            disabled=s.disabled_skills or [],
            builtin_blocked=blocked,
        )

    async def _cmd_skills_show(self, args: str = "") -> None:
        name = (args or "").strip()
        if not name:
            self._renderer.system("Usage: /skills show <name>")
            return
        if not self._require_registry():
            return
        from chaos_agent.tui.renderers import skills_table

        self._renderer.thinking.finalize()
        self._renderer.streamer.finalize()
        skills_table.render_skill_show(
            self._renderer.console,
            registry=self._runner._registry,
            skill_name=name,
        )

    async def _cmd_skills_reload(self, args: str = "") -> None:
        if not self._require_registry():
            return
        from chaos_agent.skills.loader import get_skills_dir

        registry = self._runner._registry
        before = set(registry.list_skills())
        try:
            registry.reload(get_skills_dir())
        except Exception as e:
            logger.exception("skills reload failed")
            self._renderer.system(f"重新加载失败: {e}")
            return
        after = set(registry.list_skills())
        added = sorted(after - before)
        removed = sorted(before - after)
        self.refresh_dynamic_commands()
        parts = [f"已重新扫描 {get_skills_dir()}：共 {len(after)} 项"]
        if added:
            parts.append("新增: " + ", ".join(added))
        if removed:
            parts.append("移除: " + ", ".join(removed))
        self._renderer.system("\n".join(parts))

    async def _cmd_skills_install(self, args: str = "") -> None:
        source = (args or "").strip()
        if not source:
            self._renderer.system("Usage: /skills install <git-url|本地路径>")
            return
        from chaos_agent.skills.installer import install_skill, SkillInstallError

        self._renderer.system(f"正在从 {source} 安装技能（仅拷贝文件，不执行任何脚本）…")
        try:
            installed = await install_skill(source, overwrite=False)
        except SkillInstallError as e:
            self._renderer.system(f"安装失败: {e}")
            return
        except Exception as e:
            logger.exception("skill install failed")
            self._renderer.system(f"安装失败: {e}")
            return
        if not installed:
            self._renderer.system("未发现可用的技能（缺少 SKILL.md 或验证失败）")
            return
        lines = [f"已安装 {len(installed)} 项技能："]
        for sk in installed:
            lines.append(
                f"  • {sk.name}  →  {sk.target_dir}\n"
                f"    SHA256(SKILL.md): {sk.skill_md_sha256[:16]}…  source={sk.source}"
            )
        lines.append("")
        lines.append("使用 /skills reload 让其在 TUI 中生效")
        self._renderer.system("\n".join(lines))

    async def _cmd_skills_enable(self, args: str = "") -> None:
        name = (args or "").strip()
        if not name:
            self._renderer.system("Usage: /skills enable <name>")
            return
        from chaos_agent.config.settings import settings as s

        current = list(s.disabled_skills or [])
        if name not in current:
            self._renderer.system(f"{name} 当前未被禁用")
            return
        current = [n for n in current if n != name]
        try:
            self._config_store.set_many({"disabled_skills": current})
        except Exception as e:
            self._renderer.system(f"写入配置失败: {e}")
            return
        self._renderer.system(
            f"已启用 {name}（写入 disabled_skills）。使用 /skills reload 立刻生效"
        )

    async def _cmd_skills_disable(self, args: str = "") -> None:
        name = (args or "").strip()
        if not name:
            self._renderer.system("Usage: /skills disable <name>")
            return
        from chaos_agent.config.settings import settings as s

        current = list(s.disabled_skills or [])
        if name in current:
            self._renderer.system(f"{name} 已经处于禁用状态")
            return
        current.append(name)
        try:
            self._config_store.set_many({"disabled_skills": current})
        except Exception as e:
            self._renderer.system(f"写入配置失败: {e}")
            return
        # Drop from the live registry so subsequent operations don't see it.
        if self._runner is not None and getattr(self._runner, "_registry", None):
            registry = self._runner._registry
            registry._metadata.pop(name, None)
            registry._skill_dirs.pop(name, None)
            registry._instructions_cache.pop(name, None)
        self.refresh_dynamic_commands()
        self._renderer.system(f"已禁用 {name}（写入 disabled_skills 并从槽位菜单移除）")

    async def _cmd_skills_path(self, args: str = "") -> None:
        from chaos_agent.skills.loader import get_skills_dir
        from chaos_agent.config.settings import settings as s

        resolved = get_skills_dir()
        candidates = [
            ("config.json", str(s.skills_dir)),
            ("env BLADE_AI_SKILLS_DIR", _env_or_dash("BLADE_AI_SKILLS_DIR")),
            ("dev path", str((__import__('pathlib').Path(__file__).resolve().parents[3] / 'skills'))),
        ]
        lines = [f"resolved: {resolved}"]
        lines.append("候选 (按优先级):")
        for label, value in candidates:
            lines.append(f"  - {label}: {value}")
        self._renderer.system("\n".join(lines))

    # ── Dynamic /<skill-name> commands ────────────────────────────

    def refresh_dynamic_commands(self) -> None:
        """Rebuild the dynamic command set from the live registry.

        Call after ``runner.initialize()`` and after any ``/skills reload``,
        ``/skills install``, ``/skills enable/disable``.  Conflicts with
        built-in commands are silently dropped (warning logged) so users
        can rename their skill.
        """
        from chaos_agent.tui.commands import SlashCommand

        if self._runner is None or getattr(self._runner, "_registry", None) is None:
            self._registry.replace_dynamic([])
            return

        registry = self._runner._registry
        new_cmds: list[SlashCommand] = []
        for name in sorted(registry.list_skills()):
            slash = f"/{name}"
            meta = registry.get_metadata(name)
            raw_desc = (meta.description if meta and meta.description else "技能").strip()
            # SKILL.md descriptions are multi-paragraph YAML — keep only
            # the first non-empty line, collapsed onto a single row, so the
            # slash menu doesn't leak the second paragraph into the popup.
            first_para = next(
                (line.strip() for line in raw_desc.splitlines() if line.strip()),
                raw_desc,
            )
            description = " ".join(first_para.split())
            origin = str(registry.get_skill_dir(name) or "")
            handler = self._build_dynamic_handler(name)
            new_cmds.append(
                SlashCommand(
                    name=slash,
                    description=description[:80],
                    handler=handler,
                    usage="<NL args>",
                    group="dynamic",
                    origin=origin,
                )
            )
        self._registry.replace_dynamic(new_cmds)

    def _collect_dynamic_conflicts(self, registry) -> list[str]:
        """Return skill names whose ``/<name>`` collides with a built-in."""
        conflicts: list[str] = []
        for name in registry.list_skills():
            slash = f"/{name}"
            existing = self._registry.get(slash)
            if existing is not None and existing.group != "dynamic":
                conflicts.append(name)
        return conflicts

    def _build_dynamic_handler(self, skill_name: str):
        async def handler(args: str = "") -> None:
            text = (args or "").strip()
            if not text:
                self._renderer.system(
                    f"Usage: /{skill_name} <自然语言描述>"
                )
                return
            payload = f"使用技能 {skill_name}: {text}"
            await self._conversation.handle_input(payload)

        handler.__name__ = f"_dynamic_skill_{skill_name}"
        return handler

# ── Operator install helpers ───────────────────────────────────────


async def _install_operator_helm(renderer) -> None:
    import asyncio

    renderer.system("Installing ChaosBlade Operator via Helm...")
    cmd = (
        "helm repo add chaosblade https://chaosblade-io.github.io/charts && "
        "helm install chaosblade-operator chaosblade/chaosblade-operator "
        "-n chaosblade --create-namespace"
    )
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            renderer.system("ChaosBlade Operator installed successfully via Helm.")
        else:
            error = stderr.decode(errors="replace").strip()[:300]
            renderer.system(f"Helm install failed: {error}")
    except asyncio.TimeoutError:
        renderer.system("Helm install timed out (120s). Try manually.")
    except Exception as e:
        renderer.system(f"Helm install error: {e}")


async def _install_operator_kubectl(renderer) -> None:
    import asyncio
    import os

    renderer.system("Installing ChaosBlade Operator via kubectl apply...")
    blade_path = os.path.expanduser("~/.blade-ai/vendor/chaosblade-operator.yaml")
    from chaos_agent.config.settings import settings as s

    kubeconfig = f" --kubeconfig={s.kubeconfig_path}" if s.kubeconfig_path else ""

    if os.path.isfile(blade_path):
        cmd = f"kubectl apply -f {blade_path}{kubeconfig}"
    else:
        cmd = (
            "kubectl apply -f "
            "https://raw.githubusercontent.com/chaosblade-io/chaosblade-operator/main/deploy/chaosblade-operator.yaml"
            f"{kubeconfig}"
        )

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            renderer.system("ChaosBlade Operator installed via kubectl apply.")
        else:
            error = stderr.decode(errors="replace").strip()[:300]
            renderer.system(f"kubectl apply failed: {error}")
    except asyncio.TimeoutError:
        renderer.system("kubectl apply timed out (120s). Try manually.")
    except Exception as e:
        renderer.system(f"kubectl apply error: {e}")
