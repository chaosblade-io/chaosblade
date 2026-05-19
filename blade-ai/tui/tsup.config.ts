import { writeFileSync } from "node:fs";
import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/cli.tsx"],
  format: ["esm"],
  target: "node22",
  // Drop a marker package.json next to dist/cli.js. Without it, Node 22
  // sees a bare ``.js`` file with ESM syntax and walks UP the directory
  // tree looking for a closest ``package.json`` to determine module
  // type. In a PyInstaller bundle the closest match might be the
  // user's own ``~/package.json`` (CommonJS by default), triggering
  // the ``MODULE_TYPELESS_PACKAGE_JSON`` warning + a re-parse penalty
  // on every cold start. The marker eliminates the lookup ambiguity.
  onSuccess: async () => {
    writeFileSync("dist/package.json", '{"type":"module"}\n');
  },
  banner: {
    // The bundle is ESM. Several transitive deps (rooted in
    // ``react-reconciler``) call ``require()`` at runtime — esbuild
    // wraps each into a "Dynamic require" stub that throws unless a
    // module-scope ``require`` symbol is in the closure. We polyfill
    // it with ``createRequire`` so the stubs find a real implementation.
    // Same shim is required for ``__filename``/``__dirname`` for any
    // CJS lib that needs them.
    //
    // Compressed to two physical lines (shebang + one polyfill line):
    // each empty source-map row costs a ``;`` in the mappings field,
    // so a fatter banner bloats the .map file slightly. The runtime
    // is identical either way.
    js:
      "#!/usr/bin/env node\n" +
      'import{createRequire as __cr}from"node:module";' +
      'import{fileURLToPath as __fu}from"node:url";' +
      'import{dirname as __dn}from"node:path";' +
      "const require=__cr(import.meta.url);" +
      "const __filename=__fu(import.meta.url);" +
      "const __dirname=__dn(__filename);",
  },
  bundle: true,
  minify: true,
  sourcemap: true,
  clean: true,
  splitting: false,
  shims: true,
  // Ink + react ecosystem ships ESM-only with peer deps; bundling them is
  // the whole point of this CLI build (single self-contained dist/cli.js).
  noExternal: [/.*/],
  esbuildPlugins: [
    // ``react-devtools-core`` is an optional dev-only import inside Ink's
    // ``devtools.js`` (only loaded when ``DEV=true``). It isn't a peer
    // dep, so neither ``external`` nor ``noExternal`` cleanly excludes
    // it once we force-bundle everything else. We resolve it to a
    // virtual stub instead, which keeps the dead-code path satisfied.
    {
      name: "stub-react-devtools-core",
      setup(build) {
        build.onResolve({ filter: /^react-devtools-core$/ }, () => ({
          path: "react-devtools-core",
          namespace: "stub-rdc",
        }));
        build.onLoad({ filter: /.*/, namespace: "stub-rdc" }, () => ({
          contents: "export default {};",
          loader: "js",
        }));
      },
    },
  ],
});
