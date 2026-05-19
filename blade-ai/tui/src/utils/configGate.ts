/**
 * First-time config gate — decides whether to launch the Python wizard
 * before spawning the server.
 *
 * Mirrors Python TUI ``tui/app.py:160-186`` exactly: a session is
 * "ready" iff three fields — ``llm_api_key``, ``model_name``,
 * ``api_base_url`` — resolve to non-empty values after Settings does
 * env > config.json > built-in defaults.
 *
 * Two-tier resolution:
 *
 *   1. **Fast path** (no spawn): if env vars and/or
 *      ``~/.blade-ai/config.json`` already provide all three fields
 *      explicitly, we can declare sufficiency without spawning Python.
 *      Covers the common case of any user who's launched once before.
 *
 *   2. **Slow path** (spawn ``blade-ai config-check``): only runs when
 *      the fast path can't confirm — typically a fresh install where
 *      the user set llm_api_key but model_name / api_base_url are
 *      relying on Python's built-in defaults that TS doesn't know
 *      about. ``config-check`` exits 0 iff Settings sees all three
 *      filled; ts side just reads the exit code.
 *
 * Why slow path delegates to Python: ``Settings`` has built-in
 * defaults (``qwen3.6-max-preview`` and DashScope URL) for two of the
 * three fields. Duplicating those constants in TS would couple the
 * two sides and silently drift if Python changes them. One spawn at
 * boot is the right trade for single-source-of-truth.
 *
 * Failure modes (all fail-open — proceed to TUI without wizard, user
 * can fix from there):
 *   - Spawn fails (``blade-ai`` missing AND ``python -m chaos_agent``
 *     also missing): we don't know the config state, so let the TUI
 *     start and surface the issue via the boot doctor card.
 *   - ``config-check`` exits with code 2 ("settings unavailable"): same.
 */

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

function readConfigFile(): Record<string, unknown> {
  const path = join(homedir(), ".blade-ai", "config.json");
  if (!existsSync(path)) return {};
  try {
    const raw = readFileSync(path, "utf-8");
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

/** Fast-path probe: all three fields explicit in env or config.json.
 *  Returns true ONLY when none of the values relies on Python defaults
 *  — that case has to go through the slow path. */
function allThreeExplicit(): boolean {
  const envKey = (process.env["BLADE_AI_LLM_API_KEY"] ?? "").trim();
  const envModel = (process.env["BLADE_AI_MODEL_NAME"] ?? "").trim();
  const envBase = (process.env["BLADE_AI_API_BASE_URL"] ?? "").trim();

  const cfg = readConfigFile();
  const fileKey = ((cfg["llm_api_key"] as string | undefined) ?? "").trim();
  const fileModel = ((cfg["model_name"] as string | undefined) ?? "").trim();
  const fileBase = ((cfg["api_base_url"] as string | undefined) ?? "").trim();

  return (
    (envKey || fileKey).length > 0 &&
    (envModel || fileModel).length > 0 &&
    (envBase || fileBase).length > 0
  );
}

/** Slow-path probe: spawn ``blade-ai config-check`` (or
 *  ``python -m chaos_agent ... config-check`` on ENOENT). Returns:
 *    true  → exit 0, Settings sees all three fields
 *    false → exit 1, at least one field missing
 *    true  → spawn failed (fail-open; don't trap the user in a wizard
 *            loop when we can't even check)
 */
function slowPathSufficient(): boolean {
  let result = spawnSync("blade-ai", ["config-check"], {
    // Don't inherit stdio — the wizard inherits, but the check is
    // silent. We just want the exit code.
    stdio: ["ignore", "ignore", "pipe"],
    env: process.env,
    encoding: "utf-8",
  });

  if (result.error && (result.error as NodeJS.ErrnoException).code === "ENOENT") {
    const py = process.env["BLADE_AI_PYTHON"] ?? "python";
    result = spawnSync(py, ["-m", "chaos_agent.cli.main", "config-check"], {
      stdio: ["ignore", "ignore", "pipe"],
      env: process.env,
      encoding: "utf-8",
    });
  }

  if (result.error) {
    // Spawn failed entirely (no blade-ai + no python). Fail open: let
    // the user reach the TUI; preflight card will surface real errors.
    return true;
  }
  // exit 0 = sufficient, 1 = missing fields, 2 = settings broken.
  // For 2 we fail open (same reason as ENOENT).
  if (result.status === 0) return true;
  if (result.status === 1) return false;
  return true;
}

/**
 * True when all three required config fields are reachable. False
 * triggers the wizard.
 */
export function isConfigSufficient(): boolean {
  // Fast path skips the ~150ms Python startup cost for the common
  // case where everything's been configured before.
  if (allThreeExplicit()) return true;
  // Slow path defers to Python Settings — the only thing that knows
  // about built-in non-empty defaults for model_name / api_base_url.
  return slowPathSufficient();
}

export type WizardOutcome =
  | { kind: "saved" }       // user completed → config now sufficient
  | { kind: "skipped" }     // user pressed Esc / quit (exit code 2)
  | { kind: "spawn_failed"; reason: string }      // ENOENT / spawn error
  | { kind: "wizard_error"; reason: string }      // wizard ran but exited != 0/2
  | { kind: "still_missing" }; // wizard ran successfully but key still empty

/**
 * Spawn the Python wizard synchronously and inherit stdio so the
 * user interacts with it inline. Returns a structured outcome so the
 * caller can decide whether to continue startup, ask the user to
 * retry, or exit.
 *
 * We invoke ``blade-ai config-wizard`` (the hyphenated top-level
 * command added by ``cli/main.py`` alongside this PR) rather than
 * spawning ``python -m chaos_agent`` directly — the former is what
 * pip / curl installers put on the user's PATH, and the latter
 * requires knowing the Python interpreter path. If ``blade-ai`` isn't
 * resolvable (npm-only TUI install with no Python wheel), fall back
 * to ``$BLADE_AI_PYTHON -m chaos_agent.cli.main config-wizard`` so
 * the user still has a path forward.
 */
export function runConfigWizard(): WizardOutcome {
  // Primary: PATH-resolved blade-ai. This is what pip / curl installs.
  let result = spawnSync("blade-ai", ["config-wizard"], {
    stdio: "inherit",
    env: process.env,
  });

  // PATH lookup failed (ENOENT). Fall back to Python module form so
  // npm-only installs still work, provided Python + chaos_agent are
  // reachable.
  if (result.error && (result.error as NodeJS.ErrnoException).code === "ENOENT") {
    const py = process.env["BLADE_AI_PYTHON"] ?? "python";
    result = spawnSync(py, ["-m", "chaos_agent.cli.main", "config-wizard"], {
      stdio: "inherit",
      env: process.env,
    });
  }

  if (result.error) {
    return {
      kind: "spawn_failed",
      reason: (result.error as Error).message ?? String(result.error),
    };
  }

  // Exit codes: 0 saved, 2 skipped, anything else = error/cancelled.
  if (result.status === 0) {
    return isConfigSufficient()
      ? { kind: "saved" }
      : { kind: "still_missing" };
  }
  if (result.status === 2) return { kind: "skipped" };
  // Wizard process started but exited with a non-success, non-skip
  // code — distinct from "spawn itself failed" so the caller can show
  // a more accurate error message (no point telling the user to
  // ``pip install blade-ai`` when the command was clearly resolvable).
  return {
    kind: "wizard_error",
    reason: `wizard exited with status ${result.status ?? "?"}`,
  };
}
