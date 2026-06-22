"""Onboarding wizard — single-panel, stepwise, validated config setup.

Replaces the old sequential ``prompt_async`` flow with a single
``prompt_toolkit.Application`` driving a state machine of ``WizardStep``
instances. Each step renders a consistent Rich panel header (title +
step indicator + description + inline validation status) with a native
prompt_toolkit input area below (text field, password, radio list, or
summary table).

Public entry point: ``await run(console, config_store, edit_mode=False)``.
Returns ``True`` if the user saved configuration, ``False`` if they
cancelled (in which case ``onboarding_skipped_at`` is recorded so the
caller can suppress the prompt next launch).
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import (
    BeforeInput,
    PasswordProcessor,
)
from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.config import wizard_validators
from chaos_agent.tui import strings
from chaos_agent.tui.config_store import ConfigStore
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import BREATHING_DOTS, Theme

# ── Design tokens (sourced from Theme for colorblind-safe consistency) ─
_BRAND = Theme.gradient_bright
_ACCENT = Theme.gradient_mid
_OK = Theme.state_ok       # Okabe-Ito bluish-green
_WARN = Theme.state_warn
_FAIL = Theme.state_err    # Okabe-Ito vermilion
_DIM = Theme.text_secondary
# Wizard panel border — deliberately split from ``_BRAND`` (which
# stays the bright-blue used for title text + table headers) so the
# wizard's outer chrome matches the TS TUI's WelcomeCard /
# BootDoctorCard border colour. The TS side uses
# ``Theme.text.accent = "#D88A2E"`` (Forged Amber); we mirror the
# literal hex here so a user launching ``blade-ai`` (TS) and falling
# into the Python wizard subprocess sees a single visual identity for
# all boot-time chrome instead of one blue card sandwiched between
# amber neighbours. Token-only colour, intentional duplication of the
# hex with TS — keep them in sync if the TS palette ever shifts.
_BORDER = "#D88A2E"


# ── Data types ─────────────────────────────────────────────────────────


class StepKind(str, Enum):
    ENTER = "enter"        # static page, only Enter advances
    TEXT = "text"          # single-line text input
    PASSWORD = "password"  # masked text input
    RADIO = "radio"        # arrow-selectable list
    SUMMARY = "summary"    # final review table


@dataclass
class StepResult:
    status: str = "ok"   # ok | warn | error
    message: str = ""    # rendered inline under the description
    block: bool = False  # if True, Enter does not advance


@dataclass
class RadioOption:
    value: Any
    label: str
    note: str = ""


@dataclass
class WizardCtx:
    config_store: ConfigStore
    edit_mode: bool
    updates: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    discovered_contexts: list[str] = field(default_factory=list)


@dataclass
class WizardStep:
    """One step in the wizard. ``key`` is the config field name; empty for
    pure UI steps (welcome, summary)."""
    key: str
    kind: StepKind
    title: str
    description_fn: Callable[[WizardCtx], str]
    smart_default_fn: Optional[Callable[[WizardCtx], Any]] = None
    radio_options_fn: Optional[Callable[[WizardCtx], list[RadioOption]]] = None
    validate_async: Optional[
        Callable[[Any, WizardCtx], Awaitable[StepResult]]
    ] = None
    skip_if: Optional[Callable[[WizardCtx], bool]] = None
    on_advance: Optional[Callable[[Any, WizardCtx], Awaitable[None]]] = None


# ── Helpers ────────────────────────────────────────────────────────────


def _terminal_width() -> int:
    try:
        return min(shutil.get_terminal_size((80, 24)).columns, 92)
    except Exception:
        return 80


def _render_rich_to_ansi(renderable: Any, width: int) -> str:
    """Render any Rich renderable into ANSI-coloured plain text."""
    buf = io.StringIO()
    try:
        c = RichConsole(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            width=width,
            highlight=False,
            soft_wrap=False,
        )
        c.print(renderable)
    except Exception:
        return ""
    return buf.getvalue().rstrip("\n")


def _settings_snapshot() -> dict:
    """Capture current settings.* values for edit-mode prefill."""
    from chaos_agent.config.settings import settings as s

    return {
        "llm_api_key": s.llm_api_key or "",
        "model_name": s.model_name or "",
        "api_base_url": s.api_base_url or "",
        "kubeconfig_path": s.kubeconfig_path or "",
        "kube_context": s.kube_context or "",
        "confirmation_required": bool(s.confirmation_required),
    }


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return secret[:4] + ("•" * 24) + secret[-4:]


# ── Smart defaults & validators ────────────────────────────────────────


def _default_api_key(ctx: WizardCtx) -> str:
    if ctx.edit_mode and ctx.snapshot.get("llm_api_key"):
        return ctx.snapshot["llm_api_key"]
    for env in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "BLADE_AI_LLM_API_KEY"):
        v = os.environ.get(env)
        if v:
            return v
    return ""


def _default_api_url(ctx: WizardCtx) -> str:
    if ctx.edit_mode and ctx.snapshot.get("api_base_url"):
        return ctx.snapshot["api_base_url"]
    # Pick URL based on already-chosen model (if any), else dashscope
    model = (ctx.updates.get("model_name") or "").lower()
    if model.startswith(("qwen", "qwq")):
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    if model.startswith(("gpt", "openai")):
        return "https://api.openai.com/v1"
    if model.startswith("deepseek"):
        return "https://api.deepseek.com"
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _default_model(ctx: WizardCtx) -> str:
    if ctx.edit_mode and ctx.snapshot.get("model_name"):
        return ctx.snapshot["model_name"]
    # If api key came from DASHSCOPE_*, prefer qwen; else qwen too (project default)
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("DASHSCOPE_API_KEY"):
        return "gpt-4o"
    return "qwen3.6-max-preview"


def _default_kubeconfig(ctx: WizardCtx) -> str:
    if ctx.edit_mode and ctx.snapshot.get("kubeconfig_path"):
        return ctx.snapshot["kubeconfig_path"]
    env = os.environ.get("KUBECONFIG") or os.environ.get(
        "BLADE_AI_KUBECONFIG_PATH"
    )
    if env and os.path.isfile(env.split(":")[0]):
        return env.split(":")[0]
    home = os.path.expanduser("~/.kube/config")
    if os.path.isfile(home):
        return home
    return ""


def _default_permission(ctx: WizardCtx) -> bool:
    if ctx.edit_mode:
        return bool(ctx.snapshot.get("confirmation_required", True))
    return True


async def _validate_api_key(value: Any, ctx: WizardCtx) -> StepResult:
    """Thin wrapper — defers to ``config.wizard_validators.validate_api_key``.

    The implementation moved out so the same logic backs the TS
    Ink wizard (via ``POST /api/v1/wizard/validate/api-key``) and the
    standalone CLI subcommand. We keep the StepResult shape here so
    the WizardStep contract is unchanged.
    """
    base_url = ctx.updates.get("api_base_url") or _default_api_url(ctx)
    model = ctx.updates.get("model_name") or _default_model(ctx)
    result = await wizard_validators.validate_api_key(
        api_key=value, base_url=base_url, model=model,
    )
    return StepResult(
        status=result.status, message=result.message, block=result.block,
    )


async def _validate_api_url(value: Any, ctx: WizardCtx) -> StepResult:
    """Thin wrapper — defers to ``config.wizard_validators.validate_api_url``."""
    result = await wizard_validators.validate_api_url(value)
    return StepResult(
        status=result.status, message=result.message, block=result.block,
    )


async def _validate_kubeconfig(value: Any, ctx: WizardCtx) -> StepResult:
    """Thin wrapper around ``validate_kubeconfig``.

    The validator returns the discovered context list in
    ``metadata.contexts``; we copy it onto ``ctx.discovered_contexts``
    here so the existing next-step radio-options builder still finds
    it where it expects.
    """
    result = await wizard_validators.validate_kubeconfig(value)
    contexts = result.metadata.get("contexts") if result.metadata else None
    if isinstance(contexts, list):
        ctx.discovered_contexts = [str(c) for c in contexts]
    return StepResult(
        status=result.status, message=result.message, block=result.block,
    )


async def _discover_kube_contexts(kubeconfig_path: str) -> list[str]:
    """Thin wrapper around ``wizard_validators.discover_kube_contexts``."""
    return await wizard_validators.discover_kube_contexts(kubeconfig_path)


def _kube_context_options(ctx: WizardCtx) -> list[RadioOption]:
    opts: list[RadioOption] = [
        RadioOption(value="", label="(使用 kubeconfig 当前 context)", note="默认"),
    ]
    for name in ctx.discovered_contexts:
        opts.append(RadioOption(value=name, label=name))
    return opts


def _model_options(ctx: WizardCtx) -> list[RadioOption]:
    return [
        RadioOption(
            value="qwen3.6-max-preview",
            label="qwen3.6-max-preview",
            note="阿里 · 推荐",
        ),
        RadioOption(value="qwen3-max", label="qwen3-max", note="阿里"),
        RadioOption(value="gpt-4o", label="gpt-4o", note="OpenAI"),
        RadioOption(value="deepseek-chat", label="deepseek-chat", note="DeepSeek"),
    ]


def _permission_options(ctx: WizardCtx) -> list[RadioOption]:
    return [
        RadioOption(value=True, label="✗ 确认模式", note="每次注入前手动确认（推荐）"),
        RadioOption(value=False, label="✻ 自动模式", note="跳过确认（仅限受控环境）"),
    ]


# ── Step descriptions ──────────────────────────────────────────────────


def _desc_welcome(ctx: WizardCtx) -> str:
    if ctx.edit_mode:
        return (
            "已检测到现有配置，向导将以当前值预填。\n"
            "按 Enter 开始；Esc 可在任意步取消。"
        )
    return (
        "完成几步配置即可使用 Blade AI。\n"
        "全程使用方向键与 Enter 操作；Esc 取消并稍后通过 /config wizard 重新配置。"
    )


def _desc_api_key(ctx: WizardCtx) -> str:
    hits = []
    for env in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "BLADE_AI_LLM_API_KEY"):
        if os.environ.get(env):
            hits.append(env)
    target = ctx.updates.get("api_base_url") or _default_api_url(ctx)
    line = f"将校验 key 是否能访问：{target}"
    if hits:
        line += f"\n已检测：{', '.join(hits)} ✓（默认已填入第一个）"
    return line


def _desc_model(ctx: WizardCtx) -> str:
    return (
        "选择默认推理模型。可在使用过程中通过 /model set <name> 切换。"
    )


def _desc_api_url(ctx: WizardCtx) -> str:
    return (
        "OpenAI 兼容 API 端点。已根据所选模型给出推荐值，可按需修改。"
    )


def _desc_kubeconfig(ctx: WizardCtx) -> str:
    line = "Blade AI 通过 kubectl 操作集群，需要一个可用的 kubeconfig。"
    env = os.environ.get("KUBECONFIG")
    if env:
        line += f"\n已检测：环境变量 KUBECONFIG={env}"
    elif os.path.isfile(os.path.expanduser("~/.kube/config")):
        line += "\n已检测：~/.kube/config 存在"
    return line


def _desc_kube_context(ctx: WizardCtx) -> str:
    n = len(ctx.discovered_contexts)
    return f"在 kubeconfig 中找到 {n} 个上下文，请选择默认使用的一个。"


def _desc_permission(ctx: WizardCtx) -> str:
    return (
        "确认模式（推荐）：每次故障注入前需要确认；\n"
        "自动模式：跳过确认直接执行（仅在受控环境使用）。\n"
        "运行中可随时通过 Shift+Tab 切换。"
    )


def _desc_summary(ctx: WizardCtx) -> str:
    return "请检查以下配置；按 Enter 保存并启动，按 E 返回修改，按 Esc 取消。"


# ── Step list ──────────────────────────────────────────────────────────


def _build_steps() -> list[WizardStep]:
    # Order matters: model_name → api_base_url → llm_api_key.
    # The API key validator does a live `models.list()` call, which needs
    # the *correct* base URL. Asking for the key before the URL would force
    # us to ping a fallback endpoint and report misleading "401 invalid key"
    # errors when the user's key is actually fine but for a different
    # provider. By picking model → confirming URL → then validating the
    # key, the live check hits the endpoint the user actually intends.
    return [
        WizardStep(
            key="",
            kind=StepKind.ENTER,
            title="欢迎",
            description_fn=_desc_welcome,
        ),
        WizardStep(
            key="model_name",
            kind=StepKind.RADIO,
            title="默认模型",
            description_fn=_desc_model,
            radio_options_fn=_model_options,
            smart_default_fn=_default_model,
        ),
        WizardStep(
            key="api_base_url",
            kind=StepKind.TEXT,
            title="API Base URL",
            description_fn=_desc_api_url,
            smart_default_fn=_default_api_url,
            validate_async=_validate_api_url,
        ),
        WizardStep(
            key="llm_api_key",
            kind=StepKind.PASSWORD,
            title="LLM API Key",
            description_fn=_desc_api_key,
            smart_default_fn=_default_api_key,
            validate_async=_validate_api_key,
        ),
        WizardStep(
            key="kubeconfig_path",
            kind=StepKind.TEXT,
            title="Kubeconfig 路径",
            description_fn=_desc_kubeconfig,
            smart_default_fn=_default_kubeconfig,
            validate_async=_validate_kubeconfig,
        ),
        WizardStep(
            key="kube_context",
            kind=StepKind.RADIO,
            title="Kubernetes Context",
            description_fn=_desc_kube_context,
            radio_options_fn=_kube_context_options,
            skip_if=lambda c: len(c.discovered_contexts) <= 1,
        ),
        WizardStep(
            key="confirmation_required",
            kind=StepKind.RADIO,
            title="权限模式",
            description_fn=_desc_permission,
            radio_options_fn=_permission_options,
            smart_default_fn=_default_permission,
        ),
        WizardStep(
            key="",
            kind=StepKind.SUMMARY,
            title="确认并保存",
            description_fn=_desc_summary,
        ),
    ]


# ── Wizard application ────────────────────────────────────────────────


class WizardApp:
    """Drives a list of WizardSteps inside a single prompt_toolkit Application.

    Layout: header (Rich panel) + input area (native pt) + footer hint.
    """

    SPINNER = BREATHING_DOTS

    def __init__(self, console: ChaosConsole, ctx: WizardCtx, steps: list[WizardStep]):
        self.console = console
        self.ctx = ctx
        self.steps = steps
        self.idx = 0
        self.text_buffer = Buffer(multiline=False)
        self.radio_idx = 0
        self.last_result: Optional[StepResult] = None
        self.is_validating = False
        self.spinner_task: Optional[asyncio.Task] = None
        self.spinner_pos = 0
        self.cancelled = False
        self.saved = False
        self.app: Optional[Application] = None
        self._build_app()
        self._enter_step(0)

    # ── Layout construction ────────────────────────────────────────

    def _build_app(self) -> None:
        kb = KeyBindings()

        @kb.add("enter")
        def _enter(event):
            asyncio.ensure_future(self._handle_enter())

        @kb.add("escape", eager=True)
        def _esc(event):
            self._cancel()

        @kb.add("c-c")
        def _ctrl_c(event):
            self._cancel(force_quit=True)

        @kb.add("up")
        def _up(event):
            if self._is_radio():
                self._radio_move(-1)

        @kb.add("down")
        def _down(event):
            if self._is_radio():
                self._radio_move(1)

        @kb.add("s-tab")
        def _stab(event):
            self._goto_prev()

        @kb.add("c-b")
        def _cb(event):
            self._goto_prev()

        # Summary-only shortcut: 'e' returns to the first editable step
        @kb.add("e", filter=Condition(lambda: self.steps[self.idx].kind == StepKind.SUMMARY))
        def _edit(event):
            # Walk back to the first non-static step
            for i, s in enumerate(self.steps):
                if s.kind not in (StepKind.ENTER, StepKind.SUMMARY) and not (
                    s.skip_if and s.skip_if(self.ctx)
                ):
                    self._enter_step(i)
                    return

        header = Window(
            content=FormattedTextControl(
                text=lambda: self._render_header(), focusable=False
            ),
            height=lambda: self._header_height(),
            wrap_lines=False,
        )
        text_input = Window(
            content=BufferControl(
                buffer=self.text_buffer,
                # Order matters: mask the buffer first, THEN prepend the
                # prompt arrow — otherwise "  ❯ " gets masked into "****".
                input_processors=[
                    PasswordProcessor(),
                    BeforeInput(text="  ❯ "),
                ],
                focusable=True,
            ),
            height=1,
        )
        text_input_plain = Window(
            content=BufferControl(
                buffer=self.text_buffer,
                input_processors=[BeforeInput(text="  ❯ ")],
                focusable=True,
            ),
            height=1,
        )
        radio_window = Window(
            content=FormattedTextControl(
                text=lambda: self._render_radio(), focusable=False
            ),
            height=lambda: self._radio_height(),
        )
        summary_window = Window(
            content=FormattedTextControl(
                text=lambda: self._render_summary(), focusable=False
            ),
            height=lambda: self._summary_height(),
        )
        footer = Window(
            content=FormattedTextControl(
                text=lambda: self._render_footer(), focusable=False
            ),
            height=1,
        )

        layout = Layout(
            HSplit(
                [
                    header,
                    ConditionalContainer(text_input, Condition(self._is_password)),
                    ConditionalContainer(text_input_plain, Condition(self._is_text)),
                    ConditionalContainer(radio_window, Condition(self._is_radio)),
                    ConditionalContainer(summary_window, Condition(self._is_summary)),
                    footer,
                ]
            )
        )

        self.app = Application(
            layout=layout,
            key_bindings=kb,
            full_screen=False,
            mouse_support=False,
            erase_when_done=True,
        )
        # Focus a buffer so the cursor shows in text/password steps; no-op for radio.
        try:
            layout.focus(text_input)
        except Exception:
            pass

    # ── Step navigation ────────────────────────────────────────────

    def _enter_step(self, idx: int) -> None:
        # Skip-aware advance/return: caller already resolved a non-skip step.
        self.idx = idx
        s = self.steps[idx]
        self.last_result = None
        if s.kind in (StepKind.TEXT, StepKind.PASSWORD):
            default = ""
            if s.smart_default_fn:
                try:
                    default = s.smart_default_fn(self.ctx) or ""
                except Exception:
                    default = ""
            if s.key in self.ctx.updates:
                default = str(self.ctx.updates[s.key] or "")
            self.text_buffer.document = Document(text=str(default))
        elif s.kind == StepKind.RADIO:
            options = s.radio_options_fn(self.ctx) if s.radio_options_fn else []
            self.radio_idx = 0
            target = self.ctx.updates.get(s.key)
            if target is None and s.smart_default_fn:
                try:
                    target = s.smart_default_fn(self.ctx)
                except Exception:
                    target = None
            for i, o in enumerate(options):
                if o.value == target:
                    self.radio_idx = i
                    break
        # Pre-compute header height so layout never sees a stale cache.
        # prompt_toolkit calls `_header_height()` during the layout phase
        # BEFORE `_render_header()` in the draw phase — without this priming
        # the first frame after a navigation can clip the bottom border.
        self._redraw()

    def _goto_next(self) -> None:
        n = len(self.steps)
        i = self.idx + 1
        while i < n:
            s = self.steps[i]
            if s.skip_if and s.skip_if(self.ctx):
                i += 1
                continue
            self._enter_step(i)
            return
        # Past the end (defensive — SUMMARY handles save itself)
        asyncio.ensure_future(self._save_and_exit())

    def _goto_prev(self) -> None:
        i = self.idx - 1
        while i >= 0:
            s = self.steps[i]
            if s.skip_if and s.skip_if(self.ctx):
                i -= 1
                continue
            self._enter_step(i)
            return

    def _radio_move(self, delta: int) -> None:
        s = self.steps[self.idx]
        options = s.radio_options_fn(self.ctx) if s.radio_options_fn else []
        if not options:
            return
        self.radio_idx = (self.radio_idx + delta) % len(options)
        # Radio movement does not change header height, but use the same
        # path for consistency.
        self._redraw()

    # ── Enter / validate / save ───────────────────────────────────

    async def _handle_enter(self) -> None:
        if self.is_validating:
            return
        s = self.steps[self.idx]
        # Collect value for this step
        value: Any
        if s.kind == StepKind.RADIO:
            options = s.radio_options_fn(self.ctx) if s.radio_options_fn else []
            if not options:
                self._goto_next()
                return
            value = options[self.radio_idx].value
        elif s.kind in (StepKind.TEXT, StepKind.PASSWORD):
            value = self.text_buffer.text.strip()
        elif s.kind == StepKind.SUMMARY:
            await self._save_and_exit()
            return
        else:  # ENTER
            value = None

        # Validate (if any). Spinner only for async validators.
        if s.validate_async:
            self.is_validating = True
            self._start_spinner()
            self._redraw()
            try:
                self.last_result = await s.validate_async(value, self.ctx)
            except Exception as e:
                self.last_result = StepResult(
                    status="warn", message=f"校验异常: {e}", block=False
                )
            finally:
                self.is_validating = False
                self._stop_spinner()
                self._redraw()
            if self.last_result and self.last_result.block:
                return

        # Save value to updates
        if s.key:
            if s.kind == StepKind.RADIO:
                self.ctx.updates[s.key] = value
            elif isinstance(value, str) and value:
                self.ctx.updates[s.key] = value
            elif isinstance(value, str) and not value:
                # Blank text → drop any prior value so settings default applies
                self.ctx.updates.pop(s.key, None)

        self._goto_next()

    async def _save_and_exit(self) -> None:
        try:
            # Build a complete config dict, not just the handful of
            # fields the wizard explicitly asks about.
            #
            # Precedence (last wins):
            #   1. DEFAULTS (cli/config_manager.py) — every recognised
            #      key with its built-in default. Ensures a brand-new
            #      ~/.blade-ai/config.json is fully populated so users
            #      who later open the file can see all knobs and
            #      Settings doesn't have to silently fall back for
            #      keys the wizard never touched.
            #   2. Existing config.json — anything the user previously
            #      set via ``blade-ai config set`` or a prior wizard
            #      run survives untouched.
            #   3. Wizard updates — the values the user just entered.
            from chaos_agent.cli.config_manager import DEFAULTS

            existing = self.ctx.config_store.read_all()
            merged: dict = {**DEFAULTS, **existing, **dict(self.ctx.updates)}
            self.ctx.config_store.set_many(merged)
            # Clean up legacy marker if any remained from a previous version.
            try:
                self.ctx.config_store.unset("onboarding_skipped_at")
            except Exception:
                pass
            self.saved = True
        except Exception as e:
            self.last_result = StepResult(
                status="error", message=f"保存失败: {e}", block=True
            )
            self._redraw()
            return
        if self.app:
            try:
                self.app.exit()
            except Exception:
                pass

    def _cancel(self, force_quit: bool = False) -> None:
        # Do NOT persist a "skipped" marker: without llm_api_key the app
        # can't function, so silently bypassing the wizard on subsequent
        # launches would just leave the user stuck. Treat cancel as a
        # session-only dismissal.
        self.cancelled = True
        self.saved = False
        if self.app:
            try:
                self.app.exit()
            except Exception:
                pass

    # ── Spinner ────────────────────────────────────────────────────

    def _start_spinner(self) -> None:
        if self.spinner_task and not self.spinner_task.done():
            return

        async def _tick():
            while self.is_validating:
                self.spinner_pos = (self.spinner_pos + 1) % len(self.SPINNER)
                self._redraw()
                await asyncio.sleep(0.1)

        try:
            self.spinner_task = asyncio.ensure_future(_tick())
        except Exception:
            self.spinner_task = None

    def _stop_spinner(self) -> None:
        t = self.spinner_task
        if t and not t.done():
            t.cancel()
        self.spinner_task = None

    # ── Step-kind filters ─────────────────────────────────────────

    def _kind(self) -> StepKind:
        return self.steps[self.idx].kind

    def _is_password(self) -> bool:
        return self._kind() == StepKind.PASSWORD

    def _is_text(self) -> bool:
        return self._kind() == StepKind.TEXT

    def _is_radio(self) -> bool:
        return self._kind() == StepKind.RADIO

    def _is_summary(self) -> bool:
        return self._kind() == StepKind.SUMMARY

    # ── Step counting ─────────────────────────────────────────────

    def _visible_total(self) -> int:
        return sum(
            1
            for s in self.steps
            if not (s.skip_if and s.skip_if(self.ctx))
        )

    def _visible_position(self) -> int:
        pos = 0
        for i, s in enumerate(self.steps):
            if s.skip_if and s.skip_if(self.ctx):
                continue
            pos += 1
            if i == self.idx:
                return pos
        return pos

    # ── Rendering ─────────────────────────────────────────────────

    def _header_height(self) -> int:
        # Use the actually-rendered panel's line count so we don't leave a
        # giant blank below the bottom border. Falls back to 6 on first
        # frame (before _render_header has run).
        return getattr(self, "_header_height_cached", 6) or 6

    def _radio_height(self) -> int:
        s = self.steps[self.idx]
        n = len(s.radio_options_fn(self.ctx)) if s.radio_options_fn else 0
        return max(2, n + 1)

    def _summary_height(self) -> int:
        return min(20, 6 + len(self.ctx.updates))

    def _build_header_panel(self) -> Panel:
        s = self.steps[self.idx]
        title_label = (
            strings.WIZARD_TITLE_EDIT if self.ctx.edit_mode else strings.WIZARD_TITLE_FIRST
        )
        step_label = f"Step {self._visible_position()} / {self._visible_total()}"

        body = Text()
        body.append(s.title, style=f"bold {_BRAND}")
        try:
            desc = s.description_fn(self.ctx)
        except Exception:
            desc = ""
        desc_lines = [ln for ln in desc.split("\n") if ln.strip()]
        for line in desc_lines:
            body.append("\n")
            body.append(line, style=_DIM)

        status_text = self._render_status_text()
        if status_text is not None:
            body.append("\n")
            body.append_text(status_text)

        title_renderable = Text()
        title_renderable.append("✻ ", style=f"bold {_ACCENT}")
        title_renderable.append(title_label, style=f"bold {_BRAND}")
        title_renderable.append("   ", style=_DIM)
        title_renderable.append(step_label, style=_DIM)

        return Panel(
            body,
            title=title_renderable,
            title_align="left",
            # Border colour intentionally diverges from ``_BRAND`` —
            # see _BORDER docstring for the visual-consistency
            # rationale (matches TS TUI WelcomeCard / BootDoctorCard
            # amber instead of the Python brand blue).
            border_style=_BORDER,
            padding=(0, 1),
        )

    def _refresh_header_cache(self) -> None:
        """Render the header panel and cache both the ANSI string and the
        line count. Layout queries the line count BEFORE the draw phase
        renders, so we must keep them in lock-step."""
        panel = self._build_header_panel()
        rendered = _render_rich_to_ansi(panel, _terminal_width())
        self._header_text_cached = rendered
        self._header_height_cached = rendered.count("\n") + 1

    def _redraw(self) -> None:
        """Refresh the cached panel size THEN invalidate. Use this at every
        state mutation site so layout never reads a stale height."""
        self._refresh_header_cache()
        if self.app:
            self.app.invalidate()

    def _render_header(self) -> ANSI:
        # Always re-render so dynamic content (spinner, status) updates.
        self._refresh_header_cache()
        return ANSI(self._header_text_cached)

    def _render_status_text(self) -> Optional[Text]:
        if self.is_validating:
            ch = self.SPINNER[self.spinner_pos % len(self.SPINNER)]
            t = Text()
            t.append(ch + " ", style=f"bold {Theme.gradient_bright}")
            t.append("校验中…", style=f"italic {Theme.gradient_dim}")
            return t
        r = self.last_result
        if not r or not r.message:
            return None
        if r.status == "ok":
            mark, color = "✓", _OK
        elif r.status == "warn":
            mark, color = "⚠", _WARN
        else:
            mark, color = "✗", _FAIL
        t = Text()
        t.append(f"{mark} ", style=f"bold {color}")
        t.append(r.message, style=color)
        return t

    def _render_radio(self) -> ANSI:
        s = self.steps[self.idx]
        options = s.radio_options_fn(self.ctx) if s.radio_options_fn else []
        if not options:
            return ANSI("")
        lines: list[str] = []
        for i, opt in enumerate(options):
            marker = "▸" if i == self.radio_idx else " "
            color_open = "\033[1;38;2;79;195;247m" if i == self.radio_idx else ""
            color_close = "\033[0m" if color_open else ""
            note = f"  \033[2m({opt.note})\033[0m" if opt.note else ""
            lines.append(f"  {color_open}{marker} {opt.label}{color_close}{note}")
        lines.append("")
        return ANSI("\n".join(lines))

    def _render_summary(self) -> ANSI:
        table = Table(show_header=True, header_style=f"bold {_BRAND}", box=None, padding=(0, 1))
        table.add_column("项", style=_DIM, no_wrap=True)
        table.add_column("值")
        for k, label in (
            ("llm_api_key", "API Key"),
            ("model_name", "模型"),
            ("api_base_url", "API Base URL"),
            ("kubeconfig_path", "Kubeconfig"),
            ("kube_context", "Kube Context"),
            ("confirmation_required", "权限模式"),
        ):
            v = self.ctx.updates.get(k, "")
            if k == "llm_api_key":
                v = _mask_secret(str(v))
            elif k == "confirmation_required":
                v = "✗ 确认模式" if v else "✻ 自动模式"
            elif k == "kube_context" and not v:
                v = "(使用 kubeconfig 当前 context)"
            elif not v:
                v = "—"
            table.add_row(label, str(v))
        path = self.ctx.config_store.path
        body = Text()
        body.append("将写入: ", style=_DIM)
        body.append(path + "\n\n", style=_BRAND)
        # Render the table separately via Rich
        table_ansi = _render_rich_to_ansi(table, _terminal_width() - 4)
        return ANSI(_render_rich_to_ansi(body, _terminal_width()) + "\n" + table_ansi)

    def _render_footer(self) -> ANSI:
        s = self.steps[self.idx]
        if s.kind == StepKind.SUMMARY:
            hint = "Enter 保存并启动  ·  E 返回修改  ·  Esc 取消"
        elif s.kind == StepKind.ENTER:
            hint = "Enter 开始  ·  Esc 取消"
        else:
            extra = "  ·  ↑/↓ 选择" if s.kind == StepKind.RADIO else ""
            hint = f"Enter 继续{extra}  ·  Shift+Tab 返回  ·  Esc 取消"
        return ANSI(f"  \033[2m{hint}\033[0m")

    # ── Driver ────────────────────────────────────────────────────

    async def run(self) -> bool:
        await self.app.run_async()
        return self.saved


# ── Public entry point ────────────────────────────────────────────────


async def run(
    console: ChaosConsole,
    config_store: ConfigStore,
    *,
    edit_mode: bool = False,
    session=None,  # kept for backward compat; ignored
) -> bool:
    """Run the onboarding wizard. Returns True if config was saved."""
    ctx = WizardCtx(
        config_store=config_store,
        edit_mode=edit_mode,
        snapshot=_settings_snapshot() if edit_mode else {},
    )
    steps = _build_steps()
    app = WizardApp(console, ctx, steps)
    saved = await app.run()
    if saved:
        try:
            console.print_text(strings.CONFIG_SAVED, style=_OK)
        except Exception:
            pass
        return True
    try:
        console.print_text(strings.SETUP_SKIPPED, style=_DIM)
    except Exception:
        pass
    return False
