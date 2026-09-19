/**
 * TracePage: master list + detail audit view — field grid /
 * verification / span waterfall / error rendering, the not-found
 * envelope path, the /trace auto-select, and the legacy
 * /tasks/$taskId redirect (covered in router.test.tsx).
 *
 * The active-task refetch interval itself is not clock-tested here:
 * the gate is core's ``passesTasksFilter`` (already unit-tested) and
 * fake-timer + React Query combos are notoriously flaky.
 *
 * Same harness shape as TasksPage.test: a minimal in-memory router,
 * BootContext carrying a client with ``listTasks`` + ``getTaskMetric``
 * wired.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configureI18n, type BladeClient } from "@blade-ai/core";
import { BootContext } from "./bootContext";
import { TracePage } from "./TracePage";
import { ui } from "../lib/uiText";

configureI18n("en");

afterEach(cleanup);

/** A failed task with every field populated — exercises all branches. */
const DETAIL: Record<string, unknown> = {
  task_id: "inject-abc-123",
  status: "failed",
  phase: "verifying",
  safety_status: "passed",
  gmt_create: "2026-08-18T10:00:00",
  finished_at: "2026-08-18T10:02:03",
  duration_ms: 123_000,
  model_name: "qwen-max",
  experiment_uid: "blade-uid-1",
  target: {
    namespace: "cms",
    names: ["web-1", "web-2"],
    resource_type: "pod",
  },
  params: { scope: "pod", target: "cpu", action: "fullload", "cpu-percent": "80" },
  summary: {
    total_token_input: 1200,
    total_token_output: 800,
    total_llm_calls: 3,
    total_tool_calls: 5,
  },
  verification: {
    layer1: { status: "passed", details: "probe output within baseline" },
    layer2: { status: "failed", details: "LLM judged the effect insufficient" },
    warnings: ["slow probe", "clock skew"],
  },
  spans: [
    {
      node_name: "plan",
      start_time: 1000.0,
      duration_ms: 2000,
      token_input: 100,
      token_output: 50,
      tool_calls: [],
    },
    {
      node_name: "inject",
      start_time: 1002.0,
      duration_ms: 8000,
      tool_calls: ["blade_create", "blade_status", "blade_status"],
    },
    {
      node_name: "verify",
      start_time: 1010.0,
      duration_ms: 3000,
      error: "probe timeout",
    },
  ],
  error: "task failed: verification did not pass",
};

function renderTrace(
  taskId: string | null,
  getTaskMetric: (id: string) => Promise<Record<string, unknown>>,
  listRows: Record<string, unknown>[] = [DETAIL],
) {
  const client = {
    getTaskMetric: vi.fn(getTaskMetric),
    listTasks: vi.fn(async () => ({ tasks: listRows })),
  } as unknown as BladeClient;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rootRoute = createRootRoute({
    component: () => (
      <QueryClientProvider client={queryClient}>
        <BootContext.Provider
          value={{
                    client,
                    activeSessionId: "s",
                    resetSession: async () => {},
                    switchSession: async () => {},
                    langChoice: "browser",
                    setLangChoice: () => {},
                  }}
        >
          <Outlet />
        </BootContext.Provider>
      </QueryClientProvider>
    ),
  });
  const traceRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/trace",
    component: TracePage,
  });
  const traceDetailRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/trace/$taskId",
    component: TracePage,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([traceRoute, traceDetailRoute]),
    history: createMemoryHistory({
      initialEntries: [taskId ? `/trace/${taskId}` : "/trace"],
    }),
  });
  render(<RouterProvider router={router} />);
  return { router };
}

