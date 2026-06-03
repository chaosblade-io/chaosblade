/**
 * Slash command registry.
 *
 * Mirrors the Python TUI's two-level registry (`tui/commands.py`) so
 * the two front-ends parse identically and ``/help`` renders the same
 * groups in the same order:
 *
 *   group:  general | business | skills | dynamic
 *   shape:  /<root>                           — flat command
 *           /<root> <sub> [args...]           — subcommand
 *           /<alias>                          — resolves to canonical root
 *
 * Commands are pure async handlers receiving a small ``ctx`` of
 * collaborators (client / dispatch / state snapshot / registry / exit).
 * They never touch React directly — the handler returns and the
 * component layer re-renders off the dispatched state changes.
 *
 * Command lifecycle:
 *   1. User types ``/foo bar arg`` and presses Enter.
 *   2. Composer recognises the leading slash, calls ``parseSlashCommand``
 *      which resolves alias → canonical root, then probes ``subcommands``
 *      for a sub match, and returns ``{root, sub, args, rawArgs}``.
 *   3. Composer pushes the user line to history (the slash command
 *      itself appears in scrollback like any other input).
 *   4. The matching command's handler runs. Subcommand handlers run
 *      via ``cmd.subcommands[sub].handler`` when ``sub`` is non-empty.
 *   5. Errors are caught and turned into LogItem(level=warn).
 *
 * Stream-safe gating:
 *   Composer checks ``cmd.streamSafe`` (and ``sub.streamSafe`` when a
 *   sub is matched) before dispatching while ``streamState !== "idle"``.
 *   Anything that isn't stream-safe is blocked with a "wait or Esc"
 *   notice. The default is "unsafe" so adding a new command without
 *   thinking about streaming behaviour fails closed.
 *
 * Adding a command: add an entry to ``buildRegistry()`` below.
 * Anything more than ~25 lines of handler logic should live in a
 * sibling file under ``commands/``.
 */

import { type BladeClient, TUI_PROTOCOL_VERSION } from "../api/client.js";
import { ACTIVE_LANG, t } from "../i18n/index.js";
import { replayRecording } from "../utils/replay.js";
import { PKG_VERSION } from "../utils/version.js";
import type { Action } from "./reducer.js";
import { resetStreamingCounters } from "./streamingRefs.js";
import type {
  AppState,
  ExperimentsCardItem,
  ExperimentsCardRow,
  HelpCardItem,
  HelpCardRow,
  HelpCardSection,
  ModelCardItem,
  ModelCardRow,
  ModelCardSection,
  SessionCardItem,
  SessionCardRow,
} from "./types.js";

/** Display order for ``/help`` and SlashMenu, matching Python's
 *  ``_GROUP_ORDER`` in ``tui/commands.py``. */
export type SlashGroup = "general" | "business" | "skills" | "dynamic";

export const SLASH_GROUP_ORDER: readonly SlashGroup[] = [
  "general",
  "business",
  "skills",
  "dynamic",
] as const;

export interface SlashCommandContext {
  client: BladeClient;
  sessionId: string;
  /**
   * Snapshot of AppState at the moment of dispatch. Captured fresh
   * by the Composer for each invocation; handlers must NOT cache it
   * across awaits because subsequent dispatches will have made it
   * stale.
   */
  state: AppState;
  /** The active registry — used by /help and any future introspecting command. */
  registry: SlashCommandRegistry;
  dispatch: (action: Action) => void;
  /** Closes the Ink app — used by /exit and /quit. */
  exit: () => void;
  /**
   * Allocate a fresh AbortController for a long-running command (M8
   * replay). The signal is passed into helpers that respect cancel
   * (e.g. ``replayRecording``). Cancelling any previous one is the
   * caller's responsibility — useStream.beginReplay handles that.
   */
  beginReplay: () => AbortController;
  /**
   * Allocate a fresh AbortController for the manual /compact run.
   * The signal is passed into ``streamCompactSession`` so the
   * Composer's Esc handler can interrupt the in-flight compaction.
   * Mirror of ``beginReplay`` — see useStream for the per-call
   * cancellation contract. */
  beginManualCompact: () => AbortController;
  /**
   * Submit a natural-language turn — used by /retry and /run. We
   * expose the Composer's bound ``submitTurn`` rather than calling
   * ``client.streamTurn`` directly so the AbortController + state
   * machine in useStream stay in charge (otherwise /retry would race
   * an in-flight turn it doesn't know exists). Optional because the
   * smoke harness can't easily fake the hook.
   */
  submitTurn?: (
    input: string,
    opts?: { dryRun?: boolean },
  ) => Promise<void> | void;
}

/** Subcommand under a two-level command (e.g. ``/skills list``). The
 *  parent's ``streamSafe`` is the default; specifying it on the
 *  sub overrides for that one entry only. */
export interface SlashSubcommand {
  name: string;
  description: string;
  /** Token-shaped usage hint, e.g. ``<key> <value>`` — appended after
   *  the sub name in /help and the SlashMenu. */
  usage?: string;
  /** Allow this sub mid-stream regardless of the parent's flag. */
  streamSafe?: boolean;
  /** Set to ``true`` for subs whose handler calls ``ctx.submitTurn(...)``
   *  themselves. Same semantics as the parent ``SlashCommand`` flag —
   *  Composer suppresses its synthetic slash echo so the user only sees
   *  one history entry per submit. None of the current built-ins
   *  uses this on the sub side; reserved for future commands like
   *  ``/skills run <name>`` where the sub fires a real turn. */
  dispatchesOwnTurn?: boolean;
  handler: (ctx: SlashCommandContext, args: string[]) => Promise<void>;
}

export interface SlashCommand {
  /** Canonical name, no leading slash. Lower-case. */
  name: string;
  description: string;
  group: SlashGroup;
  /** Optional alias list (each lower-case, no leading slash). Aliases
   *  resolve to this command in ``registry.get()`` and ``filter()``.  */
  aliases?: string[];
  /** Hidden commands don't appear in /help or the SlashMenu but are
   *  still callable. Used for deprecated names that we want to keep
   *  working without advertising. */
  hidden?: boolean;
  /** Token-shaped usage hint shown in /help (e.g. ``[active|failed|all]``). */
  usage?: string;
  /** Allow invocation while a turn is streaming. Defaults to ``false``
   *  so any new command without explicit thought fails closed. */
  streamSafe?: boolean;
  /** Source of dynamic skill commands — the file/path the skill was
   *  loaded from. Empty for built-ins. */
  origin?: string;
  /** Map of sub-name → SlashSubcommand. Sub names are matched
   *  case-insensitively against the next token after the root. */
  subcommands?: Record<string, SlashSubcommand>;
  /** Bare-root handler. Called when the user typed only ``/<root>`` or
   *  ``/<root> <args...>`` where the next token doesn't match any sub. */
  handler: (ctx: SlashCommandContext, args: string[]) => Promise<void>;
  /** Set to ``true`` for commands that themselves call
   *  ``ctx.submitTurn(...)`` to start a real NL turn (e.g. ``/run``,
   *  ``/inject``, ``/retry``). The Composer suppresses the synthetic
   *  slash echo for these so the user doesn't see the same content
   *  twice in scrollback (once as ``/run inject CPU``, once as
   *  ``inject CPU`` from the turn's own ``TURN_STARTED``). When the
   *  command is BLOCKED before it can fire (unknown command, stream-
   *  safe gate refusal), the Composer still echoes the slash line so
   *  the user has a context anchor for the warning that follows. */
  dispatchesOwnTurn?: boolean;
}

/** Result of ``parseSlashCommand``. ``root`` is canonical (alias
 *  resolved). ``sub`` is the matched subcommand name or ``""``. */
export interface ParsedCommand {
  /** Canonical root name (alias resolved), no slash, lower-case. */
  root: string;
  /** Matched subcommand (lower-case), or ``""``. */
  sub: string;
  /** Tokens after the root [+ sub], split on whitespace. */
  args: string[];
  /** Original argument text after the root [+ sub], untrimmed —
   *  useful for handlers that need verbatim user input (e.g.
   *  ``/run <NL>`` or ``/config set key value with spaces``). */
  rawArgs: string;
}

/** Legacy two-token parser kept for callers that don't want subcommand
 *  resolution. Returns ``{name, args}`` where ``name`` is whatever the
 *  user typed (alias NOT resolved). New code should use
 *  ``parseSlashCommand`` against a registry. */
export function parseSlashLine(
  line: string,
): { name: string; args: string[] } | null {
  if (!line.startsWith("/")) return null;
  const tokens = line
    .slice(1)
    .split(/\s+/)
    .map((t) => t.trim())
    .filter(Boolean);
  if (tokens.length === 0) return null;
  const [name, ...args] = tokens;
  if (!name) return null;
  return { name: name.toLowerCase(), args };
}

/** Two-level slash parser. Resolves aliases via ``registry.get()`` and
 *  probes the matched command's ``subcommands`` for a sub match.
 *
 *  Examples (with ``/skills`` registered with subs ``list``/``install``):
 *    ``/help``                  → {root:"help", sub:"", args:[], rawArgs:""}
 *    ``/run CPU stress``        → {root:"run", sub:"", args:["CPU","stress"], rawArgs:"CPU stress"}
 *    ``/skills list``           → {root:"skills", sub:"list", args:[], rawArgs:""}
 *    ``/skills install foo``    → {root:"skills", sub:"install", args:["foo"], rawArgs:"foo"}
 *    ``/skills nonsense``       → {root:"skills", sub:"", args:["nonsense"], rawArgs:"nonsense"}
 *    ``/quit`` (alias of /exit) → {root:"exit", sub:"", args:[], rawArgs:""}
 *
 *  Returns ``null`` if the line isn't a slash command, the registry
 *  has no matching command for the root, or the line is just ``/``.
 */
export function parseSlashCommand(
  line: string,
  registry: SlashCommandRegistry,
): ParsedCommand | null {
  if (!line.startsWith("/")) return null;
  const text = line.slice(1).trimStart();
  if (text.length === 0) return null;
  // Find the first whitespace run — root is what's before it.
  const wsMatch = text.match(/\s+/);
  const rootRaw = wsMatch ? text.slice(0, wsMatch.index) : text;
  const afterRoot = wsMatch ? text.slice(wsMatch.index! + wsMatch[0].length) : "";
  const rootKey = rootRaw.toLowerCase();
  const cmd = registry.get(rootKey);
  if (!cmd) return null;
  const root = cmd.name; // canonical (alias resolved)

  // Probe for a subcommand match against the next token.
  if (cmd.subcommands) {
    const subWsMatch = afterRoot.match(/\s+/);
    const subRaw = subWsMatch
      ? afterRoot.slice(0, subWsMatch.index)
      : afterRoot;
    const afterSub = subWsMatch
      ? afterRoot.slice(subWsMatch.index! + subWsMatch[0].length)
      : "";
    const subKey = subRaw.toLowerCase();
    if (subKey && cmd.subcommands[subKey]) {
      return {
        root,
        sub: subKey,
        args: afterSub.split(/\s+/).filter(Boolean),
        rawArgs: afterSub,
      };
    }
  }

  return {
    root,
    sub: "",
    args: afterRoot.split(/\s+/).filter(Boolean),
    rawArgs: afterRoot,
  };
}

export class SlashCommandRegistry {
  private readonly _commands: SlashCommand[];
  private readonly _byName: Map<string, SlashCommand>;

  constructor(commands: SlashCommand[]) {
    this._commands = [...commands].sort((a, b) => a.name.localeCompare(b.name));
    this._byName = new Map();
    for (const cmd of this._commands) {
      this._byName.set(cmd.name.toLowerCase(), cmd);
      for (const alias of cmd.aliases ?? []) {
        this._byName.set(alias.toLowerCase(), cmd);
      }
    }
  }

  /** All registered commands. Hidden commands are EXCLUDED by default
   *  so /help and the SlashMenu don't show deprecated entries; pass
   *  ``includeHidden`` to get the full set. */
  list(opts?: { includeHidden?: boolean }): readonly SlashCommand[] {
    if (opts?.includeHidden) return this._commands;
    return this._commands.filter((c) => !c.hidden);
  }

  /** Filter visible commands by prefix (after the leading slash).
   *  Empty prefix → full visible list. Aliases also match. */
  filter(
    prefix: string,
    opts?: { includeHidden?: boolean },
  ): SlashCommand[] {
    const p = prefix.toLowerCase();
    const pool = this.list(opts);
    if (!p) return [...pool];
    return pool.filter(
      (c) =>
        c.name.startsWith(p) ||
        (c.aliases?.some((a) => a.startsWith(p)) ?? false),
    );
  }

