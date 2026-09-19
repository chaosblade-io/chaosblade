/**
 * Confirm gate tests — the web counterpart of the TUI's ConfirmMessage
 * contract:
 *   - context card renders per-node titles with the always-render field
 *     discipline (empty values show "None", the row never hides)
 *   - prompt buttons dispatch CONFIRM_USER_DECIDED (the card itself
 *     never touches the network — Composer's pendingDecision effect
 *     owns that, covered in Composer.test.tsx)
 *   - resolved prompts collapse to the one-line chip
 *   - feedback has two semantics: L1/L2 wrap as rejected+feedback,
 *     plan_builder free_input sends the raw text as the answer
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type {
  AppState,
  ConfirmContextItem,
  ConfirmPromptItem,
} from "@blade-ai/core";
import { StoreProvider, configureI18n, useAppSelector } from "@blade-ai/core";
import { ConfirmContextView, ConfirmPromptView } from "./ConfirmMessage";

configureI18n("en");

afterEach(() => {
  cleanup();
});

// ── state probe: capture the latest pendingDecision after clicks ────

let latestDecision: AppState["pendingDecision"] | undefined;

function Probe() {
  latestDecision = useAppSelector((s) => s.pendingDecision);
  return null;
}

beforeEach(() => {
  latestDecision = undefined;
});

function renderPrompt(
  item: ConfirmPromptItem,
  /** Extra pending items rendered alongside (multi-prompt race tests).
   *  The primary item always lands in the store's pending — the
   *  first-unresolved keyboard gate reads it from there. */
  extraPending: ConfirmPromptItem[] = [],
) {
  return render(
    <StoreProvider initial={{ pending: [item, ...extraPending] }}>
      <Probe />
      <ConfirmPromptView item={item} />
    </StoreProvider>,
  );
}

// ── fixtures ────────────────────────────────────────────────────────

const L1_CONTEXT: ConfirmContextItem = {
  kind: "confirm_context",
  id: "c-ctx-1",
  taskId: "T1",
  node: "intent_confirm",
  content: "I understood your request as follows.",
  payload: {
    fault_intent: {
      fault_type: "pod-cpu fullload",
      case_resource_path: "",
      scope: "pod",
      target: "cpu",
      action: "fullload",
      namespace: "demo",
      duration_seconds: 60,
      labels: { app: "web" },
      names: ["web-1"],
      params: { "cpu-percent": "80" },
      user_description: "stress the cpu",
    },
  },
};

const L2_CONTEXT: ConfirmContextItem = {
  kind: "confirm_context",
  id: "c-ctx-2",
  taskId: "T1",
  node: "confirmation_gate",
  content: "",
  // skill_name trips the hasExecutionContent gate (mirrors a real gate
  // payload); a bare ``{duration_seconds}`` payload falls to the
  // generic fallback, which — by design — does not render the
  // duration row (same dispatch contract as the TUI).
  payload: { skill_name: "k8s-chaos-skills", duration_seconds: 120 },
};

// Widened write-set contract (CASE manifest): the entries ride the
// payload verbatim — the D4 knowing-human requirement. A payload with
// ONLY mechanism_writes (everything else empty) must still dispatch to
// the structured execution card, never the generic fallback.
const WIDENED_CONTEXT: ConfirmContextItem = {
  kind: "confirm_context",
  id: "c-ctx-3",
  taskId: "T1",
  node: "confirmation_gate",
  content: "",
  payload: {
    mechanism_writes: [
      {
        scope: "configmap",
        namespace: "kube-system",
        names: ["coredns"],
        name_prefix: "",
        description: "",
      },
      {
        scope: "configmap",
        namespace: "kube-system",
        names: [],
        name_prefix: "drill-nxdomain-",
        description: "",
      },
    ],
  },
};

function promptItem(partial: Partial<ConfirmPromptItem>): ConfirmPromptItem {
  return {
    kind: "confirm_prompt",
    id: "c-prompt-1",
    taskId: "T1",
    node: "confirmation_gate",
    selectedIndex: 0,
    mode: "select",
    feedback: "",
    resolved: false,
    ...partial,
  };
}

// ── context card ────────────────────────────────────────────────────

