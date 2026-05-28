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
  // PLUS a per-row list (e.g. "1 pod restart · accounting-6fb...qn2vr")
  // surfaced in the ResultCard "Side effects" section. The single
  // summary chip is kept for back-compat with the legacy single-row
  // ResultCard layout; the new ``sideEffects`` array drives the v3
  // multi-row section.
  const sideEffectsRaw = data["side_effects"];
  let restartCount = 0;
  const sideEffectList: string[] = [];
  if (typeof sideEffectsRaw === "object" && sideEffectsRaw !== null) {
    const restarts = (sideEffectsRaw as Record<string, unknown>)["container_restarts"];
    if (Array.isArray(restarts)) {
      restartCount = restarts.length;
      for (const r of restarts) {
        if (typeof r === "string") {
          sideEffectList.push(`pod restart · ${r}`);
        } else if (r && typeof r === "object") {
          const obj = r as Record<string, unknown>;
          const name = asString(obj["name"]) || asString(obj["pod"]);
          const reason = asString(obj["reason"]);
          const label = name
            ? reason
              ? `pod restart · ${name} (${reason})`
              : `pod restart · ${name}`
            : "pod restart";
          sideEffectList.push(label);
        }
      }
    }
    // Other side-effect categories the agent may surface in future
    // (HPA scaling, log warnings, …). Flatten anything iterable into
    // a "<key> · <value>" row so additions to the server contract
    // light up here without a client release.
    for (const [k, v] of Object.entries(sideEffectsRaw as Record<string, unknown>)) {
      if (k === "container_restarts") continue;
      if (Array.isArray(v)) {
        for (const item of v) {
          sideEffectList.push(
            `${k} · ${typeof item === "string" ? item : JSON.stringify(item)}`,
          );
        }
      }
    }
  }

  // Replan count — surfaced in Outcome section as "succeeded after N
  // auto-replan(s)". 0 means clean first-try; we omit the field then.
  const replanCount = asNumber(data["replan_count"]);

  // Live target spec — verifies "did we hit the intended target".
  let target: { namespace?: string; names?: string[] } | undefined;
  const rawTarget = data["target"];
  if (rawTarget && typeof rawTarget === "object" && !Array.isArray(rawTarget)) {
    const t = rawTarget as Record<string, unknown>;
    const ns = asString(t["namespace"]);
    const namesRaw = t["names"];
    const names = Array.isArray(namesRaw)
      ? namesRaw.map(asString).filter(Boolean)
      : [];
    if (ns || names.length > 0) {
      target = { namespace: ns || undefined, names: names.length ? names : undefined };
    }
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

  // Failure cause / hint — prefer structured failure_detail over legacy failure_reason.
  let cause: string | undefined;
  let hint: string | undefined;
  const failureDetail = data["failure_detail"] as Record<string, unknown> | undefined;
  if (failureDetail && typeof failureDetail === "object" && failureDetail["category"]) {
    const category = asString(failureDetail["category"]);
    const context = asString(failureDetail["context"]);
    cause = context ? `${category}: ${context}` : category;
    hint = asString(failureDetail["llm_analysis"]) || undefined;
  } else {
    // Legacy fallback: failure_reason shaped "<base> | llm_analysis: <hint>"
    const failureReason = asString(data["failure_reason"]);
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

  // T6 — postmortem payload. Server sends ``null`` when generation
  // was skipped (disabled / non-inject / non-whitelist failure /
  // LLM timeout); we leave the field undefined in that case so
  // PostmortemSection can shortcut on `!item.postmortem`.
  let postmortem: ResultItem["postmortem"];
  const rawPm = data["postmortem"];
  if (rawPm && typeof rawPm === "object" && !Array.isArray(rawPm)) {
    const pm = rawPm as Record<string, unknown>;
    const path = asString(pm["path"]);
    const markdown = asString(pm["markdown"]);
    const pmSummary = asString(pm["summary"]);
    if (path && markdown) {
      postmortem = { path, markdown, summary: pmSummary };
    }
  }

  return {
    taskId,
    status,
    faultType,
    bladeUid,
    duration,
    summary,
    cause,
    hint,
    target,
    replanCount: replanCount > 0 ? replanCount : undefined,
    sideEffects: sideEffectList.length > 0 ? sideEffectList : undefined,
    sideEffectsSummary: asString(data["side_effects_summary"]) || undefined,
    postmortem,
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
