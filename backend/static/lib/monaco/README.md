# Vendored Monaco editor bundle

This directory contains the AMD bundle of [Monaco Editor](https://github.com/microsoft/monaco-editor)
used by `backend/static/js/codeWorkspace.js` for the in-app Code editor.
The frontend is bundler-free (raw ESM served as static files), so the
entire Monaco tree is vendored here rather than loaded from a CDN at
runtime — that keeps the desktop build (Tauri) working offline and
sidesteps any CSP/network restrictions on hosted deployments.

## Source

- **Upstream:** https://github.com/microsoft/monaco-editor
- **Version pinned:** 0.52.2
- **Variant:** `min/vs/` (the AMD-bundled distribution; matches the
  layout Monaco's official `loader.js` expects)
- **License:** MIT (Microsoft Corporation). See
  `https://github.com/microsoft/vscode/blob/main/LICENSE.txt`.

## What's here

The `vs/` subdirectory is the verbatim copy of `node_modules/monaco-editor/min/vs/`
from the upstream tarball. It contains:

- `loader.js` — the AMD loader (entry point, loaded via `<script>`)
- `editor/editor.main.js` — the editor factory (3.7 MB minified)
- `basic-languages/` — per-language syntax tokenizers (Python, JS/TS,
  JSON, HTML, CSS, Markdown, Rust, Go, Java, C/C++, etc.)
- `language/` — language services (typescript worker, json worker, html
  worker, css worker)
- `base/`, `platform/` — Monaco's runtime (event loop, layout, etc.)
- NLS message bundles for several locales (`nls.messages.<lang>.js`)

The bundle is ~14 MB on disk; gzip-compressed over the wire it's ~3 MB.
Monaco's worker files (TypeScript, JSON, HTML, CSS) spawn as Web Workers
via `MonacoEnvironment.getWorker`, so the editor fully loads even when
the page is served over plain HTTP.

## How it's loaded

`codeWorkspace.js` injects a `<script src="/static/lib/monaco/vs/loader.js">`
on first file open, then calls:

```js
window.require.config({ paths: { vs: '/static/lib/monaco/vs' } });
window.require(['vs/editor/editor.main'], () => { /* editor ready */ });
```

Failure (network/CSP/missing file) is caught and the editor silently
falls back to the legacy `<textarea>` + `highlight.js` overlay that
existed before. See `_ensureMonaco()` and `_mountMonacoForFile()` in
`codeWorkspace.js`.

## How to refresh this bundle

When bumping Monaco to a new version, replace the contents of `vs/`
with the new `min/vs/` tree. Suggested procedure:

```sh
# 1. Fetch the upstream tarball (pinned version, no `latest`).
curl -L -o /tmp/monaco.tgz \
  https://registry.npmjs.org/monaco-editor/-/monaco-editor-0.52.2.tgz
# 2. Extract just the AMD bundle.
mkdir -p /tmp/monaco-extract
tar xzf /tmp/monaco.tgz -C /tmp/monaco-extract package/min
# 3. Replace the vendored tree in place.
rm -rf backend/static/lib/monaco/vs
mv /tmp/monaco-extract/package/min/vs backend/static/lib/monaco/vs
# 4. Verify: open the Code workspace in the app and edit a file.
#    Syntax highlighting + LSP diagnostics should appear within ~1s of
#    first file open.
```

If you bump to a major version (1.x+), the AMD API may have changed —
check Monaco's migration notes and re-test `_ensureMonaco()`,
`_mountMonacoForFile()`, and the LSP provider hooks
(`_registerLspProviders`) in `codeWorkspace.js`.

## Why vendor instead of CDN?

Three reasons, in order of importance:

1. **Tauri desktop build is offline-only by default.** The webview
   cannot reach `cdn.jsdelivr.net` from a packaged desktop build unless
   the operator opens a hole. Vendoring keeps the desktop UX identical
   to the hosted one.
2. **No CSP host-list to maintain.** Adding `cdn.jsdelivr.net` to a
   `script-src` allowlist is a recurring source of regressions when
   the upstream URL changes or is blocked by corporate proxies.
3. **Version pinning.** The bundle is checked into git, so the editor
   bits that ship with v1.0 are exactly the same bits that ship with
   v1.1 — no "works on my machine" surprises when a CDN rotates the
   version.

The cost is ~14 MB of repo size. That's a one-time hit; subsequent
updates are also 14 MB and the cost amortizes.