  /** Lookup by canonical name OR alias. Hidden commands ARE returned
   *  here — they're still callable, just not advertised. */
  get(name: string): SlashCommand | undefined {
    return this._byName.get(name.toLowerCase());
  }

  /** Group→commands map, in display order. Hidden commands excluded
   *  unless ``includeHidden`` is set. */
  listByGroup(opts?: {
    includeHidden?: boolean;
  }): Record<SlashGroup, SlashCommand[]> {
    const out = Object.fromEntries(
      SLASH_GROUP_ORDER.map((g) => [g, [] as SlashCommand[]]),
    ) as Record<SlashGroup, SlashCommand[]>;
    for (const cmd of this.list(opts)) {
      (out[cmd.group] ?? out.general).push(cmd);
    }
    return out;
  }
}

// ── built-in commands ────────────────────────────────────────────────

function pushLog(
  ctx: SlashCommandContext,
  text: string,
  level: "info" | "warn" | "ok" = "info",
): void {
  ctx.dispatch({ type: "LOG_APPENDED", text, level });
}

/** Factory for the per-density ``/mode <density>`` subcommand
 *  handlers. Direct-set semantics — no toggle, no cycle, no error
 *  if already at that value (just informs the user). */
function makeDisplayModeHandler(
  target: AppState["config"]["displayMode"],
): (ctx: SlashCommandContext, args: string[]) => Promise<void> {
  return async (ctx) => {
    const current = ctx.state.config.displayMode;
    if (current === target) {
      pushLog(ctx, t("display.already", { mode: current }), "info");
      return;
    }
    ctx.dispatch({ type: "DISPLAY_MODE_CHANGED", mode: target });
    pushLog(ctx, t("display.changed", { mode: target }), "ok");
  };
}

/** Factory for the per-mode ``/permission <mode>`` subcommand
 *  handlers. Same direct-set pattern as ``makeDisplayModeHandler``
 *  above. Mode change writes through to ``state.config.permissionMode``
 *  immediately; takes effect on the NEXT ``/turn`` because the
 *  current request (if any) was already started with its own value
 *  baked into the server-side initial_state. */
function makePermissionModeHandler(
  target: AppState["config"]["permissionMode"],
): (ctx: SlashCommandContext, args: string[]) => Promise<void> {
  return async (ctx) => {
    const current = ctx.state.config.permissionMode;
    if (current === target) {
      pushLog(ctx, t("mode.already", { mode: current }), "info");
      return;
    }
    ctx.dispatch({ type: "MODE_TOGGLED", mode: target });
    pushLog(ctx, t("mode.changed", { mode: target }), "ok");
  };
}

/** Build + dispatch the session-info card. Shared by ``/session``
 *  and the hidden ``/status`` alias so both commands produce the
 *  identical card (the only difference between the two is their
 *  ``hidden`` flag, which gates SlashMenu visibility — not output).
 *
 *  Failure path: server-state probe failed → fall back to a plain
 *  warn-log instead of a card. The card requires a populated row
 *  list, and a half-empty card on transport error would mislead
 *  more than a clear "could not read" line.
 *
 *  ``created_at``'s raw ISO ``2026-05-21T02:22:11.086040+08:00`` is
 *  intentionally NOT passed through verbatim — the card formats it
 *  via the same ``formatDateTime`` helper RuntimeDoctorCard uses
 *  (header tail), so all info-card timestamps share one column.
 *  We pass the raw ISO through ``capturedAt`` and let the renderer
 *  format. But the user's session created_at goes into its own row;
 *  format that here so the row column lines up too.
 */
async function runSessionInfoHandler(ctx: SlashCommandContext): Promise<void> {
  let state: Record<string, unknown>;
  try {
    state = await ctx.client.getSessionState(ctx.sessionId);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    pushLog(ctx, t("status.failed", { err: msg }), "warn");
    return;
  }

  const taskCount = Array.isArray(state["task_ids"])
    ? (state["task_ids"] as string[]).length
    : 0;

  const cluster = (state["cluster"] as string) || "";
  const model = (state["model_name"] as string) || "";

  const rows: SessionCardRow[] = [
    { label: t("status.session"), value: ctx.sessionId },
    {
      label: t("status.cluster"),
      value: cluster || t("common.none"),
      dim: !cluster,
    },
    {
      label: t("status.namespace"),
      value: (state["namespace"] as string) || "default",
    },
    {
      label: t("status.model"),
      value: model || t("common.unset"),
      dim: !model,
    },
    {
      label: t("status.mode"),
      value: ctx.state.config.permissionMode,
    },
    { label: t("status.tasks"), value: String(taskCount) },
  ];

  // ``capturedAt`` is the SESSION's created_at, NOT when /session was
  // invoked. The header tail then reads "when did this session start"
  // — the more useful signal of the two for a user asking "what is
  // my session?". When the server omits created_at we leave the
  // header tail empty (renderer drops the "  · …" suffix) rather
  // than fall back to "now", which would be misleadingly fresh on
  // an older session.
  const item: SessionCardItem = {
    kind: "session_card",
    id: `session-${Date.now()}`,
    capturedAt: (state["created_at"] as string) || "",
    rows,
  };
  ctx.dispatch({ type: "HISTORY_APPENDED", item });
}

/** Body of ``/run``. Treats the entire arg string as a natural-language
 *  turn and dispatches through ``submitTurn`` — same path as plain
 *  typed input. Without args we just print the usage hint instead of
 *  submitting an empty turn (which the server would reject anyway). */
async function runHandler(
  ctx: SlashCommandContext,
  args: string[],
): Promise<void> {
  const nl = args.join(" ").trim();
  if (!nl) {
    pushLog(ctx, t("run.usage"), "warn");
    return;
  }
  if (ctx.state.streamState !== "idle") {
    // Same protection as /retry — don't race a live turn. The
    // stream-safe gate at the Composer should already block this,
    // but a defense-in-depth check costs nothing and keeps the
    // command's invariants self-contained.
    pushLog(ctx, t("retry.busy"), "warn");
    return;
  }
  if (!ctx.submitTurn) {
    pushLog(ctx, t("retry.unavailable"), "warn");
    return;
  }
  await ctx.submitTurn(nl);
}

/** Shared recordings-listing helper. Used by both bare ``/recordings``
 *  and the explicit ``/recordings list`` sub so the two paths print
 *  identical output. Pulled out (rather than duplicated) so a future
 *  schema tweak (e.g. adding modified_at to the row) lands in one
 *  place. */
async function listRecordingsHandler(
  ctx: SlashCommandContext,
): Promise<void> {
  try {
    const data = await ctx.client.listRecordings();
    const items = (data["recordings"] as Array<Record<string, unknown>>) ?? [];
    if (items.length === 0) {
      pushLog(ctx, t("recordings.empty"), "info");
      return;
    }
    const head = t("recordings.head", { n: items.length });
    const rows = items.slice(0, 20).map((r) => {
      const id = (r["task_id"] as string) ?? "?";
      const size = (r["size_bytes"] as number) ?? 0;
      return `  ${id}  ·  ${formatBytes(size)}`;
    });
    pushLog(
      ctx,
      [head, ...rows, "", t("recordings.use_replay")].join("\n"),
      "info",
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    pushLog(ctx, t("recordings.failed", { err: msg }), "warn");
  }
}

/** ``/plan <NL>`` — Phase 3c.2 dry-run handler. Mirrors
 *  ``runHandler`` shape but flags the turn as ``dryRun``
 *  so the server agent runs intent + planning + safety_check +
 *  confirmation_gate without ever side-effecting (no blade_create
 *  call, no checkpoint mutation past ``confirmation_gate``). The
 *  user sees a "what would happen" preview AIMessage and can then
 *  iterate with another ``/plan`` or commit with ``/run``. */
async function planHandler(
  ctx: SlashCommandContext,
  args: string[],
): Promise<void> {
  const nl = args.join(" ").trim();
  if (!nl) {
    pushLog(ctx, t("plan.usage"), "warn");
    return;
  }
  if (ctx.state.streamState !== "idle") {
    pushLog(ctx, t("retry.busy"), "warn");
    return;
  }
  if (!ctx.submitTurn) {
    pushLog(ctx, t("retry.unavailable"), "warn");
    return;
  }
  await ctx.submitTurn(nl, { dryRun: true });
}

// ── Locator command helpers ──────────────────────────────────────────
//
// These four resolve a typed token (``E1``, ``T1``, ``1``, ``T 1`` …)
// against ``state.locators.byId`` and render the appropriate
// snapshot. Shared parsing keeps the typo-tolerance logic in one
// place; mirrors Python's ``tui/controllers/commands.py`` lines
// 486–620.

/** Normalise a user-typed locator token. Returns the canonical form
 *  (``T1`` / ``E1``) or ``null`` if the input doesn't look like a
 *  locator. Tolerates ``"1"`` as ``"T1"`` for /expand parity with
 *  Python (``/expand 1`` is a common typo path). The ``defaultKind``
 *  argument decides how a bare-number fallback is resolved. */
function normaliseLocator(
  raw: string,
  defaultKind: "T" | "E" | null,
): string | null {
  const cleaned = raw.replace(/\s+/g, "").toUpperCase();
  if (!cleaned) return null;
  // Already canonical: ``T1`` / ``E12`` / etc.
  if (/^[ET]\d+$/.test(cleaned)) return cleaned;
  // Bare number fallback: only accept if caller specified a default
  // kind. ``/expand 1`` → ``T1``; ``/show 1`` rejected (ambiguous).
  if (defaultKind && /^\d+$/.test(cleaned)) {
    return `${defaultKind}${cleaned}`;
  }
  return null;
}

async function runLocatorShowHandler(
  ctx: SlashCommandContext,
  args: string[],
): Promise<void> {
  const raw = args.join(" ").trim();
  if (!raw) {
    pushLog(ctx, t("locator.usage_show"), "warn");
    return;
  }
  const loc = normaliseLocator(raw, null);
  if (!loc) {
    pushLog(ctx, t("locator.not_found", { loc: raw }), "warn");
    return;
  }
  const item = ctx.state.locators.byId[loc];
  if (!item) {
    pushLog(ctx, t("locator.not_found", { loc }), "warn");
    return;
  }
  if (item.kind === "tool") {
    pushLog(
      ctx,
      `**[${loc}] ${item.name}**\n${item.resultPreview || t("common.none")}`,
      "info",
    );
    return;
  }
  // Result item. Render a compact summary card matching the live
  // ResultCard's signal set.
  const lines = [
    `**[${loc}] ${item.faultType || t("common.unknown")}**`,
    `  task=${item.taskId}`,
    `  status=${item.status}`,
    item.bladeUid ? `  uid=${item.bladeUid}` : "",
    item.duration ? `  duration=${item.duration}` : "",
    item.summary ? `  ${item.summary}` : "",
    item.cause ? `  cause: ${item.cause}` : "",
    item.hint ? `  hint:  ${item.hint}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  pushLog(ctx, lines, "info");
}

async function runLocatorCopyHandler(
  ctx: SlashCommandContext,
  args: string[],
): Promise<void> {
  const raw = args.join(" ").trim();
  if (!raw) {
    pushLog(ctx, t("locator.usage_copy"), "warn");
    return;
  }
  const loc = normaliseLocator(raw, null);
  if (!loc) {
    pushLog(ctx, t("locator.not_found", { loc: raw }), "warn");
    return;
  }
  const item = ctx.state.locators.byId[loc];
  if (!item) {
    pushLog(ctx, t("locator.not_found", { loc }), "warn");
    return;
  }
  if (item.kind === "tool") {
    // Triple-fence so the terminal's copy gesture grabs the whole
    // output cleanly (one click on the fence picks up everything
    // between, no trailing chrome).
    pushLog(
      ctx,
      t("locator.copy_tool_header", { loc, name: item.name }) +
        "\n```\n" +
        (item.raw || t("common.none")) +
        "\n```",
      "info",
    );
    return;
  }
  // Experiment: dump the original NL + status as a paste-friendly
  // block. JSON the structured fields so the receiving end can
  // parse if they want.
  const payload = {
    locator: loc,
    task_id: item.taskId,
    fault_type: item.faultType,
    status: item.status,
    blade_uid: item.bladeUid,
    duration: item.duration,
    user_input: item.userInput ?? "",
    summary: item.summary,
    cause: item.cause ?? "",
    hint: item.hint ?? "",
  };
  pushLog(
    ctx,
    t("locator.copy_experiment_header", { loc }) +
      "\n```json\n" +
      JSON.stringify(payload, null, 2) +
      "\n```",
    "info",
  );
}

