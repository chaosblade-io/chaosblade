/**
 * HTTP + SSE client for the Python backend.
 *
 * Keep the surface minimal so M2 components can compose freely:
 *
 *   const client = new BladeClient(serverUrl);
 *   const sid = await client.createSession();
 *   for await (const evt of client.streamTurn(sid, { input })) {
 *     ...
 *   }
 *
 * Frame parsing: each SSE frame is a ``data: {...json...}\n\n`` block.
 * We split on blank lines, concatenate ``data:`` rows of a single
 * frame, and JSON-parse. Anything we can't parse is logged via
 * ``onProtocolError`` (debug only) and skipped.
 */

import { isStreamEvent, type StreamEvent } from "./events.js";

export interface CreateSessionOpts {
  cluster?: string;
  namespace?: string;
  modelName?: string;
}

export interface TurnRequest {
  input: string;
  permission_mode?: "confirm" | "auto";
  display_mode?: "calm" | "working" | "dense";
  /** Phase 3c.2 — when true, the agent runs intent + planning but
   *  ``confirmation_gate`` emits a "what would happen" preview
   *  AIMessage and the post-gate router exits to END instead of
   *  baseline_capture / execute. Powers ``/plan <NL>``. Default
   *  false on the wire (the field is omitted from the JSON body). */
  dry_run?: boolean;
}

export interface InterruptResolve {
  interrupt_id: string;
  answer: string;
}

export interface ClientOptions {
  /** Optional protocol-error sink (e.g. dump to a debug log). */
  onProtocolError?: (frame: string, err: unknown) => void;
}

/**
 * The protocol version this TUI bundle was compiled against. Bump
 * lockstep with ``chaos_agent/server/middleware.py::PROTOCOL_VERSION``
 * whenever the SSE / envelope shape changes. Mismatch surfaces as a
 * non-fatal warning at boot — see cli.tsx + i18n ``protocol.mismatch``.
 */
export const TUI_PROTOCOL_VERSION = "1";

export class BladeClient {
  /**
   * Server's ``X-Blade-Protocol-Version`` header from the most recent
   * successful response. Captured on health() so it's populated before
   * the first session is created. ``undefined`` until a response with
   * the header has been observed (older servers won't emit it).
   */
  private _serverProtocolVersion?: string;

  constructor(
    private readonly baseUrl: string,
    private readonly opts: ClientOptions = {},
  ) {}

  /** Read-only access to the resolved server URL — used by /doctor. */
  get url(): string {
    return this.baseUrl;
  }

  /** Server's reported protocol version, or undefined before first contact. */
  get serverProtocolVersion(): string | undefined {
    return this._serverProtocolVersion;
  }

  async health(): Promise<boolean> {
    try {
      const r = await fetch(`${this.baseUrl}/api/v1/health`);
      // Capture the protocol header even on non-2xx — a 503 response
      // still carries the header and we want it for the mismatch
      // check. Only bail when the network call itself failed (caught
      // below). ``Headers.get`` returns null for missing keys, which
      // we coerce to undefined for the optional-field convention.
      const v = r.headers.get("x-blade-protocol-version");
      if (v) this._serverProtocolVersion = v;
      return r.ok;
    } catch {
      return false;
    }
  }

  /**
   * GET /api/v1/version — best-effort server version probe used by
   * /doctor. The endpoint wraps in a JSONEnvelope; we reach inside
   * for ``data.version``. Returns null on any failure (not throws)
   * because /doctor must render even when the server is broken — the
   * cell just shows "?".
   */
  async getServerVersion(): Promise<string | null> {
    try {
      const r = await fetch(`${this.baseUrl}/api/v1/version`);
      if (!r.ok) return null;
      const env = (await r.json()) as Record<string, unknown>;
      if (env["status"] === "fail") return null;
      const data = env["data"];
      if (data && typeof data === "object") {
        const v = (data as Record<string, unknown>)["version"];
        if (typeof v === "string" && v.length > 0) return v;
      }
      return null;
    } catch {
      return null;
    }
  }

