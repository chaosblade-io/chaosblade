/**
 * Tests for the progressive Static replay helper exported from
 * MainContent. The full component requires Ink + StoreProvider +
 * useAppSelector so it's exercised via smoke tests; this unit test
 * pins the boundary semantics of the chunking math so a future
 * threshold tweak fails here with a precise message rather than as a
 * subtle UX regression nobody catches.
 */

import { describe, expect, it } from "vitest";
import {
  PROGRESSIVE_REPLAY_CHUNK_SIZE,
  PROGRESSIVE_REPLAY_THRESHOLD,
  acceptChromeMeasurement,
  chromeMeasurementCap,
  initialReplayCount,
} from "./MainContent.js";

describe("initialReplayCount", () => {
  it("returns the full length for empty history", () => {
    expect(initialReplayCount(0)).toBe(0);
  });

  it("returns the full length when below threshold", () => {
    expect(initialReplayCount(1)).toBe(1);
    expect(initialReplayCount(50)).toBe(50);
    expect(initialReplayCount(PROGRESSIVE_REPLAY_THRESHOLD - 1)).toBe(
      PROGRESSIVE_REPLAY_THRESHOLD - 1,
    );
  });

  it("returns the full length when exactly at threshold", () => {
    // Equal-to-threshold uses the fast path — no chunking machinery.
    expect(initialReplayCount(PROGRESSIVE_REPLAY_THRESHOLD)).toBe(
      PROGRESSIVE_REPLAY_THRESHOLD,
    );
  });

  it("starts at CHUNK_SIZE when above threshold", () => {
    expect(initialReplayCount(PROGRESSIVE_REPLAY_THRESHOLD + 1)).toBe(
      PROGRESSIVE_REPLAY_CHUNK_SIZE,
    );
    expect(initialReplayCount(500)).toBe(PROGRESSIVE_REPLAY_CHUNK_SIZE);
    expect(initialReplayCount(5000)).toBe(PROGRESSIVE_REPLAY_CHUNK_SIZE);
  });

  it("threshold > chunk-size guarantees first chunk advances", () => {
    // Sanity: if a future refactor accidentally sets CHUNK_SIZE >=
    // THRESHOLD, the chunked branch becomes unreachable. Pin the
    // invariant so the regression is caught here.
    expect(PROGRESSIVE_REPLAY_CHUNK_SIZE).toBeLessThan(
      PROGRESSIVE_REPLAY_THRESHOLD,
    );
  });
});

describe("chromeMeasurementCap", () => {
  it("uses the absolute ceiling on large terminals", () => {
    // rows is large enough that ``rows - MIN_PENDING_BUDGET`` exceeds
    // the absolute ceiling — the min() kicks in and we get the
    // ceiling (35).
    expect(chromeMeasurementCap(80)).toBe(35);
    expect(chromeMeasurementCap(1000)).toBe(35);
  });

  it("scales down on small terminals to keep MIN_PENDING_BUDGET reachable", () => {
    // rows=20: rows - 6 = 14 < 35 → cap = 14. An honest chrome of 15
    // would still be rejected (correctly), but 14 is accepted.
    expect(chromeMeasurementCap(20)).toBe(14);
    expect(chromeMeasurementCap(30)).toBe(24);
  });

  it("floors at MIN_PENDING_BUDGET (=6) on tiny terminals", () => {
    // rows=12: rows - 6 = 6. cap = max(6, 6) = 6. The dynamic
    // measurement path is effectively dead on terminals this small,
    // but the cap never collapses below MIN_PENDING_BUDGET, so the
    // ``measured > cap`` rejection still has well-defined semantics.
    expect(chromeMeasurementCap(12)).toBe(6);
    expect(chromeMeasurementCap(6)).toBe(6);
    expect(chromeMeasurementCap(0)).toBe(6);
  });
});

