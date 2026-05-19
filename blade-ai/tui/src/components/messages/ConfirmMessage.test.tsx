/**
 * Confirm-card render tests after the M2 split:
 *
 *   * ``ConfirmContextMessage`` carries the heavy plan / safety / intent
 *     body — three node-specific layouts (Layer 1 / Layer 2 / generic)
 *     selected from ``item.node`` + ``payload`` shape. Renders WITHOUT
 *     a select widget — the prompt now lives in its own pending item.
 *   * ``ConfirmPromptMessage`` carries only the live select widget +
 *     resolved-state collapse line.
 */

import { render as inkRender } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import {
  ConfirmContextMessage,
  ConfirmPromptMessage,
} from "./ConfirmMessage.js";
import { StoreProvider } from "../../state/store.js";
import type {
  ConfirmContextItem,
  ConfirmPromptItem,
} from "../../state/types.js";

const baseContext = (
  overrides: Partial<ConfirmContextItem> = {},
): ConfirmContextItem => ({
  kind: "confirm_context",
  id: "c-ctx-1",
  taskId: "task-abc",
  content: "(plain content)",
  ...overrides,
});

const basePrompt = (
  overrides: Partial<ConfirmPromptItem> = {},
): ConfirmPromptItem => ({
  kind: "confirm_prompt",
  id: "c-prompt-1",
  taskId: "task-abc",
  selectedIndex: 0,
  mode: "select",
  feedback: "",
  resolved: false,
  ...overrides,
});

const render = (node: React.JSX.Element) =>
  inkRender(<StoreProvider>{node}</StoreProvider>);

describe("ConfirmContextMessage", () => {
  describe("intent_confirm (Layer 1)", () => {
    const item = baseContext({
      node: "intent_confirm",
      content: "故障类型: node-cpu-fullload\n范围: node",
      payload: {
        type: "intent_confirm",
        fault_intent: {
          fault_type: "node-cpu-fullload",
          scope: "node",
          target: "cpu",
          action: "fullload",
          namespace: "cms-demo",
          names: ["cn-hongkong.10.0.1.101"],
          params: { cpu_percent: 80 },
          user_description: "注入cpu故障",
        },
        intent_confidence: 0.92,
      },
    });

    it("renders the fault_type field", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("node-cpu-fullload");
    });

    it("renders the namespace field", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("cms-demo");
    });

    it("renders the names field", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("cn-hongkong.10.0.1.101");
    });

    it("renders params as key=value pairs", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("cpu_percent=80");
    });

    it("formats intent_confidence as a percentage", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("92%");
    });

    it("includes the task id in the chrome", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("task-abc");
    });

    it("does NOT render any select-widget options", () => {
      // Post-split: the prompt lives in ConfirmPromptMessage; the
      // context card is read-only.
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).not.toContain("告诉 agent 别的话");
      expect(frame).not.toContain("提交意图");
    });
  });

  describe("confirmation_gate (Layer 2)", () => {
    const item = baseContext({
      node: "confirmation_gate",
      content: "blade create node cpu fullload",
      payload: {
        skill_name: "node-cpu-fullload",
        target: { namespace: "cms-demo", names: ["node-1"] },
        plan_summary: "blade create node cpu fullload --names node-1",
        safety_status: "safe",
        safety_reason: null,
      },
    });

    it("renders the skill name", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("node-cpu-fullload");
    });

    it("renders the plan summary verbatim", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain(
        "blade create node cpu fullload --names node-1",
      );
    });

    it("renders the safety status as the SAFE badge", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("SAFE");
    });

    it("composes the target line as namespace + names", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("cms-demo");
      expect(frame).toContain("node-1");
    });

    it("renders confirm_required as the WARNING badge with reason", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "pod-network-delay",
          plan_summary: "blade create pod network delay",
          safety_status: "confirm_required",
          safety_reason: "Same target already has an active experiment",
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("WARNING");
      const normalized = frame
        .replace(/[║╔╗╚╝═─│╭╮╰╯]/g, " ")
        .replace(/\s+/g, " ");
      expect(normalized).toContain(
        "Same target already has an active experiment",
      );
    });
  });

  describe("generic fallback", () => {
    it("renders raw content when payload is missing", () => {
      const item = baseContext({
        content: "raw content from older server",
      });
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("raw content from older server");
    });

    it("renders raw content when node is unknown", () => {
      const item = baseContext({
        node: "future_unknown_node",
        payload: { whatever: "data" },
        content: "fallback body text",
      });
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("fallback body text");
    });

    it("renders raw content when node is known but payload is missing", () => {
      const item = baseContext({
        node: "intent_confirm",
        content: "old-style content body",
      });
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("old-style content body");
    });
  });
});

describe("ConfirmPromptMessage", () => {
  it("shows intent-confirm option labels for Layer 1", () => {
    const item = basePrompt({ node: "intent_confirm" });
    const { lastFrame } = render(<ConfirmPromptMessage item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("提交意图");
    expect(frame).toContain("调整意图");
    expect(frame).toContain("告诉 agent 别的话…");
  });

  it("shows execution-gate option labels for Layer 2", () => {
    const item = basePrompt({ node: "confirmation_gate" });
    const { lastFrame } = render(<ConfirmPromptMessage item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("开始注入");
    expect(frame).toContain("取消");
  });

  it("collapses to the answered marker once resolved=approved", () => {
    const item = basePrompt({
      node: "intent_confirm",
      resolved: true,
      answer: "approved",
    });
    const { lastFrame } = render(<ConfirmPromptMessage item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("已确认");
    expect(frame).not.toContain("提交意图");
  });

  it("collapses to the cancelled marker once resolved=rejected", () => {
    const item = basePrompt({
      node: "confirmation_gate",
      resolved: true,
      answer: "rejected",
    });
    const { lastFrame } = render(<ConfirmPromptMessage item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("已取消");
    expect(frame).not.toContain("开始注入");
  });
});
