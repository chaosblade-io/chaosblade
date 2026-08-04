// ESLint flat config.
//
// Deliberately minimal: the `"lint": "eslint src"` script shipped in the
// very first TUI commit but eslint itself was never installed and no
// config ever existed, so `npm run lint` had never run. Rather than
// invent a house style retroactively, this config enables only the
// upstream recommended sets — the same correctness-oriented rules the
// TypeScript and ESLint teams ship as defaults.
//
// `tsconfig.json` already runs in strict mode, so type errors are caught
// by `npm run typecheck`. What this adds on top is the class of problems
// the compiler does not flag: unused code, unsafe empty catches,
// misuse of promises, and so on.
//
// Type-aware rules (typescript-eslint's `recommendedTypeChecked`) are NOT
// enabled: they require a full type graph per lint run and would flag a
// large body of pre-existing code. Enable them deliberately, as a
// separate decision, once the base set is clean.

import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default tseslint.config(
  {
    // Build output and dependencies are never linted.
    ignores: ["dist/**", "node_modules/**", "**/*.d.ts"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    // Two `eslint-disable-next-line react-hooks/exhaustive-deps` comments
    // already exist in the tree (BootRunner, WizardCard). Registering the
    // plugin is what makes those suppressions resolve instead of erroring
    // as an unknown rule.
    plugins: { "react-hooks": reactHooks },
    languageOptions: {
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Ink components take `children` implicitly; React 19's automatic
      // JSX runtime means `React` need not be in scope.
      "no-undef": "off",
      // This is a terminal UI: matching ANSI escapes (`\x1b[2K`) and
      // stripping C0 control bytes from pasted input is core domain work,
      // not a mistake. The rule fires on `\x..` escapes just as it does on
      // raw bytes, so there is no spelling that satisfies it — every hit
      // in this tree is a false positive.
      "no-control-regex": "off",
      // Ink owns stdout: a stray `console.log` is written outside the render
      // tree and corrupts the current frame. Exactly one deliberate
      // `console.warn` exists (useStream's unknown-phase guard) and it
      // already carries a disable comment — enabling the rule is what makes
      // that annotation meaningful rather than inert.
      "no-console": "error",
      // `_`-prefixed bindings are the codebase's existing signal for
      // "deliberately unused" (see `Header`'s `_props`, `useInputHistory`'s
      // `_current`, `InputPrompt`'s `_onExit`). Honour it instead of
      // deleting parameters that exist to document a signature.
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },
  {
    // Test files legitimately use non-null assertions and loose typing
    // around mock fixtures.
    files: ["**/*.test.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
);
