# Code Workspace + LSP — Smoke Test

A manual checklist for verifying the Monaco editor swap and the LSP
bridge end-to-end in a Tauri build. Run this after pulling a new
version that touches any of:

- `backend/static/lib/monaco/` (vendored Monaco bundle)
- `backend/static/js/codeWorkspace.js` (Monaco mount + LSP wiring)
- `backend/src/lsp_bridge.py` (subprocess management, path confinement)
- `backend/routes/lsp_routes.py` (WebSocket route + availability check)
- `backend/requirements.txt` (`python-lsp-server[all]` added)

Estimated time: **10–15 minutes** for a clean environment. If you
already have pylsp installed it's closer to 5.

---

## 0. Prerequisites

You'll need these on the machine you're testing from:

| What | Why | How to install |
|---|---|---|
| Python 3.10+ | Backend runtime | Already bundled by Tauri (`resources/python/`) |
| `python-lsp-server[all]` | The Python language server pylsp | `pip install "python-lsp-server[all]"` inside the Tauri-bundled venv, or `pip install` system-wide |
| (optional) `typescript-language-server` | TS/JS language server | `npm install -g typescript-language-server` |
| (optional) `rust-analyzer` | Rust language server | `rustup component add rust-analyzer` |

Without pylsp, the Monaco editor and the rest of the Code Workspace
will still work — diagnostics and hover just won't be available. The
LSP status pill (see §5) will read **"LSP: off"** instead of
**"LSP: ok"**.

## 1. Build and run

```sh
# from the repo root
cd src-tauri
cargo tauri dev          # for a fast dev loop
# or
cargo tauri build        # for a release build
```

Open the app. The window should appear with the default chat view.

## 2. Open the Code Workspace

1. Click **Code** in the left sidebar (the icon-rail button, or the
   full sidebar entry labeled "Code").
2. A modal opens. If you've never set a workspace, click **Pick
   workspace…** and choose any folder. A good test target is a small
   Python project (or even a single `test.py` file you create on the
   fly).

**Expected:** The file tree fills with the folder's contents.

**Failure mode:** Tree is empty and the file search returns nothing.
→ The workspace root was rejected by the server. Open the backend log
(`logs/devspace.log` in the Tauri data dir, or the stdout in dev) and
look for `_resolve_tool_path_in_workspace` errors. The most common
cause is a Windows path with mixed separators.

## 3. Open a Python file

Click any `.py` file in the tree.

**Expected:**
- The right-hand editor pane becomes a **Monaco editor** (not the old
  text-only view). You'll see:
  - Syntax-coloured keywords (`def`, `class`, `import`, etc.)
  - A line-number gutter on the left
  - No `<textarea>` scrollbar (Monaco owns the scroll)
  - A path label at the top: `path/to/your/file.py`
- The LSP status pill (next to the path label) reads **"LSP: pending"**
  briefly, then **"LSP: ok"** in green within ~1 second.

**Failure mode A:** Editor is still the text-only view, no line numbers.
→ Monaco failed to load. Open DevTools (`Ctrl+Shift+I` in the app or
right-click → Inspect Element) and look for `[codeWorkspace] Monaco init
threw:` or `could not load Monaco AMD loader`. The most common cause is
the vendored `backend/static/lib/monaco/` directory being incomplete —
re-extract from the npm tarball per `backend/static/lib/monaco/README.md`.

**Failure mode B:** Editor is Monaco but pill stays "LSP: pending" or
shows "LSP: off" in yellow.
→ pylsp is not installed. Run `pip install "python-lsp-server[all]"`
in the Tauri-bundled venv and restart the app. The venv path is
`resources/python/.venv/` (or similar) inside the Tauri data dir on
Windows; on macOS/Linux it's a user-data subfolder.

## 4. Type something — verify diagnostics

In the open Python file, type a deliberate syntax error:

```python
def broken_function(:
    return 1
```

Wait ~1 second.

**Expected:**
- A red squiggle appears under the `(` on line 1.
- Hover the squiggle: a tooltip reads something like
  `E999 SyntaxError: invalid syntax`.

**Failure mode:** No squiggle after 2 seconds.
→ Open DevTools console and look for `[lsp]` lines. If you see
`LSP initialize failed:`, the bridge is reaching the server but the
handshake is failing. The most common cause is `python-lsp-server`
missing a plugin (the `[all]` extra is what pulls in pyflakes, which
provides the syntax diagnostics).

## 5. Verify hover and completion

With the cursor on a known function call, e.g. `os.path.join`:

**Hover:** Move the mouse over `join` and wait ~1 second.
**Expected:** A tooltip appears with the function signature and a
short docstring.

**Completion:** Type `os.pa` and press `Ctrl+Space`.
**Expected:** A completion list appears with `os.path` and related
entries.

**Failure mode:** Nothing appears.
→ pylsp works (we got diagnostics in §4) but the providers aren't
firing. Check DevTools for the `[codeWorkspace]` log lines that
mention `registerXProvider` — if those are absent, Monaco's
`monaco.languages` is not in the global namespace, which means the
AMD bundle didn't fully load.

## 6. Verify go-to-definition (single file)

Place the cursor on a function call inside the open file, e.g.
`some_function_call()` and press **F12** (or right-click → **Go to
Definition**).

**Expected:** The cursor jumps to the function's `def` line in the
same file.

**Failure mode:** Nothing happens.
→ Check that `_lspState` is `'ok'` in DevTools
(`window._lspSocket?.readyState` should be `1` — OPEN).
If the state is correct but the keybinding doesn't fire, it's a
Monaco focus issue — click in the editor first.

## 7. Verify cross-file go-to-definition (requires a real project)

1. Create a second Python file in the same workspace, e.g.
   `utils.py` with `def helper(): return 42`.
2. In another file, `import` it and call `helper()`. Place the cursor
   on `helper` and press F12.

**Expected:** The Code Workspace modal navigates to `utils.py` and
the cursor lands on the `def helper` line. The path label updates
to `utils.py`.

**Failure mode:** Pill says "LSP: off" or no navigation happens.
→ pylsp is running but the goto-def is not finding the target. This
is the area most likely to have regressions — file a bug with the
console output.

## 8. TypeScript (optional)

If you installed `typescript-language-server` (prerequisites), open
any `.ts` file.

**Expected:** Same as Python — Monaco with TS syntax highlighting,
LSP status pill green, diagnostics on syntax errors, completion on
`Ctrl+Space`.

**Failure mode:** Pill says "LSP: off".
→ The `typescript-language-server` binary isn't on PATH. On Windows
the bridge looks for `typescript-language-server.cmd` and
`typescript-language-server.exe`; verify with
`where typescript-language-server` in cmd.

## 9. Rust (optional)

Same as TypeScript. Open a `.rs` file. Without rust-analyzer the
pill is yellow; with it, full LSP features.

## 10. Save and reload

Press `Ctrl+S` while editing a file.

**Expected:**
- The save indicator clears (no orange "unsaved" dot).
- The file is written to disk (verify with `cat` or your editor).
- If the file had a syntax error, the red squiggle is cleared and
  the LSP server re-analyzes with the new content.

**Failure mode:** Save error toast.
→ Check the backend log for `Save failed:` lines. The most common
cause is a workspace permission issue.

## 11. Toggle "Enable LSP" in the toolbar

Click the **Enable LSP** switch in the editor toolbar (next to the
status pill).

**Expected:** The pill turns yellow with text "LSP: off" within
~1 second. Re-enable: the pill returns to green.

The setting persists across reloads (localStorage key
`code.lsp_enabled`).

## 12. Test the "Default to Agent mode" persistence

In the chat input bar, click the **Chat** button (the bottom-right
toggle next to the model selector). Reload the page.

**Expected:** The toggle is still on **Chat**.

Click **Agent**. Reload. → Still on **Agent**.

**Why this matters:** the v1 default is "agent" for fresh users. Once
a user makes an explicit choice, that choice persists — there's no
way for a future code change to override it without a localStorage
clear.

---

## What to file if something breaks

Each failure mode above points to a likely root cause. When filing
a bug, include:

1. The exact step number where it failed.
2. The DevTools console output (right-click → Inspect → Console).
3. The backend log entry that matches (search for the file path).
4. The output of:
   ```sh
   pip show python-lsp-server
   # and
   python -c "import pylsp; print(pylsp.__version__)"
   ```

The bridge layers (route → registry → session → framer) are
independently logged. A failure at any layer produces a distinct
log line that pinpoints the layer:

| Layer | Log prefix | What it tells you |
|---|---|---|
| Route | `lsp:` (no path qualifier) | WS upgrade / auth / availability check failed |
| Registry | `lsp:` with `(lang, root)` | Subprocess spawn / reap / refcount |
| Session | `lsp {lang} stderr:` | Language server's stderr (e.g. config errors) |

A clean run produces no log lines from any of these layers beyond
"lsp: starting python for cwd=...".
