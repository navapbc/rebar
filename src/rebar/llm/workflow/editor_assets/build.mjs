/**
 * Build the editor bundle: src/index.js (+ its CSS imports) -> dist/editor.js +
 * dist/editor.css. The committed dist/ is what editor.py serves and what ships in the
 * wheel, so end users never need npm — only a contributor changing the editor rebuilds.
 */
import * as esbuild from "esbuild";

await esbuild.build({
  entryPoints: ["src/index.js"],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  outfile: "dist/editor.js",
  loader: {
    ".css": "css",
    // Inline the bpmn icon font + any svg so dist/editor.css is fully self-contained
    // (one css file to serve, no font asset paths to wire up).
    ".woff": "dataurl",
    ".woff2": "dataurl",
    ".ttf": "dataurl",
    ".eot": "dataurl",
    ".svg": "dataurl",
  },
  jsx: "automatic",
  jsxImportSource: "@bpmn-io/properties-panel/preact",
  minify: true,
  sourcemap: false,
  logLevel: "info",
});