  /**
   * GET /api/v1/preflight — boot-time environment self-check. Returns
   * the parsed envelope's data, or null on any failure. Used by the
   * boot screen's "环境自检" card; the TUI must still render even when
   * the endpoint is broken / missing (older server), so we never throw.
   */
  async getPreflight(): Promise<Record<string, unknown> | null> {
    try {
      const r = await fetch(`${this.baseUrl}/api/v1/preflight`);
      if (!r.ok) return null;
      const env = (await r.json()) as Record<string, unknown>;
      if (env["status"] === "fail") return null;
      const data = env["data"];
      if (data && typeof data === "object") return data as Record<string, unknown>;
      return null;
    } catch {
      return null;
    }
  }

  async createSession(opts: CreateSessionOpts = {}): Promise<string> {
    const r = await fetch(`${this.baseUrl}/api/v1/sessions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        cluster: opts.cluster,
        namespace: opts.namespace,
        model_name: opts.modelName,
      }),
    });
    if (!r.ok) throw new Error(`createSession failed: HTTP ${r.status}`);
    const data = (await r.json()) as { session_id?: string };
    if (!data.session_id) throw new Error("createSession: no session_id");
    return data.session_id;
  }

  async deleteSession(sid: string): Promise<void> {
    // Hard 3s timeout via AbortSignal — exit must never block on a
    // half-stuck socket. Node's fetch has no built-in timeout; without
    // an explicit signal a wedged server would freeze the TUI forever
    // and leave the user staring at a hung goodbye card.
    await fetch(`${this.baseUrl}/api/v1/sessions/${sid}`, {
      method: "DELETE",
      signal: AbortSignal.timeout(3000),
    }).catch(() => undefined);
  }

  /**
   * PATCH the goodbye-card stats into the session file before
   * ``deleteSession`` is called. Field names must match the Python
   * ``SessionStatsPayload`` (snake_case) — those keys hit
   * ``TuiSessionStore.update_stats`` directly. Swallows network errors
   * AND bounds the wait at 3s — exit must not block on this.
   */
  async patchSessionStats(
    sid: string,
    stats: Partial<{
      message_count: number;
      injection_count: number;
      injection_success: number;
      injection_fail: number;
      recovery_count: number;
    }>,
  ): Promise<void> {
    await fetch(`${this.baseUrl}/api/v1/sessions/${sid}/stats`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(stats),
      signal: AbortSignal.timeout(3000),
    }).catch(() => undefined);
  }

  async getSessionState(sid: string): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/sessions/${sid}/state`);
    if (!r.ok) throw new Error(`getSessionState failed: HTTP ${r.status}`);
    return (await r.json()) as Record<string, unknown>;
  }

