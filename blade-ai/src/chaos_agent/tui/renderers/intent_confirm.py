"""Intent confirm renderer — structured intent summary panel.

Renders a visually distinct intent confirmation dialog with:
- Double-line border for attention (brand blue)
- Structured fault intent display (scope/target/action/namespace/params)
- LLM intent confidence with a low-confidence warning row
- Absolute-count risk meter (PR-D3 §9.2 — working / dense modes only)
- Clear Y/N action prompt

Used at the intent_confirm gate before agent_loop to let the user
verify the LLM's understanding before execution begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from prompt_toolkit import PromptSession
from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.renderers._layout import make_field_table
from chaos_agent.tui.state import DisplayMode
from chaos_agent.tui.theme import Colors, Icons, Theme

# Confidence below this threshold flips the panel from a neutral display
# to one that surfaces a yellow warning row, nudging the user to read the
# fields carefully before approving. Picked to match the spec in
# docs/design/tui-design-analysis.md §16.3.
LOW_CONFIDENCE_THRESHOLD = 0.7

# Risk-tier breakpoints (PR-D3 §9.2). Counts are *absolute* — we
# deliberately do not compute "X% of cluster" because that would require
# a kubectl probe from a renderer (perf + permissions, see design doc
# §16). Tier names are stable strings so a grep across the codebase finds
# everywhere they're rendered.
_RISK_TIER_LOW_MAX = 2     # ≤2 → low
_RISK_TIER_MID_MAX = 9     # 3–9 → medium; 10+ → high


@dataclass(frozen=True)
class _RiskInfo:
    """Risk summary derived from ``fault_intent`` without any cluster probe.

    Three kinds, in order of confidence:

    * ``concrete``   — exact count from ``names`` (e.g. 2 pods listed).
    * ``bounded``    — upper bound from ``params.count`` (e.g. count=5).
    * ``unbounded``  — label/percent/namespace scope, exact count
      depends on cluster state at injection time. We can't probe from
      the renderer, so we report the qualitative scope.
    """

    kind: str               # "concrete" | "bounded" | "unbounded"
    target: str             # "pod" / "node" / "container" / ...
    count: int = 0          # for concrete + bounded
    descriptor: str = ""    # for unbounded ("labels", "namespace")
    sample: str = ""        # for concrete: first 1-3 names, comma-joined


def _confidence_style(confidence: float) -> str:
    """Map a 0..1 confidence into a theme color: low=red, mid=warn, high=ok."""
    if confidence < 0.5:
        return Colors.ERROR
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return Colors.WARNING
    return Colors.SUCCESS


def _low_confidence_hint(fault_intent: dict, confidence: float) -> str:
    """Build a field-aware warning suffix for low-confidence intents (§16.3).

    The doc mockup shows ``╰─ ⚠ "prod" 可能指 cms-prod 也可能指 prod-payment``
    — i.e. a candidate-list of plausible alternative values for the
    ambiguous field. The LLM doesn't currently emit alternative
    candidates (that would be a separate prompt change), so the next
    best thing is naming the **specific values** the user should
    sanity-check, not just "please verify."

    Two layers of escalation:

    * ``confidence < 0.5`` (very low) → leads with "强烈建议" so the
      user reads the warning as something close to a stop signal,
      not generic friendly nag.
    * ``0.5 <= confidence < LOW_CONFIDENCE_THRESHOLD`` (just below the
      threshold) → leads with "建议", a softer prod.

    On top of either, a single pattern flag may append: a namespace
    containing ``prod`` / ``production`` is the highest-stakes signal
    we can detect from the renderer alone, so it gets a dedicated
    "请确认非生产环境" tail. Other patterns (unbounded scope, empty
    target) are caught by the risk meter row above and don't need to
    duplicate here.
    """
    namespace = str(fault_intent.get("namespace") or "default")
    target = str(fault_intent.get("target") or "?")
    action = str(fault_intent.get("action") or "?")

    lead = "强烈建议" if confidence < 0.5 else "建议"  # 强烈建议 / 建议
    msg = (
        f"{lead}逐项核对："  # 逐项核对：
        f"namespace={namespace} · target={target} · action={action}"
    )

    ns_lower = namespace.lower()
    if "prod" in ns_lower or "production" in ns_lower:
        # 名 含 'prod' 字样，请确认非生产环境
        msg += "。namespace 含 'prod' 字样，请确认非生产环境"

    return msg


def _compute_risk_info(fault_intent: dict) -> Optional[_RiskInfo]:
    """Extract a ``_RiskInfo`` from the fault_intent dict.

    Returns ``None`` if there's nothing useful to surface (e.g. an
    intent that's still half-formed). Pure function for testability —
    no cluster probe, no LLM call.
    """
    if not fault_intent:
        return None
    target = str(fault_intent.get("target") or "resource")

    # 1) Concrete: ``names`` enumerates exact resources → exact count.
    names = fault_intent.get("names") or []
    if names:
        sample = ", ".join(str(n) for n in names[:3])
        if len(names) > 3:
            sample += f", \u2026 (+{len(names) - 3})"
        return _RiskInfo(
            kind="concrete", target=target, count=len(names), sample=sample
        )

    # 2) Bounded: ``params.count`` is the chaos-blade upper bound.
    params = fault_intent.get("params") or {}
    raw_count = params.get("count") or params.get("Count")
    try:
        bounded = int(raw_count) if raw_count is not None else None
    except (TypeError, ValueError):
        bounded = None
    if bounded is not None and bounded > 0:
        return _RiskInfo(kind="bounded", target=target, count=bounded)

    # 3) Unbounded: label / percent / namespace scope. We surface the
    #    scope so the user knows the count is runtime-determined; we
    #    don't probe the cluster.
    if fault_intent.get("labels"):
        return _RiskInfo(kind="unbounded", target=target, descriptor="labels")
    if "percent" in params:
        return _RiskInfo(
            kind="unbounded",
            target=target,
            descriptor=f"percent={params['percent']}",
        )
    if (fault_intent.get("scope") or "").lower() == "namespace":
        return _RiskInfo(
            kind="unbounded", target=target, descriptor="namespace"
        )
    return None


def _risk_tier(count: int) -> tuple[str, str, str]:
    """Return ``(tier_label, theme_color, sparkline)`` for an absolute count.

    Tier glyphs use box-drawing-block sparklines so the bar reads as
    "fill height" — small at low risk, full at high. Matches the
    docs/design §10 mockup ``▆▂▁`` style.
    """
    if count <= _RISK_TIER_LOW_MAX:
        return ("low", Theme.state_ok, "\u2581\u2581\u2581")     # ▁▁▁
    if count <= _RISK_TIER_MID_MAX:
        return ("medium", Theme.state_warn, "\u2581\u2583\u2585")  # ▁▃▅
    return ("high", Theme.state_err, "\u2586\u2587\u2588")          # ▆▇█


def _render_risk_summary(
    risk: _RiskInfo, display_mode: DisplayMode
) -> Optional[Text]:
    """Render the risk row as a Rich ``Text``, or ``None`` to suppress.

    * ``calm``    — always returns ``None``. Calm mode hides the risk
      meter entirely; the user opted out of the differentiating UI.
    * ``working`` — count + tier label, no sparkline. Compact.
    * ``dense``   — adds the box-drawing sparkline before the tier.
    """
    if display_mode == DisplayMode.CALM:
        return None

    txt = Text()
    txt.append("  Risk: ", style=Colors.DIM)

    if risk.kind == "concrete":
        tier, color, bar = _risk_tier(risk.count)
        txt.append(f"{risk.count} {risk.target}", style=f"bold {color}")
        if risk.sample:
            txt.append(f" ({risk.sample})", style=Colors.MUTED)
        txt.append(" \u00b7 ", style=Colors.DIM)
        if display_mode == DisplayMode.DENSE:
            txt.append(f"{bar} ", style=color)
        txt.append(tier, style=f"bold {color}")
    elif risk.kind == "bounded":
        tier, color, bar = _risk_tier(risk.count)
        txt.append(f"\u2264 {risk.count} {risk.target}", style=f"bold {color}")
        txt.append(" \u00b7 ", style=Colors.DIM)
        if display_mode == DisplayMode.DENSE:
            txt.append(f"{bar} ", style=color)
        txt.append(tier, style=f"bold {color}")
    else:  # unbounded
        # No count → no sparkline. Surface the scope qualifier so the
        # user knows the actual N is determined at injection time, not
        # at confirm time.
        descriptor_label = {
            "labels": "\u6807\u7b7e\u5339\u914d",       # 标签匹配
            "namespace": "\u6574\u4e2a namespace",       # 整个 namespace
        }.get(risk.descriptor, risk.descriptor)
        txt.append(
            f"{risk.target} \u00b7 {descriptor_label}",
            style=f"bold {Theme.state_warn}",
        )
        txt.append(
            "  (\u8fd0\u884c\u65f6\u786e\u5b9a)", style=Colors.MUTED  # 运行时确定
        )

    txt.append("\n")
    return txt


def build_body(
    info: dict,
    display_mode: DisplayMode = DisplayMode.WORKING,
) -> RenderableType:
    """Construct the renderable body for an intent_confirm panel.

    Returns a Rich ``Group`` of (preamble Text, fields Table, risk Text,
    footer Text) so that the field rows go through Rich's own column-width
    logic (``cell_len``) instead of Python's character-count ``ljust``.
    CJK glyphs occupy two terminal cells; the previous ``ljust(38)``
    shifted the closing ``│`` by half a column for every Chinese
    character in the value.

    The PR-D3 risk row is inserted between the field table and the
    confidence line: working mode shows ``count + tier`` and dense adds
    a box-drawing sparkline; calm hides it entirely (the differentiator
    the user opted out of).

    Pure function so it can be rendered to a console for snapshot tests
    without spawning a real PromptSession.
    """
    fault_intent = info.get("fault_intent", {})
    confidence = float(info.get("intent_confidence") or 0.0)

    fields: list[tuple[str, object]] = [
        ("Fault Type", fault_intent.get("fault_type", "unknown")),
        ("Scope", fault_intent.get("scope", "unknown")),
        ("Target", fault_intent.get("target", "unknown")),
        ("Action", fault_intent.get("action", "unknown")),
        ("Namespace", fault_intent.get("namespace", "unknown")),
    ]
    if fault_intent.get("labels"):
        fields.append(("Labels", fault_intent["labels"]))
    if fault_intent.get("names"):
        fields.append(("Resources", ", ".join(fault_intent["names"])))
    if fault_intent.get("params"):
        params_str = ", ".join(f"{k}={v}" for k, v in fault_intent["params"].items())
        fields.append(("Params", params_str))

    pre = Text()
    pre.append("\n")
    pre.append(
        "  The following fault injection intent has been identified:\n",
        style=Colors.DIM,
    )

    table = make_field_table(fields, label_min_width=12, value_min_width=38)

    risk_block: Optional[Text] = None
    risk_info = _compute_risk_info(fault_intent)
    if risk_info is not None:
        risk_block = _render_risk_summary(risk_info, display_mode)

    post = Text()
    post.append("\n")
    if confidence > 0:
        conf_style = _confidence_style(confidence)
        post.append("  Confidence: ", style=Colors.DIM)
        post.append(f"{confidence:.2f}", style=f"bold {conf_style}")
        post.append("\n")
        if confidence < LOW_CONFIDENCE_THRESHOLD:
            # PR-A2 §16.3 — surface the warning as a subordinate line
            # under the confidence value, mirroring ThinkingPrinter's
            # ``└─ <justification>`` visual hierarchy. The hint names
            # the specific fields the user should re-verify rather than
            # generic "please double-check" filler, so the warning is
            # actionable instead of decorative.
            hint = _low_confidence_hint(fault_intent, confidence)
            post.append("  └─ ", style=Colors.DIM)  # └─
            post.append(f"{Icons.WARNING} ", style=f"bold {conf_style}")
            post.append(hint, style=conf_style)
            post.append("\n")

    if fault_intent.get("user_description"):
        post.append(
            f"  Description: {fault_intent['user_description']}\n",
            style=Colors.MUTED,
        )

    post.append("\n")
    post.append("  " + "\u2500" * 52 + "\n", style=Colors.DIM)
    post.append("  [Y] Confirm & Execute    [N] Continue Conversation", style="bold")
    post.append("\n")

    if risk_block is not None:
        return Group(pre, table, risk_block, post)
    return Group(pre, table, post)


async def run(
    console: ChaosConsole,
    info: dict,
    session: Optional[PromptSession] = None,
    display_mode: DisplayMode = DisplayMode.WORKING,
) -> str:
    """Render an intent confirmation panel and return 'approved' or 'rejected'."""
    body = build_body(info, display_mode=display_mode)

    title = Text()
    title.append(f" {Icons.AGENT} ", style=f"bold {Colors.BRAND}")
    title.append("Intent Confirmation", style=f"bold {Colors.BRAND}")

    panel = Panel(
        body,
        title=title,
        border_style=Colors.BRAND,
        box=box.DOUBLE,
        padding=(0, 1),
    )
    console.print(panel)
    console.bell()

    sess = session or PromptSession()
    answer = await sess.prompt_async("Confirm intent? [Y/n]: ")
    answer = (answer or "").strip().lower()
    if answer in ("", "y", "yes"):
        return "approved"
    return "rejected"