describe("TracePage", () => {
  it("renders the header, field grid, verification, spans and error", async () => {
    renderTrace("inject-abc-123", async () => DETAIL);

    // Header (h1): status + fault label. Scoped to the heading —
    // "failed" also appears as the L2 layer status further down, so an
    // unscoped text query is a two-element collision.
    const h1 = await screen.findByRole("heading", { level: 1 });
    expect(h1).toHaveTextContent("failed");
    expect(h1).toHaveTextContent("pod-cpu-fullload");
    expect(screen.getByText("inject-abc-123")).toBeInTheDocument();

    // Field grid — target composes namespace/names/type/params.
    expect(
      screen.getByText(/cms\/web-1, web-2 \(pod, scope=pod, target=cpu/),
    ).toBeInTheDocument();
    expect(screen.getByText(/verifying · safety passed/)).toBeInTheDocument();
    // Second-resolution window + duration, per the audit-view contract.
    expect(
      screen.getByText(/2026-08-18 10:00:00 → 2026-08-18 10:02:03/),
    ).toBeInTheDocument();
    expect(screen.getByText("qwen-max")).toBeInTheDocument();
    expect(
      screen.getByText(/1200↓ 800↑ tokens · LLM ×3 · tools ×5/),
    ).toBeInTheDocument();
    expect(screen.getByText("blade-uid-1")).toBeInTheDocument();

    // Verification: both layers with full details + the warnings list.
    // L1's "passed" span is unique on the page (the safety field embeds
    // its "passed" inside a longer text node, which exact-match text
    // queries don't hit).
    expect(screen.getByText("L1")).toBeInTheDocument();
    expect(screen.getByText("passed")).toBeInTheDocument();
    expect(
      screen.getByText("probe output within baseline"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("LLM judged the effect insufficient"),
    ).toBeInTheDocument();
    expect(screen.getByText(/2 warning/)).toBeInTheDocument();
    expect(screen.getByText("slow probe")).toBeInTheDocument();

    // Spans: names, duration/token/tool meta, and the summary line —
    // node count, window, token sum and tool total derive from the
    // rows (3 spans; 13s window; only `plan` carries tokens; inject
    // holds 3 tool calls).
    expect(screen.getByText("plan")).toBeInTheDocument();
    expect(screen.getByText("verify")).toBeInTheDocument();
    // plan's row meta: duration + tokens; the summary line carries the
    // same token figures — disambiguate by the duration prefix.
    expect(screen.getByText(/2s · 100↓50↑/)).toBeInTheDocument();
    expect(
      screen.getByText(/blade_create×1 blade_status×2/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/3 nodes · Σ 13s · tokens 100↓50↑ · tools ×3/),
    ).toBeInTheDocument();

    // Error dump in full.
    expect(
      screen.getByText("task failed: verification did not pass"),
    ).toBeInTheDocument();
  });

  it("colours unverified with caution, not failure — distinct from unrecovered", async () => {
    // Honest ignorance (observation channel unavailable) must not read as
    // counter-evidence: "unverified" shares the caution colour with
    // "partial", while "unrecovered" keeps red. One page, both verdicts —
    // inject side spelled "overall", recover side spelled "level".
    const detail = {
      ...DETAIL,
      verification: { ...(DETAIL.verification as Record<string, unknown>), overall: "unverified" },
      recover_verification: { level: "unrecovered", layer1: { status: "failed" } },
    };
    renderTrace("inject-abc-123", async () => detail);

    const unknown = await screen.findByText("unverified");
    expect(unknown.className).toContain("text-warning");
    expect(unknown.className).not.toContain("text-danger");

    const notRecovered = screen.getByText("unrecovered");
    expect(notRecovered.className).toContain("text-danger");
  });

  it("auto-selects the newest task on the bare /trace route", async () => {
    const { router } = renderTrace(null, async () => DETAIL);
    // The list's first row wins; the URL becomes its deep link without
    // a history push (replace), and the detail body renders.
    await screen.findByRole("heading", { level: 1 });
    expect(router.state.location.pathname).toBe("/trace/inject-abc-123");
  });

  it("shows the empty state when no tasks exist", async () => {
    renderTrace(null, async () => DETAIL, []);
    expect(await screen.findByText(ui().traceEmpty)).toBeInTheDocument();
  });

  it("surfaces the server's not-found message as the error detail", async () => {
    renderTrace("nope", async () => {
      throw new Error("getTaskMetric: Task not found: nope");
    });
    expect(
      await screen.findByText(ui().taskDetailLoadFailed),
    ).toBeInTheDocument();
    expect(screen.getByText(/Task not found: nope/)).toBeInTheDocument();
  });

  it("renders placeholders for an empty detail record", async () => {
    renderTrace("t-x", async () => ({ task_id: "t-x" }));
    // No spans → the explicit none-recorded line; no error section.
    expect(await screen.findByText(ui().taskDetailSpansNone)).toBeInTheDocument();
    expect(screen.queryByText(ui().taskDetailError)).toBeNull();
    // Verification section is absent entirely without the payload.
    expect(screen.queryByText(ui().taskDetailVerification)).toBeNull();
  });

  it("sizes the span window to cover every span's end", async () => {
    // Regression pin for the window formula: a long early span plus a
    // short late one. The CLI's max-offset + last-duration formula
    // would give 51s here (and overflow the long bar); the web window
    // is max(offset + duration) = 100s → "1m40s".
    renderTrace("t-clip", async () => ({
      task_id: "t-clip",
      status: "success",
      phase: "done",
      spans: [
        { node_name: "long-runner", start_time: 1000, duration_ms: 100_000 },
        { node_name: "late", start_time: 1050, duration_ms: 1000 },
      ],
    }));
    expect(await screen.findByText(/Σ 1m40s/)).toBeInTheDocument();
  });

  it("renders the trace narrative: intent fields, timeline, checklist, recovery verdict, postmortem, feasibility", async () => {
    renderTrace("inject-trace-1", async () => ({
      ...DETAIL,
      task_id: "inject-trace-1",
      safety_reason: "whitelist matched",
      fault_spec: {
        case_resource_path: "skills/k8s/cpu.md",
        user_description: "压测 web 前端",
        duration_seconds: 600,
      },
      postmortem: {
        path: "/tmp/pm.md",
        summary: "演练成功，指标如期恶化。",
        // Background carries a bullet whose label happens to look like
        // a timestamp — only a section-scoped parse keeps it out of the
        // timeline (the rendered list must hold exactly two entries).
        markdown: [
          "## Summary", "", "演练成功。",
          "## Background", "", "- **09:59:59** 背景段的时间戳样行", "",
          "## Timeline", "",
          "- **10:00:01** 注入生效",
          "- **10:02:00** 验证通过", "",
          "## Key Metrics", "", "- CPU 80%",
        ].join("\n"),
      },
      verification: {
        // L1 passed + L2 failed → the holistic verdict lands between:
        // "partial" next to the section title.
        overall: "partial",
        layer1: { status: "passed", details: "probe output within baseline" },
        layer2: { status: "failed", details: "LLM judged the effect insufficient" },
        warnings: ["slow probe", "clock skew"],
        checklist: {
          items: [
            { step: 1, status: "passed", evidence: "cpu 80% observed" },
            { step: 2, status: "skipped", evidence: "no p95 probe" },
          ],
        },
      },
      recover_verification: {
        level: "recovered",
        layer1: { status: "passed", details: "pod ready" },
        layer2: { status: "passed", details: "" },
        warnings: [],
      },
      feasibility_report: {
        severity: "ok",
        message: "headroom sufficient",
        recommendation: "proceed",
      },
    }));

    // Intent fields from fault_spec.
    expect(await screen.findByText("skills/k8s/cpu.md")).toBeInTheDocument();
    expect(screen.getByText("压测 web 前端")).toBeInTheDocument();
    expect(screen.getByText("600s")).toBeInTheDocument();
    expect(screen.getByText(/whitelist matched/)).toBeInTheDocument();

    // Postmortem timeline entries — parsed client-side from the
    // markdown's Timeline section only: exactly two timestamp rows,
    // the Background lookalike stays out (the full-report <pre> holds
    // the raw markdown, so only exact-match standalone spans count).
    expect(screen.getAllByText(/^\d{2}:\d{2}:\d{2}$/)).toHaveLength(2);
    expect(screen.getByText("注入生效")).toBeInTheDocument();
    expect(screen.getByText("验证通过")).toBeInTheDocument();

    // Checklist items with evidence (glyph column + step + status).
    expect(screen.getByText(ui().taskDetailChecklist)).toBeInTheDocument();
    expect(screen.getByText("cpu 80% observed")).toBeInTheDocument();
    expect(screen.getByText("no p95 probe")).toBeInTheDocument();

    // Inject-side holistic verdict (verification.overall) next to its
    // section title — the web-side twin of the preview page's
    // 「验证： partial」 chip.
    expect(screen.getByText("partial")).toBeInTheDocument();

    // Recovery verdict with its holistic level.
    expect(
      screen.getByText(ui().taskDetailRecoverVerification),
    ).toBeInTheDocument();
    expect(screen.getByText("recovered")).toBeInTheDocument();
    expect(screen.getByText("pod ready")).toBeInTheDocument();

    // Postmortem summary + the full-report disclosure.
    expect(screen.getByText("演练成功，指标如期恶化。")).toBeInTheDocument();
    expect(screen.getByText(ui().taskDetailFullReport)).toBeInTheDocument();

    // Feasibility verdict.
    expect(screen.getByText(ui().taskDetailFeasibility)).toBeInTheDocument();
    expect(screen.getByText("headroom sufficient")).toBeInTheDocument();
  });

  it("says so when the postmortem exists but carries no timeline", async () => {
    renderTrace("t-no-tl", async () => ({
      task_id: "t-no-tl",
      status: "success",
      postmortem: { path: "/tmp/p.md", summary: "s", markdown: "## Summary\n\n只有摘要。" },
    }));
    expect(
      await screen.findByText(ui().taskDetailTimelineNone),
    ).toBeInTheDocument();
    // Absent payloads hide their sections entirely.
    expect(screen.queryByText(ui().taskDetailFeasibility)).toBeNull();
    expect(screen.queryByText(ui().taskDetailRecoverVerification)).toBeNull();
  });
});