async function runLocatorRerunHandler(
  ctx: SlashCommandContext,
  args: string[],
): Promise<void> {
  const raw = args.join(" ").trim();
  if (!raw) {
    pushLog(ctx, t("locator.usage_rerun"), "warn");
    return;
  }
  const loc = normaliseLocator(raw, "E");
  if (!loc) {
    pushLog(ctx, t("locator.not_found", { loc: raw }), "warn");
    return;
  }
  if (!loc.startsWith("E")) {
    // ``/rerun T1`` is meaningless — tools aren't user-issued.
    pushLog(ctx, t("locator.rerun_not_experiment"), "warn");
    return;
  }
  const item = ctx.state.locators.byId[loc];
  if (!item || item.kind !== "result") {
    pushLog(ctx, t("locator.not_found", { loc }), "warn");
    return;
  }
  const desc = item.userInput
    ? item.userInput.trim()
    : `[no original prompt cached for ${loc}]`;
  pushLog(ctx, t("locator.rerun_hint", { loc, desc }), "info");
}

async function runLocatorExpandHandler(
  ctx: SlashCommandContext,
  args: string[],
): Promise<void> {
  const raw = args.join(" ").trim();
  if (!raw) {
    pushLog(ctx, t("locator.usage_expand"), "warn");
    return;
  }
  // ``/expand 1`` and ``/expand T1`` both resolve to T1 — this is the
  // typo-tolerant default the Python TUI also offers.
  const loc = normaliseLocator(raw, "T");
  if (!loc) {
    pushLog(ctx, t("locator.not_found", { loc: raw }), "warn");
    return;
  }
  if (!loc.startsWith("T")) {
    pushLog(ctx, t("locator.expand_not_tool"), "warn");
    return;
  }
  const item = ctx.state.locators.byId[loc];
  if (!item || item.kind !== "tool") {
    pushLog(ctx, t("locator.not_found", { loc }), "warn");
    return;
  }
  const body = item.raw || item.resultPreview || t("common.none");
  pushLog(
    ctx,
    `**[${loc}] ${item.name}** · ${item.node || "?"}\n${body}`,
    "info",
  );
}

/**
 * ANSI viewport-clear (preserves scrollback). Emitted by /clear before
 * dispatching HISTORY_CLEARED so the visible terminal frame matches
 * the now-empty state.
 *
 *   \x1b[H   — cursor home (top-left)
 *   \x1b[J   — erase from cursor to end of screen (i.e. whole viewport)
 *
 * Why we DON'T emit \x1b[3J (erase scrollback) anymore (Phase 3.4):
 * the previous sequence ``\x1b[3J\x1b[2J\x1b[H`` wiped the user's
 * entire scrollback history, matching ``bash clear`` semantics but
 * diverging from how Claude Code / qwen-code / modern TUIs treat
 * /clear. Users expect /clear to give them a fresh dialogue surface
 * WITHOUT losing the prior session as recoverable scroll history —
 * "clear" is about starting a new chat, not wiping the record. The
 * scrollback wipe was also asymmetric with the in-app history reset:
 * ``HISTORY_CLEARED`` only drops the in-memory ``history`` array,
 * leaving the user no in-app way to recover anything, AND the
 * terminal scrollback was simultaneously nuked — total information
 * loss. Switching to viewport-only clear leaves the terminal-level
 * scrollback intact so the user can scroll up to read prior turns
 * even after /clear, matching the mental model "the conversation
 * starts fresh, but the record is preserved".
 *
 * Bumping ``historyRemountKey`` (in the reducer's HISTORY_CLEARED
 * case) is what forces Ink's ``<Static>`` to remount and start a
 * fresh append-only stream from the cleared viewport — that part
 * is unchanged. Ink will write new items starting at the (now
 * home) cursor, naturally overlaying whatever scrollback content
 * sits underneath the new viewport contents as the user scrolls.
 */
const CLEAR_SCREEN = "\x1b[H\x1b[J";

/** Build the command registry. ``dynamicCommands`` are appended to
 *  the built-in set — used by BootOrchestrator to inject one
 *  ``/<skill-name>`` per loaded skill at boot time. Built-in names
 *  always win over a colliding dynamic name (we silently drop the
 *  dynamic on collision; the conflict is logged at registration). */
export function buildRegistry(opts?: {
  dynamicCommands?: SlashCommand[];
}): SlashCommandRegistry {
  const builtIn = buildBuiltInCommands();
  const builtInNames = new Set(builtIn.map((c) => c.name));
  const dynamic = (opts?.dynamicCommands ?? [])
    .filter((c) => !builtInNames.has(c.name))
    .map((c) => ({ ...c, group: "dynamic" as const }));
  return new SlashCommandRegistry([...builtIn, ...dynamic]);
}

