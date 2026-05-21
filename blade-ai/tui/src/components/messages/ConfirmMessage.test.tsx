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

    it("renders the v3 [✻ INTENT] bracket title chip", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      const frame = lastFrame() ?? "";
      // chip label is uppercased ASCII so it survives the box-drawing
      // strip; we don't depend on any CJK title text here.
      expect(frame).toContain("[");
      expect(frame).toContain("INTENT");
      expect(frame).toContain("]");
    });

    it("renders the '决策信号' section heading when risk or confidence exists", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").toContain("决策信号");
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

    it("does NOT render the inline plan_summary body", () => {
      // The inline plan_summary field was retired — its 500-char
      // markdown payload routinely pushed the card past viewport rows
      // and triggered Ink cursor desync (ghost copies of dyn frame
      // leaking into scrollback). The plan file path (plan_path) is
      // now the only "plan" indicator; ``cat <planPath>`` is the
      // escape hatch for full content. Lock the contract here so a
      // future refactor doesn't re-introduce the inline body
      // accidentally.
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      expect(lastFrame() ?? "").not.toContain(
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

    it("renders the v3 [⚠ EXECUTE] bracket title chip", () => {
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("[");
      expect(frame).toContain("EXECUTE");
      expect(frame).toContain("]");
    });

    it("places the safety section at the BOTTOM when status is safe", () => {
      // 'safe' status renders quietly inside the '── 安全检查' section
      // headed by a divider line — never as a top alert.
      // We match the divider-prefixed section heading so the card
      // title '确认执行计划' doesn't collide with the substring check.
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("── 安全检查");
      // Sanity check: '── 执行计划' heading must appear ABOVE
      // '── 安全检查' in the rendered frame (top-to-bottom order).
      const planIdx = frame.indexOf("── 执行计划");
      const safetyIdx = frame.indexOf("── 安全检查");
      expect(planIdx).toBeGreaterThan(-1);
      expect(safetyIdx).toBeGreaterThan(-1);
      expect(planIdx).toBeLessThan(safetyIdx);
    });

    it("floats the safety alert to the TOP when status is a warning", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          plan_summary: "blade create node cpu fullload",
          safety_status: "warning",
          safety_reason: "compound effects possible",
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      // Top alert: WARNING badge must appear BEFORE the
      // '── 执行计划' section heading.
      const warningIdx = frame.indexOf("WARNING");
      const planHeadingIdx = frame.indexOf("── 执行计划");
      expect(warningIdx).toBeGreaterThan(-1);
      expect(planHeadingIdx).toBeGreaterThan(-1);
      expect(warningIdx).toBeLessThan(planHeadingIdx);
      // And the bottom '── 安全检查' heading should NOT appear —
      // adaptive placement means safety shows in exactly one place.
      expect(frame).not.toContain("── 安全检查");
    });

    it("renders the Parameters section when payload carries a params dict", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          target: { namespace: "cms-demo", names: ["node-1"] },
          plan_summary: "blade create",
          safety_status: "safe",
          params: { cpu_percent: 80, timeout: 600 },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("── 故障参数");
      expect(frame).toContain("cpu_percent=80");
      expect(frame).toContain("timeout=600");
    });

    it("ALWAYS renders Parameters + Target health sections (even when empty)", () => {
      // Empty payload — no params, no health_report. The two sections
      // should still render with placeholder values so the reader
      // knows "agent did look at this, there's just nothing notable".
      // Previously these sections were gated on ``hasParamsContent``
      // / ``hasHealthIssues`` and disappeared on a clean turn, which
      // read as "agent forgot to check".
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          target: { namespace: "default", names: ["node-1"] },
          plan_summary: "blade create",
          safety_status: "safe",
          // no params
          // no target_health_report
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("── 故障参数");
      // Empty params → "—" placeholder
      expect(frame).toContain("—");
      expect(frame).toContain("── 目标健康");
      // No health_report → "check not run" placeholder
      expect(frame).toContain("未执行检查");
    });

    it("renders Target health 'all clear' when check ran with no issues", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          target: { namespace: "default", names: ["node-1"] },
          plan_summary: "blade create",
          safety_status: "safe",
          target_health_report: {
            overall: "ok",
            summary: "",
            issues: [],
          },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("── 目标健康");
      expect(frame).toContain("目标无异常");
    });

    it("renders the Target health section when health_report.overall != ok", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          target: { namespace: "default", names: ["node-1"] },
          plan_summary: "blade create",
          safety_status: "safe",
          target_health_report: {
            overall: "block",
            summary: "node.disk_pressure(block)",
            issues: [
              {
                severity: "block",
                code: "node.disk_pressure",
                message: "DiskPressure condition active",
                duration_hint: "103d",
              },
            ],
          },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("── 目标健康");
      expect(frame).toContain("node.disk_pressure");
      expect(frame).toContain("DiskPressure");
      expect(frame).toContain("103d");
    });

    it("renders the Conflicting experiments section + hint when conflict_uids non-empty", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          target: { namespace: "default", names: ["node-1"] },
          plan_summary: "blade create",
          safety_status: "warning",
          conflict_uids: ["b62ac6d9b907d620", "32e2dd17209337c0"],
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("── 冲突实验");
      expect(frame).toContain("b62ac6d9b907d620");
      expect(frame).toContain("32e2dd17209337c0");
      expect(frame).toContain("/show experiments");
    });

    it("renders 'Attempt N' inside the Execution-plan section when pipeline_attempt > 1", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          plan_summary: "blade create",
          safety_status: "safe",
          pipeline_attempt: 2,
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      // Attempt now lives as a Field row under '── 执行计划', not
      // as a title suffix. The number itself + the label both show.
      expect(frame).toContain("第 2 次尝试");
      expect(frame).toContain("尝试次数");
      // And it must NOT appear inline next to the card title.
      const titleLine =
        frame.split("\n").find((l) => l.includes("EXECUTE")) ?? "";
      expect(titleLine).not.toContain("第 2 次尝试");
    });

    it("renders a two-line health row when the issue code exceeds 11 chars", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          plan_summary: "blade create",
          safety_status: "safe",
          target_health_report: {
            overall: "block",
            summary: "node.disk_pressure(block)",
            issues: [
              {
                severity: "block",
                code: "node.disk_pressure", // 18 chars > 11 → two-line
                message: "DiskPressure condition active on kubelet",
                duration_hint: "103d",
              },
            ],
          },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      const lines = frame.split("\n").map((l) => l.replace(/[│╭╮╰╯─━]/g, "").trim());
      // code on one line, message on a separate line — the two
      // shouldn't share a row when the code overflows the name col.
      const codeLine = lines.find((l) => l.includes("node.disk_pressure"));
      expect(codeLine).toBeDefined();
      expect(codeLine).not.toContain("DiskPressure");
    });

    it("skips the Execution-plan heading when no plan fields are present", () => {
      // Safety-only payload (e.g. early policy block) — must not
      // render a dangling '── 执行计划' heading with an empty body.
      // (The card title '确认执行计划' still appears at the top;
      // we check for the divider-prefixed heading instead.)
      const safetyOnly = baseContext({
        node: "confirmation_gate",
        payload: {
          safety_status: "blocked",
          safety_reason: "namespace not allowed",
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={safetyOnly} />);
      const frame = lastFrame() ?? "";
      expect(frame).not.toContain("── 执行计划");
      // Safety alert still appears at the top.
      expect(frame).toContain("BLOCKED");
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

  it("collapses to the ARMED chip once resolved=approved", () => {
    // Forge × Operator redesign: resolved-approved collapses to a
    // chip "● ARMED · 继续执行" (operator vocabulary) rather than
    // the previous neutral "已确认" line.
    const item = basePrompt({
      node: "intent_confirm",
      resolved: true,
      answer: "approved",
    });
    const { lastFrame } = render(<ConfirmPromptMessage item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("ARMED");
    expect(frame).not.toContain("提交意图");
  });

  it("collapses to the ABORTED chip once resolved=rejected", () => {
    // Resolved-rejected collapses to "● ABORTED · 已停止" — the
    // operator-vocabulary counterpart of the ARMED chip above.
    const item = basePrompt({
      node: "confirmation_gate",
      resolved: true,
      answer: "rejected",
    });
    const { lastFrame } = render(<ConfirmPromptMessage item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("ABORTED");
    expect(frame).not.toContain("开始注入");
  });
});
