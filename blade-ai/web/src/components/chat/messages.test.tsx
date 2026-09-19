/**
 * Message stream rendering: one fixture per P1 kind plus the
 * unknown-kind fallback. Fixtures are prefilled into the store via
 * StoreProvider's ``initial`` escape hatch and rendered through the
 * real HistoryList, so the assertions cover the dispatch switch and
 * the individual renderers together.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { HistoryItem, ToolItem } from "@blade-ai/core";
import { StoreProvider, configureI18n } from "@blade-ai/core";
import { HistoryList } from "./HistoryList";

configureI18n("en");

afterEach(() => cleanup());

const toolRunning: ToolItem = {
  kind: "tool",
  id: "tool-1",
  callId: "c1",
  name: "blade_create",
  node: "execute",
  status: "running",
  resultPreview: "",
  raw: "",
  startedAt: 0,
};

const toolDone: ToolItem = {
  ...toolRunning,
  id: "tool-2",
  status: "success",
  resultPreview: "experiment uid=abc123",
  raw: "experiment uid=abc123\nfull raw output",
  elapsedMs: 1500,
  locator: "T1",
};

function renderHistory(history: HistoryItem[]) {
  return render(
    <StoreProvider initial={{ history }}>
      <HistoryList />
    </StoreProvider>,
  );
}

describe("HistoryList message renderers", () => {
  it("renders the user bubble and the agent reply", () => {
    renderHistory([
      { kind: "user", id: "u1", text: "inject cpu pressure" },
      { kind: "agent", id: "a1", text: "on it — planning the blast radius" },
    ]);
    expect(screen.getByText("inject cpu pressure")).toBeInTheDocument();
    expect(
      screen.getByText("on it — planning the blast radius"),
    ).toBeInTheDocument();
  });

  it("renders thinking / log / error / system rows", () => {
    renderHistory([
      { kind: "thinking", id: "th1", durationMs: 2000 },
      {
        kind: "log",
        id: "l1",
        level: "warn",
        text: "slow backend",
        tag: "execute",
      },
      { kind: "error", id: "e1", text: "boom happened" },
      { kind: "system", id: "s1", text: "session resumed" },
    ]);
    expect(screen.getByText(/Thought for 2s/)).toBeInTheDocument();
    expect(screen.getByText("slow backend")).toBeInTheDocument();
    expect(screen.getByText(/execute ·/)).toBeInTheDocument();
    expect(screen.getByText("boom happened")).toBeInTheDocument();
    expect(screen.getByText("session resumed")).toBeInTheDocument();
  });

  it("renders tool cards: running state, finished state with full raw output and locator", () => {
    renderHistory([toolRunning, toolDone]);
    expect(screen.getAllByText("blade_create")).toHaveLength(2);
    expect(screen.getByText("running…")).toBeInTheDocument();
    // Full ``raw`` wins over the single-line ``resultPreview`` (same
    // contract as the TUI card) — both raw lines render, not just the
    // 80-char teaser.
    expect(screen.getByText(/experiment uid=abc123/)).toBeInTheDocument();
    expect(screen.getByText(/full raw output/)).toBeInTheDocument();
    expect(screen.getByText("1.5s")).toBeInTheDocument();
    expect(screen.getByText("T1")).toBeInTheDocument();
  });

  it("pulses only the running tool's status dot, reduced-motion safe", () => {
    // The right-rail "tool execution queue" panel was cancelled in
    // favour of these chat cards (zero-duplication rule); its one real
    // increment — the alive feel of a spinner — lives here as the
    // running dot's pulse, always paired with motion-reduce fallback.
    const { container } = renderHistory([toolRunning, toolDone]);
    const pulsing = container.querySelectorAll(".animate-pulse");
    expect(pulsing).toHaveLength(1);
    expect(pulsing[0].className).toContain("motion-reduce:animate-none");
    expect(pulsing[0].className).toContain("bg-warning-dot");
  });

  it("renders a tool_group as one card per tool", () => {
    renderHistory([
      { kind: "tool_group", id: "g1", tools: [toolRunning, toolDone] },
    ]);
    expect(screen.getAllByText("blade_create")).toHaveLength(2);
  });

  it("renders the no-output placeholder for a finished tool with empty body", () => {
    renderHistory([
      {
        ...toolDone,
        id: "tool-3",
        resultPreview: "",
        raw: "",
        locator: undefined,
      },
    ]);
    expect(screen.getByText("(no output)")).toBeInTheDocument();
  });

  it("honours placeholderKey — sanitized tools point at the confirm card", () => {
    renderHistory([
      {
        ...toolDone,
        id: "tool-4",
        resultPreview: "",
        raw: "",
        locator: undefined,
        placeholderKey: "tool.captured_in_confirm",
      },
    ]);
    expect(
      screen.getByText("(output delivered via the confirm card below)"),
    ).toBeInTheDocument();
  });

  it("suppresses the no-output placeholder for a canceled tool", () => {
    // TUI contract: an empty body on a canceled call is expected (the
    // status glyph already says what happened) — rendering "(no
    // output)" there reads like missing data.
    renderHistory([
      {
        ...toolDone,
        id: "tool-5",
        status: "canceled",
        resultPreview: "",
        raw: "",
        locator: undefined,
      },
    ]);
    expect(screen.queryByText("(no output)")).not.toBeInTheDocument();
  });

  it("renders a result card with fault details", () => {
    renderHistory([
      {
        kind: "result",
        id: "r1",
        taskId: "task-1",
        status: "success",
        faultType: "pod-cpu fullload",
        experimentUid: "abc123",
        duration: "60s",
        summary: "cpu at 80%",
      },
    ]);
    expect(screen.getByText("Injection succeeded")).toBeInTheDocument();
    expect(screen.getByText(/pod-cpu fullload/)).toBeInTheDocument();
    expect(screen.getByText("cpu at 80%")).toBeInTheDocument();
  });

  it("renders the full result card: outcome fields, attempts, side effects, replay hint", () => {
    renderHistory([
      {
        kind: "result",
        id: "r2",
        taskId: "task-2",
        status: "success",
        faultType: "pod-cpu fullload",
        experimentUid: "uid-abc",
        duration: "60s",
        summary: "cpu at 80%",
        target: { namespace: "demo", names: ["web-1", "web-2"] },
        replanCount: 2,
        sideEffects: ["ContainerRestarts · 1"],
      },
    ]);
    expect(screen.getByText("Outcome")).toBeInTheDocument();
    expect(screen.getByText("demo · web-1, web-2")).toBeInTheDocument();
    expect(screen.getByText("uid-abc")).toBeInTheDocument();
    expect(screen.getByText("succeeded after 2 auto-replan(s)")).toBeInTheDocument();
    expect(screen.getByText("Side effects")).toBeInTheDocument();
    expect(screen.getByText("ContainerRestarts · 1")).toBeInTheDocument();
    // Replay hint carries the task id.
    expect(
      screen.getByText("/replay task-2 instant — for full timeline"),
    ).toBeInTheDocument();
  });

  it("renders failure analysis for failed results and recovery notes for partial", () => {
    renderHistory([
      {
        kind: "result",
        id: "r3",
        taskId: "task-3",
        status: "failed",
        faultType: "pod-network loss",
        experimentUid: "",
        duration: "",
        summary: "",
        cause: "target pod not found",
        hint: "check the label selector",
      },
    ]);
    expect(screen.getByText("Failure analysis")).toBeInTheDocument();
    expect(screen.getByText("target pod not found")).toBeInTheDocument();
    expect(screen.getByText("check the label selector")).toBeInTheDocument();

    cleanup();
    renderHistory([
      {
        kind: "result",
        id: "r4",
        taskId: "task-4",
        status: "partial",
        faultType: "pod-mem load",
        experimentUid: "uid-x",
        duration: "30s",
        summary: "mem at 60%",
        cause: "recovered early",
        hint: "retry with longer duration",
      },
    ]);
    expect(screen.getByText("Recovery notes")).toBeInTheDocument();
    expect(screen.getByText("recovered early")).toBeInTheDocument();
  });

  it("renders postmortem and alternatives cards when present", () => {
    renderHistory([
      {
        kind: "result",
        id: "r5",
        taskId: "task-5",
        status: "success",
        faultType: "pod-cpu fullload",
        experimentUid: "uid-pm",
        duration: "60s",
        summary: "ok",
        postmortem: {
          path: "/tmp/postmortem.md",
          markdown: "# Postmortem body",
          summary: "short summary",
        },
        alternatives: "- try pod-mem instead",
      },
    ]);
    expect(screen.getByText("Postmortem")).toBeInTheDocument();
    // Bodies render through the Markdown pipeline — assert the
    // SEMANTIC elements (h1 / li), not the raw source markers.
    expect(
      screen.getByRole("heading", { name: "Postmortem body" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/\/tmp\/postmortem.md/)).toBeInTheDocument();
    expect(screen.getByText("Alternatives")).toBeInTheDocument();
    expect(
      screen.getByText("try pod-mem instead").closest("li"),
    ).not.toBeNull();
  });

  it("renders turn_usage and memory_compaction as flat dim rows, not the JSON fallback", () => {
    renderHistory([
      {
        kind: "memory_compaction",
        id: "mc1",
        succeeded: true,
        tokensBefore: 12000,
        tokensAfter: 4500,
        messagesCompacted: 9,
        durationMs: 6300,
        layer: "llm_summary",
      },
      {
        kind: "turn_usage",
        id: "tu1",
        inputTokens: 198,
        outputTokens: 89,
        cachedTokens: 0,
        endedAt: new Date(2026, 7, 20, 14, 32, 7).getTime(),
      },
    ]);
    // compaction line (success path) and the ⚡ usage row both show
    // inline — never collapsed behind the UnknownMessage <details>.
    expect(screen.getByText(/12\.0k → 4\.5k tokens/)).toBeInTheDocument();
    expect(screen.getByText(/turn used 287 tokens/)).toBeInTheDocument();
    expect(screen.queryByText(/\[turn_usage\]/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/\[memory_compaction\]/),
    ).not.toBeInTheDocument();
  });

  it("routes all nine info-card kinds to their dedicated renderers", () => {
    // Regression guard for the dispatch switch: every card kind from
    // core's shared slash commands + boot seeds must render its card
    // title — never fall through to the UnknownMessage JSON block.
    renderHistory([
      {
        kind: "welcome_card",
        id: "w1",
        modelName: "m",
        permissionMode: "confirm",
        kubeconfig: "",
        namespace: "default",
        version: "0.1.0",
      },
      {
        kind: "boot_doctor_card",
        id: "bd1",
        checks: [],
        passedCount: 0,
        totalCount: 0,
        capturedAt: "",
      },
      { kind: "pending_tasks_card", id: "pt1", tasks: [] },
      {
        kind: "runtime_doctor_card",
        id: "rd1",
        reachable: true,
        serverUrl: "http://x",
        cluster: "c",
        tuiVersion: "0.1.0",
        serverVersion: "0.1.0",
        tuiProtocol: "1",
        serverProtocol: "1",
        lang: "en",
        mode: "confirm",
        capturedAt: "",
        checks: [],
        passedCount: 0,
        totalCount: 0,
        preflightUnavailable: false,
      },
      {
        kind: "memory_card",
        id: "mc1",
        sessionId: "sess_x",
        startedAt: "",
        status: "active",
        cluster: "c",
        namespace: "default",
        recentTasks: [],
        totalTasks: 0,
        stats: {},
        memoryDir: "",
        capturedAt: "",
      },
      { kind: "help_card", id: "hc1", capturedAt: "", sections: [], tip: "" },
      { kind: "session_card", id: "sc1", capturedAt: "", rows: [] },
      {
        kind: "experiments_card",
        id: "ec1",
        capturedAt: "",
        totalCount: 0,
        rows: [],
      },
      {
        kind: "model_card",
        id: "mo1",
        capturedAt: "",
        activeModel: "",
        apiBaseUrl: "",
        totalCount: 0,
        sections: [],
      },
    ]);
    expect(screen.getByText("Blade-ai")).toBeInTheDocument();
    expect(screen.getByText("Environment self-check")).toBeInTheDocument();
    expect(screen.getByText("Unfinished tasks")).toBeInTheDocument();
    expect(screen.getByText("Diagnostics")).toBeInTheDocument();
    expect(screen.getByText("Session memory")).toBeInTheDocument();
    expect(screen.getByText("Commands")).toBeInTheDocument();
    // "Session" appears twice: the session_card title AND the
    // memory_card's Session row label — both cards rendered.
    expect(screen.getAllByText("Session")).toHaveLength(2);
    expect(screen.getByText("Experiments")).toBeInTheDocument();
    expect(screen.getByText("Models")).toBeInTheDocument();
    // None of the nine fell through to the JSON fallback.
    expect(screen.queryByText(/no dedicated renderer/)).not.toBeInTheDocument();
  });

  it("keeps unknown kinds visible via the JSON fallback", () => {
    // A P2 kind (confirm prompt) must not vanish in P1 — losing it
    // would wedge the turn with no way for the user to answer.
    const confirmLike = {
      kind: "confirm",
      id: "c1",
      prompt: "approve injection?",
    } as unknown as HistoryItem;
    renderHistory([confirmLike]);
    expect(screen.getByText(/\[confirm\]/)).toBeInTheDocument();
  });
});