describe("acceptChromeMeasurement", () => {
  it("accepts a reasonable mid-range value", () => {
    expect(acceptChromeMeasurement(20, 80)).toBe(20);
    expect(acceptChromeMeasurement(15, 80)).toBe(15);
  });

  it("rejects values below the minimum chrome", () => {
    // Below 5 means Composer hasn't fully laid out yet — keep prior.
    expect(acceptChromeMeasurement(4, 80)).toBeNull();
    expect(acceptChromeMeasurement(0, 80)).toBeNull();
    expect(acceptChromeMeasurement(-3, 80)).toBeNull();
  });

  it("rejects values above the cap", () => {
    // Large terminal — cap = 35. Reading 36 is pathological.
    expect(acceptChromeMeasurement(36, 80)).toBeNull();
    expect(acceptChromeMeasurement(80, 80)).toBeNull();
  });

  it("rejects NaN (the regression that motivated this helper)", () => {
    // The legacy ``measured < 5`` / ``measured > cap`` pair silently
    // let NaN through because both operators return false for NaN.
    // NaN would then propagate via ``rows - NaN - 2 = NaN`` into
    // every MaxSizedBox budget. ``Number.isFinite`` is the first
    // guard here so NaN must come back as null.
    expect(acceptChromeMeasurement(Number.NaN, 80)).toBeNull();
  });

  it("rejects Infinity (also bypasses the < / > guards)", () => {
    expect(acceptChromeMeasurement(Number.POSITIVE_INFINITY, 80)).toBeNull();
    expect(acceptChromeMeasurement(Number.NEGATIVE_INFINITY, 80)).toBeNull();
  });

  it("accepts values right at the cap boundary", () => {
    // Large terminal: cap = 35, value = 35 → accept (not <, not >).
    expect(acceptChromeMeasurement(35, 80)).toBe(35);
    // Small terminal: cap = 14, value = 14 → accept.
    expect(acceptChromeMeasurement(14, 20)).toBe(14);
  });

  it("accepts the minimum boundary value 5", () => {
    expect(acceptChromeMeasurement(5, 80)).toBe(5);
  });

  it("on a tiny terminal where cap collapses to 6, real chrome is rejected and the fallback wins", () => {
    // rows=10 → cap = max(6, 10-6) = 6. A real chrome of 8 is
    // rejected; caller falls back to CHROME_ROWS_RESERVE (26).
    // This is intentional: tiny terminals can't fit our chrome at
    // all, so the fallback is as good as any honest reading.
    expect(acceptChromeMeasurement(8, 10)).toBeNull();
  });
});

describe("acceptChromeMeasurement — hysteresis (issue #1301)", () => {
  it("suppresses ±1 oscillation when prev is provided", () => {
    // prev=20, measured=21 → keep 20 (not 21)
    expect(acceptChromeMeasurement(21, 80, 20)).toBe(20);
    // prev=20, measured=19 → keep 20 (not 19)
    expect(acceptChromeMeasurement(19, 80, 20)).toBe(20);
    // prev=20, measured=20 → keep 20 (exact match)
    expect(acceptChromeMeasurement(20, 80, 20)).toBe(20);
  });

  it("accepts jumps > 1 row even with prev", () => {
    // Real layout change: chrome grows from 20 to 25
    expect(acceptChromeMeasurement(25, 80, 20)).toBe(25);
    // Real layout change: chrome shrinks from 20 to 15
    expect(acceptChromeMeasurement(15, 80, 20)).toBe(15);
    // Boundary: exactly ±2 should pass through
    expect(acceptChromeMeasurement(22, 80, 20)).toBe(22);
    expect(acceptChromeMeasurement(18, 80, 20)).toBe(18);
  });

  it("ignores prev when it is undefined (backward compatibility)", () => {
    // No prev → behaves exactly as before
    expect(acceptChromeMeasurement(20, 80)).toBe(20);
    expect(acceptChromeMeasurement(21, 80)).toBe(21);
  });

  it("hysteresis does not bypass other guards", () => {
    // NaN still rejected even with prev
    expect(acceptChromeMeasurement(Number.NaN, 80, 20)).toBeNull();
    // Below 5 still rejected
    expect(acceptChromeMeasurement(3, 80, 20)).toBeNull();
    // Above cap still rejected
    expect(acceptChromeMeasurement(36, 80, 20)).toBeNull();
    // Infinity still rejected
    expect(acceptChromeMeasurement(Number.POSITIVE_INFINITY, 80, 20)).toBeNull();
  });
});
