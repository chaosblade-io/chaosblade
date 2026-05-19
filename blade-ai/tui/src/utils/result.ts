/**
 * Parse a Python ``result`` event's content (a JSONEnvelope-shaped
 * string) into a ResultItem suitable for ResultCard rendering.
 *
 * Envelope shape (best-effort):
 *   {
 *     "status": "success" | "fail" | ...,
 *     "data": {
 *       "task_id": "...",
 *       "fault_type": "...",
 *       "blade_uid": "...",
 *       "duration_ms": 95400,
 *       "verification": "...",
 *       "side_effects": { "container_restarts": [...] },
 *       "failure_reason": "<base> | llm_analysis: <hint>",
 *       ...
 *     }
 *   }
 *
 * We tolerate missing fields and never throw — anything we can't read
 * collapses to an empty string in the ResultItem, and the card hides
 * empty rows.
 */

import type { ResultItem } from "../state/types.js";

interface Envelope {
  status?: string;
  data?: Record<string, unknown>;
  message?: string;
}

export function parseResultEnvelope(
  raw: string,
  fallbackTaskId: string,
): Omit<ResultItem, "kind" | "id"> {
  let env: Envelope = {};
  try {
    env = JSON.parse(raw) as Envelope;
    if (typeof env !== "object" || env === null) env = {};
  } catch {
    // Not JSON — return a degenerate result with raw text in summary.
    return {
      taskId: fallbackTaskId,
      status: "unknown",
      faultType: "",
      bladeUid: "",
      duration: "",
      summary: raw.split("\n").find((l) => l.trim()) ?? "",
    };
  }

  const data = (env.data ?? {}) as Record<string, unknown>;
  const taskId = asString(data["task_id"]) || fallbackTaskId;
  const faultType = asString(data["fault_type"]);
  const bladeUid = asString(data["blade_uid"]);
  const duration = formatDurationMs(asNumber(data["duration_ms"]));
  const verification = asString(data["verification"]);

  // Side effects → terse summary chip ("HPA scaled · 1 pod restarted")
  const sideEffects = data["side_effects"];
  let restartCount = 0;
  if (typeof sideEffects === "object" && sideEffects !== null) {
    const restarts = (sideEffects as Record<string, unknown>)["container_restarts"];
    if (Array.isArray(restarts)) restartCount = restarts.length;
  }

  // Compose a single-line effect summary. Verification often itself
  // is a multi-line string — we take the first non-empty line, strip
  // emoji-style ✓/✗ prefixes the agent loves to add.
  const verifLine =
    verification
      .split("\n")
      .map((l) => l.trim().replace(/^[✓✗•\-•]+\s*/, ""))
      .find((l) => l.length > 0) ?? "";

  const parts: string[] = [];
  if (verifLine) parts.push(verifLine);
  if (restartCount > 0) parts.push(`${restartCount} pod restart${restartCount === 1 ? "" : "s"}`);
  const summary = parts.join(" · ");

  // Failure cause / hint split — failure_reason is shaped
  // "<base> | llm_analysis: <hint>" by Python's enrich_failure_reason.
  const failureReason = asString(data["failure_reason"]);
  let cause: string | undefined;
  let hint: string | undefined;
  if (failureReason) {
    const sep = " | llm_analysis: ";
    if (failureReason.includes(sep)) {
      const idx = failureReason.indexOf(sep);
      cause = failureReason.slice(0, idx).trim();
      hint = failureReason.slice(idx + sep.length).trim();
    } else {
      cause = failureReason;
    }
  }

  // Status mapping. The Python envelope's outer ``status`` is just
  // "ok" / "fail" of the API call itself; the inject's actual outcome
  // lives in ``data.task_state`` or is implied by the presence of a
  // failure_reason.
  const taskState = asString(data["task_state"]);
  let status: ResultItem["status"] = "unknown";
  if (taskState === "injected" || taskState === "recovered") status = "success";
  else if (taskState === "partial_recovered") status = "partial";
  else if (taskState === "failed" || cause) status = "failed";
  // ``ResponseStatus`` enum from Python is "success" | "fail" — see
  // models/schemas.py. Anything else falls through to "unknown".
  else if (env.status === "success") status = "success";

  return {
    taskId,
    status,
    faultType,
    bladeUid,
    duration,
    summary,
    cause,
    hint,
  };
}

function asString(v: unknown): string {
  return typeof v === "string" ? v : "";
}
function asNumber(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function formatDurationMs(ms: number): string {
  if (!ms || ms <= 0) return "";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return s > 0 ? `${m}m${s.toString().padStart(2, "0")}s` : `${m}m`;
}