function buildBuiltInCommands(): SlashCommand[] {
  return [
    {
      name: "help",
      description: t("command.help.desc"),
      group: "general",
      aliases: ["?"],
      streamSafe: true,
      async handler(ctx) {
        ctx.dispatch({
          type: "HISTORY_APPENDED",
          item: buildHelpCard(ctx.registry),
        });
      },
    },
    {
      name: "clear",
      description: t("command.clear.desc"),
      group: "general",
      // /clear wipes scrollback — fine to fire mid-stream because the
      // active turn's pending stays put (HISTORY_CLEARED only nukes
      // committed history). Kept stream-safe for parity with Python's
      // _STREAM_SAFE-adjacent behaviour: /clear is the user's primary
      // "I can't see anything" recovery and blocking it would be cruel.
      streamSafe: true,
      async handler(ctx) {
        // Wipe what's already burn-in'd to scrollback BEFORE we
        // dispatch HISTORY_CLEARED. The dispatch then bumps
        // historyRemountKey, which forces ``<Static>`` to remount and
        // start its own append-only stream from scratch.
        try {
          process.stdout.write(CLEAR_SCREEN);
        } catch {
          // Best-effort — tests / non-TTY contexts may have a stub stdout.
        }
        ctx.dispatch({ type: "HISTORY_CLEARED" });
      },
    },
    {
      name: "exit",
      description: t("command.exit.desc"),
      group: "general",
      aliases: ["quit"],
      streamSafe: true,
      async handler(ctx) {
        ctx.exit();
      },
    },
    {
      name: "doctor",
      description: t("command.doctor.desc"),
      group: "general",
      streamSafe: true,
      async handler(ctx) {
        // /doctor is best-effort and never throws — every probe falls
        // back to a sentinel so the diagnostic card still renders when
        // the server is down. This is the command users reach for
        // *because* something is wrong, so it must always succeed.
        //
        // Fan out four real probes in parallel:
        //   1. ``health()`` — liveness check on the server
        //   2. ``getSessionState()`` — pulls the live cluster name
        //   3. ``getServerVersion()`` — server build identity
        //   4. ``getPreflight()`` — the same seven live checks the
        //      boot screen runs (LLM key valid / kubeconfig parses /
        //      kubectl works / blade works / skills loaded / k8s
        //      reachable / chaosblade_operator installed). Re-running
        //      it here means session-mid changes — a revoked LLM key,
        //      a dropped k8s link, an uninstalled operator — show up
        //      instead of the user staring at the stale boot snapshot.
        //
        // ``Promise.all`` — preflight is the slowest probe (server-side
        // 8s budget) but the others typically finish in well under 1s,
        // so we wait on the slowest. Acceptable: the user invoked
        // ``/doctor`` precisely to inspect the current state, a couple
        // of seconds for a definitive answer is fine.
        const [reachable, sessionState, serverVersion, preflight] =
          await Promise.all([
            ctx.client.health().catch(() => false),
            ctx.client.getSessionState(ctx.sessionId).catch(() => null),
            ctx.client.getServerVersion(),
            ctx.client.getPreflight(),
          ]);

        let cluster = "";
        if (sessionState && typeof sessionState === "object") {
          cluster = (sessionState["cluster"] as string) || "";
        }

        // Map the preflight envelope into the renderer-shaped check
        // rows. Mirrors ``BootOrchestrator``'s mapping so both cards
        // see the same fields with the same fallbacks.
        const rawChecks =
          (preflight?.["checks"] as Array<Record<string, unknown>> | undefined) ??
          [];
        const checks = rawChecks.map((c) => ({
          name: (c["name"] as string) ?? "",
          severity: ((c["severity"] as string) ?? "warning") as
            | "blocking"
            | "warning",
          passed: Boolean(c["passed"]),
          message: (c["message"] as string) ?? "",
          fix: (c["fix"] as string) ?? "",
        }));
        const passedCount =
          (preflight?.["passed_count"] as number) ??
          checks.filter((c) => c.passed).length;
        const totalCount =
          (preflight?.["total_count"] as number) ?? checks.length;

        // Card item is fully assembled at the call site — the renderer
        // is presentational. Multiple ``/doctor`` invocations within
        // a session each push a new card, so the ID embeds the call
        // timestamp to avoid React key collisions in scrollback.
        ctx.dispatch({
          type: "HISTORY_APPENDED",
          item: {
            kind: "runtime_doctor_card",
            id: `runtime-doctor-${Date.now()}`,
            reachable,
            serverUrl: ctx.client.url,
            cluster,
            tuiVersion: PKG_VERSION,
            serverVersion,
            tuiProtocol: TUI_PROTOCOL_VERSION,
            serverProtocol: ctx.client.serverProtocolVersion ?? null,
            lang: ACTIVE_LANG,
            mode: ctx.state.config.permissionMode,
            capturedAt: new Date().toISOString(),
            checks,
            passedCount,
            totalCount,
            preflightUnavailable: preflight === null,
          },
        });
      },
    },
    {
      // ``/mode`` is the DISPLAY-DENSITY toggle, mirroring Python's
      // ``calm | working | dense`` (Python ``tui/controllers/commands.py``
      // ``_register_all`` near line 125). The previous TS revision used
      // ``/mode`` for the permission toggle (auto / confirm); that has
      // been moved to ``/permission`` so the two front-ends parse and
      // mean the same thing.
      name: "mode",
      description: t("command.mode.desc"),
      group: "general",
      // No ``usage`` — the three subcommands (calm / working / dense)
      // below already enumerate every valid value, and showing the same
      // list twice ("/mode [calm|working|dense]" header + three sub
      // rows) just thickens the help card without adding signal. The
      // SlashMenu Enter-autocomplete branch still fires because
      // ``subcommands`` is set; users picking ``/mode`` from the menu
      // still get the trailing-space buffer + sub-menu fan-out.
      streamSafe: true,
      subcommands: {
        calm: {
          name: "calm",
          description: t("command.mode.calm.desc"),
          streamSafe: true,
          handler: makeDisplayModeHandler("calm"),
        },
        working: {
          name: "working",
          description: t("command.mode.working.desc"),
          streamSafe: true,
          handler: makeDisplayModeHandler("working"),
        },
        dense: {
          name: "dense",
          description: t("command.mode.dense.desc"),
          streamSafe: true,
          handler: makeDisplayModeHandler("dense"),
        },
      },
      // Reachable in two cases — bare ``/mode`` (no token), or
      // ``/mode <unknown>`` (token that didn't match any sub). Both
      // surface a hint rather than acting. The previous bare-cycle
      // semantic was a foot-gun: users would land on a mode without
      // knowing which one, then re-invoke to "fix" it. Explicit
      // ``/mode calm`` / ``working`` / ``dense`` still routes through
      // the subcommands above and never reaches this handler.
      async handler(ctx, args) {
        const arg = (args[0] ?? "").toLowerCase();
        if (arg.length > 0) {
          pushLog(ctx, t("display.usage_unknown", { value: arg }), "warn");
          return;
        }
        pushLog(
          ctx,
          t("display.usage_missing", { mode: ctx.state.config.displayMode }),
          "warn",
        );
      },
    },
    {
      // Permission mode. Same structural shape as ``/mode`` above —
      // two named subs (``auto`` / ``confirm``), no usage hint
      // (subs already enumerate the values), bare invocation falls
      // through to the handler below which surfaces a "missing arg"
      // hint instead of implicitly toggling. The previous bare-toggle
      // semantic was a foot-gun: ``confirm`` is the safe default and
      // a stray ``/permission`` (no arg) silently flipping the user
      // into ``auto`` removes the safety gate without their knowing.
      name: "permission",
      description: t("command.permission.desc"),
      group: "general",
      // No ``usage`` — the two subs below enumerate every valid
      // value, mirroring the ``/mode`` cleanup. SlashMenu's
      // Enter-autocomplete branch still kicks in because
      // ``subcommands`` is set.
      streamSafe: true,
      subcommands: {
        auto: {
          name: "auto",
          description: t("command.permission.auto.desc"),
          streamSafe: true,
          handler: makePermissionModeHandler("auto"),
        },
        confirm: {
          name: "confirm",
          description: t("command.permission.confirm.desc"),
          streamSafe: true,
          handler: makePermissionModeHandler("confirm"),
        },
      },
      // Bare ``/permission`` (no token) or unknown-arg path. Subs
      // ``/permission auto`` / ``/permission confirm`` route through
      // the subcommand map above and never reach this handler.
      async handler(ctx, args) {
        const arg = (args[0] ?? "").toLowerCase();
        if (arg.length > 0) {
          pushLog(ctx, t("mode.usage_unknown", { value: arg }), "warn");
          return;
        }
        pushLog(
          ctx,
          t("mode.usage_missing", { mode: ctx.state.config.permissionMode }),
          "warn",
        );
      },
    },
    {
      name: "retry",
      description: t("command.retry.desc"),
      group: "general",
      // /retry only fires when streamState === "idle" (handler enforces);
      // not stream-safe at the gate level so streaming still rejects.
      // The handler calls ``ctx.submitTurn`` which itself dispatches
      // ``TURN_STARTED`` (the real user echo) — flag suppresses the
      // synthetic slash echo so the user doesn't see two consecutive
      // ``/retry``-related lines for one keystroke.
      dispatchesOwnTurn: true,
      async handler(ctx) {
        const last = ctx.state.lastTurnInput?.trim();
        if (!last) {
          pushLog(ctx, t("retry.no_input"), "warn");
          return;
        }
        if (ctx.state.streamState !== "idle") {
          // Refuse to retry mid-turn — would race with the live
          // AbortController in useStream and leave streamState
          // inconsistent. The user can /cancel first.
          pushLog(ctx, t("retry.busy"), "warn");
          return;
        }
        if (!ctx.submitTurn) {
          // Smoke / test contexts don't wire submitTurn through.
          pushLog(ctx, t("retry.unavailable"), "warn");
          return;
        }
        const preview = last.length > 60 ? `${last.slice(0, 59)}…` : last;
        pushLog(ctx, t("retry.resubmitting", { input: preview }), "info");
        await ctx.submitTurn(last);
      },
    },
    {
      // ``/session`` shows the current session's metadata (cluster /
      // namespace / model / mode / created / task count). Renamed from
      // the previous ``/status`` to free that name up for parity with
      // Python — Python deprecates ``/status`` in favour of
      // ``/review`` (task-specific). ``/status`` survives below as a
      // hidden alias so muscle memory keeps working.
      name: "session",
      description: t("command.session.desc"),
      group: "general",
      streamSafe: true,
      async handler(ctx) {
        await runSessionInfoHandler(ctx);
      },
    },
    {
      // Hidden alias of ``/session`` — kept callable so users who
      // typed ``/status`` historically still get session info. Hidden
      // from /help and the SlashMenu so we're not advertising a name
      // that diverges from Python (which uses ``/status`` to mean a
      // task-status review). New code should reach ``/session``
      // directly.
      name: "status",
      description: t("command.status.desc"),
      group: "general",
      hidden: true,
      streamSafe: true,
      async handler(ctx) {
        await runSessionInfoHandler(ctx);
      },
    },
    {
      // ``/run [NL]`` — explicit way to fire a natural-language turn.
      // Mirrors Python's ``/run <description>`` (``tui/controllers/
      // commands.py`` near line 176). Equivalent to typing the same
      // text into the prompt without the leading slash; we honour it
      // so users with muscle memory from the Python TUI don't have
      // to retrain.
      //
      // ``dispatchesOwnTurn`` suppresses Composer's synthetic slash
      // echo — the handler chains through ``ctx.submitTurn(nl)``
      // which itself fires ``TURN_STARTED`` with the unwrapped NL
      // string. Without the suppression the user would see both
      // ``/run inject CPU`` AND ``inject CPU`` echoed back-to-back.
      name: "run",
      description: t("command.run.desc"),
      group: "business",
      usage: "<NL>",
      dispatchesOwnTurn: true,
      async handler(ctx, args) {
        await runHandler(ctx, args);
      },
    },
    {
      // ``/plan <NL>`` — Phase 3c.2 dry-run preview. Calls submitTurn
      // with ``dryRun: true`` so the server's agent runs intent
      // clarification + planning + safety_check + confirmation_gate
      // and emits a preview AIMessage instead of pausing on
      // ``interrupt()`` or invoking ``blade_create``. The flow ends
      // with the user reading the plan in scrollback; they can then
      // iterate with another ``/plan`` or commit via ``/run``.
      //
      // ``dispatchesOwnTurn`` suppresses the synthetic slash echo —
      // ``submitTurn`` already pushes a real ``TURN_STARTED`` with
      // the unwrapped NL, so without the flag the user would see
      // both ``/plan inject CPU`` and ``inject CPU`` back-to-back.
      name: "plan",
      description: t("command.plan.desc"),
      group: "business",
      usage: "<NL>",
      dispatchesOwnTurn: true,
      async handler(ctx, args) {
        await planHandler(ctx, args);
      },
    },
    {
      // ``/show <E#|T#>`` — re-render the snapshot for a locator.
      // Mirrors Python's ``_cmd_show`` (``tui/controllers/commands.py``
      // line 486+). For tools we print the raw output; for results
      // we print a compact textual summary. Read-only → streamSafe.
      name: "show",
      description: t("command.show.desc"),
      group: "business",
      usage: "<E#|T#>",
      streamSafe: true,
      async handler(ctx, args) {
        await runLocatorShowHandler(ctx, args);
      },
    },
    {
      // ``/copy <E#|T#>`` — print the locator's payload as a
      // copy-friendly text block. We don't write to the system
      // clipboard (pbcopy / xclip availability varies in
      // container/SSH environments where the TUI most often runs);
      // a plain text block lets the user select-and-Cmd-C with the
      // terminal's own copy gesture.
      name: "copy",
      description: t("command.copy.desc"),
      group: "business",
      usage: "<E#|T#>",
      streamSafe: true,
      async handler(ctx, args) {
        await runLocatorCopyHandler(ctx, args);
      },
    },
    {
      // ``/rerun <E#>`` — surface the original NL description so the
      // user can paste-and-edit it to re-issue the experiment. We
      // deliberately don't auto-execute: re-running a destructive
      // experiment without an explicit confirm is exactly the
      // foot-gun ``intent_confirm`` exists to prevent.
      name: "rerun",
      description: t("command.rerun.desc"),
      group: "business",
      usage: "<E#>",
      streamSafe: true,
      async handler(ctx, args) {
        await runLocatorRerunHandler(ctx, args);
      },
    },
    {
      // ``/expand <T#>`` — print the FULL cached output for a tool.
      // Pairs with the truncated head shown inline in scrollback;
      // ``/expand`` is the explicit "give me everything". Accepts
      // ``T1``, ``1``, or ``T 1`` for typo tolerance, matching
      // Python.
      name: "expand",
      description: t("command.expand.desc"),
      group: "business",
      usage: "<T#>",
      streamSafe: true,
      async handler(ctx, args) {
        await runLocatorExpandHandler(ctx, args);
      },
    },
    {
      // ``/tasks [active|failed|all] [N]`` — recent tasks list. Mirrors
      // Python's ``_cmd_tasks`` (``tui/controllers/commands.py:457+``)
      // which honours an ``active|failed|all`` filter argument; we add
      // a numeric ``N`` slot so users can override the default page
      // size of 10. Either or both args can appear in any order.
      name: "tasks",
      description: t("command.tasks.desc"),
      group: "business",
      usage: "[active|failed|all] [N]",
      streamSafe: true,
      async handler(ctx, args) {
        const { filter, limit } = parseTasksArgs(args);
        try {
          const data = await ctx.client.listTasks();
          const tasks = (data["tasks"] as Array<Record<string, unknown>>) ?? [];
          if (tasks.length === 0) {
            pushLog(ctx, t("tasks.empty"), "info");
            return;
          }
          const total = (data["total"] as number) ?? tasks.length;
          // Python applies the filter in-process because the DB-level
          // ``task_state`` column doesn't capture the user-facing
          // active/failed slices (which mix ``phase`` + ``status``).
          // We mirror that exactly — ``_passes_filter`` in
          // ``renderers/tasks_table.py:58``.
          const visible = tasks.filter((tk) => passesTasksFilter(tk, filter));
          if (visible.length === 0) {
            pushLog(ctx, t("tasks.empty_filter", { filter, total }), "info");
            return;
          }
          const head = t("tasks.head_filter", {
            filter,
            n: Math.min(limit, visible.length),
            total: visible.length,
            grand: total,
          });
          const sliced = visible.slice(0, limit);
          const rows = formatTasksTable(sliced);
          pushLog(ctx, [head, ...rows].join("\n"), "info");
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          pushLog(ctx, t("tasks.failed", { err: msg }), "warn");
        }
      },
    },
    {
      // ``/review [task_id|E#]`` — show the metric/result card for a
      // task. Mirrors Python ``_cmd_review`` (``tui/controllers/
      // commands.py:434+``). Falls back to "most recent task in the
      // store" when no id is given so users can simply type ``/review``
      // after a finished turn to see what landed.
      //
      // ``E#`` locator is also accepted because users routinely chain
      // ``/show E1`` and ``/review E1`` in the same flow — resolving
      // the locator here saves them an extra copy/paste.
      name: "review",
      description: t("command.review.desc"),
      group: "business",
      usage: "[task_id|E#]",
      streamSafe: true,
      async handler(ctx, args) {
        try {
          let taskId = (args[0] || "").trim();
          // Locator path: /review E1 → resolve to the experiment's
          // taskId via the locators map.
          if (taskId && /^E\d+$/i.test(taskId)) {
            const norm = normaliseLocator(taskId, "E");
            const record = norm ? ctx.state.locators.byId[norm] : undefined;
            if (record && "taskId" in record && record.taskId) {
              taskId = record.taskId;
            } else {
              pushLog(ctx, t("locator.not_found", { loc: taskId }), "warn");
              return;
            }
          }
          if (!taskId) {
            // No arg → fall back to most recent task. Use listTasks
            // and take [0]; Python does the same.
            const list = await ctx.client.listTasks();
            const tasks =
              (list["tasks"] as Array<Record<string, unknown>>) ?? [];
            if (tasks.length === 0) {
              pushLog(ctx, t("review.no_recent"), "warn");
              return;
            }
            taskId = (tasks[0]?.["task_id"] as string) || "";
            if (!taskId) {
              pushLog(ctx, t("review.no_recent"), "warn");
              return;
            }
          }
          const data = await ctx.client.getMetric(taskId);
          pushLog(ctx, formatReviewCard(taskId, data), "info");
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          pushLog(ctx, t("review.failed", { err: msg }), "warn");
        }
      },
    },
    {
      // ``/experiments`` — list every fault scenario the loaded skill
      // catalog exposes. Backed by ``GET /api/v1/skills`` which is
      // cached server-side (per-skill directory fingerprint hits a
      // disk cache at ``~/.blade-ai/memory/tool_cache/...``); typical
      // calls return in <50ms. First call on a freshly-installed
      // skill that ships without a ``references/catalogue/`` dir DOES
      // pay an LLM round-trip — but bundled skills all have catalogue
      // dirs, so the LLM path is uncommon in practice.
      //
      // A loading log is dispatched up front in case the cache really
      // does miss + the LLM path fires. Cache HIT is fast enough that
      // the user sees the loading line for ~0 frames.
      name: "experiments",
      description: t("command.experiments.desc"),
      group: "business",
      streamSafe: true,
      async handler(ctx) {
        pushLog(ctx, t("experiments.loading"), "info");
        try {
          const data = await ctx.client.listSkills();
          ctx.dispatch({
            type: "HISTORY_APPENDED",
            item: buildExperimentsCard(data),
          });
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          pushLog(ctx, t("experiments.failed", { err: msg }), "warn");
        }
      },
    },
    {
      // ``/recover [task_id|list]`` — fault recovery entry point.
      //
      //   /recover list             → list interrupted (injecting/
      //                                injected) tasks via /api/v1/metric
      //   /recover <task_id>        → POST /api/v1/recover, render result
      //
      // The bare-root handler treats the first arg as a task_id; if
      // the user typed ``/recover list`` ``list`` is matched as a sub
      // before we get here. Mirrors Python's two-handler split
      // (_cmd_recover_list + _cmd_recover) on the same command name.
      //
      // Not stream-safe at the gate level — recovery is a real cluster
      // mutation, must run idle. Also not own-turn (request/response
      // not SSE) so the synthetic slash echo is fine.
      name: "recover",
      description: t("command.recover.desc"),
      group: "business",
      // ``list`` lives under subcommands so it's intentionally NOT
      // in the usage hint — the parent header would otherwise read
      // "<task_id|latest|list>" while the same ``list`` row appears
      // again just below. The bare-handler positional args are
      // ``task_id`` and ``latest`` only.
      usage: "<task_id|latest>",
      subcommands: {
        list: {
          name: "list",
          description: t("command.recover.list.desc"),
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.listTasks();
              const tasks =
                (data["tasks"] as Array<Record<string, unknown>>) ?? [];
              // Direct mirror of Python's ``list_interrupted_tasks``:
              // SQL filter ``task_state IN ('injecting','injected')``
              // (see ``persistence/task_store_sqlite.py:227``). We
              // accept the value from either ``task_state`` (legacy /
              // SQL column name) or ``status`` (newer metric envelope
              // field) because the server's API serialiser has used
              // both names across versions; whichever surfaces first
              // satisfies the same semantic check.
              //
              // Tasks in OTHER states are deliberately excluded:
              //   - ``planning`` / mid-injection → no blade resource
              //     allocated yet; recover would fail with NO_BLADE_UID
              //   - ``failed`` / ``error`` → already terminal; recover
              //     graph would error on the missing checkpoint
              //   - ``recovered`` / ``partial_recovered`` → already
              //     past the recovery boundary
              const candidates = tasks.filter((tk) => {
                const status = ((tk["status"] as string) || "").toLowerCase();
                const taskState =
                  ((tk["task_state"] as string) || "").toLowerCase();
                return (
                  status === "injecting" ||
                  status === "injected" ||
                  taskState === "injecting" ||
                  taskState === "injected"
                );
              });
              if (candidates.length === 0) {
                pushLog(ctx, t("recover.list_empty"), "info");
                return;
              }
              const head = t("recover.list_head", { n: candidates.length });
              const rows = candidates.slice(0, 20).map((row) => {
                const id = (row["task_id"] as string) ?? "?";
                const fault = formatFaultType(row);
                const created = (row["gmt_create"] as string) ||
                  (row["created_at"] as string) ||
                  "";
                const short = created.replace("T", " ").slice(0, 19);
                return `  ${id}  ${fault || t("common.unknown")}  · ${short}`;
              });
              pushLog(
                ctx,
                [head, ...rows, "", t("recover.list_hint")].join("\n"),
                "info",
              );
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("recover.list_failed", { err: msg }), "warn");
            }
          },
        },
      },
      async handler(ctx, args) {
        let taskId = (args[0] || "").trim();
        // ``/recover latest`` — translate to the most recently
        // completed task. Mirrors Python ``_cmd_recover``
        // (``tui/controllers/commands.py:840``) which does the same
        // alias on its ``conversation.last_task_id``. The translation
        // is a single-shot keyword match (case-insensitive) — actual
        // task ids never collide because they're UUIDs prefixed with
        // ``task-``.
        if (taskId.toLowerCase() === "latest") {
          if (!ctx.state.lastTaskId) {
            pushLog(ctx, t("recover.no_latest"), "warn");
            return;
          }
          taskId = ctx.state.lastTaskId;
        }
        if (!taskId) {
          pushLog(ctx, t("recover.usage"), "warn");
          return;
        }
        if (ctx.state.streamState !== "idle") {
          // Defence-in-depth — Composer's gate already caught the
          // streaming case (recover isn't stream-safe), but a future
          // direct-dispatch caller might bypass that. Recovery
          // mutates the cluster, so reject hard.
          pushLog(ctx, t("recover.busy"), "warn");
          return;
        }
        pushLog(ctx, t("recover.starting", { id: taskId }), "info");
        try {
          const env = await ctx.client.recoverTask(taskId);
          pushLog(ctx, formatRecoverResult(taskId, env), "info");
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          pushLog(ctx, t("recover.failed", { id: taskId, err: msg }), "warn");
        }
      },
    },
    {
      // ``/skills <list|path>`` — skill catalog inspection. Mirrors
      // Python's two-level shape (``tui/controllers/commands.py:1202+``).
      // ``list`` enumerates the loaded skills via the same
      // ``GET /api/v1/skills`` endpoint as ``/experiments`` but
      // renders just the category counts so users can see the shape
      // of their catalog without the per-fault detail.
      //
      // ``path`` is intentionally omitted in this round — Python reads
      // server-side ``settings.skills_dir`` to compute it, but the TS
      // TUI has no API for that yet. Adding it is Phase 3 work.
      // Bare ``/skills`` prints usage so users discover ``list``.
      name: "skills",
      description: t("command.skills.desc"),
      group: "skills",
      streamSafe: true,
      subcommands: {
        list: {
          name: "list",
          description: t("command.skills.list.desc"),
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.listSkills();
              pushLog(ctx, formatSkillsList(data), "info");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("skills.list_failed", { err: msg }), "warn");
            }
          },
        },
        show: {
          name: "show",
          description: t("command.skills.show.desc"),
          usage: "<name>",
          streamSafe: true,
          async handler(ctx, args) {
            const name = (args[0] || "").trim();
            if (!name) {
              pushLog(ctx, t("skills.show_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.showSkill(name);
              pushLog(ctx, formatSkillShow(data), "info");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("skills.show_failed", { name, err: msg }), "warn");
            }
          },
        },
        reload: {
          name: "reload",
          description: t("command.skills.reload.desc"),
          // NOT stream-safe: re-scanning the skills directory mid-turn
          // could yank a skill the in-flight ReAct loop is using.
          async handler(ctx) {
            try {
              const data = await ctx.client.reloadSkills();
              pushLog(ctx, formatSkillsReload(data), "ok");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("skills.reload_failed", { err: msg }), "warn");
            }
          },
        },
        install: {
          name: "install",
          description: t("command.skills.install.desc"),
          usage: "<git-url|path>",
          // NOT stream-safe: even though the install never touches
          // the live registry, the follow-up reload (which the user
          // is told to run) IS unsafe — keep the whole sub gated.
          async handler(ctx, args) {
            const source = args.join(" ").trim();
            if (!source) {
              pushLog(ctx, t("skills.install_usage"), "warn");
              return;
            }
            pushLog(ctx, t("skills.install_starting", { source }), "info");
            try {
              const data = await ctx.client.installSkill(source);
              pushLog(ctx, formatSkillsInstall(data), "ok");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(
                ctx,
                t("skills.install_failed", { source, err: msg }),
                "warn",
              );
            }
          },
        },
        enable: {
          name: "enable",
          description: t("command.skills.enable.desc"),
          usage: "<name>",
          // NOT stream-safe — writes to disabled_skills config and
          // triggers settings.reload(); same logic as /config write.
          async handler(ctx, args) {
            const name = (args[0] || "").trim();
            if (!name) {
              pushLog(ctx, t("skills.enable_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.enableSkill(name);
              if (!data["was_disabled"]) {
                pushLog(ctx, t("skills.enable_noop", { name }), "info");
                return;
              }
              pushLog(ctx, t("skills.enable_ok", { name }), "ok");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(
                ctx,
                t("skills.enable_failed", { name, err: msg }),
                "warn",
              );
            }
          },
        },
        disable: {
          name: "disable",
          description: t("command.skills.disable.desc"),
          usage: "<name>",
          async handler(ctx, args) {
            const name = (args[0] || "").trim();
            if (!name) {
              pushLog(ctx, t("skills.disable_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.disableSkill(name);
              if (!data["was_enabled"]) {
                pushLog(ctx, t("skills.disable_noop", { name }), "info");
                return;
              }
              pushLog(ctx, t("skills.disable_ok", { name }), "ok");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(
                ctx,
                t("skills.disable_failed", { name, err: msg }),
                "warn",
              );
            }
          },
        },
        path: {
          name: "path",
          description: t("command.skills.path.desc"),
          // Read-only — server hits ``get_skills_dir()`` and returns
          // resolution metadata. Safe to run mid-stream.
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.getSkillsDir();
              const resolved = (data["resolved"] as string) || "(unknown)";
              const candidates =
                (data["candidates"] as Array<Record<string, unknown>>) ?? [];
              const lines: string[] = [
                t("skills.path_head", { dir: resolved }),
                t("skills.path_candidates_head"),
              ];
              for (const c of candidates) {
                const label = (c["label"] as string) || "?";
                const value = (c["value"] as string) || "—";
                lines.push(`  - ${label}: ${value || "—"}`);
              }
              pushLog(ctx, lines.join("\n"), "info");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("skills.path_failed", { err: msg }), "warn");
            }
          },
        },
      },
      async handler(ctx) {
        // Bare ``/skills`` → usage. Matches Python; we don't assume a
        // default action that might surprise the user.
        pushLog(ctx, t("skills.usage"), "info");
      },
    },
    {
      // ``/config`` — proxy for the server's whitelist-gated config
      // store. Mirrors Python TUI ``_cmd_config_*``
      // (``tui/controllers/commands.py:848+``). The server enforces
      // the writable key set; we just shape arguments and render the
      // response.
      //
      //   /config list                       → GET, render the dict
      //   /config get <key>                  → GET, surface a single key
      //   /config set <key> <value>          → POST, write whitelist gates
      //   /config unset <key>                → DELETE
      //   /config path                       → GET, surface config_path only
      //
      // Read subs are stream-safe; write subs (set / unset) are NOT
      // — they trigger ``settings.reload()`` server-side and a
      // mid-stream reload could yank state from the in-flight turn.
      name: "config",
      description: t("command.config.desc"),
      group: "skills",
      streamSafe: true,
      subcommands: {
        list: {
          name: "list",
          description: t("command.config.list.desc"),
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.getConfig();
              pushLog(ctx, formatConfigList(data), "info");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("config.failed", { err: msg }), "warn");
            }
          },
        },
        get: {
          name: "get",
          description: t("command.config.get.desc"),
          usage: "<key>",
          streamSafe: true,
          async handler(ctx, args) {
            const key = (args[0] || "").trim();
            if (!key) {
              pushLog(ctx, t("config.get_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.getConfig();
              const cfg =
                (data["config"] as Record<string, unknown>) ?? {};
              const val = cfg[key];
              if (val === undefined) {
                pushLog(ctx, t("config.unset", { key }), "info");
                return;
              }
              pushLog(ctx, `${key}: ${String(val)}`, "info");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("config.failed", { err: msg }), "warn");
            }
          },
        },
        set: {
          name: "set",
          description: t("command.config.set.desc"),
          usage: "<key> <value>",
          // NOT stream-safe: write triggers settings.reload() server-side.
          async handler(ctx, args) {
            const key = (args[0] || "").trim();
            // Use the rest of args as the value so spaces survive
            // (e.g. ``/config set api_base_url https://foo/v1``).
            const value = args.slice(1).join(" ").trim();
            if (!key || !value) {
              pushLog(ctx, t("config.set_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.setConfig(key, value);
              const coerced = String(data["value"] ?? value);
              const hot = !!data["hot_reload"];
              const tail = hot ? "" : t("config.set_cold_tail");
              pushLog(
                ctx,
                t("config.set_ok", { key, value: coerced, tail }),
                "ok",
              );
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("config.set_failed", { key, err: msg }), "warn");
            }
          },
        },
        unset: {
          name: "unset",
          description: t("command.config.unset.desc"),
          usage: "<key>",
          // NOT stream-safe — same reason as ``set``.
          async handler(ctx, args) {
            const key = (args[0] || "").trim();
            if (!key) {
              pushLog(ctx, t("config.unset_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.unsetConfig(key);
              if (!data["was_present"]) {
                pushLog(ctx, t("config.unset_noop", { key }), "info");
                return;
              }
              const hot = !!data["hot_reload"];
              const tail = hot ? "" : t("config.set_cold_tail");
              pushLog(
                ctx,
                t("config.unset_ok", { key, tail }),
                "ok",
              );
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(
                ctx,
                t("config.unset_failed", { key, err: msg }),
                "warn",
              );
            }
          },
        },
        path: {
          name: "path",
          description: t("command.config.path.desc"),
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.getConfig();
              const p = (data["config_path"] as string) ?? "(unknown)";
              pushLog(ctx, p, "info");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("config.failed", { err: msg }), "warn");
            }
          },
        },
      },
      async handler(ctx) {
        pushLog(ctx, t("config.usage"), "info");
      },
    },
    {
      // ``/memory`` — TUI session memory inspection / cleanup.
      // Mirrors Python ``_cmd_memory_*`` (``tui/controllers/commands.py:1101+``).
      // Scoped to ``ctx.sessionId`` because that's the session the
      // user is talking through right now. /memory show is stream-safe;
      // /memory clear is NOT (it deletes the on-disk file the active
      // session writes to).
      name: "memory",
      description: t("command.memory.desc"),
      group: "skills",
      streamSafe: true,
      subcommands: {
        show: {
          name: "show",
          description: t("command.memory.show.desc"),
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.getMemoryInfo(ctx.sessionId);
              // Dispatch a bordered MemoryCard rather than a plain
              // log line so the snapshot reads as a persistent
              // diagnostic record in scrollback (parallel to /doctor's
              // RuntimeDoctorCard). Each /memory show invocation
              // pushes its own card with its own ``capturedAt`` so
              // multiple snapshots stack in obvious chronological
              // order.
              const stats: Record<string, number | string> = {};
              const rawStats =
                (data["stats"] as Record<string, unknown>) ?? {};
              for (const [k, v] of Object.entries(rawStats)) {
                if (typeof v === "number" || typeof v === "string") {
                  stats[k] = v;
                }
              }
              ctx.dispatch({
                type: "HISTORY_APPENDED",
                item: {
                  kind: "memory_card",
                  id: `memory-${Date.now()}`,
                  sessionId: (data["tui_session_id"] as string) ?? "",
                  startedAt: (data["started_at"] as string) ?? "",
                  status: (data["status"] as string) ?? "active",
                  cluster: (data["cluster_name"] as string) ?? "",
                  namespace: (data["namespace"] as string) ?? "",
                  recentTasks:
                    (data["task_ids_recent"] as string[] | undefined) ?? [],
                  totalTasks:
                    (data["task_count_total"] as number | undefined) ??
                    ((data["task_ids_recent"] as string[] | undefined)?.length ??
                      0),
                  stats,
                  memoryDir: (data["memory_dir"] as string) ?? "",
                  capturedAt: new Date().toISOString(),
                },
              });
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("memory.show_failed", { err: msg }), "warn");
            }
          },
        },
        clear: {
          name: "clear",
          description: t("command.memory.clear.desc"),
          // NOT stream-safe — deletes the file the live session is
          // updating.
          async handler(ctx) {
            try {
              const data = await ctx.client.clearMemory(ctx.sessionId);
              const cleared = !!data["cleared_session_file"];
              pushLog(
                ctx,
                cleared ? t("memory.clear_ok") : t("memory.clear_noop"),
                cleared ? "ok" : "info",
              );
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("memory.clear_failed", { err: msg }), "warn");
            }
          },
        },
        path: {
          name: "path",
          description: t("command.memory.path.desc"),
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.getMemoryInfo(ctx.sessionId);
              const dir = (data["memory_dir"] as string) ?? "(unknown)";
              pushLog(ctx, dir, "info");
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("memory.show_failed", { err: msg }), "warn");
            }
          },
        },
      },
      async handler(ctx) {
        pushLog(ctx, t("memory.usage"), "info");
      },
    },
    {
      // ``/model`` — switch active LLM. Mirrors Python TUI's
      // intent: list candidates, set the active model. Currently
      // always reports "restart required" because the server
      // captures the LLM at startup; the TS handler reads the
      // server's ``restart_required`` flag explicitly so the UX
      // automatically picks up a future hot-swap path.
      //
      //   /model           → usage
      //   /model list      → list candidates with the active marker
      //   /model set <id>  → write to config + report restart status
      //
      // ``set`` is NOT stream-safe — same reason ``/config set`` and
      // any other settings.reload() trigger isn't: a mid-stream
      // reload could yank state from the in-flight turn.
      name: "model",
      description: t("command.model.desc"),
      group: "skills",
      streamSafe: true,
      subcommands: {
        list: {
          name: "list",
          description: t("command.model.list.desc"),
          streamSafe: true,
          async handler(ctx) {
            try {
              const data = await ctx.client.getModel();
              ctx.dispatch({
                type: "HISTORY_APPENDED",
                item: buildModelCard(data),
              });
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("model.failed", { err: msg }), "warn");
            }
          },
        },
        set: {
          name: "set",
          description: t("command.model.set.desc"),
          usage: "<model-id>",
          // NOT stream-safe — see comment above.
          async handler(ctx, args) {
            const id = (args[0] || "").trim();
            if (!id) {
              pushLog(ctx, t("model.set_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.setModel(id);
              const restart = !!data["restart_required"];
              pushLog(
                ctx,
                t("model.set_ok", {
                  id: (data["active"] as string) || id,
                  tail: restart ? t("model.set_restart_tail") : "",
                }),
                "ok",
              );
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(ctx, t("model.set_failed", { id, err: msg }), "warn");
            }
          },
        },
      },
      async handler(ctx) {
        pushLog(ctx, t("model.usage"), "info");
      },
    },
    {
      // ``/compact`` — force the LangGraph inject thread to compact
      // its message list NOW. Mirrors Python ``_cmd_compact``
      // (``tui/controllers/commands.py:1038``). Server-side runs the
      // unified PreReasoningHook with ``force=True``, the same code
      // path auto-compaction takes.
      //
      // The route streams SSE: ``memory_compaction`` phase events as
      // the hook progresses, then a terminal ``result`` envelope, then
      // ``done``. This gives the user real-time feedback during the
      // multi-second LLM summariser call instead of a silent stall.
      //
      // NOT stream-safe — compaction mutates the checkpoint thread
      // the running turn is reading; doing it mid-stream would corrupt
      // the in-flight ReAct loop. Composer's gate refuses the call.
      name: "compact",
      description: t("command.compact.desc"),
      group: "business",
      async handler(ctx) {
        if (ctx.state.streamState !== "idle") {
          pushLog(ctx, t("compact.busy"), "warn");
          return;
        }
        // Open the client-side spinner slot. The Composer mounts
        // ManualCompactIndicator while ``currentManualCompact !== null``
        // so the user sees a continuous "正在压缩…" row + elapsed
        // timer + esc-to-cancel hint for the entire operation
        // (noop / strip-only / LLM-driven, all uniform).
        ctx.dispatch({ type: "COMPACT_MANUAL_STARTED" });
        // AbortController routed to streamCompactSession's fetch so
        // Esc from the Composer can interrupt mid-call. The Composer
        // wires its Esc binding to ``cancelManualCompact`` whenever
        // the spinner slot is non-null.
        const ctrl = ctx.beginManualCompact();
        let resultPayload: Record<string, unknown> | null = null;
        let sawFailure = false;
        try {
          for await (const evt of ctx.client.streamCompactSession(
            ctx.sessionId,
            undefined,
            ctrl.signal,
          )) {
            if (evt.type === "memory_compaction") {
              // Phase events drive nothing visible now — the spinner
              // is already up from COMPACT_MANUAL_STARTED above and
              // stays up until the slot closes. Only failure needs
              // surfacing here so the user sees a real error reason
              // instead of just "failed".
              if (evt.compaction_phase === "failed") {
                sawFailure = true;
                pushLog(
                  ctx,
                  t("compact.failed", { err: evt.content || "hook failure" }),
                  "warn",
                );
              }
              // Intentionally NO log on "started" or "completed" —
              // they're noise compared to the running spinner +
              // final result line.
            } else if (evt.type === "result") {
              resultPayload = (evt.payload ?? {}) as Record<string, unknown>;
            } else if (evt.type === "error") {
              sawFailure = true;
              pushLog(
                ctx,
                t("compact.failed", { err: evt.content || "stream error" }),
                "warn",
              );
            }
            // ``done`` is the stream terminator — streamCompactSession's
            // generator returns on it, so we don't need to handle it
            // here. Any other event type is ignored (forward-compat).
          }
        } catch (err) {
          // Distinguish user-initiated Esc cancel from genuine errors:
          // an AbortError when ctrl.signal aborted is the user's
          // explicit cancel, surface it as a friendly "cancelled" line
          // instead of a generic "compaction failed: AbortError".
          const isAbort =
            err instanceof Error &&
            (err.name === "AbortError" || ctrl.signal.aborted);
          if (isAbort) {
            pushLog(ctx, t("compact.cancelled"), "warn");
          } else {
            const msg = err instanceof Error ? err.message : String(err);
            pushLog(ctx, t("compact.failed", { err: msg }), "warn");
          }
          return;
        } finally {
          // Always close the spinner slot — success, failure, or
          // cancel. Without the finally, an abort would leak the
          // indicator on screen indefinitely.
          ctx.dispatch({ type: "COMPACT_MANUAL_DONE" });
        }
        if (resultPayload && !sawFailure) {
          pushLog(ctx, formatCompactResult(resultPayload), "ok");
        }
      },
    },
    {
      // ``/recordings`` — list or export task recording tapes.
      //
      //   /recordings                  → list (default sub when no arg)
      //   /recordings list             → same as above (explicit)
      //   /recordings export <id> <p>  → fetch the tape from server,
      //                                   serialise as JSONL, write to <p>
      //
      // Mirror of Python ``_cmd_recordings`` (``tui/controllers/
      // commands.py:735``). The Phase-3 finishing-touch ``export``
      // sub closes the parity gap with Python TUI's recordings
      // family — purely client-side: server already exposes the
      // raw events via ``getRecording`` and Node's ``fs`` does the
      // local write, so no new server endpoint is needed.
      name: "recordings",
      description: t("command.recordings.desc"),
      group: "business",
      streamSafe: true,
      subcommands: {
        list: {
          name: "list",
          description: t("command.recordings.list.desc"),
          streamSafe: true,
          async handler(ctx) {
            await listRecordingsHandler(ctx);
          },
        },
        export: {
          name: "export",
          description: t("command.recordings.export.desc"),
          usage: "<task_id> <out_path>",
          // Read on the server side, write to local FS — read-only
          // wrt the server. Stream-safe so the user can capture a
          // tape while another turn is running without waiting.
          streamSafe: true,
          async handler(ctx, args) {
            const taskId = (args[0] || "").trim();
            const outPath = (args.slice(1).join(" ") || "").trim();
            if (!taskId || !outPath) {
              pushLog(ctx, t("recordings.export_usage"), "warn");
              return;
            }
            try {
              const data = await ctx.client.getRecording(taskId);
              const events =
                (data["events"] as Array<Record<string, unknown>>) ?? [];
              if (events.length === 0) {
                pushLog(
                  ctx,
                  t("recordings.export_empty", { id: taskId }),
                  "warn",
                );
                return;
              }
              // Lazy-import fs only inside the handler — keeps the
              // module tree React-renderer-clean and avoids dragging
              // ``node:fs`` into any future browser-target build.
              const { writeFileSync, existsSync, mkdirSync } = await import(
                "node:fs"
              );
              const { resolve, dirname } = await import("node:path");
              const expanded = outPath.startsWith("~")
                ? `${process.env.HOME ?? ""}${outPath.slice(1)}`
                : outPath;
              const abs = resolve(expanded);
              if (existsSync(abs)) {
                pushLog(
                  ctx,
                  t("recordings.export_exists", { path: abs }),
                  "warn",
                );
                return;
              }
              const parent = dirname(abs);
              if (!existsSync(parent)) {
                mkdirSync(parent, { recursive: true });
              }
              const jsonl =
                events.map((e) => JSON.stringify(e)).join("\n") + "\n";
              writeFileSync(abs, jsonl, "utf-8");
              const bytes = Buffer.byteLength(jsonl, "utf-8");
              pushLog(
                ctx,
                t("recordings.export_ok", {
                  bytes,
                  path: abs,
                  events: events.length,
                }),
                "ok",
              );
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              pushLog(
                ctx,
                t("recordings.export_failed", { id: taskId, err: msg }),
                "warn",
              );
            }
          },
        },
      },
      async handler(ctx) {
        // Bare ``/recordings`` → list (matches Python's "default sub
        // is list" semantics so users keep their muscle memory).
        await listRecordingsHandler(ctx);
      },
    },
    {
      name: "replay",
      description: t("command.replay.desc"),
      group: "business",
      usage: "<task_id> [speed|instant]",
      // Replay drives its own AbortController + dispatches REPLAY_STARTED;
      // safe to fire while another stream is in flight only if we don't
      // mix events. The Composer guard treats /replay as not-stream-safe
      // (default false here), forcing the user to wait for the active
      // turn to settle first.
      async handler(ctx, args) {
        const taskId = args[0];
        if (!taskId) {
          pushLog(ctx, t("replay.usage"), "warn");
          return;
        }
        const speedArg = (args[1] ?? "").toLowerCase();
        let speed = 4;
        if (speedArg === "instant" || speedArg === "0") {
          speed = Infinity;
        } else if (speedArg) {
          const n = parseFloat(speedArg);
          if (Number.isFinite(n) && n > 0) speed = n;
        }
        try {
          const data = await ctx.client.getRecording(taskId);
          const events =
            (data["events"] as Array<Record<string, unknown>>) ?? [];
          if (events.length === 0) {
            pushLog(ctx, t("replay.empty", { id: taskId }), "warn");
            return;
          }
          const speedLabel =
            !Number.isFinite(speed) || speed <= 0 ? "instant" : `${speed}x`;
          pushLog(
            ctx,
            t("replay.starting", {
              id: taskId,
              n: events.length,
              speed: speedLabel,
            }),
            "info",
          );
          ctx.dispatch({ type: "REPLAY_STARTED", taskId });
          // Phase 2.1 — clear the streaming token counter so the
          // LoadingIndicator's live "~N tokens" estimate restarts
          // from 0 for the replay (rather than displaying the
          // residual count left by the prior real turn). The replay
          // recorder does NOT route through useStream's token
          // handler, so the ref would otherwise stay at its prior
          // value for the entire replay duration.
          resetStreamingCounters();
          const controller = ctx.beginReplay();
          const stats = await replayRecording(events, ctx.dispatch, taskId, {
            speed,
            signal: controller.signal,
          });
          ctx.dispatch({ type: "REPLAY_ENDED", aborted: stats.aborted });
          const tail = stats.aborted ? t("replay.aborted_tail") : "";
          pushLog(
            ctx,
            t("replay.done", {
              converted: stats.converted,
              skipped: stats.skipped,
              duration: formatMs(stats.elapsedMs),
              tail,
            }),
            stats.aborted ? "warn" : "ok",
          );
        } catch (err) {
          ctx.dispatch({ type: "REPLAY_ENDED", aborted: true });
          const msg = err instanceof Error ? err.message : String(err);
          pushLog(ctx, t("replay.failed", { id: taskId, err: msg }), "warn");
        }
      },
    },
  ];
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m${s}s`;
}

// ── /tasks filter helpers ────────────────────────────────────────────
//
// Mirrors of ``renderers/tasks_table.py`` — kept as plain Sets so the
// gating logic in the handler reads as a direct port of the Python
// version. Resists drift if either side adds a new state name later
// (the test suite locks the literal sets in place).

const TASKS_FILTERS = new Set(["active", "failed", "all"] as const);
const TASKS_ACTIVE_PHASES = new Set([
  "planning",
  "executing",
  "verifying",
  "dry_run_planned",
]);
const TASKS_FAILED_STATUSES = new Set(["failed", "error"]);

export type TasksFilter = "active" | "failed" | "all";

/** Parse the args slot of ``/tasks [active|failed|all] [N]``. Either
 *  arg may appear in either order; unknown tokens are silently
 *  ignored (Python does the same). Defaults: filter ``all``, limit
 *  10. Exported for unit tests. */
export function parseTasksArgs(args: string[]): {
  filter: TasksFilter;
  limit: number;
} {
  let filter: TasksFilter = "all";
  let limit = 10;
  for (const arg of args) {
    const lower = arg.toLowerCase();
    if (TASKS_FILTERS.has(lower as TasksFilter)) {
      filter = lower as TasksFilter;
      continue;
    }
    const n = parseInt(arg, 10);
    if (Number.isFinite(n) && n > 0) {
      limit = n;
    }
  }
  return { filter, limit };
}

/** Direct port of ``_passes_filter`` from ``renderers/tasks_table.py:58``.
 *  Active = mid-pipeline phase + non-failed status; failed = error/failed
 *  status; all = everything. Exported for unit tests.
 *
 *  ``phase`` is intentionally NOT lower-cased — Python compares against
 *  the lowercase literals in ``_ACTIVE_PHASES`` directly, so a
 *  capitalised phase from a misbehaving server would fail to match
 *  there too. Mirroring that strictness keeps the two TUIs reporting
 *  the same row count on the same data. ``status`` IS lowercased
 *  because Python explicitly does ``status.lower()`` before the set
 *  membership check. */
export function passesTasksFilter(
  task: Record<string, unknown>,
  flt: TasksFilter,
): boolean {
  if (flt === "all") return true;
  const phase = (task["phase"] as string) || "";
  const status = ((task["status"] as string) || "").toLowerCase();
  if (flt === "active") {
    return TASKS_ACTIVE_PHASES.has(phase) && !TASKS_FAILED_STATUSES.has(status);
  }
  if (flt === "failed") {
    return TASKS_FAILED_STATUSES.has(status);
  }
  return true;
}

/** Build a fault-type label out of a metric row. Prefers the
 *  joined ``scope-target-action`` form (matches Python's tasks_table)
 *  and falls back to ``skill_name`` / ``fault_type`` literal. */
function formatFaultType(row: Record<string, unknown>): string {
  const params = row["params"];
  if (params && typeof params === "object") {
    const p = params as Record<string, unknown>;
    const scope = (p["scope"] as string) || "";
    const target = (p["target"] as string) || "";
    const action = (p["action"] as string) || "";
    const joined = [scope, target, action].filter(Boolean).join("-");
    if (joined) return joined;
  }
  return (row["skill_name"] as string) || (row["fault_type"] as string) || "";
}

// ── /review · /experiments · /recover · /skills formatters ──────────
//
// Each formatter is a pure ``data → string`` transform so the handler
// stays a thin coordinator. Kept compact — no rich-table parity yet,
// just legible plain text the LogItem renderer can show as-is.

/** Format the metric envelope returned by ``/api/v1/metric/{id}`` into
 *  a multi-line review card. Echoes the Python ``review_panel`` shape
 *  (header + key fields + verification hint) but stays in plain text
 *  so it slots into the existing LogItem flow. */
function formatReviewCard(
  taskId: string,
  data: Record<string, unknown>,
): string {
  const status = ((data["status"] as string) || "?").toLowerCase();
  const phase = (data["phase"] as string) || "—";
  const fault = formatFaultType(data) || t("common.unknown");
  const created = (data["gmt_create"] as string) ||
    (data["created_at"] as string) ||
    "";
  const createdShort = created.replace("T", " ").slice(0, 19);
  const summary = (data["summary"] as Record<string, unknown>) || {};
  const durationMs = (summary["total_duration_ms"] as number) || 0;
  const blade = (data["blade_uid"] as string) || "";
  const lines = [
    t("review.head", { id: taskId }),
    `  ${stateGlyph(status)} ${t("review.status_label")}: ${status}`,
    `  ${t("review.fault_label")}: ${fault}`,
    `  ${t("review.phase_label")}: ${phase}`,
  ];
  if (blade) lines.push(`  ${t("review.uid_label")}: ${blade}`);
  if (durationMs > 0) {
    lines.push(`  ${t("review.duration_label")}: ${formatMs(durationMs)}`);
  }
  if (createdShort) {
    lines.push(`  ${t("review.created_label")}: ${createdShort}`);
  }
  const result = data["result"];
  if (typeof result === "string" && result) {
    lines.push("");
    lines.push(`  ${result.split("\n")[0]}`);
  }
  return lines.join("\n");
}

/** Build an ExperimentsCardItem snapshot from the ``/api/v1/skills``
 *  envelope. Pure: the renderer (``ExperimentsCard.tsx``) takes the
 *  assembled rows and presents them with no API coupling, so a
 *  ``/experiments`` fired while the skill registry is still loading
 *  captures what the user actually saw — not what the registry
 *  mutates to a few frames later.
 *
 *  Display rules:
 *    - Categories are flattened to a single row sequence in the
 *      server's iteration order. Skill packs are typically 1:1
 *      (one use case per category), so nesting would add chrome
 *      around a single row.
 *    - Fault entries that lack ``use_case_name`` fall back through
 *      ``fault_type`` → ``name`` → ``"?"`` so a partial-data row
 *      still renders something rather than a blank line.
 *    - Empty ``fault_symptom`` is preserved as ``""`` — the
 *      renderer substitutes a localised placeholder so the symptom
 *      column always carries text.
 */
function buildExperimentsCard(
  data: Record<string, unknown>,
): ExperimentsCardItem {
  const totalRaw = data["total"];
  const total = typeof totalRaw === "number" ? totalRaw : 0;
  const categories =
    (data["categories"] as Array<Record<string, unknown>>) ?? [];
  const rows: ExperimentsCardRow[] = [];
  for (const cat of categories) {
    const faults =
      (cat["faults"] as Array<Record<string, unknown>>) ?? [];
    for (const fault of faults) {
      const useCase =
        (fault["use_case_name"] as string) ||
        (fault["fault_type"] as string) ||
        (fault["name"] as string) ||
        "?";
      const symptom = (fault["fault_symptom"] as string) || "";
      rows.push({ useCaseName: useCase, faultSymptom: symptom });
    }
  }
  return {
    kind: "experiments_card",
    id: `experiments-${Date.now()}`,
    capturedAt: new Date().toISOString(),
    totalCount: total || rows.length,
    rows,
  };
}

/** Format the recover result envelope. The recover endpoint returns
 *  ``status: "success"`` on recovery, ``status: "fail"`` with a
 *  populated ``data`` block on verification failure. We surface
 *  ``error`` / ``recovery_level`` so the user sees why instead of a
 *  bare "failed". */
function formatRecoverResult(
  taskId: string,
  env: Record<string, unknown>,
): string {
  const status = (env["status"] as string) || "";
  const data = (env["data"] as Record<string, unknown>) || {};
  const result = (data["result"] as string) || "";
  const blade = (data["blade_uid"] as string) || "";
  const targets = (data["targets"] as Array<Record<string, unknown>>) || [];
  const targetSummary = targets
    .map((tg) => {
      const name = (tg["name"] as string) || "?";
      const ns = (tg["namespace"] as string) || "";
      return ns ? `${name}@${ns}` : name;
    })
    .join(", ");
  if (status === "success") {
    const lines = [
      t("recover.success_head", { id: taskId, level: result || "recovered" }),
    ];
    if (blade) lines.push(`  ${t("review.uid_label")}: ${blade}`);
    if (targetSummary) lines.push(`  ${t("recover.targets_label")}: ${targetSummary}`);
    return lines.join("\n");
  }
  const errMsg =
    (data["error"] as string) ||
    (env["message"] as string) ||
    t("recover.unknown_error");
  const lines = [t("recover.fail_head", { id: taskId })];
  if (blade) lines.push(`  ${t("review.uid_label")}: ${blade}`);
  if (targetSummary) lines.push(`  ${t("recover.targets_label")}: ${targetSummary}`);
  lines.push(`  ${t("recover.error_label")}: ${errMsg}`);
  return lines.join("\n");
}

/** Render the masked config dict + path for ``/config list``.
 *  Mirrors Python TUI's ``_cmd_config_list`` output ordering — a
 *  ``key: value`` line per entry, sorted by key for stability. */
function formatConfigList(data: Record<string, unknown>): string {
  const cfg = (data["config"] as Record<string, unknown>) ?? {};
  const path = (data["config_path"] as string) ?? "";
  const lines: string[] = [t("config.head")];
  const keys = Object.keys(cfg).sort();
  for (const k of keys) {
    lines.push(`  ${k}: ${String(cfg[k] ?? "")}`);
  }
  if (path) {
    lines.push("");
    lines.push(t("config.path_tail", { path }));
  }
  return lines.join("\n");
}

// ``formatMemorySnapshot`` was removed when /memory show migrated to
// the bordered ``MemoryCard`` (see commands.ts handler). The old
// ``memory.head`` / ``memory.cluster_label`` etc. i18n keys remain
// in en.ts/zh.ts as legacy entries — keep them around so a future
// downgrade or alternative renderer can read them; they're unused
// at the call site now.

/** Render the compact result envelope. Two cases:
 *  - ``compacted: false`` — nothing to do (under budget or empty).
 *  - ``compacted: true``  — show before / after / saved + the layer.
 */
function formatCompactResult(data: Record<string, unknown>): string {
  const before = Number(data["tokens_before"] ?? 0);
  const after = Number(data["tokens_after"] ?? 0);
  const saved = Number(data["tokens_saved"] ?? 0);
  const compacted = !!data["compacted"];
  const layer = (data["layer"] as string) ?? "noop";
  if (!compacted) {
    return t("compact.noop", { before, layer });
  }
  const pct = before > 0 ? Math.floor((saved * 100) / before) : 0;
  return t("compact.ok", { before, after, saved, pct, layer });
}

/** Build a ModelCardItem snapshot from the ``/api/v1/model``
 *  envelope. Pure: the renderer (``ModelCard.tsx``) takes the
 *  assembled sections and presents them with no API coupling, so
 *  ``/model list`` snapshots stay static after burn-in (a later
 *  ``/model set`` doesn't mutate a card already in scrollback).
 *
 *  Display rules:
 *    - Providers cluster in the order they first appear in the
 *      ``candidates`` array (stable; the server's curated list is
 *      already grouped meaningfully).
 *    - Active model lives in its own section row, marked
 *      ``active=true`` — renderer paints ● + bold + forge.fire.
 *    - If the active model id isn't in any curated provider list
 *      (user pointed at a private model), a synthetic ``custom``
 *      section is appended carrying just that id + a localised
 *      "not in curated list" note. Without this the user would
 *      otherwise see no marker for their running model at all.
 */
function buildModelCard(data: Record<string, unknown>): ModelCardItem {
  const active = (data["active"] as string) || "";
  const baseUrl = (data["api_base_url"] as string) || "";
  const candidates =
    (data["candidates"] as Array<Record<string, unknown>>) ?? [];

  // Group by provider, preserving first-occurrence order so the
  // visual clustering follows whatever the server already curated.
  const seen: string[] = [];
  const byProvider = new Map<string, ModelCardRow[]>();
  let activeFound = false;
  for (const cand of candidates) {
    const provider = (cand["provider"] as string) || "other";
    const id = (cand["id"] as string) || "?";
    const note = (cand["note"] as string) || "";
    const isActive = id === active && active.length > 0;
    if (isActive) activeFound = true;
    if (!byProvider.has(provider)) {
      byProvider.set(provider, []);
      seen.push(provider);
    }
    byProvider.get(provider)!.push({
      id,
      active: isActive,
      ...(note ? { note: `— ${note}` } : {}),
    });
  }
  const sections: ModelCardSection[] = seen.map((provider) => ({
    provider,
    rows: byProvider.get(provider) ?? [],
  }));

  // Custom model: active id wasn't anywhere in the curated list.
  // Surface a single-row synthetic section so the user still sees
  // the running model marked active.
  let totalCount = candidates.length;
  if (active && !activeFound) {
    sections.push({
      provider: t("model.card.custom_section"),
      rows: [
        {
          id: active,
          active: true,
          note: t("model.card.custom_note"),
        },
      ],
    });
    totalCount += 1;
  }

  return {
    kind: "model_card",
    id: `model-${Date.now()}`,
    capturedAt: new Date().toISOString(),
    activeModel: active,
    apiBaseUrl: baseUrl,
    totalCount,
    sections,
  };
}

/** Render the `/skills show <name>` detail card. Surfaces metadata
 *  (version / category / target / required_tools / tags / scripts)
 *  + the SKILL.md instructions header so the user gets a quick
 *  glance without round-tripping to the filesystem. */
function formatSkillShow(data: Record<string, unknown>): string {
  const name = (data["name"] as string) || "?";
  const meta = (data["metadata"] as Record<string, unknown>) || {};
  const dir = (data["skill_dir"] as string) || "";
  const instructions = (data["instructions"] as string) || "";

  const lines: string[] = [t("skills.show_head", { name })];
  const desc = (meta["description"] as string) || "";
  if (desc) lines.push(`  ${desc}`);
  lines.push("");

  const category = (meta["category"] as string) || "";
  const target = (meta["target"] as string) || "";
  const version = (meta["version"] as string) || "";
  if (category) lines.push(`  category: ${category}`);
  if (target) lines.push(`  target:   ${target}`);
  if (version) lines.push(`  version:  ${version}`);

  const tools = (meta["required_tools"] as string[]) ?? [];
  if (tools.length > 0) {
    lines.push(`  required_tools: ${tools.join(", ")}`);
  }
  const tags = (meta["tags"] as string[]) ?? [];
  if (tags.length > 0) {
    lines.push(`  tags: ${tags.join(", ")}`);
  }
  const scripts = (meta["scripts"] as Array<Record<string, unknown>>) ?? [];
  if (scripts.length > 0) {
    lines.push("");
    lines.push(t("skills.show_scripts_head", { n: scripts.length }));
    for (const s of scripts.slice(0, 8)) {
      const sn = (s["name"] as string) || "?";
      const sd = (s["description"] as string) || "";
      lines.push(`  • ${sn}${sd ? ` — ${sd}` : ""}`);
    }
    if (scripts.length > 8) {
      lines.push(`  … +${scripts.length - 8}`);
    }
  }
  if (dir) {
    lines.push("");
    lines.push(`  dir: ${dir}`);
  }
  // Instructions can be huge (full SKILL.md). Cap to the first
  // ~200 chars + line so the chat log stays readable; users who
  // want the full body can ``cat`` the dir.
  const head = instructions.split("\n").slice(0, 1).join("");
  if (head) {
    lines.push("");
    const trimmed = head.length > 200 ? `${head.slice(0, 199)}…` : head;
    lines.push(trimmed);
  }
  return lines.join("\n");
}

/** Render the diff envelope returned by ``/skills reload``. */
function formatSkillsReload(data: Record<string, unknown>): string {
  const total = (data["total"] as number) ?? 0;
  const dir = (data["skills_dir"] as string) || "";
  const added = (data["added"] as string[]) ?? [];
  const removed = (data["removed"] as string[]) ?? [];
  const lines: string[] = [t("skills.reload_head", { dir, total })];
  if (added.length === 0 && removed.length === 0) {
    lines.push(t("skills.reload_no_change"));
  } else {
    if (added.length > 0) {
      lines.push(t("skills.reload_added", { items: added.join(", ") }));
    }
    if (removed.length > 0) {
      lines.push(t("skills.reload_removed", { items: removed.join(", ") }));
    }
  }
  return lines.join("\n");
}

/** Render the install envelope's ``installed`` rows + the next-action
 *  hint the server suggests. */
function formatSkillsInstall(data: Record<string, unknown>): string {
  const installed = (data["installed"] as Array<Record<string, unknown>>) ?? [];
  if (installed.length === 0) {
    return t("skills.install_none");
  }
  const lines: string[] = [t("skills.install_head", { n: installed.length })];
  for (const sk of installed) {
    const n = (sk["name"] as string) || "?";
    const d = (sk["target_dir"] as string) || "";
    const sha = (sk["skill_md_sha256"] as string) || "";
    const shaShort = sha ? sha.slice(0, 16) + "…" : "";
    lines.push(`  • ${n}  →  ${d}`);
    if (shaShort) {
      lines.push(`    SHA256(SKILL.md): ${shaShort}`);
    }
  }
  lines.push("");
  lines.push(t("skills.install_next"));
  return lines.join("\n");
}

/** Compact category listing for ``/skills list`` — one line per
 *  category with a fault count. Less verbose than ``/experiments``
 *  on purpose; users who want per-fault detail run that command
 *  instead. */
function formatSkillsList(data: Record<string, unknown>): string {
  const categories = (data["categories"] as Array<Record<string, unknown>>) ?? [];
  if (categories.length === 0) {
    return t("skills.list_empty");
  }
  const total = (data["total"] as number) ?? 0;
  const lines: string[] = [t("skills.list_head", { n: categories.length, total })];
  for (const cat of categories) {
    const name = (cat["category"] as string) || "?";
    const faults = (cat["faults"] as Array<Record<string, unknown>>) ?? [];
    const desc = (cat["description"] as string) || "";
    lines.push(`  • ${name}  ·  ${faults.length}  ${desc && desc !== name ? `— ${desc}` : ""}`.trimEnd());
  }
  lines.push("");
  lines.push(t("skills.list_tail"));
  return lines.join("\n");
}

// ── helpers ──────────────────────────────────────────────────────────

/** Build a HelpCardItem snapshot from the registry. Pure: the renderer
 *  (``HelpCard.tsx``) takes the assembled sections and presents them
 *  with no registry coupling, so a /help fired while skill commands
 *  are still loading captures what the user actually saw — not what
 *  the registry mutates to a few frames later.
 *
 *  Display rules:
 *    - Groups follow ``SLASH_GROUP_ORDER``; empty groups are dropped
 *      (no dangling section heading over zero rows).
 *    - Hidden commands are excluded — they remain callable, just not
 *      advertised here.
 *    - Aliases collapse into the name with a ``·`` separator
 *      (``/exit · /quit``) rather than the legacy ``(/quit)`` tail,
 *      tighter and matches the v3 chip vocabulary.
 *    - Subcommands sort alphabetically; the parent's ``/cmd`` prefix
 *      is omitted on the sub row because the indent + visual grouping
 *      already disambiguates (and the prefix would just shove the
 *      description column further right).
 */
function buildHelpCard(registry: SlashCommandRegistry): HelpCardItem {
  const groupLabels: Record<SlashGroup, string> = {
    general: t("help.group.general"),
    business: t("help.group.business"),
    skills: t("help.group.skills"),
    dynamic: t("help.group.dynamic"),
  };
  const grouped = registry.listByGroup();
  const sections: HelpCardSection[] = [];
  for (const group of SLASH_GROUP_ORDER) {
    const cmds = grouped[group];
    if (!cmds || cmds.length === 0) continue;
    const rows: HelpCardRow[] = [];
    for (const c of cmds) {
      const aliasTail =
        c.aliases && c.aliases.length > 0
          ? ` · ${c.aliases.map((a) => `/${a}`).join(" · ")}`
          : "";
      const usage = c.usage ? ` ${c.usage}` : "";
      rows.push({
        kind: "top",
        name: `/${c.name}${usage}${aliasTail}`,
        description: c.description,
      });
      if (c.subcommands) {
        for (const sub of Object.values(c.subcommands).sort((a, b) =>
          a.name.localeCompare(b.name),
        )) {
          const subUsage = sub.usage ? ` ${sub.usage}` : "";
          rows.push({
            kind: "sub",
            name: `${sub.name}${subUsage}`,
            description: sub.description,
          });
        }
      }
    }
    sections.push({ heading: groupLabels[group], rows });
  }
  return {
    kind: "help_card",
    id: `help-${Date.now()}`,
    capturedAt: new Date().toISOString(),
    sections,
    tip: t("help.card.tip"),
  };
}

function stateGlyph(state: string): string {
  switch (state) {
    case "injected":
    case "recovered":
    case "completed":
      return "✓";
    case "failed":
    case "rolled_back":
      return "✗";
    case "cancelled":
    case "interrupted":
      return "⊘";
    default:
      return "·";
  }
}

function truncate(s: string, n: number): string {
  if (!s) return "—";
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}

function formatTasksTable(tasks: Array<Record<string, unknown>>): string[] {
  const parsed = tasks.map((row) => {
    const id = (row["task_id"] as string) ?? "?";
    const status =
      (row["status"] as string) || (row["task_state"] as string) || "?";
    const fault = formatFaultType(row) || t("common.unknown");
    const phase = (row["phase"] as string) || "—";
    const summary = (row["summary"] as Record<string, unknown>) || {};
    const durationMs = (summary["total_duration_ms"] as number) || 0;
    const duration = durationMs > 0 ? formatMs(durationMs) : "—";
    const created =
      (row["gmt_create"] as string) || (row["created_at"] as string) || "";
    const createdShort = created.replace("T", " ").slice(0, 19) || "—";
    return { id, status, fault, phase, duration, createdShort };
  });

  const col = {
    st: Math.max(6, ...parsed.map((r) => `${stateGlyph(r.status)} ${r.status}`.length)) + 1,
    id: Math.max(7, ...parsed.map((r) => truncate(r.id, 22).length)) + 1,
    ft: Math.max(10, ...parsed.map((r) => truncate(r.fault, 22).length)) + 1,
    ph: Math.max(5, ...parsed.map((r) => truncate(r.phase, 14).length)) + 1,
    du: Math.max(8, ...parsed.map((r) => r.duration.length)) + 1,
  };

  const hdr =
    `  ${"STATUS".padEnd(col.st)}` +
    `${"TASK ID".padEnd(col.id)}` +
    `${"FAULT TYPE".padEnd(col.ft)}` +
    `${"PHASE".padEnd(col.ph)}` +
    `${"DURATION".padStart(col.du)}` +
    `  CREATED`;
  const sep =
    `  ${"─".repeat(col.st - 1)} ` +
    `${"─".repeat(col.id - 1)} ` +
    `${"─".repeat(col.ft - 1)} ` +
    `${"─".repeat(col.ph - 1)} ` +
    `${"─".repeat(col.du - 1)} ` +
    `${"─".repeat(19)}`;

  const lines = [hdr, sep];
  for (const r of parsed) {
    const stCol = `${stateGlyph(r.status)} ${r.status}`.padEnd(col.st);
    const idCol = truncate(r.id, 22).padEnd(col.id);
    const ftCol = truncate(r.fault, 22).padEnd(col.ft);
    const phCol = truncate(r.phase, 14).padEnd(col.ph);
    const duCol = r.duration.padStart(col.du);
    lines.push(`  ${stCol}${idCol}${ftCol}${phCol}${duCol}  ${r.createdShort}`);
  }
  return lines;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
