/**
 * Wizard HTTP client — thin wrapper over the Python
 * ``/api/v1/wizard`` surface.
 *
 * The TS Ink wizard renders the UI; this module is the ONLY place that
 * talks to the server. All business logic — URL shape rules, openai
 * SDK calls, kubectl probes, config-file writes — lives in Python
 * (``chaos_agent.config.wizard_validators`` + ``ConfigStore``). We
 * just shuttle JSON over HTTP and present the result.
 *
 * Each call:
 *   · Never throws on protocol errors. Returns a ValidationResult-shaped
 *     object even when the server is down (status="error" + a transport
 *     message), so wizard components can render the failure inline
 *     instead of crashing the Ink tree.
 *   · Honours a 10s default timeout via AbortSignal — the longest
 *     legitimate path (api-key models.list()) is gated to 5s
 *     server-side, so 10s gives slack for network without leaving
 *     the wizard hung indefinitely.
 */

export interface ValidationResult {
  status: "ok" | "warn" | "error";
  message: string;
  /** Advisory — true means the UI must not let the user advance. */
  block: boolean;
  /** Structured extras (model counts, discovered contexts, etc.). */
  metadata: Record<string, unknown>;
}

export interface ModelPreset {
  id: string;
  label: string;
  vendor: string;
  hint: string;
}

export interface SaveResult {
  /** ``"success"`` from JSONEnvelope. ``"error"`` if all writes failed. */
  status: "success" | "error";
  message: string;
  /** Keys that landed on disk (subset of submitted). */
  savedKeys: string[];
  /** Resolved config-file path (e.g. ``~/.blade-ai/config.json``). */
  savedPath: string;
  /** Per-key write errors (partial-failure case). */
  errors: Record<string, string>;
}

const DEFAULT_TIMEOUT_MS = 10_000;

/** Build a transport-error ValidationResult — when fetch() itself throws. */
function transportError(message: string): ValidationResult {
  return {
    status: "error",
    message,
    block: true,
    metadata: {},
  };
}