  /**
   * List all task metrics. Backed by the existing
   * ``GET /api/v1/metric`` endpoint which already returns a
   * ``{total, tasks: [...]}`` shape for the Python CLI's ``metric``
   * command — we reuse it here for ``/tasks``.
   *
   * The endpoint wraps results in a JSONEnvelope ``{status, data, ...}``.
   * We surface envelope-level failures as thrown errors instead of
   * silently returning a payload-less envelope, so callers don't show
   * a misleading "no tasks yet" line when the server actually
   * errored.
   */
  async listTasks(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/metric`);
    if (!r.ok) throw new Error(`listTasks failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    // Python's ``ResponseStatus`` is exactly "success" or "fail" — see
    // models/schemas.py. The "ok" string was a misread of the JSON
    // shape; only "fail" is a real error sentinel.
    const status = env["status"];
    if (status === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`listTasks: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * Fetch a task's recorded TUIEvent timeline. Returns
   * ``{task_id, events: [{ts, type, data}, ...], total}``.
   */
  async getRecording(taskId: string): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/recordings/${encodeURIComponent(taskId)}`,
    );
    if (!r.ok) throw new Error(`getRecording failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`getRecording: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /** List the available recordings on disk. */
  async listRecordings(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/recordings`);
    if (!r.ok) throw new Error(`listRecordings failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`listRecordings: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * Fetch metrics for a single task. Backed by
   * ``GET /api/v1/metric/{task_id}``. Used by ``/review`` to render
   * the per-task review card.
   *
   * Server semantics: returns ``status: "fail"`` with code
   * ``TASK_NOT_FOUND`` when the id has no record. Surfaced here as a
   * thrown error so /review can show a focused warning instead of a
   * blank card.
   */
  async getMetric(taskId: string): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/metric/${encodeURIComponent(taskId)}`,
    );
    if (!r.ok) throw new Error(`getMetric failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`getMetric: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * GET /api/v1/skills — list every supported fault capability with
   * LLM-generated use-case examples. Heavy call (the server invokes
   * the LLM once per skill on a cold cache), so /skills list is
   * expected to take a few seconds the first time.
   *
   * Returns the data envelope's payload: ``{total, categories: [...]}``.
   */
  async listSkills(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/skills`);
    if (!r.ok) throw new Error(`listSkills failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`listSkills: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/recover — trigger fault recovery for an injected
   * task. The endpoint is request/response (not SSE) — the server
   * runs the recover graph synchronously and returns the final
   * envelope, so this can take tens of seconds for a real cluster.
   *
   * The handler ``/recover <id>`` shows a "running…" line and waits;
   * the user can Esc-cancel via the same channel they cancel turns.
   */
  async recoverTask(taskId: string): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/recover`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    });
    if (!r.ok) throw new Error(`recoverTask failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    // Recover returns a fail envelope on verification failure with
    // ``data`` populated — we still want callers to see the data so
    // they can render the failure reason. Hand the full envelope back
    // and let the handler decide how to format.
    return env;
  }

  /**
   * GET /api/v1/config — fetch the masked display config + the
   * resolved ``~/.blade-ai/config.json`` path.
   *
   * Server returns ``{config: {...}, config_path: "..."}``. The TS
   * /config slash uses both — list / get hit ``data.config``, path
   * just shows ``data.config_path``.
   */
  async getConfig(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/config`);
    if (!r.ok) throw new Error(`getConfig failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`getConfig: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/config/{key} — set a single key. Server enforces a
   * write whitelist and rejects unknown / sensitive keys with a
   * ``status: fail / code: INVALID_PARAMS`` envelope, which we
   * surface as a thrown error so /config set can render the refusal
   * with the server's reason.
   *
   * ``value`` is always sent as a string. Type coercion happens
   * server-side via ``ConfigStore._coerce`` (the same path Python
   * TUI's ``/config set`` uses), so e.g. ``timeout_kubectl="45"``
   * lands as int 45 on disk.
   */
  async setConfig(
    key: string,
    value: string,
  ): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/config/${encodeURIComponent(key)}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value }),
      },
    );
    if (!r.ok) throw new Error(`setConfig failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`setConfig: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * DELETE /api/v1/config/{key} — revert a key to its default. Same
   * whitelist as setConfig.
   */
  async unsetConfig(key: string): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/config/${encodeURIComponent(key)}`,
      { method: "DELETE" },
    );
    if (!r.ok) throw new Error(`unsetConfig failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`unsetConfig: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * GET /api/v1/memory/{tui_session_id} — snapshot of the named TUI
   * session: cluster / namespace / started_at, recent task ids, stats,
   * resolved memory_dir.
   *
   * 404-equivalent (no record) surfaces as ``status: fail``; we throw
   * so /memory show can render "no memory yet" cleanly instead of
   * showing an empty snapshot.
   */
  async getMemoryInfo(
    tuiSessionId: string,
  ): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/memory/${encodeURIComponent(tuiSessionId)}`,
    );
    if (!r.ok) throw new Error(`getMemoryInfo failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`getMemoryInfo: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * DELETE /api/v1/memory/{tui_session_id} — drop the on-disk TUI
   * session file. Does NOT touch graph checkpoint threads (those are
   * still reachable via /recover). Server always returns 200; the
   * ``data.cleared_session_file`` flag tells the caller whether
   * anything was actually deleted (false when nothing existed).
   */
  async clearMemory(tuiSessionId: string): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/memory/${encodeURIComponent(tuiSessionId)}`,
      { method: "DELETE" },
    );
    if (!r.ok) throw new Error(`clearMemory failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`clearMemory: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/sessions/{sid}/compact — force a compaction pass on
   * the conversation thread tied to ``sid``. ``threadId`` is optional;
   * when absent the server picks the most recent task id stored
   * against the session (matches Python TUI's
   * ``conversation.conversation_thread_id`` auto-resolution).
   *
   * Returns ``{tokens_before, tokens_after, tokens_saved, compacted,
   * layer}`` — the handler renders these directly so the user sees
   * an honest savings number. ``compacted: false`` covers two cases:
   * the thread had no messages, or it was already under budget.
   */
  async compactSession(
    sid: string,
    threadId?: string,
  ): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/sessions/${encodeURIComponent(sid)}/compact`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ thread_id: threadId ?? null }),
      },
    );
    if (!r.ok) throw new Error(`compactSession failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`compactSession: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * GET /api/v1/skills_dir — resolve the skills directory + candidate
   * priorities. Used by ``/skills path`` to mirror Python's
   * ``_cmd_skills_path``. Lightweight; doesn't touch the LLM-heavy
   * catalog generator.
   */
  async getSkillsDir(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/skills_dir`);
    if (!r.ok) throw new Error(`getSkillsDir failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`getSkillsDir: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * GET /api/v1/skills/{name} — full metadata + SKILL.md instructions
   * for a single skill. Used by ``/skills show <name>`` to render a
   * detail card. Throws on ``status: fail`` so unknown / not-loaded
   * names surface as an actionable warning instead of an empty card.
   */
  async showSkill(name: string): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(name)}`,
    );
    if (!r.ok) throw new Error(`showSkill failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`showSkill: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/skills/reload — re-scan the skills directory.
   * Returns ``{total, added: [...], removed: [...]}`` so the handler
   * can show a precise diff after the rescan.
   */
  async reloadSkills(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/skills/reload`, {
      method: "POST",
    });
    if (!r.ok) throw new Error(`reloadSkills failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`reloadSkills: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/skills/install — copy a skill from a git URL or
   * local path into ``skills_dir`` (no setup scripts run).
   * Returns ``{installed: [{name, target_dir, skill_md_sha256}]}``.
   * The TS handler walks ``installed`` and prompts the user to run
   * ``/skills reload`` for activation.
   */
  async installSkill(source: string): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/skills/install`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ source }),
    });
    if (!r.ok) throw new Error(`installSkill failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`installSkill: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/skills/{name}/enable — drop ``name`` from
   * ``settings.disabled_skills`` so the next reload picks it up.
   * Idempotent — server reports ``was_disabled: false`` when nothing
   * changed.
   */
  async enableSkill(name: string): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(name)}/enable`,
      { method: "POST" },
    );
    if (!r.ok) throw new Error(`enableSkill failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`enableSkill: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/skills/{name}/disable — add ``name`` to
   * ``settings.disabled_skills`` and drop it from the live registry.
   * Idempotent — server reports ``was_enabled: false`` when the
   * skill was already disabled.
   */
  async disableSkill(name: string): Promise<Record<string, unknown>> {
    const r = await fetch(
      `${this.baseUrl}/api/v1/skills/${encodeURIComponent(name)}/disable`,
      { method: "POST" },
    );
    if (!r.ok) throw new Error(`disableSkill failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`disableSkill: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * GET /api/v1/model — list candidate LLMs + the currently active
   * one. Returns ``{active, api_base_url, candidates: [{id, provider,
   * note?}]}``. ``active`` is reported separately from ``candidates``
   * so a custom model name (one not in the curated list) still
   * surfaces as the running model.
   */
  async getModel(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/model`);
    if (!r.ok) throw new Error(`getModel failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`getModel: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * POST /api/v1/model — set the active model name. Server accepts
   * any non-empty string (custom deployments need that flexibility);
   * the canonical writable list is server-side and surfaced via
   * ``getModel()``.
   *
   * Response carries ``restart_required`` — currently always true
   * because ``model_name`` is a cold key. A future LLM-rebuild path
   * may flip this to false; the TS handler reads the field
   * explicitly so the UX matches whatever the server reports.
   */
  async setModel(modelName: string): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/api/v1/model`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model_name: modelName }),
    });
    if (!r.ok) throw new Error(`setModel failed: HTTP ${r.status}`);
    const env = (await r.json()) as Record<string, unknown>;
    if (env["status"] === "fail") {
      const msg = (env["message"] as string) ?? "server error";
      throw new Error(`setModel: ${msg}`);
    }
    const data = env["data"];
    if (data && typeof data === "object") return data as Record<string, unknown>;
    return env;
  }

  /**
   * Run one turn and stream events. Yields each StreamEvent as it
   * arrives; returns when the server emits ``done`` or the stream
   * closes. Throws if the request itself fails (network / 4xx).
   */
  async *streamTurn(
    sid: string,
    body: TurnRequest,
    signal?: AbortSignal,
  ): AsyncGenerator<StreamEvent, void, void> {
    const r = await fetch(`${this.baseUrl}/api/v1/sessions/${sid}/turn`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "text/event-stream",
        // Force the server to close the connection after sending
        // ``done``. Why this matters at exit time: the Python SSE
        // route sets ``Connection: keep-alive`` on its response,
        // and undici's HTTP agent will return the socket to its
        // keep-alive pool when the stream completes. The next
        // request on this base URL — typically the exit-path
        // PATCH /sessions/:id/stats or DELETE /sessions/:id —
        // picks up that pooled socket. uvicorn's HTTP/1.1 state
        // machine for an SSE connection that just finished is
        // unstable: the next request on it can hang for seconds
        // until our 3s AbortSignal fires. Sending ``connection:
        // close`` here makes the server close after ``done``, the
        // socket leaves undici's pool, and follow-up calls open
        // a fresh connection in ~1ms instead of timing out.
        // (controller.abort() in useStream.ts can't fix this on
        // its own — by the time the for-await loop exits, the
        // socket has already been pooled, and aborting an
        // already-completed fetch is a no-op for pool state.)
        connection: "close",
      },
      body: JSON.stringify({
        input: body.input,
        permission_mode: body.permission_mode ?? "confirm",
        display_mode: body.display_mode ?? "calm",
        // Phase 3c.2 — only emit ``dry_run`` when explicitly true so
        // the wire stays tiny for the common ``/run`` path. Server
        // defaults the field to false on the model side.
        ...(body.dry_run ? { dry_run: true } : {}),
      }),
      signal,
    });
    if (!r.ok || !r.body) {
      throw new Error(`streamTurn failed: HTTP ${r.status}`);
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) return;
        buf += decoder.decode(value, { stream: true });

        // SSE frames are separated by blank lines. We accept both
        // ``\n\n`` and ``\r\n\r\n``.
        let sep: number;
        while (true) {
          const lf = buf.indexOf("\n\n");
          const crlf = buf.indexOf("\r\n\r\n");
          if (lf < 0 && crlf < 0) break;
          if (lf < 0) sep = crlf;
          else if (crlf < 0) sep = lf;
          else sep = Math.min(lf, crlf);

          const frameLen = sep;
          const skip = buf.startsWith("\r\n", sep) ? 4 : 2;
          const frame = buf.slice(0, frameLen);
          buf = buf.slice(frameLen + skip);

          const evt = parseFrame(frame, this.opts.onProtocolError);
          if (evt) {
            yield evt;
            if (evt.type === "done") return;
          }
        }
      }
    } finally {
      try {
        await reader.cancel();
      } catch {
        // ignore
      }
    }
  }

  async resolveInterrupt(sid: string, body: InterruptResolve): Promise<void> {
    const r = await fetch(`${this.baseUrl}/api/v1/sessions/${sid}/interrupt`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`resolveInterrupt failed: HTTP ${r.status}`);
  }

  async cancelTurn(sid: string): Promise<void> {
    await fetch(`${this.baseUrl}/api/v1/sessions/${sid}/cancel`, {
      method: "POST",
    }).catch(() => undefined);
  }
}

/**
 * Parse a single SSE frame into a StreamEvent.
 *
 * The Python side emits ``data: {json}`` only (no ``event:`` row), so
 * we concatenate every ``data:`` line in the frame and JSON-parse the
 * result. Other SSE fields (``id:``, ``retry:``, comments) are ignored.
 */
function parseFrame(
  frame: string,
  onError?: (frame: string, err: unknown) => void,
): StreamEvent | null {
  if (!frame.trim()) return null;

  const dataParts: string[] = [];
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith(":")) continue; // SSE comment
    if (rawLine.startsWith("data:")) {
      dataParts.push(rawLine.slice(5).trimStart());
    }
    // We ignore ``event:`` / ``id:`` / ``retry:`` rows — Python doesn't
    // use them, and a future addition shouldn't break the parser.
  }
  if (dataParts.length === 0) return null;

  const payload = dataParts.join("\n");
  try {
    const obj = JSON.parse(payload) as unknown;
    if (isStreamEvent(obj)) return obj;
    onError?.(frame, new Error("frame is not a StreamEvent"));
    return null;
  } catch (err) {
    onError?.(frame, err);
    return null;
  }
}
