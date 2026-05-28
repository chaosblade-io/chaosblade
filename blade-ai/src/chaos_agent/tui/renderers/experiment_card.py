"""Experiment card renderer — synthesised hypothesis header.

PR-D2 — after the user approves an intent at ``intent_confirm``, but
before the phase timeline kicks in, blade-ai paints a card that frames
the run as a *chaos experiment*. The card is built **entirely** from
data already in the approved ``fault_intent``; we do not introduce a
new state field, do not call the LLM, and do not probe the cluster.

The header is a deliberate differentiator from a generic AI assistant:
chaos engineering treats every fault injection as an experiment with a
hypothesis, blast radius, and rollback plan. Surfacing those three lines
turns a console of tool calls into something that reads like a postmortem
or an experiment log — which is what reviewers actually want.

Density behavior (PR-D1 §17.1):

* ``calm``    — hidden. The user opted out of the differentiating UI.
* ``working`` — hypothesis + blast radius (2-line gist).
* ``dense``   — adds a parameters row and the rollback hint, plus the
  same ``▆▇█`` sparkline used in the confirm risk meter so the eye reads
  the two cards as a single visual family.
"""

from __future__ import annotations

from typing import Optional

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.renderers.intent_confirm import (
    _compute_risk_info,
    _risk_tier,
)
from chaos_agent.tui.state import DisplayMode
from chaos_agent.tui.theme import Colors, Icons, Theme

# Action → (中文动词, 自然反向). Used both for the hypothesis sentence
# ("假设：注入 X 时…") and the rollback line ("回滚：…"). Keep the table
# tiny — chaos-blade has many actions but we only need a friendly label
# for the common ones; everything else falls through to the raw action
# string with no rollback hint.
_ACTION_LABELS: dict[str, tuple[str, str]] = {
    "fullload":       ("CPU 满载",       "stop-cpu-fullload"),
    "load":           ("负载注入",       "stop-load"),
    "drop":           ("网络丢包",       "stop-drop"),
    "kill":           ("强杀",           "重新拉起容器"),
    "fail":           ("失败注入",       "stop-fail"),
    "burn":           ("内存满载",       "stop-burn-mem"),
    "fill":           ("磁盘填充",       "stop-fill"),
    "corrupt":        ("数据错乱",       "stop-corrupt"),
    "duplicate":      ("数据复制",       "stop-duplicate"),
    "reorder":        ("数据乱序",       "stop-reorder"),
    "occupy":         ("端口占用",       "stop-occupy"),
}


def _action_label(action: str) -> str:
    """Friendly Chinese label for a chaos-blade action; fall back to raw."""
    if not action:
        return "未知动作"
    label = _ACTION_LABELS.get(action.lower())
    return label[0] if label else action


def _rollback_hint(action: str) -> str:
    """Natural inverse for the action; empty string when unknown.

    We deliberately don't invent a rollback if we can't name one — a
    fabricated "stop-foo" hint is worse than silence because the user
    might trust it.
    """
    if not action:
        return ""
    label = _ACTION_LABELS.get(action.lower())
    return label[1] if label else ""


def _hypothesis_sentence(fault_intent: dict) -> str:
    """Compose the "假设" sentence from intent fields.

    PR-E6 — when ``intent_clarification`` populates ``hypothesis`` with
    a concrete prediction (e.g. "HPA 应在 60s 内扩到 ≥3 副本"), prefer
    that. Falls back to the synthesized template only when the LLM
    couldn't infer a measurable claim. The fallback's shape is
    deliberately identical across cards so a reviewer skimming a
    transcript can spot the differentiated hypotheses at a glance.

        Fallback: 在 <namespace> 注入 <target> <action_label> 时，
                  系统应保持基线表现且关键路径不受影响。
    """
    custom = str(fault_intent.get("hypothesis") or "").strip()
    if custom:
        return custom
    namespace = str(fault_intent.get("namespace") or "default")
    target = str(fault_intent.get("target") or "resource")
    action = _action_label(fault_intent.get("action", ""))
    return (
        f"在 {namespace} 注入 {target} {action} 时，"
        "\u7cfb\u7edf\u5e94\u4fdd\u6301\u57fa\u7ebf\u8868\u73b0\u4e14\u5173\u952e\u8def\u5f84\u4e0d\u53d7\u5f71\u54cd"
    )


def _success_criteria_rows(fault_intent: dict) -> Optional[Text]:
    """PR-E6 — list the concrete pass/fail criteria when the LLM gave any.

    Returns None when ``success_criteria`` is missing or empty so the
    renderer can skip the row entirely; a "no criteria" stub would
    suggest the agent did look and found none, which is a worse lie
    than silence.
    """
    criteria = fault_intent.get("success_criteria") or []
    if not isinstance(criteria, list):
        return None
    cleaned = [str(c).strip() for c in criteria if str(c).strip()]
    if not cleaned:
        return None
    txt = Text()
    for i, c in enumerate(cleaned):
        if i > 0:
            txt.append("\n")
        txt.append("  \u9a8c\u6536\uff1a", style=Colors.DIM)  # 验收
        txt.append(c, style=Colors.MUTED)
    txt.append("\n")
    return txt


