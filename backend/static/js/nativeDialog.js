// static/js/nativeDialog.js
//
// Thin bridge to the host's native OS dialogs when DevSpace runs inside the
// Tauri desktop shell. The backend is served over http://127.0.0.1:<port>/, so
// the only way to reach the real OS folder picker is the Tauri IPC bridge that
// Tauri injects into the webview (see src-tauri/capabilities/default.json,
// which grants `dialog:allow-open` to this remote origin).
//
// pickDirectory() is intentionally defensive: it returns a tri-state so callers
// can fall back to the in-app directory browser when we're NOT under Tauri
// (e.g. opened in a plain browser during dev) or if the native call is blocked.
//   { available: true,  path: '<chosen folder>' }  → user picked a folder
//   { available: true,  path: null }               → user cancelled the dialog
//   { available: false, path: null }               → no native picker; fall back

// Resolve a low-level Tauri `invoke` regardless of which global Tauri exposes.
function _invoke() {
  const T = window.__TAURI__;
  if (T && T.core && typeof T.core.invoke === 'function') return T.core.invoke.bind(T.core);
  const internals = window.__TAURI_INTERNALS__;
  if (internals && typeof internals.invoke === 'function') return internals.invoke.bind(internals);
  return null;
}

// True when running inside the Tauri webview (the desktop app), false in a
// regular browser.
export function isTauri() {
  return !!(window.__TAURI__ || window.__TAURI_INTERNALS__);
}

// Normalize whatever the dialog command returns into a single path string|null.
// A single-directory open returns a string (or null); be tolerant of the
// array / { path } shapes other modes/versions use.
function _toPath(res) {
  if (typeof res === 'string') return res || null;
  if (Array.isArray(res)) return (typeof res[0] === 'string' && res[0]) ? res[0] : null;
  if (res && typeof res === 'object' && typeof res.path === 'string') return res.path || null;
  return null;
}

/**
 * Open the native OS "choose a folder" dialog.
 * @param {string} [defaultPath] - folder to start in (e.g. the current
 *        workspace) so the picker doesn't always open at the home directory.
 * @returns {Promise<{available: boolean, path: string|null}>}
 */
export async function pickDirectory(defaultPath) {
  const options = { directory: true, multiple: false, title: 'Select folder' };
  if (defaultPath) options.defaultPath = defaultPath;

  try {
    // Preferred: the high-level plugin API injected when withGlobalTauri is on.
    const T = window.__TAURI__;
    if (T && T.dialog && typeof T.dialog.open === 'function') {
      const res = await T.dialog.open(options);
      return { available: true, path: _toPath(res) };
    }
    // Fallback: call the plugin command straight through the IPC bridge.
    const invoke = _invoke();
    if (invoke) {
      const res = await invoke('plugin:dialog|open', { options });
      return { available: true, path: _toPath(res) };
    }
  } catch (e) {
    // The bridge exists but the call failed (capability mismatch, user-level
    // error, …). Treat as "no native picker" so the caller shows the in-app
    // browser rather than leaving the user stuck.
    console.warn('[nativeDialog] native folder picker failed, falling back:', e);
    return { available: false, path: null };
  }

  // Not running under Tauri at all.
  return { available: false, path: null };
}

/**
 * Open a URL in the user's default browser.
 *
 * Inside the Tauri desktop shell the webview is navigated to the backend's
 * http://127.0.0.1:<port>/ origin, and there `window.open(url, '_blank')` is a
 * silent no-op (Wry doesn't spawn a browser window for it). That's why links
 * meant to open externally — notably the deep-research "Visual Report" button —
 * appeared to do nothing in the desktop app. Route those through the Tauri
 * opener plugin instead (capabilities/default.json grants `opener:allow-open-url`
 * for the loopback origin). In a plain browser we keep native window.open.
 *
 * @param {string} url - absolute or root-relative URL. Relative URLs (some
 *        callers pass an empty apiBase) are resolved against the current origin,
 *        since the OS / opener plugin needs a full URL.
 */
export async function openExternalUrl(url) {
  let abs = url;
  try { abs = new URL(url, window.location.href).href; } catch (_) {}

  if (isTauri()) {
    try {
      const invoke = _invoke();
      if (invoke) {
        await invoke('plugin:opener|open_url', { url: abs });
        return;
      }
    } catch (e) {
      // Capability/scope mismatch or user-level failure — fall through to
      // window.open so we at least try (and so dev-in-browser keeps working).
      console.warn('[nativeDialog] opener plugin failed, falling back to window.open:', e);
    }
  }
  window.open(abs, '_blank', 'noopener');
}

// Expose globally too, so call sites that don't import this module can reach it.
if (typeof window !== 'undefined') window.openExternalUrl = openExternalUrl;

/**
 * Open a Deep Research visual report.
 *
 * The report opens in the user's EXTERNAL default browser, which carries none
 * of the in-app session cookie — a plain GET would 401 "Not authenticated". So
 * we first mint a short-lived report token from the (cookie-authed) WebView and
 * open the token-bearing URL. If minting fails we fall back to the bare URL so
 * cookie-capable contexts (dev-in-browser) still work.
 *
 * @param {string} id - research session id
 * @param {string} [apiBase] - API base; defaults to window.API_BASE or ''
 */
export async function openResearchReport(id, apiBase) {
  const base = (apiBase != null ? apiBase : (window.API_BASE || ''));
  let url = `${base}/api/research/report/${encodeURIComponent(id)}`;
  try {
    const res = await fetch(
      `${base}/api/research/report-link/${encodeURIComponent(id)}`,
      { method: 'POST', credentials: 'same-origin' },
    );
    if (res.ok) {
      const data = await res.json();
      if (data && data.url) url = `${base}${data.url}`;
    } else {
      console.warn('[nativeDialog] report-link mint returned', res.status);
    }
  } catch (e) {
    console.warn('[nativeDialog] report-link mint failed, opening bare URL:', e);
  }
  return openExternalUrl(url);
}

if (typeof window !== 'undefined') window.openResearchReport = openResearchReport;

export default { isTauri, pickDirectory, openExternalUrl, openResearchReport };
