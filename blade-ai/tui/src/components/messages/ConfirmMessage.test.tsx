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

    it("renders risk / confidence rows when payload carries them", () => {
      // Section heading was removed 2026-05-26 per UX request; the
      // rows themselves stay. We assert the risk-meter glyph and the
      // confidence percentage to confirm the content didn't regress.
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      const frame = lastFrame() ?? "";
      // Risk meter renders a tier glyph + target word.
      expect(frame).toMatch(/▁|▂|▃|▅|█/);
      // Confidence row prints a percentage.
      expect(frame).toMatch(/\d+%/);
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

    it("places the safety row at the BOTTOM when status is safe", () => {
      // Section dividers were removed 2026-05-26; we now use the
      // skill-name row as the top anchor and the SAFE glyph as the
      // bottom marker to confirm the ordering survived.
      const { lastFrame } = render(<ConfirmContextMessage item={item} />);
      const frame = lastFrame() ?? "";
      const skillIdx = frame.indexOf("node-cpu-fullload");
      const safetyIdx = frame.indexOf("SAFE");
      expect(skillIdx).toBeGreaterThan(-1);
      expect(safetyIdx).toBeGreaterThan(-1);
      expect(skillIdx).toBeLessThan(safetyIdx);
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
      // WARNING badge must appear BEFORE the skill-name row.
      const warningIdx = frame.indexOf("WARNING");
      const skillIdx = frame.indexOf("node-cpu-fullload");
      expect(warningIdx).toBeGreaterThan(-1);
      expect(skillIdx).toBeGreaterThan(-1);
      expect(warningIdx).toBeLessThan(skillIdx);
      // Adaptive placement: warning shouldn't ALSO render as a bottom
      // SAFE row — only one safety chip per card.
      expect(frame).not.toContain("SAFE");
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
      expect(frame).toContain("cpu_percent=80");
      expect(frame).toContain("timeout=600");
    });

    it("ALWAYS renders Parameters + Target health rows (even when empty)", () => {
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
      // Empty params → "—" placeholder
      expect(frame).toContain("—");
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
      expect(frame).toContain("node.disk_pressure");
      expect(frame).toContain("DiskPressure");
      expect(frame).toContain("103d");
    });

    it("renders the feasibility section when severity is impossible", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "pod-mem-load",
          target: { namespace: "cms-demo", names: ["accounting-xyz"] },
          plan_summary: "blade create",
          safety_status: "safe",
          feasibility_report: {
            severity: "impossible",
            headroom: 0.021,
            current_value: "230Mi (95.8%)",
            limit_value: "240Mi",
            target_value: "235Mi (98%)",
            message: "Memory at 95.8% (230Mi/240Mi), target 98% — only 5Mi headroom",
            recommendation: "Pick a Pod with lower memory usage",
          },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("230Mi/240Mi");
      expect(frame).toContain("only 5Mi");
      expect(frame).toContain("Pick a Pod");
    });

    it("renders feasibility all-clear when severity is ok", () => {
      const cr = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "pod-mem-load",
          target: { namespace: "cms-demo", names: ["accounting-xyz"] },
          plan_summary: "blade create",
          safety_status: "safe",
          feasibility_report: {
            severity: "ok",
            headroom: 0.38,
            current_value: "100Mi (41.7%)",
            limit_value: "240Mi",
            target_value: "192Mi (80%)",
            message: "Sufficient headroom (38%)",
            recommendation: "",
          },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={cr} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("注入可行");
      expect(frame).toContain("headroom 38%");
      expect(frame).toContain("100Mi (41.7%)");
      expect(frame).toContain("192Mi");
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
      // Attempt now lives as a Field row inside the execution-plan block, not
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

    it("skips the Execution-plan rows when no plan fields are present", () => {
      // Safety-only payload (e.g. early policy block) — must not
      // render skill / target rows with empty values. Section dividers
      // were removed 2026-05-26; we assert the absence of the inline
      // skill label as a proxy for "execution-plan block didn't render".
      const safetyOnly = baseContext({
        node: "confirmation_gate",
        payload: {
          safety_status: "blocked",
          safety_reason: "namespace not allowed",
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={safetyOnly} />);
      const frame = lastFrame() ?? "";
      // Field labels for skill/target should NOT render when there's
      // no plan content.
      expect(frame).not.toContain("技能");
      // Safety alert still appears at the top.
      expect(frame).toContain("BLOCKED");
    });

    it("renders the safety score panel when payload.safety_score is present", () => {
      // E10 — multi-dimensional safety score should render overall +
      // 4 dimensions when the payload carries it.
      const withScore = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "pod-cpu-fullload",
          target: { namespace: "prod", names: ["api-gateway"] },
          plan_summary: "blade create",
          safety_status: "safe",
          safety_reason: null,
          safety_score: {
            overall: 78,
            level: "high",
            blast_radius: { value: 30, explanation: "scope=pod (30), 1 target (+0)" },
            frequency: { value: 0, explanation: "no conflicts, first attempt" },
            time: { value: 70, explanation: "business hours, weekday" },
            topology: { value: 70, explanation: "production namespace 'prod'" },
            weights: { blast_radius: 0.4, topology: 0.3, frequency: 0.2, time: 0.1 },
          },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={withScore} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("78");                       // overall value
      expect(frame).toContain("production namespace");     // topology explanation
      expect(frame).toContain("business hours");           // time explanation
    });

    it("omits the safety score panel when payload.safety_score is missing", () => {
      // Backward compatibility — older server builds don't emit
      // safety_score. Card should render normally without the panel.
      const noScore = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "pod-cpu-fullload",
          target: { namespace: "default", names: ["p1"] },
          plan_summary: "blade create",
          safety_status: "safe",
          safety_reason: null,
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={noScore} />);
      const frame = lastFrame() ?? "";
      // Score panel section heading should NOT appear when no score data.
      // We look for the en/zh i18n strings for safety_score section.
      expect(frame).not.toContain("Safety score");
      expect(frame).not.toContain("风险评分");
    });

    it("renders the Fault row when payload.fault_intent is populated", () => {
      // task-f8320b6ff844 regression: the L2 card previously had no
      // semantic indicator of fault category — operators had to guess
      // from ``params`` keys. Wiring fault_intent from confirmation_gate
      // to ExecutionConfirmCard surfaces the L1 classification.
      const withIntent = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "k8s-chaos-skills",
          target: { namespace: "cms-demo", names: ["accounting-6fbdb464c7-qn2vr"] },
          plan_summary: "blade create",
          safety_status: "safe",
          safety_reason: null,
          fault_intent: {
            fault_type: "pod-mem-load",
            scope: "pod",
            target: "mem",
            action: "load",
          },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={withIntent} />);
      const frame = lastFrame() ?? "";
      // The fault_type appears verbatim.
      expect(frame).toContain("pod-mem-load");
      // The (scope/target/action) triple appears as supplementary context.
      expect(frame).toContain("pod/mem/load");
    });

    it("omits the Fault row when fault_intent is missing/null", () => {
      // Dry-run / clarification-incomplete path: producer sets
      // fault_intent=None when FaultSpec has no derivable fault_type.
      // The row must not render an empty "Fault: " line in that case.
      const noIntent = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "skill",
          target: { namespace: "default", names: ["p1"] },
          plan_summary: "blade create",
          safety_status: "safe",
          fault_intent: null,
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={noIntent} />);
      const frame = lastFrame() ?? "";
      // Skill still renders, but no Fault label.
      expect(frame).toContain("skill");
      expect(frame).not.toContain("故障");
      expect(frame).not.toContain("Fault");
    });

    it("omits the Fault row when fault_intent.fault_type is empty string", () => {
      // Half-populated dict edge case: rendering must guard on
      // ``fault_type`` truthiness (not just dict presence) — otherwise
      // an empty string would render as "  ()" or just "  " in the
      // value column.
      const blankType = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "skill",
          plan_summary: "blade create",
          safety_status: "safe",
          fault_intent: { fault_type: "", scope: "pod", target: "", action: "" },
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={blankType} />);
      const frame = lastFrame() ?? "";
      expect(frame).not.toContain("故障");
      expect(frame).not.toContain("Fault");
    });

    it("renders Complexity row only when is_complex === true", () => {
      // is_complex=true badge surfaces "this routed through
      // save_fault_plan and has a formal plan". The TS code was
      // previously not reading the field at all (ConfirmMessage.tsx
      // had a comment naming it but no read site).
      const complexPlan = baseContext({
        node: "confirmation_gate",
        payload: {
          skill_name: "node-cpu-fullload",
          plan_summary: "complex plan markdown",
          safety_status: "safe",
          is_complex: true,
        },
      });
      const { lastFrame } = render(<ConfirmContextMessage item={complexPlan} />);
      const frame = lastFrame() ?? "";
      // i18n strings (zh dict is active by default in tests; check both
      // so the test isn't locale-coupled).
      const matchesZh = frame.includes("复杂度") || frame.includes("复杂任务");
      const matchesEn = frame.includes("Complexity") || frame.includes("complex");
      expect(matchesZh || matchesEn).toBe(true);
    });

    it("omits Complexity row for simple plans (is_complex absent or false)", () => {
      // Silence on the happy path — simple plans don't carry a
      // redundant "simple plan" badge. Guarantee that false / undefined
      // both suppress the row.
      for (const flag of [false, undefined]) {
        const simple = baseContext({
          node: "confirmation_gate",
          payload: {
            skill_name: "node-cpu-fullload",
            plan_summary: "simple plan",
            safety_status: "safe",
            ...(flag !== undefined ? { is_complex: flag } : {}),
          },
        });
        const { lastFrame } = render(<ConfirmContextMessage item={simple} />);
        const frame = lastFrame() ?? "";
        expect(frame).not.toContain("复杂度");
        expect(frame).not.toContain("Complexity");
        expect(frame).not.toContain("复杂任务");
      }
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
