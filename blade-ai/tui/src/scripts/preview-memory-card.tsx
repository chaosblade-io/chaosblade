/**
 * Local preview for ``MemoryCard``. Renders the doctor-style card
 * with three mock data shapes so you can visually compare:
 *
 *   1. ``empty``       — brand-new session, no tasks, zeroed stats
 *                        (your current /memory show output)
 *   2. ``with_tasks``  — a session with a few recent inject/recover
 *                        tasks and non-zero stats
 *   3. ``warn``        — a session with failed injections (warn row)
 *
 * Run:
 *
 *   cd tui && npx tsx src/scripts/preview-memory-card.tsx
 *
 * (Or substitute your usual ``tsx``/``ts-node`` invocation.)
 *
 * Press Ctrl+C to exit.
 */

import { render, Box, Text } from "ink";
import React from "react";
import { MemoryCard } from "../components/MemoryCard.js";
import type { MemoryCardItem } from "../state/types.js";

const EMPTY: MemoryCardItem = {
  kind: "memory_card",
  id: "preview-empty",
  sessionId: "sess_c74ee022867d",
  startedAt: "2026-05-21T19:41:45.481492+08:00",
  status: "active",
  cluster: "",
  namespace: "default",
  recentTasks: [],
  totalTasks: 0,
  stats: {
    message_count: 0,
    injection_count: 0,
    injection_success: 0,
    injection_fail: 0,
    recovery_count: 0,
  },
  memoryDir: "/Users/jiangzelin/.blade-ai/memory",
  capturedAt: new Date().toISOString(),
};

const WITH_TASKS: MemoryCardItem = {
  kind: "memory_card",
  id: "preview-with-tasks",
  sessionId: "sess_a1b2c3d4e5f6",
  startedAt: "2026-05-21T14:08:32.000000+08:00",
  status: "active",
  cluster: "starops-test",
  namespace: "cms-demo",
  recentTasks: [
    "task-eb426de422f9",
    "task-22a10e076d65",
    "task-6fa972685984",
  ],
  totalTasks: 12,
  stats: {
    message_count: 47,
    injection_count: 8,
    injection_success: 7,
    injection_fail: 1,
    recovery_count: 3,
  },
  memoryDir: "/Users/jiangzelin/.blade-ai/memory",
  capturedAt: new Date().toISOString(),
};

const WARN: MemoryCardItem = {
  kind: "memory_card",
  id: "preview-warn",
  sessionId: "sess_warn123example",
  startedAt: "2026-05-21T10:00:00.000000+08:00",
  status: "active",
  cluster: "prod-cluster-01",
  namespace: "payments",
  recentTasks: ["task-3db42c21-cc39-41ef-b454-a0b0f0156dfe"],
  totalTasks: 5,
  stats: {
    message_count: 124,
    injection_count: 5,
    injection_success: 1,
    injection_fail: 4,
    recovery_count: 2,
  },
  memoryDir: "/Users/jiangzelin/.blade-ai/memory",
  capturedAt: new Date().toISOString(),
};

const App: React.FC = () => (
  <Box flexDirection="column">
    <Box paddingLeft={2} marginTop={1}>
      <Text bold color="cyan">
        ── 1. Empty session (your current scenario) ──
      </Text>
    </Box>
    <MemoryCard item={EMPTY} />

    <Box paddingLeft={2} marginTop={2}>
      <Text bold color="cyan">
        ── 2. Session with recent tasks ──
      </Text>
    </Box>
    <MemoryCard item={WITH_TASKS} />

    <Box paddingLeft={2} marginTop={2}>
      <Text bold color="cyan">
        ── 3. Session with failed injections (warn) ──
      </Text>
    </Box>
    <MemoryCard item={WARN} />

    <Box paddingLeft={2} marginTop={1} marginBottom={1}>
      <Text color="gray">(Ctrl+C to exit)</Text>
    </Box>
  </Box>
);

render(<App />);
