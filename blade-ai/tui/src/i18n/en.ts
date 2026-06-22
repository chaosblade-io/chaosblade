/**
 * English message dictionary. Keys use dot-separated namespaces:
 *   thinking.*    — loading-phrase pool (rotated 15s)
 *   error.<key>.* — actionable-error label + suggestions
 *   command.*     — slash command descriptions
 *   header.*      — Header chrome
 *   input.*       — InputPrompt placeholder, hints
 *   loading.*     — LoadingIndicator chrome
 *   result.*      — ResultCard labels + status text
 *   confirm.*     — ConfirmMessage chrome + actions
 *   slash.*       — SlashMenu hint row
 *   replay.*      — /replay command status / messages
 *   tasks.*       — /tasks command headings
 *   recordings.*  — /recordings command headings
 *   mode.*        — /mode command output
 *   status.*      — /status command labels
 *   common.*      — shared chrome (none / unknown / etc.)
 */

import type { Dict } from "./index.js";

export const en: Dict = {
  // -- thinking phrase pool -----------------------------------------
  // Domain-coloured phrases for blade-ai. The cycler shows one of
  // these on the LoadingIndicator while there is no more specific
  // label in flight. Each phrase maps to something the agent
  // plausibly does over the inject pipeline (intent → safety →
  // baseline → execute → verify → recover), so the rotation reads
  // like the agent narrating its own work — not like a generic
  // "thinking…" placeholder.
  "thinking.phrases": [
    "thinking",
    "evaluating blast radius",
    "checking safety constraints",
    "reviewing target health",
    "consulting skill playbooks",
    "selecting injection vector",
    "capturing baseline metrics",
    "observing system response",
    "weighing rollback paths",
    "drafting fault plan",
  ],

  // -- generic LoadingIndicator chrome ------------------------------
  "loading.esc_to_cancel": "esc to cancel",
  "loading.thinking_label": "thinking",
  "loading.responding_label": "responding",
  "loading.tokens_estimate": "~{n} tokens",

  // -- Overflow / dynamic-frame height constraint -------------------
  "overflow.more_lines": "({count} lines folded · Ctrl+O to expand)",
  "overflow.show_more_hint": "Press Ctrl+O to expand folded content",

  // -- ThinkingMessage (collapsed thinking session row) -------------
  "thinking.collapsed": "Thought for {duration}",

  // -- TurnUsageMessage (per-turn token total appended at TURN_DONE)
  "turn.usage": "turn used {total} tokens (input {input} tokens, output {output} tokens)",

  // -- error labels (actionable hints) ------------------------------
  "error.init_failed.label": "INIT FAILED",
  "error.init_failed.suggestions": [
    "/status — confirm the session is healthy",
    "restart blade-ai (Ctrl+C then re-launch) if the server failed to come up",
  ],

  "error.cluster_unreachable.label": "CLUSTER UNREACHABLE",
  "error.cluster_unreachable.suggestions": [
    "verify ``kubectl get ns`` works in another terminal",
    "/status — see the cluster + namespace currently used",
    "/tasks — check whether any in-flight task needs recovery first",
  ],

  "error.stream_error.label": "STREAM ERROR",
  "error.stream_error.suggestions": [
    "/retry — resubmit the last message (stream drops are usually transient)",
    "/clear — drop the current scrollback if state feels stale",
  ],

  "error.conversation_error.label": "CONVERSATION ERROR",
  "error.conversation_error.suggestions": [
    "/clear — wipe scrollback and restart the dialogue",
    "/status — confirm session metadata",
  ],

  "error.replay_failed.label": "REPLAY FAILED",
  "error.replay_failed.suggestions": [
    "/recordings — list recordings actually present on disk",
    "/replay <task_id> — confirm the task_id is exact",
  ],

  "error.command_failed.label": "COMMAND FAILED",
  "error.command_failed.suggestions": ["/help — list available commands"],

  "error.session_expired.label": "SESSION EXPIRED",
  "error.session_expired.suggestions": [
    "restart blade-ai — the server lost the session (likely a server restart)",
  ],

  // -- error message generic chrome ---------------------------------
  "error.next_label": "next:",

  // -- slash command descriptions -----------------------------------
  "command.help.desc": "List available commands",
  "command.clear.desc": "Clear the terminal scrollback for this session",
  "command.exit.desc": "Exit blade-ai",
  "command.mode.desc": "Set display density — pick one of the subcommands below",
  "command.mode.calm.desc": "calm — minimal density, key signals only",
  "command.mode.working.desc": "working — default density, full tool output",
  "command.mode.dense.desc": "dense — high density, every diagnostic field",
  "command.permission.desc": "Set permission mode — pick one of the subcommands below",
  "command.permission.auto.desc": "auto — agent injects without asking; effective on the next /turn",
  "command.permission.confirm.desc": "confirm — show the ARMED/ABORTED gate before each injection (default)",
  "command.session.desc": "Show session info (cluster / namespace / model / mode)",
  "command.run.desc": "Submit a natural-language turn (same as typing without slash)",
  "command.plan.desc": "Fault-injection preview (Dry-Run): intent + plan + safety_check; non-fault chat falls through to /run semantics",
  "plan.usage": "usage: /plan <fault description> — e.g. /plan inject cpu fault into node-1",
  // Shown when /run is invoked with no NL body.
  "run.usage": "usage: /run <natural-language description>",
  // -- /show /copy /rerun /expand (locator command family) ----------
  "command.show.desc": "Show a locator snapshot: /show E1 | T3",
  "command.copy.desc": "Print a locator as a copyable text block: /copy E1 | T3",
  "command.rerun.desc": "Surface an experiment's original prompt for re-issue: /rerun E1",
  "command.expand.desc": "Expand a tool call's full output: /expand T1 (also accepts /expand 1)",
  "locator.usage_show": "usage: /show <E#|T#>, e.g. /show E1",
  "locator.usage_copy": "usage: /copy <E#|T#>, e.g. /copy T3",
  "locator.usage_rerun": "usage: /rerun <E#>, e.g. /rerun E1",
  "locator.usage_expand": "usage: /expand <T#>, e.g. /expand T1",
  "locator.not_found": "no locator '{loc}' — /show only sees [E#]/[T#] from this session.",
  "locator.rerun_not_experiment": "/rerun is for experiments (E#); use /expand for tools.",
  "locator.expand_not_tool": "/expand is for tools (T#); use /show for experiments.",
  "locator.copy_tool_header": "# {loc} {name} output (copy the block below)",
  "locator.copy_experiment_header": "# {loc} experiment snapshot (copy the JSON below)",
  "locator.rerun_hint": "[{loc}] original description: {desc}\nCopy the line above as your next input to re-issue; it will pass through intent_confirm again.",
  "command.status.desc": "Show current session info",
  "command.tasks.desc": "List recent fault-injection tasks (filter active / failed / all; numeric arg caps the count)",
  "command.recordings.desc": "Recordings: list (default) / export <task_id> <path>",
  "command.recordings.list.desc": "List task recordings available for /replay",
  "command.recordings.export.desc": "Export a task recording as a local JSONL file",
  "recordings.export_usage": "usage: /recordings export <task_id> <out_path>",
  "recordings.export_empty": "recording {id} is empty — nothing to export",
  "recordings.export_exists": "target exists: {path} (refusing to overwrite; move it or pick another)",
  "recordings.export_ok": "exported {events} events / {bytes} bytes → {path}",
  "recordings.export_failed": "export {id} failed: {err}",
  "command.replay.desc": "Replay a task recording — pass task_id (optional speed number or 'instant')",
  "command.doctor.desc": "Show diagnostic info (server / cluster / version / lang)",
  "command.retry.desc": "Resubmit the last natural-language turn (e.g. after a stream error)",

  // -- /retry runtime ------------------------------------------------
  "retry.no_input": "no previous turn to retry — send a message first",
  "retry.busy": "still streaming — wait for the current turn or press Esc to cancel first",
  "retry.unavailable": "/retry is not wired in this context",
  "retry.resubmitting": "retrying: {input}",

  // -- protocol version mismatch -------------------------------------
  "protocol.mismatch": "protocol version mismatch — TUI={tui}, server={server}; some events may render incorrectly. Update with `npm install -g @blade-ai/tui` or `pip install -U blade-ai`.",

  // -- slash command runtime errors ---------------------------------
  "command.handler_failed": "/{name} failed: {msg}",
  // Mid-stream interception when a non-stream-safe command is invoked.
  "command.busy_block": "wait for the current task to finish or press Esc first",

  // -- /help group headings (mirrors Python's _GROUP_LABELS) --------
  "help.group.general": "General",
  "help.group.business": "Business",
  "help.group.skills": "Skills",
  "help.group.dynamic": "Skills",
  // Legacy group keys — still consumed by boot cards / runtime labels
  // outside the slash registry. Migrated to the four-group taxonomy
  // above; these remain so existing t() lookups don't 404.
  "help.group.session": "Session",
  "help.group.tasks": "Tasks",
  "help.group.history": "History",
  "help.card.title": "Commands",
  "help.card.tip": "Tip: type / then TAB to autocomplete",

  // -- /doctor output -----------------------------------------------
  "doctor.head": "Diagnostics",
  "doctor.server": "server",
  "doctor.server_unreachable": "(unreachable)",
  "doctor.cluster": "cluster",
  "doctor.cluster_none": "(none)",
  "doctor.tui_version": "tui version",
  "doctor.server_version": "server version",
  "doctor.protocol": "protocol",
  "doctor.lang": "language",
  "doctor.mode": "permission mode",
  "doctor.terminal_bg": "terminal background",
  "doctor.preflight": "environment self-check",
  "doctor.fix.server_unreachable":
    "Check the blade-ai server is running and reachable at the URL above; restarting the TUI will respawn an embedded server locally.",
  "doctor.fix.protocol_mismatch":
    "Restart the server or upgrade the TUI so the two protocol versions match; mismatched protocols may cause event parsing errors.",
  "doctor.fix.preflight_unavailable":
    "Preflight probe didn't respond — re-run /doctor, or confirm the server is a recent build (older versions lack /api/v1/preflight).",

  // -- slash command outputs ----------------------------------------
  "mode.usage_unknown": "unknown mode '{value}' — expected 'auto' or 'confirm'",
  "mode.usage_missing": "/permission needs an arg — try 'auto' / 'confirm' (current: {mode})",
  "mode.already": "permission mode already {mode}",
  "mode.changed": "permission mode → **{mode}** (takes effect on the next /turn)",

  // -- /mode (display density: calm / working / dense) --------------
  "display.usage_unknown": "unknown density '{value}' — expected 'calm' / 'working' / 'dense'",
  "display.usage_missing": "/mode needs an arg — try 'calm' / 'working' / 'dense' (current: {mode})",
  "display.already": "display mode already {mode}",
  "display.changed": "display mode → **{mode}**",

  "tasks.empty": "no tasks yet — submit a fault description to start one",
  "tasks.head": "last {n} task(s) of {total}:",
  "tasks.empty_filter": "{total} task(s) total, none match [{filter}]",
  "tasks.head_filter": "{filter} · showing {n}/{total} (of {grand} total):",
  "tasks.failed": "failed to list tasks: {err}",

  // -- /review --------------------------------------------------------
  "command.review.desc": "Show a task's review card — pass task_id or E# locator (defaults to most recent)",
  "review.no_recent": "no task to review yet — run /run first to inject a fault",
  "review.failed": "failed to read task: {err}",
  "review.head": "▸ task review · {id}",
  "review.status_label": "status",
  "review.fault_label": "fault",
  "review.phase_label": "phase",
  "review.uid_label": "blade uid",
  "review.duration_label": "duration",
  "review.created_label": "created",

  // -- /experiments ---------------------------------------------------
  "command.experiments.desc": "list every fault scenario the loaded skills can run",
  "experiments.loading": "loading the fault catalog …",
  "experiments.failed": "failed to load experiments: {err}",
  "experiments.empty": "no experiments found — check that /skills directory contains SKILL.md files",
  "experiments.head": "fault catalog ({total} use cases):",
  "experiments.fault_count_unit": "case(s)",
  "experiments.card.title": "Experiments",
  "experiments.card.count": "{n} cases",
  "experiments.card.symptom_empty": "(no symptom)",

  // -- /recover -------------------------------------------------------
  "command.recover.desc": "Fault recovery — pass task_id / latest, or 'list' sub to see recoverable tasks",
  "command.recover.list.desc": "list tasks still in injecting / injected state",
  "recover.list_empty": "no recoverable tasks (injecting/injected)",
  "recover.list_head": "recoverable tasks ({n}):",
  "recover.list_hint": "run **/recover <task_id>** to trigger recovery",
  "recover.list_failed": "failed to list recoverable tasks: {err}",
  "recover.usage": "usage: **/recover <task_id|latest>** — or /recover list to see candidates",
  "recover.no_latest": "no completed task in this session yet — run /run first",
  "recover.busy": "still streaming — wait for the current turn to finish, then retry",
  "recover.starting": "recovering task **{id}** … this hits the cluster and may take tens of seconds",
  "recover.failed": "recover {id} failed: {err}",
  "recover.success_head": "✓ task {id} recovered ({level})",
  "recover.fail_head": "✗ task {id} recovery failed",
  "recover.targets_label": "targets",
  "recover.error_label": "reason",
  "recover.unknown_error": "unknown error",

  // -- /skills --------------------------------------------------------
  "command.skills.desc": "Skills catalog: list / show / reload / install / enable / disable",

  // -- /config --------------------------------------------------------
  "command.config.desc": "Server-side config read/write (list / get / set / unset / path)",
  "command.config.list.desc": "List visible config (sensitive fields are masked)",
  "command.config.get.desc": "Read a single config key: /config get <key>",
  "command.config.set.desc": "Write a key (hot-reloads when possible; some keys need a TUI restart)",
  "command.config.unset.desc": "Remove a key so its default takes over",
  "command.config.path.desc": "Print the resolved ~/.blade-ai/config.json path",
  "config.usage": "usage:\n  /config list                    — list everything\n  /config get <key>               — read one\n  /config set <key> <value>       — write + hot-reload\n  /config unset <key>             — revert to default\n  /config path                    — print config.json path",
  "config.head": "Current config:",
  "config.path_tail": "config.json: {path}",
  "config.failed": "failed to read config: {err}",
  "config.unset": "{key} is unset (default in use)",
  "config.get_usage": "usage: /config get <key>",
  "config.set_usage": "usage: /config set <key> <value>",
  "config.set_ok": "set {key} = {value}{tail}",
  "config.set_cold_tail": " · ⚠ cold key — restart the TUI for full effect",
  "config.set_failed": "failed to set {key}: {err}",
  "config.unset_usage": "usage: /config unset <key>",
  "config.unset_ok": "unset {key}{tail}",
  "config.unset_noop": "{key} was not set; nothing to do",
  "config.unset_failed": "failed to unset {key}: {err}",

  // -- /memory --------------------------------------------------------
  "command.memory.desc": "TUI-session memory (show / clear / path)",
  "command.memory.show.desc": "Show the current TUI session snapshot + recent tasks",
  "command.memory.clear.desc": "Delete the current TUI session file (does NOT clear graph threads)",
  "command.memory.path.desc": "Print the memory_dir",
  "memory.usage": "usage:\n  /memory show     — show session snapshot\n  /memory clear    — delete session file\n  /memory path     — print memory_dir",
  "memory.head": "TUI session: {sid}",
  "memory.cluster_label": "cluster",
  "memory.ns_label": "namespace",
  "memory.started_label": "started_at",
  "memory.status_label": "status",
  "memory.recent_tasks_head": "recent tasks ({shown}/{total}):",
  "memory.stats_head": "stats:",
  "memory.show_failed": "failed to read memory: {err}",
  "memory.clear_ok": "deleted the current session snapshot file",
  "memory.clear_noop": "no snapshot file to delete (session may not have persisted yet)",
  "memory.clear_failed": "failed to delete session snapshot: {err}",

  // -- /compact -------------------------------------------------------
  "command.compact.desc": "Force-compact the current session's context (saves LLM tokens)",
  "compact.busy": "still streaming — wait for the current turn to finish, then retry",
  "compact.starting": "compacting the current session context …",
  "compact.in_progress": "  LLM summariser is running — this can take several seconds",
  "compact.failed": "compaction failed: {err}",
  // ManualCompactIndicator: spinner row visible for the entire
  // /compact lifetime (noop / strip / LLM all uniform).
  "compact.indicator_label": "compacting session context…",
  "compact.indicator_meta": "({elapsed} · esc to cancel)",
  "compact.cancelled": "compaction cancelled",
  "compact.noop": "{before} tokens, no compaction needed ({layer})",
  "compact.ok": "compacted ({layer}): {before} → {after} tokens (saved {saved} / {pct}%)",

  // -- Memory compaction (live SSE event from PreReasoningHook) --
  // Phase 4: main turn SSE forwards ``memory_compaction`` events; UI
  // uses a dedicated spinner during the call (mutex with
  // LoadingIndicator); a final history row records the result.
  "compaction.indicator_label": "compacting memory",
  "compaction.indicator_meta": "({tokens} tokens · {elapsed})",
  "compaction.success_line":
    "✓ compacted {messages} messages: {before} → {after} tokens · saved {saved} ({percent}%) · took {duration}",
  "compaction.failure_line": "✗ compaction failed: {reason} · took {duration}",
  "compaction.failure_unknown": "unknown reason",

  // -- /model ---------------------------------------------------------
  "command.model.desc": "Select the active LLM (list / set)",
  "command.model.list.desc": "List candidate models with the active one marked",
  "command.model.set.desc": "Switch the active model (writes config; requires server restart to apply)",
  "model.usage": "usage:\n  /model list          — list candidate models\n  /model set <id>      — switch active model",
  "model.head": "active model: {active}",
  "model.base_url_label": "api_base_url",
  "model.list_tail": "use **/model set <id>** to switch; takes effect on the next /turn",
  "model.custom_note": "custom (not in the curated list, but usable)",
  "model.card.title": "Models",
  "model.card.count": "{n} models",
  "model.card.tip": "Tip: /model set <id> to switch · takes effect on the next /turn",
  "model.card.custom_section": "custom",
  "model.card.custom_note": "— not in the curated list",
  "model.card.unset": "(unset)",
  "model.failed": "failed to load model list: {err}",
  "model.set_usage": "usage: /model set <model-id>",
  "model.set_ok": "wrote model_name = {id}{tail}",
  "model.set_restart_tail": " · ⚠ restart blade-ai-server to load the new model",
  "model.set_failed": "set model {id} failed: {err}",
  "command.skills.list.desc": "list loaded skills grouped by category (same data source as /experiments, summary view)",
  "command.skills.show.desc": "Show metadata + entry scripts for one skill: /skills show <name>",
  "command.skills.reload.desc": "Re-scan skills_dir and refresh the loaded set",
  "command.skills.install.desc": "Install a skill from a git URL or local path (no setup scripts run)",
  "command.skills.enable.desc": "Re-enable a previously disabled skill",
  "command.skills.disable.desc": "Disable a skill (file kept, removed from registry)",
  "skills.usage": "usage:\n  /skills list                — list skills\n  /skills show <name>         — skill detail\n  /skills reload              — re-scan skills_dir\n  /skills install <url|path>  — install (copy only)\n  /skills enable <name>       — re-enable\n  /skills disable <name>      — disable",
  "skills.list_failed": "failed to list skills: {err}",
  "skills.list_empty": "no skills found",
  "skills.list_head": "skill categories ({n} group(s), {total} use case(s)):",
  "skills.list_tail": "run **/experiments** to see the per-fault detail under each category",
  "skills.show_usage": "usage: /skills show <name>",
  "skills.show_failed": "failed to read skill {name}: {err}",
  "skills.show_head": "▸ skill · {name}",
  "skills.show_scripts_head": "scripts ({n}):",
  "skills.reload_failed": "skills reload failed: {err}",
  "skills.reload_head": "rescanned {dir} ({total} skills)",
  "skills.reload_no_change": "  (no additions / removals)",
  "skills.reload_added": "  + added: {items}",
  "skills.reload_removed": "  - removed: {items}",
  "skills.install_usage": "usage: /skills install <git-url|local-path>",
  "skills.install_starting": "installing from {source} (file copy only — no scripts run) …",
  "skills.install_failed": "install failed: {err}",
  "skills.install_none": "no skills installed (missing SKILL.md or validation failed)",
  "skills.install_head": "installed {n} skill(s):",
  "skills.install_next": "run **/skills reload** to activate",
  "skills.enable_usage": "usage: /skills enable <name>",
  "skills.enable_failed": "enable {name} failed: {err}",
  "skills.enable_noop": "{name} is not currently disabled — nothing to do",
  "skills.enable_ok": "enabled {name} (run /skills reload to apply)",
  "skills.disable_usage": "usage: /skills disable <name>",
  "skills.disable_failed": "disable {name} failed: {err}",
  "skills.disable_noop": "{name} is already disabled",
  "skills.disable_ok": "disabled {name} (run /skills reload to refresh dynamic commands)",
  "command.skills.path.desc": "Print the resolved skills_dir + candidate priority list",
  "skills.path_head": "resolved: {dir}",
  "skills.path_candidates_head": "candidates (by priority):",
  "skills.path_failed": "failed to read skills_dir: {err}",

  "recordings.empty": "no recordings on disk yet",
  "recordings.head": "{n} recording(s) (most recent first):",
  "recordings.use_replay": "use **/replay <task_id>** to replay",
  "recordings.failed": "failed to list recordings: {err}",

  "replay.usage": "usage: **/replay <task_id> [speed]** — speed is a number (default 4x) or 'instant'",
  "replay.empty": "recording {id} is empty",
  "replay.starting": "replaying **{id}** — {n} event(s) at {speed} · esc to abort",
  "replay.done": "replay done · {converted} converted · {skipped} skipped · {duration}{tail}",
  "replay.aborted_tail": " (aborted)",
  "replay.failed": "failed to replay {id}: {err}",
  "replay.unknown_command": "unknown command: /{name} — try /help",

  "status.session": "session id",
  "status.cluster": "cluster",
  "status.namespace": "namespace",
  "status.model": "model",
  "status.mode": "permission mode",
  "status.created": "created",
  "status.tasks": "tasks",
  "status.failed": "failed to read session state: {err}",
  "session.card.title": "Session",

  // -- Header chrome ------------------------------------------------
  "header.brand_tag": "(TS preview)",
  "header.commands_hint": "/help · /doctor · /mode · /exit",
  "header.connected_to": "connected to {url}",
  "header.no_cluster": "(no-cluster)",
  "header.default_agent": "agent",

  // -- Input placeholder --------------------------------------------
  "input.placeholder": "Type your message · /help for commands",
  // Shown in place of the regular placeholder while the agent is
  // streaming a reply — signals the locked Enter key without blocking
  // the user from drafting their next message.
  "input.placeholder_streaming": "agent finishing — Enter sends after current turn",

  // -- AgentMessage truncation hint (shown only while pending — full
  // text appears in scrollback after TURN_DONE moves the item to
  // <Static>) ----------------------------------------------------------
  "agent.truncated_earlier": "… +{n} earlier lines · full text in scrollback after turn",

  // -- ResultCard chrome --------------------------------------------
  "result.label.fault": "Fault",
  "result.label.uid": "Blade UID",
  "result.label.duration": "Duration",
  "result.label.summary": "Summary",
  "result.label.cause": "Cause",
  "result.label.hint": "Hint",
  "result.label.why_partial": "Why partial",
  // v3 short chip labels (rendered inside [], all uppercase)
  "result.chip.success": "SUCCESS",
  "result.chip.partial": "PARTIAL",
  "result.chip.failed": "FAILED",
  "result.chip.unknown": "RESULT",
  // v3 in-card section headings
  "result.section.outcome": "Outcome",
  "result.section.effect": "Effect verified",
  "result.section.recovery_notes": "Recovery notes",
  "result.section.failure_analysis": "Failure analysis",
  "result.section.side_effects": "Side effects",
  "result.label.target": "Target",
  "result.label.attempts": "Attempts",
  "result.label.side_effect_item": "Side effect",
  "result.side_effects_none": "No collateral impact detected",
  "result.attempts.label": "succeeded after {n} auto-replan(s)",
  "result.status.success": "Injection succeeded",
  "result.status.partial": "Partial recovery",
  "result.status.failed": "Injection failed",
  "result.status.unknown": "Result",
  "result.status.success.recover": "Recovery succeeded",
  "result.status.failed.recover": "Recovery failed",
  "result.show_for_timeline": "/replay {id} instant — for full timeline",

  // -- Postmortem (T6) ---------------------------------------------
  "postmortem.title": "Postmortem",
  "postmortem.saved_at": "Full markdown: {path}",

  // -- PlanPreviewSection (injection plan / alternatives) ----------------
  "plan_preview.title": "Injection Plan Preview",
  "plan_preview.alternatives_title": "Alternatives",

  // -- ConfirmMessage chrome ----------------------------------------
  "confirm.title": "Confirm intent",
  "confirm.body_empty": "(no plan summary received)",
  "confirm.proceed": "proceed",
  "confirm.refine": "refine",
  "confirm.answered": "confirmation answered",
  "confirm.answered_rejected": "confirmation cancelled",

  // -- ConfirmMessage Layer 1 (intent_confirm) ----------------------
  "confirm.intent.title": "Confirm fault intent",
  "confirm.intent.proceed": "submit",
  "confirm.intent.refine": "refine",

  // -- ConfirmMessage Layer 2 (confirmation_gate) -------------------
  "confirm.execution.title": "Confirm execution plan",
  "confirm.execution.proceed": "inject",
  "confirm.execution.cancel": "cancel",
  "confirm.targetChange.chip": "DRIFT",
  "confirm.targetChange.title": "Target change confirmation",
  "confirm.targetChange.preamble": "The agent is attempting to operate on a different target than approved.",
  "confirm.targetChange.agentReason": "Agent reasoning",
  "confirm.targetChange.agentReasonEmpty": "Agent did not provide a reason",
  "confirm.targetChange.original": "Original target",
  "confirm.targetChange.proposed": "Proposed target",
  "confirm.targetChange.approve": "approve change",
  "confirm.targetChange.reject": "reject",
  "confirm.planChange.chip": "PLAN",
  "confirm.planChange.title": "Plan Change Confirmation",
  "confirm.planChange.preamble": "Agent has determined the original fault type is not viable after replanning and proposes an alternative:",
  "confirm.planChange.reason": "Reason for change",
  "confirm.planChange.original": "Original fault type",
  "confirm.planChange.proposed": "Proposed fault type",
  "confirm.planChange.approve": "approve change",
  "confirm.planChange.reject": "reject",

  // -- ConfirmMessage field labels ----------------------------------
  "confirm.field.fault_type": "Fault type",
  "confirm.field.scope": "Scope",
  "confirm.field.target": "Target",
  "confirm.field.action": "Action",
  "confirm.field.namespace": "Namespace",
  "confirm.field.labels": "Labels",
  "confirm.field.names": "Names",
  "confirm.field.params": "Params",
  "confirm.field.user_description": "User intent",
  "confirm.field.skill": "Skill",
  "confirm.field.plan_summary": "Plan",
  "confirm.field.safety_status": "Safety check",
  "confirm.field.safety_reason": "Safety reason",
  "confirm.field.risk": "Risk",
  "confirm.field.safety": "Safety",

  // -- ConfirmMessage preamble / subtitle ---------------------------
  "confirm.intent.preamble": "Identified the following fault injection intent:",
  "confirm.execution.preamble": "Confirm the execution plan:",
  "confirm.generic.preamble": "Please confirm:",

  // -- v3 title chip labels (bracket chip style, short uppercase) ---
  "confirm.intent.chip": "INTENT",
  "confirm.execution.chip": "EXECUTE",
  "confirm.generic.chip": "CONFIRM",

  // -- v3 in-card section headings ----------------------------------
  "confirm.section.decision_signals": "Decision signals",
  "confirm.section.execution_plan": "Execution plan",
  "confirm.section.safety_check": "Safety check",
  "confirm.section.parameters": "Parameters",
  "confirm.section.target_health": "Target health",
  "confirm.section.conflicts": "Conflicting experiments",
  "confirm.section.audit_trail": "Audit trail",
  "confirm.section.safety_score": "Safety score",
  // E10 — multi-dimensional safety score panel labels
  "safety_score.overall": "Overall",
  "safety_score.blast_radius": "Blast radius",
  "safety_score.frequency": "Frequency",
  "safety_score.time": "Time",
  "safety_score.topology": "Topology",
  "safety_score.level.low": "low",
  "safety_score.level.medium": "medium",
  "safety_score.level.high": "high",
  "safety_score.level.critical": "critical",
  // -- v3 extra field labels
  "confirm.field.attempt": "Attempt",
  "confirm.field.plan_path": "Plan file",
  "confirm.field.clarification_round": "Clarification",
  "confirm.field.intent_reasoning": "Reasoning",
  "confirm.field.health_summary": "Summary",
  // Fault classification — L1's fault_type + (scope/target/action)
  // triple, surfaced in L2 so operators see "this is mem-load" at a
  // glance instead of inferring from ``params`` keys. Distinct from
  // ``confirm.field.fault_type`` (which lives in L1 and shows the bare
  // type only); this label denotes the full 3-axis semantic tag.
  "confirm.field.fault": "Fault",
  // Complexity flag — is_complex=true means the agent ran
  // save_fault_plan and produced a formal multi-section plan markdown.
  // Only rendered when true so simple plans don't carry a noisy
  // "simple plan" badge.
  "confirm.field.complexity": "Complexity",
  "confirm.complexity.complex": "complex (formal plan generated)",
  "confirm.attempt.label": "attempt {n}",
  "confirm.clarification.label": "{n} clarification round(s)",
  "confirm.plan_saved": "saved ({path}) · /show plan to view",
  "confirm.field.conflicts": "Conflicts",
  "confirm.conflicts.hint": "/show experiments to inspect",
  // Empty-state placeholders so the Parameters / Target health
  // sections always render even when there's "nothing notable" — the
  // section heading itself signals "we did look at this", and the
  // empty value tells the user "all clear" / "no params specified"
  // rather than leaving the section out (which could read as "the
  // agent forgot to check").
  "confirm.field.health": "health",
  "confirm.params.none": "—",
  "confirm.health.all_clear": "all targets healthy",
  "confirm.health.not_run": "check not run",
  "confirm.field.feasibility": "feasibility",
  "confirm.feasibility.all_clear": "injection feasible",
  "confirm.feasibility.not_run": "check not run",
  "confirm.intent.low_conf_audit": "Why this intent:",

  // -- Forge × Operator redesign: banner + headline + armed chip ----
  "confirm.intent.banner": "INTENT CHECK",
  "confirm.execution.banner": "EXECUTE · this hits production",
  "confirm.intent.headline": "Soft check: is this the fault you meant?",
  "confirm.execution.headline": "Hard check: actually push this to the cluster?",
  "confirm.armed_chip": "ARMED",
  "confirm.aborted_chip": "ABORTED",
  "confirm.armed_tail": "proceeding",
  "confirm.aborted_tail": "stopped",

  // -- Risk meter / confidence tier ---------------------------------
  "confirm.tier.low": "low",
  "confirm.tier.medium": "medium",
  "confirm.tier.high": "high",
  "confirm.risk.runtime": "determined at runtime",
  "confirm.risk.scope.labels": "label match",
  "confirm.risk.scope.namespace": "entire namespace",
  "confirm.risk.scope.percent": "percent {value}",

  // -- Low-confidence warning tail ----------------------------------
  "confirm.confidence.warn_strong": "Strongly recommend verifying each field",
  "confirm.confidence.warn_soft": "Recommend verifying each field",
  "confirm.confidence.warn_prod": "namespace contains 'prod' — confirm this is not production",

  // -- Safety badge -------------------------------------------------
  "confirm.safety.safe": "SAFE",
  "confirm.safety.warning": "WARNING",
  "confirm.safety.blocked": "BLOCKED",
  "confirm.safety.all_clear": "Safety check passed",

  // -- Select component hints --------------------------------------
  "select.options.hint": "A-Z jump · ↑↓ select · Enter confirm · Esc cancel",
  "select.feedback.hint": "Enter send · Esc back to options",
  "select.feedback.placeholder": "tell the agent something else…",

  // -- YesNoFeedbackSelect generic defaults (any yes/no/free-text prompt)
  "select.yesno.yes": "Yes",
  "select.yesno.no": "No",
  "select.yesno.feedback": "Tell me something else…",

  // -- ConfirmMessage option labels ---------------------------------
  "confirm.option.feedback": "Tell the agent something else…",

  // -- ConfirmMessage Plan Builder --------------------------------
  "confirm.plan_builder.title": "Plan Guide",
  "confirm.plan_builder.default_question": "Please select an option",
  "confirm.plan_builder.free_input": "Free input",
  "confirm.field.intent_confidence": "Intent confidence",

  // -- SlashMenu hint -----------------------------------------------
  "slash.menu.hint": "↑↓ select · Enter/Tab apply · Esc dismiss",
  "slash.menu.empty": "(no matching commands)",
  "slash.menu.more_above": "↑ {n} more",
  "slash.menu.more_below": "↓ {n} more",

  // -- Footer / common ----------------------------------------------
  "footer.help_hint": "? for help",

  // -- boot screen: welcome card ------------------------------------
  "welcome.welcome_back": "Welcome back!",
  "welcome.mode_label": "mode",
  "welcome.mode.auto": "auto",
  "welcome.mode.confirm": "confirm",
  "welcome.tips_header": "Tips for getting started",
  "welcome.tip.describe": "Describe the fault you want, e.g. \"inject CPU stress on the nginx pods in default namespace\"",
  "welcome.tip.help": "Use /help to list all commands",
  "welcome.tip.doctor": "Use /doctor to inspect the runtime environment",
  "welcome.tip.retry": "Use /retry to resubmit the last turn after a stream error",
  "welcome.tip.mode": "Press Shift+Tab to toggle permission mode (confirm ↔ auto)",
  "welcome.runtime_header": "Runtime",
  "welcome.bottom_hint": "Type a natural-language fault description, or /help to see commands",

  // -- boot screen: doctor card -------------------------------------
  "boot.doctor.title": "Environment self-check",
  "boot.doctor.summary": "{passed}/{total} passed",
  "boot.doctor.passed_short": "passed",
  "boot.doctor.fixes_header": "Suggested fixes",
  "boot.doctor.captured_at": "captured at {time} (re-run /doctor for live)",
  "boot.doctor.unavailable": "preflight endpoint unavailable — older server or fetch error",

  // -- boot screen: pending tasks card ------------------------------
  "boot.pending.title": "Unfinished tasks",
  "boot.pending.empty": "No unfinished tasks",

  // -- boot screen: progress phase labels (shown next to spinner) ---
  "boot.progress.spawning": "Starting blade-ai backend…",
  "boot.progress.health": "Waiting for backend to be ready…",
  "boot.progress.session": "Creating session…",
  "boot.progress.preflight": "Running environment self-check…",
  "boot.progress.tasks": "Checking pending tasks…",

  // -- exit screen: goodbye card ------------------------------------
  // Mirrors the Python TUI's `tui/renderers/goodbye.py` so switching
  // between the two front-ends gives the same farewell.
  "goodbye.title": "See you next time",
  "goodbye.farewell": "Thanks for using blade-ai",
  "goodbye.section.overview": "Session overview",
  "goodbye.section.activity": "Activity",
  "goodbye.label.session_id": "Session ID",
  "goodbye.label.duration": "Duration",
  "goodbye.label.cluster_ns": "Cluster / namespace",
  "goodbye.label.messages": "Messages",
  "goodbye.label.injections": "Fault injections",
  "goodbye.label.recoveries": "Recoveries",
  "goodbye.value.count": "{n}",
  "goodbye.cluster_auto": "(auto)",

  "common.none": "(none)",
  "common.unset": "(unset)",
  "common.unknown": "(unknown)",

  // -- Phase Stepper (5-step todo list shown during inject turns) --
  // ``recovery`` is intentionally absent — recover is a separate flow
  // (its own graph + task_id space), already covered by the boot-time
  // PendingTasksCard, and bounded by ``blade --timeout`` auto-cleanup.
  // A future recover-mode stepper will be added separately.
  //
  // The five steps mirror the actual graph node sequence in
  // ``src/chaos_agent/agent/graph.py``:
  //   intent      → intent_clarification (incl. Layer-1 confirm)
  //   agent_loop  → agent_loop (planning, phase1 tools)
  //   safety      → safety_check / confirmation_gate (Layer-2 confirm)
  //   execute     → baseline_capture / execute_loop / direct_execute
  //   verify      → verifier_loop
  "phase.stepper.title": "Inject todos",
  "phase.label.intent": "Intent",
  "phase.label.agent_loop": "Plan",
  "phase.label.safety": "Safety check",
  "phase.label.execute": "Inject",
  "phase.label.verify": "Verify",

  // -- ToolMessage card chrome --------------------------------------
  "tool.running": "running…",
  "tool.no_output": "(no output)",
  "tool.more_lines": "… +{n} more lines",
  "tool.captured_in_confirm": "(output delivered via the confirm card below)",

  // -- WizardCard ---------------------------------------------------
  "wizard.step.welcome": "Welcome",
  "wizard.step.model": "Model",
  "wizard.step.api_url": "API URL",
  "wizard.step.api_key": "API Key",
  "wizard.step.kubeconfig": "Kubeconfig",
  "wizard.step.kube_context": "K8s Context",
  "wizard.step.permission": "Permission",
  "wizard.step.summary": "Review",
  "wizard.welcome.title": "blade-ai setup",
  "wizard.welcome.section": "Hello",
  "wizard.welcome.body1": "8 steps and you're ready. Each step has a smart default — press Enter to accept.",
  "wizard.welcome.body2": "Esc cancels at any time (nothing saved). ← goes back; 1-8 jumps to a completed step.",
  "wizard.welcome.fields_section": "You'll configure",
  "wizard.model.title": "Default model",
  "wizard.model.recommended_section": "Recommended",
  "wizard.model.other_section": "Other",
  "wizard.model.custom_section": "Custom Model ID",
  "wizard.model.custom_option": "Custom model ID...",
  "wizard.model.custom_hint": "Any OpenAI-compatible model",
  "wizard.model.label": "Model ID",
  "wizard.model.placeholder": "e.g. gpt-4-turbo / deepseek-r1 / gemini-2.5-pro",
  "wizard.api_url.title": "API Base URL",
  "wizard.api_url.section": "Input",
  "wizard.api_url.label": "URL",
  "wizard.api_key.title": "LLM API Key",
  "wizard.api_key.section": "Input",
  "wizard.api_key.label": "API Key",
  "wizard.kubeconfig.title": "Kubeconfig path",
  "wizard.kubeconfig.section": "Input",
  "wizard.kubeconfig.label": "Path",
  "wizard.kube_context.title": "K8s Context",
  "wizard.kube_context.section": "Discovered",
  "wizard.permission.title": "Permission mode",
  "wizard.permission.section": "Mode",
  "wizard.permission.confirm_label": "confirm (recommended for prod)",
  "wizard.permission.confirm_hint": "ask before each fault injection",
  "wizard.permission.auto_label": "auto",
  "wizard.permission.auto_hint": "skip confirmation (test clusters only)",
  "wizard.summary.title": "Review & save",
  "wizard.summary.section_config": "Config",
  "wizard.summary.section_result": "Save result",
  "wizard.summary.model": "Model",
  "wizard.summary.api_url": "API URL",
  "wizard.summary.api_key": "API Key",
  "wizard.summary.kubeconfig": "Kubeconfig",
  "wizard.summary.kube_context": "K8s Context",
  "wizard.summary.kube_context_default": "(use kubeconfig current-context)",
  "wizard.summary.permission": "Permission",
  "wizard.summary.custom_tag": "(custom)",
  "wizard.summary.saved_to": "Saved to",
  "wizard.summary.saved_keys": "Written keys",
  "wizard.summary.save_error": "Save failed",
  "wizard.validation.in_progress": "Validating…",
  "wizard.returned_hint": "Returned to this step — re-validate or edit",
  "wizard.hint.welcome": "Enter to start  ·  Esc to cancel",
  "wizard.hint.radio_with_back": "A-Z select  ·  ↑↓ move  ·  Enter confirm  ·  ← back  ·  Esc cancel",
  "wizard.hint.text_with_back": "Enter confirm  ·  ← back  ·  1-8 jump  ·  Esc cancel",
  "wizard.hint.model_custom": "Enter confirm  ·  Esc back to presets  ·  ← previous step",
  "wizard.hint.summary": "Enter to save  ·  1-7 jump back to edit  ·  ← back  ·  Esc cancel",
  "wizard.hint.saved": "Saved — press Enter to continue",
  "wizard.hint.save_failed": "Save failed — ← back to fix or Esc to exit",
  "wizard.cancel_message": "Setup wizard cancelled — blade-ai exiting. You'll be prompted again next launch.",
  "wizard.model.empty_error": "Model ID cannot be empty",
};