describe("ConfirmContextView", () => {
  it("renders the L1 title and the always-render fault intent fields", () => {
    render(<ConfirmContextView item={L1_CONTEXT} />);

    expect(screen.getByText("Confirm fault intent")).toBeInTheDocument();
    expect(screen.getByText("pod-cpu fullload")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    // duration_seconds renders as seconds.
    expect(screen.getByText("60s")).toBeInTheDocument();
    expect(screen.getByText("app=web")).toBeInTheDocument();
    expect(screen.getByText("cpu-percent=80")).toBeInTheDocument();
  });

  it("always renders empty fields as None — the row never hides", () => {
    // case_resource_path is "" in the fixture: the row must still be
    // there, showing the None placeholder (always-render discipline).
    render(<ConfirmContextView item={L1_CONTEXT} />);
    expect(screen.getByText("Case file")).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
  });

  it("renders the L2 title and the gate-level duration", () => {
    render(<ConfirmContextView item={L2_CONTEXT} />);
    expect(screen.getByText("Confirm execution plan")).toBeInTheDocument();
    expect(screen.getByText("120s")).toBeInTheDocument();
    expect(screen.getByText("k8s-chaos-skills")).toBeInTheDocument();
    // Params row is ALWAYS rendered — empty shows the em dash, and
    // the absent health/feasibility reports render as not-run rows
    // (never silently missing).
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getAllByText(/check not run/)).toHaveLength(2);
  });

  it("widened contract renders the manifest entries verbatim — never the generic fallback", () => {
    render(<ConfirmContextView item={WIDENED_CONTEXT} />);
    // Structured execution card (not the generic fallback: the card
    // title proves the dispatch), danger label, and BOTH entry shapes
    // rendered as single lines — names list and prefix selector.
    expect(screen.getByText("Confirm execution plan")).toBeInTheDocument();
    expect(screen.getByText("Writes beyond victim")).toBeInTheDocument();
    expect(screen.getByText("configmap/kube-system: coredns")).toBeInTheDocument();
    expect(screen.getByText("configmap/kube-system: 'drill-nxdomain-' (prefix)")).toBeInTheDocument();
    expect(
      screen.getByText("approving authorizes these cluster writes"),
    ).toBeInTheDocument();
  });

  it("execution card floats the safety alert to the top on a problem status", () => {
    render(
      <ConfirmContextView
        item={{
          kind: "confirm_context",
          id: "c-exec-warn",
          taskId: "T1",
          node: "confirmation_gate",
          content: "",
          payload: {
            skill_name: "k8s-chaos-skills",
            safety_status: "warning",
            safety_reason: "target node has DiskPressure",
            duration_seconds: 60,
          },
        }}
      />,
    );
    expect(
      screen.getByText(/target node has DiskPressure/),
    ).toBeInTheDocument();
  });

  it("target-change card renders the original → proposed drift diff", () => {
    // Regression: tool_screener used to fall to the default title
    // ("confirm.answered") with NO payload rendering at all — the
    // operator approved a target drift blind.
    render(
      <ConfirmContextView
        item={{
          kind: "confirm_context",
          id: "c-tc",
          taskId: "T2",
          node: "tool_screener",
          content: "",
          payload: {
            type: "target_change",
            agent_reason: "label selector drifted",
            original: { namespace: "demo", names: ["web-1"] },
            proposed: { namespace: "demo", labels: { app: "web" } },
          },
        }}
      />,
    );
    expect(
      screen.getByText("Target change confirmation"),
    ).toBeInTheDocument();
    expect(screen.getByText(/label selector drifted/)).toBeInTheDocument();
    expect(screen.getByText(/names=\[web-1\]/)).toBeInTheDocument();
    expect(screen.getByText(/labels=\{app=web\}/)).toBeInTheDocument();
  });

  it("plan-change card renders the fault-type diff with the duration bound", () => {
    render(
      <ConfirmContextView
        item={{
          kind: "confirm_context",
          id: "c-pc",
          taskId: "T3",
          node: "plan_change_confirm",
          content: "",
          payload: {
            type: "plan_change",
            reason: "disk fill not viable on this node",
            original: {
              scope: "pod",
              fault_target: "disk",
              fault_action: "fill",
              fault_spec: { duration_seconds: 60 },
            },
            proposed: {
              scope: "pod",
              fault_target: "cpu",
              fault_action: "fullload",
            },
          },
        }}
      />,
    );
    expect(
      screen.getByText("Plan Change Confirmation"),
    ).toBeInTheDocument();
    expect(screen.getByText(/disk fill not viable/)).toBeInTheDocument();
    expect(screen.getByText(/pod-disk-fill/)).toBeInTheDocument();
    expect(screen.getByText(/60s/)).toBeInTheDocument();
    expect(screen.getByText(/pod-cpu-fullload/)).toBeInTheDocument();
  });

  it("intent card surfaces low confidence, its hint, and the clarification round", () => {
    render(
      <ConfirmContextView
        item={{
          ...L1_CONTEXT,
          id: "c-int-conf",
          payload: {
            fault_intent: { ...(L1_CONTEXT.payload?.["fault_intent"] as object) },
            intent_confidence: 0.4,
            clarification_round: 2,
            intent_reasoning: "guessed the target from a typo",
          },
        }}
      />,
    );
    // Confidence row + per-field hint + reasoning (low-confidence
    // gate) + the always-rendered clarification row.
    expect(screen.getByText(/Intent confidence: 40%/)).toBeInTheDocument();
    expect(
      screen.getByText(/namespace=demo · target=cpu · action=fullload/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/guessed the target from a typo/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("2 clarification round(s)"),
    ).toBeInTheDocument();
    // Risk row: one concrete name → low tier.
    expect(screen.getByText(/Risk: low · 1 cpu/)).toBeInTheDocument();
  });

  it("plan-builder card renders the question", () => {
    render(
      <ConfirmContextView
        item={{
          kind: "confirm_context",
          id: "c-pb",
          taskId: "T4",
          node: "plan_builder",
          content: "",
          payload: { question: "Which recovery plan do you prefer?" },
        }}
      />,
    );
    expect(screen.getByText("Plan Guide")).toBeInTheDocument();
    expect(
      screen.getByText("Which recovery plan do you prefer?"),
    ).toBeInTheDocument();
  });

  it("batch intent renders one row per fault with the count in the title", () => {
    render(
      <ConfirmContextView
        item={{
          kind: "confirm_context",
          id: "c-batch",
          taskId: "T6",
          node: "intent_confirm",
          content: "",
          payload: {
            batch_faults: [
              {
                scope: "pod",
                target: "cpu",
                action: "fullload",
                namespace: "demo",
                names: ["web-1"],
                duration_seconds: 60,
              },
              {
                scope: "pod",
                target: "mem",
                action: "load",
                namespace: "demo",
                names: [],
                duration_seconds: 120,
              },
            ],
          },
        }}
      />,
    );
    // Title carries the {n}-interpolated count (placeholder parity
    // with the TUI — a stray {count} would render literally).
    expect(screen.getByText(/2 faults/)).toBeInTheDocument();
    expect(screen.getByText(/pod-cpu-fullload/)).toBeInTheDocument();
    expect(screen.getByText(/pod-mem-load/)).toBeInTheDocument();
    // Empty names list renders the wildcard.
    expect(screen.getByText(/@ demo\/\*/)).toBeInTheDocument();
  });

  it("unknown nodes fall back to the generic card with the content body", () => {
    render(
      <ConfirmContextView
        item={{
          kind: "confirm_context",
          id: "c-gen",
          taskId: "T5",
          node: "some_future_gate",
          content: "please confirm this custom step",
        }}
      />,
    );
    expect(screen.getByText("Confirm intent")).toBeInTheDocument();
    expect(
      screen.getByText("please confirm this custom step"),
    ).toBeInTheDocument();
  });
});

// ── prompt ──────────────────────────────────────────────────────────

describe("ConfirmPromptView", () => {
  it("renders L2 buttons and dispatches approved on inject", () => {
    renderPrompt(promptItem({}));

    fireEvent.click(screen.getByText("inject"));
    expect(latestDecision).toEqual({ taskId: "T1", answer: "approved" });

    cleanup();
    latestDecision = undefined;
    renderPrompt(promptItem({}));
    fireEvent.click(screen.getByText("cancel"));
    expect(latestDecision).toEqual({ taskId: "T1", answer: "rejected" });
  });

  it("renders L1 labels (submit / refine) for intent_confirm", () => {
    renderPrompt(promptItem({ node: "intent_confirm" }));
    expect(screen.getByText("submit")).toBeInTheDocument();
    expect(screen.getByText("refine")).toBeInTheDocument();
  });

  it("collapses to the armed / aborted chip once resolved", () => {
    const { unmount } = renderPrompt(
      promptItem({ resolved: true, answer: "approved" }),
    );
    expect(screen.getByText(/ARMED · proceeding/)).toBeInTheDocument();
    unmount();

    renderPrompt(promptItem({ resolved: true, answer: "rejected" }));
    expect(screen.getByText(/ABORTED · stopped/)).toBeInTheDocument();
  });

  it("feedback wraps as rejected + feedback field (L1/L2 semantics)", () => {
    renderPrompt(promptItem({}));

    fireEvent.click(screen.getByText("Tell the agent something else…"));
    fireEvent.change(screen.getByPlaceholderText("Tell the agent something else…"), {
      target: { value: "use 50% instead" },
    });
    fireEvent.click(screen.getByText("Send"));

    expect(latestDecision).toEqual({
      taskId: "T1",
      answer: "rejected",
      feedback: "use 50% instead",
    });
  });

  it("Enter approves from the keyboard when nothing interactive holds focus", () => {
    renderPrompt(promptItem({}));
    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(latestDecision).toEqual({ taskId: "T1", answer: "approved" });
  });

  it("Esc rejects the gate from the keyboard", () => {
    renderPrompt(promptItem({}));
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(latestDecision).toEqual({ taskId: "T1", answer: "rejected" });
  });

  it("yields to the focused element — a focused button's Enter must NOT approve", () => {
    // Safety contract: typing in the composer or activating a focused
    // button must never trip the global approve shortcut (a stray
    // Enter must not authorise a production injection).
    renderPrompt(promptItem({}));
    const approveBtn = screen.getByText("inject");
    approveBtn.focus();
    fireEvent.keyDown(approveBtn, { key: "Enter" });
    // Probe mirrors the reducer's initial pendingDecision (null), so
    // "no decision" asserts null, not undefined.
    expect(latestDecision).toBeNull();
  });

  it("decides exactly once — held-key repeat / double activation is swallowed", () => {
    renderPrompt(promptItem({}));
    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(latestDecision).toEqual({ taskId: "T1", answer: "approved" });
    // A second decision (Esc held down, double-tap, …) must not
    // overwrite the first answer.
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(latestDecision).toEqual({ taskId: "T1", answer: "approved" });
  });

  it("multi-prompt race: one Enter approves only the FIRST unresolved gate", () => {
    // The reducer's documented protocol race (L1492): an L1
    // intent_confirm left unresolved when the L2 confirmation_gate
    // arrives stays live in pending alongside it. The keyboard must
    // answer gates one at a time (the TUI MainContent's
    // firstUnresolvedPromptId contract) — one keystroke, one gate.
    const first = promptItem({ id: "c-prompt-l1", taskId: "T-L1" });
    const second = promptItem({ id: "c-prompt-l2", taskId: "T-L2" });
    render(
      <StoreProvider initial={{ pending: [first, second] }}>
        <Probe />
        <ConfirmPromptView item={first} />
        <ConfirmPromptView item={second} />
      </StoreProvider>,
    );
    fireEvent.keyDown(document.body, { key: "Enter" });
    // Without the first-unresolved gate BOTH window listeners fire
    // and the second dispatch overwrites pendingDecision (one Enter
    // approving two production gates). With it, only T-L1 answers.
    expect(latestDecision).toEqual({ taskId: "T-L1", answer: "approved" });
  });

  it("hides the kbd hint while the feedback box owns Enter/Esc", () => {
    renderPrompt(promptItem({}));
    expect(
      screen.getByText("Enter ↵ confirm · Esc cancel"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("Tell the agent something else…"));
    expect(
      screen.queryByText("Enter ↵ confirm · Esc cancel"),
    ).not.toBeInTheDocument();
  });

  it("plan_builder options dispatch the raw key; free_input sends the text", () => {
    renderPrompt(
      promptItem({
        node: "plan_builder",
        payload: {
          options: [
            { key: "A", label: "Plan A", recommended: true },
            { key: "free_input", label: "Free input" },
          ],
        },
      }),
    );

    fireEvent.click(screen.getByText(/Plan A/));
    expect(latestDecision).toEqual({ taskId: "T1", answer: "A" });

    cleanup();
    latestDecision = undefined;
    renderPrompt(
      promptItem({
        node: "plan_builder",
        payload: { options: [{ key: "free_input", label: "Free input" }] },
      }),
    );
    fireEvent.click(screen.getByText("Free input"));
    fireEvent.change(
      screen.getByPlaceholderText("Tell the agent something else…"),
      { target: { value: "my own plan" } },
    );
    fireEvent.click(screen.getByText("Send"));
    // plan_builder free_input: the text IS the answer, no rejected wrap.
    expect(latestDecision).toEqual({ taskId: "T1", answer: "my own plan" });
  });

  it("plan_builder WITHOUT options falls back to a standing free-input box", () => {
    // The TUI's PlanSelectionPrompt fallback: no server options → free
    // input is the only control. An approve/reject row here would send
    // "approved" to a resume contract that expects a plan key.
    renderPrompt(promptItem({ node: "plan_builder", payload: {} }));
    // No approve/reject buttons (the default-row labels are
    // proceed/refine) …
    expect(screen.queryByText("proceed")).not.toBeInTheDocument();
    expect(screen.queryByText("refine")).not.toBeInTheDocument();
    // … the input box stands open with the free-input placeholder …
    fireEvent.change(screen.getByPlaceholderText("Free input"), {
      target: { value: "split into two steps" },
    });
    fireEvent.click(screen.getByText("Send"));
    expect(latestDecision).toEqual({
      taskId: "T1",
      answer: "split into two steps",
    });
  });
});
