/**
 * Headless smoke for i18n locale detection.
 *
 * The i18n module captures the active language at *module load*, so
 * we cannot exercise multiple languages from a single process. We
 * spawn a child Node process per case with the appropriate env
 * variables set, run a tiny TS snippet that imports the module and
 * prints ``ACTIVE_LANG`` + a sample translation, and assert against
 * stdout.
 */

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const tsxBin = resolve(__dirname, "..", "node_modules", ".bin", "tsx");
const probeScript = resolve(__dirname, "..", "src", "i18n", "index.ts");

function run(env) {
  const result = spawnSync(
    tsxBin,
    [
      "-e",
      `import { ACTIVE_LANG, t } from "${probeScript}";` +
        `console.log("LANG=" + ACTIVE_LANG);` +
        `console.log("CLEAR=" + t("command.clear.desc"));` +
        `console.log("MISS=" + t("nonexistent.key"));` +
        `console.log("FALLBACK=" + t("error.next_label"));`,
    ],
    {
      env: {
        ...process.env,
        // Wipe any pre-existing override before applying the case's env.
        BLADE_AI_LANG: "",
        LC_ALL: "",
        LANG: "",
        ...env,
      },
      encoding: "utf-8",
    },
  );
  if (result.status !== 0) {
    throw new Error(
      `child probe failed (status=${result.status}): ${result.stderr}`,
    );
  }
  const lines = result.stdout.trim().split("\n");
  const out = {};
  for (const line of lines) {
    const idx = line.indexOf("=");
    if (idx > 0) out[line.slice(0, idx)] = line.slice(idx + 1);
  }
  return out;
}

const failures = [];
function assert(cond, msg) {
  if (!cond) failures.push(msg);
}

// Case 1: BLADE_AI_LANG=en (explicit)
let out = run({ BLADE_AI_LANG: "en" });
assert(out.LANG === "en", `Case 1: BLADE_AI_LANG=en → ${out.LANG}`);
assert(out.CLEAR && out.CLEAR.startsWith("Clear"),
  `Case 1: en /clear desc should start with "Clear"; got "${out.CLEAR}"`);

// Case 2: BLADE_AI_LANG=zh
out = run({ BLADE_AI_LANG: "zh" });
assert(out.LANG === "zh", `Case 2: BLADE_AI_LANG=zh → ${out.LANG}`);
assert(out.CLEAR && out.CLEAR.includes("清空"),
  `Case 2: zh /clear desc should contain "清空"; got "${out.CLEAR}"`);

// Case 3: BLADE_AI_LANG=zh-CN (BCP 47 with dash)
out = run({ BLADE_AI_LANG: "zh-CN" });
assert(out.LANG === "zh", `Case 3: BLADE_AI_LANG=zh-CN → ${out.LANG}`);

// Case 4: BLADE_AI_LANG=zh_TW (POSIX-ish, traditional but maps to zh)
out = run({ BLADE_AI_LANG: "zh_TW" });
assert(out.LANG === "zh",
  `Case 4: BLADE_AI_LANG=zh_TW should still route to zh; got ${out.LANG}`);

// Case 5: BLADE_AI_LANG=en-US (BCP 47)
out = run({ BLADE_AI_LANG: "en-US" });
assert(out.LANG === "en", `Case 5: BLADE_AI_LANG=en-US → ${out.LANG}`);

// Case 6: BLADE_AI_LANG=ZH (uppercase)
out = run({ BLADE_AI_LANG: "ZH" });
assert(out.LANG === "zh", `Case 6: ZH (uppercase) → ${out.LANG}`);

// Case 7: BLADE_AI_LANG=fr (unknown — fallback to en)
out = run({ BLADE_AI_LANG: "fr" });
assert(out.LANG === "en", `Case 7: unknown lang "fr" → ${out.LANG} (should fallback to en)`);

// Case 8: LC_ALL drives detection when BLADE_AI_LANG unset
out = run({ LC_ALL: "zh_CN.UTF-8" });
assert(out.LANG === "zh", `Case 8: LC_ALL=zh_CN.UTF-8 → ${out.LANG}`);

// Case 9: LC_ALL=en_US.UTF-8 → en
out = run({ LC_ALL: "en_US.UTF-8" });
assert(out.LANG === "en", `Case 9: LC_ALL=en_US.UTF-8 → ${out.LANG}`);

// Case 10: LC_ALL=C → en (POSIX/C locale is English-equivalent)
out = run({ LC_ALL: "C" });
assert(out.LANG === "en", `Case 10: LC_ALL=C → ${out.LANG}`);

// Case 11: BLADE_AI_LANG overrides LC_ALL
out = run({ BLADE_AI_LANG: "zh", LC_ALL: "en_US.UTF-8" });
assert(out.LANG === "zh", `Case 11: BLADE_AI_LANG=zh overrides LC_ALL=en_US`);

// Case 12: LC_ALL not set, LANG=zh_CN
out = run({ LANG: "zh_CN" });
assert(out.LANG === "zh", `Case 12: LANG=zh_CN → ${out.LANG}`);

// Case 13: nothing set → en
out = run({});
assert(out.LANG === "en", `Case 13: no env → ${out.LANG} (should be en)`);

// Case 14: locale starts with "en_zh" (rare contrived) — must NOT route to zh
// (regression for the M9 self-check finding that ``includes("_zh")`` was broken)
out = run({ LC_ALL: "en_ZH.UTF-8" });
assert(out.LANG === "en",
  `Case 14: LC_ALL=en_ZH.UTF-8 should be en (starts with "en"); got ${out.LANG}`);

// Case 15: missing key returns key itself
out = run({});
assert(out.MISS === "nonexistent.key",
  `Case 15: missing key should return the key; got "${out.MISS}"`);

// Case 16: zh dict ALSO falls back to en for completeness — every key
// in en should resolve in zh too. We probe one and verify it's a string.
out = run({ BLADE_AI_LANG: "zh" });
assert(out.FALLBACK && out.FALLBACK !== "error.next_label",
  `Case 16: zh dict has error.next_label translation; got "${out.FALLBACK}"`);

// Done.
if (failures.length > 0) {
  console.error("\n--- FAILURES ---");
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log("✓ all 16 i18n locale-detect cases passed");
process.exit(0);
