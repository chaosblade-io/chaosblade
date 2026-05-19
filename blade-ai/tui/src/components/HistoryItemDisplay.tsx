/**
 * Route a HistoryItem to the right per-kind component.
 *
 * Kept tiny so MainContent can map history + pending uniformly without
 * caring about the discriminated-union details.
 *
 * The optional ``isPending`` prop signals "this item is rendering in
 * the dynamic area below ``<Static>``, not in scrollback". Components
 * that grow unboundedly during streaming (today: AgentMessage's token
 * stream) consume it to cap their visible height — without that cap
 * the dynamic frame outgrows ``stdout.rows`` and Ink's fullscreen-
 * redraw branch fires on every token, producing the visible flicker +
 * scroll-position thrash users observe during inject. Mirrors the
 * approach qwen-code (Ink v7) uses with its ``MaxSizedBox`` /
 * ``availableTerminalHeight`` pair: same lever, simpler implementation.
 */

import type { HistoryItem } from "../state/types.js";
import { BootDoctorCard } from "./boot/BootDoctorCard.js";
import { PendingTasksCard } from "./boot/PendingTasksCard.js";
import { WelcomeCard } from "./boot/WelcomeCard.js";
import { PhaseStepperCard } from "./PhaseStepperCard.js";
import { ResultCard } from "./result/ResultCard.js";
import { RuntimeDoctorCard } from "./RuntimeDoctorCard.js";
import { AgentMessage } from "./messages/AgentMessage.js";
import {
  ConfirmContextMessage,
  ConfirmPromptMessage,
} from "./messages/ConfirmMessage.js";
import { ErrorMessage } from "./messages/ErrorMessage.js";
import { LogMessage } from "./messages/LogMessage.js";
import { SystemMessage } from "./messages/SystemMessage.js";
import { ThinkingMessage } from "./messages/ThinkingMessage.js";
import { ToolGroupMessage } from "./messages/ToolGroupMessage.js";
import { ToolMessage } from "./messages/ToolMessage.js";
import { TurnUsageMessage } from "./messages/TurnUsageMessage.js";
import { UserMessage } from "./messages/UserMessage.js";
import { MemoryCompactionMessage } from "./messages/MemoryCompactionMessage.js";

export const HistoryItemDisplay: React.FC<{
  item: HistoryItem;
  /** True while the item is rendering in the dynamic area (pending);
   *  false / undefined when it has been committed to ``<Static>``
   *  history. Forwarded to ``AgentMessage`` so it can clamp its
   *  visible height during streaming. */
  isPending?: boolean;
  /** Per-pending-item row budget computed by ``MainContent`` from
   *  ``terminal.rows - CHROME_ROWS_RESERVE``. Used by components that
   *  embed unbounded content (agent stream, tool stdout) to wrap
   *  their body in ``MaxSizedBox`` so the dynamic frame stays
   *  bounded. ``undefined`` means "unbounded" (Static rendering OR
   *  user toggled ``constrainHeight`` off via Ctrl+O). */
  availableTerminalHeight?: number;
  /** Set when the dispatched ``item`` is a ``confirm_prompt`` —
   *  ``true`` for the first unresolved prompt in pending, ``false``
   *  for any later one. Routed through to ``ConfirmPromptMessage``'s
   *  ``isFocused`` prop so only the leading prompt's Select consumes
   *  arrows / Enter. ``undefined`` for non-prompt items (ignored). */
  isPromptFocused?: boolean;
}> = ({ item, isPending, availableTerminalHeight, isPromptFocused }) => {
  switch (item.kind) {
    case "user":
      return <UserMessage item={item} />;
    case "agent":
      return (
        <AgentMessage
          item={item}
          isPending={isPending}
          availableTerminalHeight={availableTerminalHeight}
        />
      );
    case "tool":
      return (
        <ToolMessage
          item={item}
          isPending={isPending}
          availableTerminalHeight={availableTerminalHeight}
        />
      );
    case "tool_group":
      return (
        <ToolGroupMessage
          item={item}
          isPending={isPending}
          availableTerminalHeight={availableTerminalHeight}
        />
      );
    case "result":
      return <ResultCard item={item} />;
    case "confirm_context":
      return <ConfirmContextMessage item={item} />;
    case "confirm_prompt":
      return (
        <ConfirmPromptMessage item={item} isFocused={isPromptFocused ?? true} />
      );
    case "log":
      return <LogMessage item={item} />;
    case "system":
      return <SystemMessage item={item} />;
    case "error":
      return <ErrorMessage item={item} />;
    case "thinking":
      return <ThinkingMessage item={item} />;
    case "turn_usage":
      return <TurnUsageMessage item={item} />;
    case "memory_compaction":
      return <MemoryCompactionMessage item={item} />;
    case "welcome_card":
      return <WelcomeCard item={item} />;
    case "boot_doctor_card":
      return <BootDoctorCard item={item} />;
    case "pending_tasks_card":
      return <PendingTasksCard item={item} />;
    case "runtime_doctor_card":
      return <RuntimeDoctorCard item={item} />;
    case "phase_stepper":
      return <PhaseStepperCard item={item} />;
  }
};