/** Coerce server envelope's ``data`` into ValidationResult. */
function parseValidationResult(data: unknown): ValidationResult {
  if (!data || typeof data !== "object") {
    return transportError("invalid server response (no data)");
  }
  const d = data as Record<string, unknown>;
  const rawStatus = d["status"];
  const status: ValidationResult["status"] =
    rawStatus === "ok" || rawStatus === "warn" || rawStatus === "error"
      ? rawStatus
      : "error";
  return {
    status,
    message: typeof d["message"] === "string" ? (d["message"] as string) : "",
    block: d["block"] === true,
    metadata:
      d["metadata"] && typeof d["metadata"] === "object"
        ? (d["metadata"] as Record<string, unknown>)
        : {},
  };
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

export class WizardClient {
  constructor(
    private readonly baseUrl: string,
    private readonly timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ) {}

  /**
   * GET /api/v1/wizard/needs-setup — determines whether the wizard
   * should run, or whether a config-file parse error should be surfaced.
   *
   * Returns ``{ needsSetup, configError }``:
   *   - ``configError`` non-null  → config.json exists but is corrupt;
   *     caller should abort boot and show the error.
   *   - ``needsSetup`` true       → essential keys missing; show wizard.
   *   - both false/null           → config is fine; proceed to session.
   *
   * On any transport failure → fail-open: ``{ needsSetup: false,
   * configError: null }`` so a network glitch doesn't block the user.
   */
  async needsWizardSetup(): Promise<{
    needsSetup: boolean;
    configError: string | null;
  }> {
    const FAIL_OPEN = { needsSetup: false, configError: null };
    try {
      const r = await fetchWithTimeout(
        `${this.baseUrl}/api/v1/wizard/needs-setup`,
        { method: "GET" },
        this.timeoutMs,
      );
      if (!r.ok) return FAIL_OPEN;
      const env = (await r.json()) as Record<string, unknown>;
      if (env["status"] !== "success") return FAIL_OPEN;
      const data = env["data"];
      if (!data || typeof data !== "object") return FAIL_OPEN;
      const d = data as Record<string, unknown>;
      const configError =
        typeof d["config_error"] === "string"
          ? (d["config_error"] as string)
          : null;
      return {
        needsSetup: d["needs_setup"] === true,
        configError,
      };
    } catch {
      return FAIL_OPEN;
    }
  }

  /** GET /api/v1/wizard/model-presets — returns the recommended LLM list. */
  async fetchModelPresets(): Promise<ModelPreset[]> {
    try {
      const r = await fetchWithTimeout(
        `${this.baseUrl}/api/v1/wizard/model-presets`,
        { method: "GET" },
        this.timeoutMs,
      );
      if (!r.ok) return [];
      const env = (await r.json()) as Record<string, unknown>;
      if (env["status"] !== "success") return [];
      const data = env["data"];
      if (!data || typeof data !== "object") return [];
      const presets = (data as Record<string, unknown>)["presets"];
      if (!Array.isArray(presets)) return [];
      return presets.filter(
        (p): p is ModelPreset =>
          !!p &&
          typeof p === "object" &&
          typeof (p as Record<string, unknown>)["id"] === "string",
      );
    } catch {
      return [];
    }
  }

  /** POST /api/v1/wizard/validate/url. */
  async validateUrl(url: string): Promise<ValidationResult> {
    return this.postValidation("/validate/url", { url });
  }

  /** POST /api/v1/wizard/validate/api-key — live models.list() probe. */
  async validateApiKey(args: {
    apiKey: string;
    baseUrl: string;
    model?: string;
  }): Promise<ValidationResult> {
    return this.postValidation("/validate/api-key", {
      api_key: args.apiKey,
      base_url: args.baseUrl,
      ...(args.model ? { model: args.model } : {}),
    });
  }

  /** POST /api/v1/wizard/validate/kubeconfig — file check + ctx discovery. */
  async validateKubeconfig(path: string): Promise<ValidationResult> {
    return this.postValidation("/validate/kubeconfig", { path });
  }

  /**
   * POST /api/v1/wizard/save — persist the accumulated config dict.
   *
   * Always returns a SaveResult, even on transport failures (status=
   * "error" + message describing the failure) so the wizard's
   * Summary step can render the outcome instead of crashing.
   */
  async saveConfig(config: Record<string, unknown>): Promise<SaveResult> {
    try {
      const r = await fetchWithTimeout(
        `${this.baseUrl}/api/v1/wizard/save`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ config }),
        },
        this.timeoutMs,
      );
      const env = (await r.json()) as Record<string, unknown>;
      const status: SaveResult["status"] =
        env["status"] === "success" ? "success" : "error";
      const data =
        env["data"] && typeof env["data"] === "object"
          ? (env["data"] as Record<string, unknown>)
          : {};
      const savedKeysRaw = data["saved_keys"];
      const errorsRaw = data["errors"];
      return {
        status,
        message:
          typeof env["message"] === "string"
            ? (env["message"] as string)
            : status === "success"
              ? "已保存"
              : "保存失败",
        savedKeys: Array.isArray(savedKeysRaw)
          ? (savedKeysRaw.filter((k) => typeof k === "string") as string[])
          : [],
        savedPath:
          typeof data["saved_path"] === "string"
            ? (data["saved_path"] as string)
            : "",
        errors:
          errorsRaw && typeof errorsRaw === "object"
            ? (errorsRaw as Record<string, string>)
            : {},
      };
    } catch (e) {
      return {
        status: "error",
        message: `transport error: ${e instanceof Error ? e.message : String(e)}`,
        savedKeys: [],
        savedPath: "",
        errors: {},
      };
    }
  }

  // ── internals ─────────────────────────────────────────────────────

  private async postValidation(
    path: string,
    body: Record<string, unknown>,
  ): Promise<ValidationResult> {
    try {
      const r = await fetchWithTimeout(
        `${this.baseUrl}/api/v1/wizard${path}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
        this.timeoutMs,
      );
      const env = (await r.json()) as Record<string, unknown>;
      if (env["status"] !== "success") {
        return {
          status: "error",
          message:
            typeof env["message"] === "string"
              ? (env["message"] as string)
              : "server rejected request",
          block: true,
          metadata: {},
        };
      }
      return parseValidationResult(env["data"]);
    } catch (e) {
      return transportError(
        `transport error: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  }
}
