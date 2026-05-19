"""Message renderers — user / system / error lines with role glyph prefix.

Post PR-B1: the colored ┃ left rail is gone. Each message is one line with
a single role-colored glyph at column 1 (``>`` for user, ``ℹ`` for system,
``✗`` for error). The content trails it without quote-frame indentation —
this matches the calmer rhythm Claude Code / Qwen Code use and stops every
line from looking like a blockquote.
"""

from __future__ import annotations

from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Colors, Icons


# Each message renderer now leads with a blank line so the
# conversation has a consistent ``marginTop=1`` rhythm — the doc §3
# P2#11 promise. Without it, user input, agent reply, and system
# notes all mashed into one continuous block which is what made the
# screen "feel stiff" even after the rule line was removed.
def render_user(console: ChaosConsole, content: str) -> None:
    """Render user message with a blue ``>`` role glyph."""
    text = Text()
    text.append(f" {Icons.USER} ", style=f"bold {Colors.USER}")
    text.append(content)
    console.print("")
    console.print(text)


def render_system(console: ChaosConsole, content: str) -> None:
    """Render system message — dim italic, deliberately *quiet*.

    System messages are usually JIT hints (``提示：试试...``) or status
    notifications. They are NOT primary content, so they shouldn't
    compete with agent replies for attention. Italic + dim renders
    them as "supplementary chrome", not "another speaker".
    """
    if not content:
        return
    text = Text()
    text.append(f"{Icons.SYSTEM} ", style=Colors.DIM)
    text.append(content, style=f"italic {Colors.MUTED}")
    console.print("")
    console.print(text)


def render_error(console: ChaosConsole, content: str, task_id: str = "") -> None:
    """Render error message with a red ``✗`` role glyph."""
    text = Text()
    text.append(f" {Icons.FAIL} ", style=f"bold {Colors.ERROR}")
    text.append(content, style=Colors.ERROR)
    if task_id:
        text.append(f"  (task: {task_id})", style=Colors.DIM)
    console.print("")
    console.print(text)


def render_error_recovery(
    console: ChaosConsole,
    content: str,
    error_type: str = "unknown",
    suggestions: list[str] | None = None,
) -> None:
    """Render an error with recovery suggestions.

    Used for stream interruptions, tool timeouts, and agent errors
    to provide actionable next steps. Wired into the dispatch path
    via :func:`render_error_with_suggestions`, which selects the
    right ``suggestions`` list from the error message's keywords.
    """
    from rich.panel import Panel

    body = Text()
    body.append(f"{content}\n\n", style=Colors.ERROR)

    if suggestions:
        body.append("下一步:\n", style="bold")
        for suggestion in suggestions:
            body.append(f"  \u2022 {suggestion}\n")

    title = Text()
    title.append(f" {Icons.FAIL} ", style=f"bold {Colors.ERROR}")
    title.append(error_type.upper())

    console.print(
        Panel(
            body,
            title=title,
            border_style=Colors.ERROR,
            padding=(0, 1),
        )
    )


# ── Keyword → suggestion lookup (§8.4 actionable error recovery) ────────


# Each entry: (keyword tuple, error-type label, suggestion list).
# Order matters — more specific patterns must precede broader ones.
# Suggestions point at commands the dispatcher actually serves;
# doc §8.4's mockup invented "/switch-context" / "/retry" but those
# don't exist, and a hint that resolves to "unknown command" is worse
# than no hint, so we stick to commands we have today.
_ERROR_SUGGESTIONS: list[tuple[tuple[str, ...], str, list[str]]] = [
    # Initialization / config issues — highest signal: env is broken.
    (
        ("not initialized", "failed to initialize", "init failed"),
        "INIT FAILED",
        [
            "/doctor — 重跑环境自检（kubectl / Operator / API Key）",
            "/config wizard — 重新打开配置向导",
        ],
    ),
    # kubectl-context / cluster-connection issues.
    (
        (
            "kubeconfig", "kube context", "kube_context",
            "context invalid", "context not found", "connection refused",
        ),
        "CLUSTER UNREACHABLE",
        [
            "/doctor — 检查 kubectl 连通性",
            "/config get kube_context — 查看当前集群上下文",
            "/tasks active — 确认是否有进行中的任务需要先恢复",
        ],
    ),
    # Streaming / network mid-conversation hiccups.
    (
        ("stream error", "stream interrupted", "stream timeout"),
        "STREAM ERROR",
        [
            "重新发送原消息再试一次（流式中断通常是临时性的）",
            "/doctor — 检查 LLM API 连通性",
            "/clear — 清空当前对话上下文重新开始",
        ],
    ),
    # Conversation-level controller errors.
    (
        ("conversation error", "conversation failed", "failed to start"),
        "CONVERSATION ERROR",
        [
            "/doctor — 环境自检",
            "/clear — 清空对话上下文",
            "/tasks active — 检查是否有未结束的任务阻塞",
        ],
    ),
    # Replay-specific failures.
    (
        ("replay failed", "cannot rehydrate", "recording parse"),
        "REPLAY FAILED",
        [
            "/recordings list — 查看本机可用录像",
            "/replay <task_id> --instant — 跳过原始时序看是否仍报错",
        ],
    ),
    # Command dispatch failures.
    (
        ("command failed", "unknown command"),
        "COMMAND FAILED",
        [
            "/help — 查看所有可用命令",
        ],
    ),
]


def _suggestions_for_error(content: str) -> tuple[str, list[str]] | None:
    """Match the error message against the keyword table.

    Returns ``(error_type_label, suggestions)`` on first match, or
    ``None`` if no entry fires. Substring match is case-insensitive
    so a wrapped exception message (``"Failed to initialize agent
    runner: connection refused"``) still trips the more specific
    "kubeconfig" entry — order in ``_ERROR_SUGGESTIONS`` puts the
    init-failure entry first because the wrapper string is the
    user's primary signal.
    """
    if not content:
        return None
    haystack = content.lower()
    for keywords, label, suggestions in _ERROR_SUGGESTIONS:
        if any(kw.lower() in haystack for kw in keywords):
            return label, suggestions
    return None


def render_error_with_suggestions(
    console: ChaosConsole,
    content: str,
    task_id: str = "",
) -> None:
    """Public entry for the dispatch path. Routes per match.

    * **match** → panel via :func:`render_error_recovery` with the
      matched label as the title and the canned slash-command list
      as the body. The ``task_id`` (if any) lands on a dim line
      below the panel so a postmortem reader can correlate.
    * **no match** → fall back to :func:`render_error` so unrelated
      errors don't grow into a panel they don't deserve.
    """
    matched = _suggestions_for_error(content)
    if matched is None:
        render_error(console, content, task_id=task_id)
        return

    label, suggestions = matched
    render_error_recovery(
        console,
        content,
        error_type=label,
        suggestions=suggestions,
    )
    if task_id:
        # Dim subordinate line so postmortem readers can correlate
        # the panel with a recording id.
        tail = Text()
        tail.append(f"  task: {task_id}", style=Colors.DIM)
        console.print(tail)