def _blast_radius_text(fault_intent: dict, display_mode: DisplayMode) -> Optional[Text]:
    """One-line blast-radius summary keyed off the same risk-tier logic.

    Returns None when ``_compute_risk_info`` has nothing useful (i.e. the
    intent didn't enumerate names, set a count, or use a label/scope) —
    in that case we just omit the row rather than print "unknown".
    """
    risk = _compute_risk_info(fault_intent)
    if risk is None:
        return None

    txt = Text()
    txt.append("  \u7206\u70b8\u534a\u5f84\uff1a", style=Colors.DIM)  # 爆炸半径

    if risk.kind == "concrete":
        _, color, bar = _risk_tier(risk.count)
        if display_mode == DisplayMode.DENSE:
            txt.append(f"{bar} ", style=color)
        txt.append(f"{risk.count} {risk.target}", style=f"bold {color}")
        if risk.sample:
            txt.append(f" \u2014 {risk.sample}", style=Colors.MUTED)
    elif risk.kind == "bounded":
        _, color, bar = _risk_tier(risk.count)
        if display_mode == DisplayMode.DENSE:
            txt.append(f"{bar} ", style=color)
        txt.append(f"\u2264 {risk.count} {risk.target}", style=f"bold {color}")
    else:
        # Unbounded: surface the qualifier instead of a fabricated count.
        descriptor_label = {
            "labels": "\u6807\u7b7e\u5339\u914d",       # 标签匹配
            "namespace": "\u6574\u4e2a namespace",       # 整个 namespace
        }.get(risk.descriptor, risk.descriptor)
        txt.append(
            f"{risk.target} \u00b7 {descriptor_label}",
            style=f"bold {Theme.state_warn}",
        )
        txt.append(
            "  (\u8fd0\u884c\u65f6\u786e\u5b9a)", style=Colors.MUTED
        )
    txt.append("\n")
    return txt


def _params_row(fault_intent: dict) -> Optional[Text]:
    """Dense-only: list the chaos-blade parameters that drive the action."""
    params = fault_intent.get("params") or {}
    if not params:
        return None
    txt = Text()
    txt.append("  \u53c2\u6570\uff1a", style=Colors.DIM)  # 参数
    items = ", ".join(f"{k}={v}" for k, v in params.items())
    txt.append(items, style=Colors.MUTED)
    txt.append("\n")
    return txt


def _rollback_row(fault_intent: dict) -> Optional[Text]:
    """Dense-only: the rollback hint, or None when we can't name one."""
    action = fault_intent.get("action", "")
    hint = _rollback_hint(action)
    if not hint:
        return None
    txt = Text()
    txt.append("  \u56de\u6eda\uff1a", style=Colors.DIM)  # 回滚
    txt.append(hint, style=Colors.MUTED)
    txt.append("\n")
    return txt


def build_card(
    fault_intent: dict,
    display_mode: DisplayMode = DisplayMode.WORKING,
) -> Optional[RenderableType]:
    """Compose the experiment card body.

    Returns ``None`` for ``calm`` mode or when ``fault_intent`` is
    empty — both cases mean "no card should print." The renderer call
    site can then skip ``console.print``.

    Pure function so it's snapshot-testable without spawning a console.
    """
    if display_mode == DisplayMode.CALM:
        return None
    if not fault_intent:
        return None

    body = Text()
    body.append("\n")
    body.append("  \u5047\u8bbe\uff1a", style=Colors.DIM)  # 假设
    body.append(_hypothesis_sentence(fault_intent), style=Colors.MUTED)
    body.append("\n")

    blast = _blast_radius_text(fault_intent, display_mode)
    if blast is not None:
        body.append_text(blast)

    extras: list[Text] = []
    # PR-E6 — verifier criteria show in working AND dense (it's the
    # whole point of the differentiated hypothesis: tell the user what
    # we'll *check*). Calm has already short-circuited above.
    criteria = _success_criteria_rows(fault_intent)
    if criteria is not None:
        extras.append(criteria)
    if display_mode == DisplayMode.DENSE:
        params = _params_row(fault_intent)
        if params is not None:
            extras.append(params)
        rollback = _rollback_row(fault_intent)
        if rollback is not None:
            extras.append(rollback)

    if extras:
        body.append("\n")
        return Group(body, *extras)
    return body


def render(
    console: ChaosConsole,
    fault_intent: dict,
    display_mode: DisplayMode = DisplayMode.WORKING,
    state=None,
) -> None:
    """Print the experiment card to the console (no-op in calm mode).

    PR-D4 — when ``state`` is supplied, the card allocates a locator
    (``E#``) and stores the ``fault_intent`` as a snapshot so the user
    can later run ``/show E1`` / ``/copy E1`` / ``/rerun E1``. Locator
    allocation happens regardless of ``display_mode`` (calm doesn't
    skip the bookkeeping — it just hides the visible label).
    """
    # An empty intent has nothing to record — return before touching the
    # allocator so we don't mint a hollow E# that /show would resolve to {}.
    if not fault_intent:
        return

    # Allocate FIRST so calm mode still records the snapshot. The locator
    # is needed for a later /mode dense + /show E1 to surface this card.
    locator: str = ""
    if state is not None and getattr(state, "locators", None) is not None:
        locator = state.locators.allocate_experiment(
            {"fault_intent": dict(fault_intent or {})}
        )

    body = build_card(fault_intent, display_mode=display_mode)
    if body is None:
        # Calm mode: snapshot recorded above, just don't paint the panel.
        return

    title = Text()
    title.append(f" {Icons.AGENT} ", style=f"bold {Colors.BRAND}")
    title.append("Experiment Card", style=f"bold {Colors.BRAND}")
    # Locator is suppressed in calm — the user opted out of the
    # differentiator that locators are part of. Working renders it dim
    # so the eye finds the card content first; dense lifts it to bold
    # because postmortem readers scan by id.
    if locator and display_mode != DisplayMode.CALM:
        title.append("  ")
        if display_mode == DisplayMode.DENSE:
            title.append(f"[{locator}]", style=f"bold {Colors.BRAND}")
        else:
            title.append(f"[{locator}]", style=Colors.DIM)

    panel = Panel(
        body,
        title=title,
        border_style=Colors.BRAND,
        box=box.ROUNDED,
        padding=(0, 1),
    )
    console.print(panel)
