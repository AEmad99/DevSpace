// static/js/desktopNotifications.js
// Bridge the frontend's existing Web Notification calls to native OS
// notifications when running inside the DevSpace (Tauri) desktop shell.
//
// Why: the app already fires `new Notification(...)` in several places
// (research/chat completion, calendar reminders, notes, tasks, email). In a
// plain browser that uses the Web Notifications API. But inside a Tauri
// WebView2 window there is no permission prompt and the Web API stays
// permanently "default" — so none of those notifications ever appear in the
// desktop app. The Tauri notification plugin is the supported path there.
//
// Strategy: only when `window.__TAURI__.notification` is present (i.e. we're in
// the desktop shell), replace `window.Notification` with a thin shim that:
//   - reports a cached permission state synchronously (the existing call sites
//     read `Notification.permission === 'granted'` synchronously), and
//   - routes construction to the plugin's `sendNotification`.
// We proactively request permission on load so the cached state becomes
// "granted" before any long task finishes. In a normal browser this module is
// a no-op and the native Web API is left untouched.

const tauriNotif = (typeof window !== 'undefined' && window.__TAURI__)
  ? window.__TAURI__.notification
  : null;

if (tauriNotif && typeof tauriNotif.sendNotification === 'function') {
  // Cached permission so the synchronous `Notification.permission` getter the
  // existing code relies on can answer immediately. Resolved async below.
  let _perm = 'default';

  const send = (title, options = {}) => {
    try {
      tauriNotif.sendNotification({
        title: title != null ? String(title) : 'DevSpace',
        body: options && options.body ? String(options.body) : undefined,
      });
    } catch (_) { /* never let a notification failure break the caller */ }
  };

  // Shim standing in for the Web Notification constructor. Existing sites do
  // `new Notification(title, { body, icon, tag })` purely for the side effect,
  // so we fire-and-forget; the returned instance just needs to not throw.
  class DesktopNotification {
    constructor(title, options = {}) {
      this.title = title;
      this.options = options;
      send(title, options);
    }
    // No-ops for API compatibility with code that pokes at the instance.
    close() {}
    addEventListener() {}
    removeEventListener() {}

    static get permission() { return _perm; }

    static async requestPermission() {
      try {
        let granted = false;
        if (typeof tauriNotif.isPermissionGranted === 'function') {
          granted = await tauriNotif.isPermissionGranted();
        }
        if (!granted && typeof tauriNotif.requestPermission === 'function') {
          const res = await tauriNotif.requestPermission();
          granted = res === 'granted' || res === true;
        } else if (granted) {
          // already granted
        } else {
          // No request API available; assume the plugin can send.
          granted = true;
        }
        _perm = granted ? 'granted' : 'denied';
      } catch (_) {
        _perm = 'denied';
      }
      return _perm;
    }
  }

  // Install the shim and resolve permission immediately so the synchronous
  // `=== 'granted'` checks at the call sites start passing.
  try {
    Object.defineProperty(window, 'Notification', {
      value: DesktopNotification,
      configurable: true,
      writable: true,
    });
  } catch (_) {
    // If the property is locked down for some reason, fall back to assignment.
    try { window.Notification = DesktopNotification; } catch (_) {}
  }
  DesktopNotification.requestPermission();
}
