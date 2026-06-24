// DevSpace — Tauri desktop shell.
//
// Launches the Python/FastAPI backend as a child process on a free loopback
// port, waits until it accepts connections, then points the webview at it.
// Until then the window shows a local splash (../dist/index.html). Auth is
// enabled, so first launch shows the backend's first-run setup (create the
// local owner account).
//
// Self-contained: an installed build ships a relocatable CPython runtime and the
// backend source as bundled resources (see tauri.conf.json `bundle.resources`
// and backend_paths()), so it runs on any machine with no dev-machine deps. User
// data lives in a per-user appdata dir via ODYSSEUS_DATA_DIR. Paths are also
// overridable via DEVSPACE_PYTHON / DEVSPACE_BACKEND_DIR for testing; `cargo
// tauri dev` falls back to the repo's backend + odysseus-ref venv.

use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Emitter, Manager};

/// Holds the backend child so we can terminate it when the app exits.
///
/// On Windows we also pin a Job Object handle (`_job`) for the app's lifetime.
/// The job is configured with KILL_ON_JOB_CLOSE, so when this process dies for
/// ANY reason — graceful exit, panic, or the dev server being Ctrl+C'd — the OS
/// terminates the whole backend tree (uvicorn AND every MCP-server subprocess
/// it spawned). `child.kill()` alone only reaps the direct uvicorn process and
/// would orphan the MCP-server grandchildren, which is what was leaking dozens
/// of `python.exe` processes across restarts.
struct BackendProcess {
    child: Mutex<Option<Child>>,
    // Kept alive solely so the job handle isn't closed early. `usize` because a
    // raw HANDLE isn't Send/Sync; we never dereference it after assignment.
    #[cfg(windows)]
    _job: Mutex<Option<usize>>,
}

impl BackendProcess {
    fn new() -> Self {
        BackendProcess {
            child: Mutex::new(None),
            #[cfg(windows)]
            _job: Mutex::new(None),
        }
    }
}

/// Create a Job Object set to kill all its processes (and their descendants)
/// when the last handle to the job closes, assign `child` to it, and return the
/// job handle as a `usize` to keep alive for the process lifetime. Returns
/// `None` on any failure, in which case we fall back to plain `child.kill()`.
#[cfg(windows)]
struct JobGuard(Option<usize>);

#[cfg(windows)]
impl Drop for JobGuard {
    fn drop(&mut self) {
        if let Some(h) = self.0.take() {
            if h != 0 {
                unsafe {
                    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
                    CloseHandle(h as HANDLE);
                }
            }
        }
    }
}

#[cfg(windows)]
fn assign_to_kill_on_close_job(child: &Child) -> Option<usize> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let mut job = JobGuard(Some(
            CreateJobObjectW(std::ptr::null(), std::ptr::null()) as usize,
        ));
        if job.0 == Some(0) {
            log::warn!("CreateJobObject failed; backend tree won't be auto-killed on exit");
            return None;
        }

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = SetInformationJobObject(
            job.0.unwrap() as HANDLE,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if ok == 0 {
            log::warn!("SetInformationJobObject failed; backend tree won't be auto-killed");
            return None;
        }

        if AssignProcessToJobObject(job.0.unwrap() as HANDLE, child.as_raw_handle() as HANDLE) == 0
        {
            log::warn!("AssignProcessToJobObject failed; backend tree won't be auto-killed");
            return None;
        }

        log::info!("Backend (pid {}) assigned to kill-on-close job object", child.id());
        Some(job.0.take().unwrap())
    }
}

/// Ask the OS for an unused TCP port on loopback.
fn pick_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|addr| addr.port())
        .expect("could not allocate a local port for the backend")
}

/// Resolve (python_exe, backend_dir).
///
/// Priority:
///   1. Env overrides (DEVSPACE_PYTHON / DEVSPACE_BACKEND_DIR) — for testing.
///   2. Bundled resources — a self-contained install ships its own relocatable
///      CPython under `<resources>/python/python.exe` and the backend source
///      under `<resources>/backend`. This is what makes the installed app run
///      on any machine with no dev-machine dependency.
///   3. Dev fallback — the repo's `../backend` + the odysseus-ref venv, anchored
///      to this crate's compile-time location, for `cargo tauri dev`.
fn backend_paths(app: &tauri::AppHandle) -> (std::path::PathBuf, std::path::PathBuf) {
    // 1. Explicit env overrides win.
    let env_python = std::env::var_os("DEVSPACE_PYTHON").map(std::path::PathBuf::from);
    let env_backend = std::env::var_os("DEVSPACE_BACKEND_DIR").map(std::path::PathBuf::from);

    // 2. Bundled resources (installed app). resource_dir() points at the
    //    install's resources root; the python runtime + backend are copied
    //    there by tauri.conf.json `bundle.resources`. Tauri's exact on-disk
    //    layout depends on whether resources were declared as a map (clean
    //    `python/`, `backend/` targets) or a glob (path preserved under
    //    `resources/...`), so probe both rather than hard-coding one.
    //    Also probe the install root and the exe's parent dir, since some
    //    installer builds or corrupted installs leave the bundled files there.
    let (res_python, res_backend) = match app.path().resource_dir() {
        Ok(res) => {
            let exe_parent = std::env::current_exe()
                .ok()
                .and_then(|p| p.parent().map(|p| p.to_path_buf()));
            // Collect every plausible base dir the bundler might have used.
            let mut bases: Vec<std::path::PathBuf> =
                std::iter::once(res.clone()).chain(exe_parent.clone()).collect();
            // Also try a "resources/" sibling of the exe parent in case the
            // NSIS template put the assets there instead of under the
            // standard `resources/` returned by `resource_dir()`.
            if let Some(p) = exe_parent
                .as_ref()
                .and_then(|p| p.parent().map(|p| p.join("resources")))
            {
                bases.push(p);
            }

            let py = bases
                .iter()
                .map(|b| b.join("python").join("python.exe"))
                .find(|p| p.exists());
            let be = bases
                .iter()
                .flat_map(|b| [b.join("backend"), b.join("resources").join("backend")])
                .find(|p| p.join("app.py").exists());

            if py.is_none() {
                log::error!(
                    "Bundled Python not found. Probed: {}",
                    bases
                        .iter()
                        .map(|b| format!("{}/python/python.exe", b.display()))
                        .collect::<Vec<_>>()
                        .join(", ")
                );
            }
            if be.is_none() {
                log::error!(
                    "Bundled backend not found. Probed: {}",
                    bases
                        .iter()
                        .flat_map(|b| {
                            [
                                format!("{}/backend/app.py", b.display()),
                                format!("{}/resources/backend/app.py", b.display()),
                            ]
                        })
                        .collect::<Vec<_>>()
                        .join(", ")
                );
            }
            (py, be)
        }
        Err(_) => (None, None),
    };

    // 3. Dev fallback anchored to the crate location.
    let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| manifest_dir.clone());
    let dev_backend = repo_root.join("backend");
    let dev_python = repo_root
        .join("odysseus-ref")
        .join("venv")
        .join("Scripts")
        .join("python.exe");

    let python = env_python
        .or(res_python)
        .unwrap_or(dev_python);
    let backend_dir = env_backend
        .or(res_backend)
        .unwrap_or(dev_backend);
    (python, backend_dir)
}

/// Per-user, writable data directory for the backend (DB, settings, RAG index,
/// uploads). Kept OUT of the install/resources tree so it survives reinstalls
/// and works even when the app is installed to a read-only location. The backend
/// reads this via the ODYSSEUS_DATA_DIR env var (see src/constants.py).
fn backend_data_dir(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    let dir = app.path().app_data_dir().ok()?.join("data");
    if let Err(e) = std::fs::create_dir_all(&dir) {
        log::warn!("could not create data dir {}: {e}", dir.display());
        return None;
    }
    Some(dir)
}

/// Bundled FastEmbed model cache (the local embedding model, pre-downloaded).
/// Pointing the backend at this via FASTEMBED_CACHE_PATH means semantic memory /
/// RAG / tool selection work fully OFFLINE on first launch — no model download.
/// Returns None in dev (the backend then uses its data-dir cache as before).
fn bundled_fastembed_cache(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    let res = app.path().resource_dir().ok()?;
    [
        res.join("fastembed_cache"),
        res.join("resources").join("fastembed_cache"),
    ]
    .into_iter()
    .find(|p| p.is_dir())
}

/// Where we record the PID of the backend we spawned, so a later run can clean
/// up a tree that was orphaned by an unclean exit (the rare case where the job
/// object failed to bind). Lives in the temp dir; one line, the PID.
fn backend_pid_file() -> std::path::PathBuf {
    std::env::temp_dir().join("devspace_backend.pid")
}

/// Belt-and-suspenders cleanup: if a previous run left a PID file, kill that
/// backend process and its whole subtree before we start a new one. Guarded so
/// it only fires when the recorded PID is *still a live `python.exe`* — this
/// prevents nuking an unrelated process that happened to reuse the PID. The job
/// object should already have handled teardown; this only matters if it didn't.
#[cfg(windows)]
fn sweep_stale_backend() {
    use std::collections::HashMap;
    use windows_sys::Win32::Foundation::{CloseHandle, FALSE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};

    let path = backend_pid_file();
    let Ok(contents) = std::fs::read_to_string(&path) else { return };
    // One-shot: drop the file regardless of what happens below.
    let _ = std::fs::remove_file(&path);
    let Ok(root) = contents.trim().parse::<u32>() else { return };

    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return;
        }

        // Snapshot every process as (pid, parent_pid, exe_name).
        let mut procs: Vec<(u32, u32, String)> = Vec::new();
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        if Process32FirstW(snap, &mut entry) != 0 {
            loop {
                let len = entry
                    .szExeFile
                    .iter()
                    .position(|&c| c == 0)
                    .unwrap_or(entry.szExeFile.len());
                let name = String::from_utf16_lossy(&entry.szExeFile[..len]);
                procs.push((entry.th32ProcessID, entry.th32ParentProcessID, name));
                if Process32NextW(snap, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snap);

        // PID-reuse guard: only proceed if the recorded PID is still a python.exe.
        let root_is_python = procs
            .iter()
            .any(|(pid, _, name)| *pid == root && name.eq_ignore_ascii_case("python.exe"));
        if !root_is_python {
            return;
        }

        // Walk the parent→child graph to collect the whole subtree rooted at `root`.
        let mut children: HashMap<u32, Vec<u32>> = HashMap::new();
        for (pid, ppid, _) in &procs {
            children.entry(*ppid).or_default().push(*pid);
        }
        let mut tree = vec![root];
        let mut i = 0;
        while i < tree.len() {
            if let Some(kids) = children.get(&tree[i]) {
                for &k in kids {
                    if !tree.contains(&k) {
                        tree.push(k);
                    }
                }
            }
            i += 1;
        }

        log::warn!(
            "Sweeping {} orphaned backend process(es) from a previous run (root pid {root})",
            tree.len()
        );
        // Kill leaves before parents.
        for &pid in tree.iter().rev() {
            let h = OpenProcess(PROCESS_TERMINATE, FALSE, pid);
            if !h.is_null() {
                TerminateProcess(h, 1);
                CloseHandle(h);
            }
        }
    }
}

/// No-op on non-Windows (the job object / sweep are Windows-specific for now).
#[cfg(not(windows))]
fn sweep_stale_backend() {}

/// Spawn `uvicorn app:app` for the backend on the given port.
///
/// Auth is ENABLED: on first launch (no users configured yet) the backend
/// redirects the webview to its first-run setup page so the owner creates their
/// account; thereafter a 7-day session cookie (persisted by WebView2) keeps
/// them signed in. The app still binds to loopback only, so the account is the
/// single local owner — not a public multi-user deployment.
fn spawn_backend(app: &tauri::AppHandle, port: u16) -> std::io::Result<Child> {
    let (python, backend_dir) = backend_paths(app);
    log::info!(
        "Starting backend: {} (cwd {}) on 127.0.0.1:{}",
        python.display(),
        backend_dir.display(),
        port
    );

    let mut cmd = Command::new(&python);
    cmd.current_dir(&backend_dir)
        .arg("-m")
        .arg("uvicorn")
        .arg("app:app")
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .env("AUTH_ENABLED", "true")
        .env("PYTHONUNBUFFERED", "1");

    // Persist user data outside the install tree so it survives reinstalls and
    // a read-only install location. No-op in dev (backend uses its repo data/).
    if let Some(data_dir) = backend_data_dir(app) {
        cmd.env("ODYSSEUS_DATA_DIR", &data_dir);
        log::info!("Backend data dir: {}", data_dir.display());
    }

    // Point the embedding model at the bundled cache so RAG/semantic memory work
    // offline on first launch (installed app only; dev keeps its data-dir cache).
    if let Some(fe_cache) = bundled_fastembed_cache(app) {
        cmd.env("FASTEMBED_CACHE_PATH", &fe_cache);
        log::info!("Bundled FastEmbed cache: {}", fe_cache.display());
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW — keep the Python console window hidden.
        cmd.creation_flags(0x0800_0000);
    }

    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    cmd.spawn()
}

/// Poll the port until the backend accepts a TCP connection (or we give up).
fn wait_for_backend(port: u16, attempts: u32, delay: Duration) -> bool {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    for _ in 0..attempts {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(800)).is_ok() {
            return true;
        }
        std::thread::sleep(delay);
    }
    false
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // Single-instance guard MUST be the first plugin: a second launch hands
        // its args to the running instance (which we use to focus the window)
        // and then exits *before* our setup() runs, so it never spawns a second
        // backend. This is what stops multiple backend trees from stacking up.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        // Native OS dialogs (used by the in-app folder pickers). The webview is
        // navigated to the backend's http://127.0.0.1:<port>/ origin, so the
        // matching `remote.urls` entry in capabilities/default.json is what lets
        // the frontend reach this plugin's `open` command.
        .plugin(tauri_plugin_dialog::init())
        // Lets the frontend open external URLs (e.g. the deep-research Visual
        // Report) in the system browser. window.open('_blank') does nothing
        // from the remote-origin webview, so the report button invokes
        // `plugin:opener|open_url` instead. Scoped to the loopback origin in
        // capabilities/default.json.
        .plugin(tauri_plugin_opener::init())
        // Native OS notifications — the frontend pings these when a deep-research
        // or agent run finishes while the window isn't focused. Reachable from the
        // loopback-origin webview via the capability's `remote.urls` entry.
        .plugin(tauri_plugin_notification::init())
        .manage(BackendProcess::new())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // Clean up any backend tree orphaned by a previous unclean exit
            // before we start a fresh one (no-op if nothing leaked).
            sweep_stale_backend();

            // DEVSPACE_PORT pins the backend port (useful for testing); else a free one.
            let port = std::env::var("DEVSPACE_PORT")
                .ok()
                .and_then(|s| s.parse::<u16>().ok())
                .unwrap_or_else(pick_free_port);

            // Bail early with a clear splash message if the bundled runtime
            // is missing — the splash listens for `backend-status` and will
            // display this string instead of looping forever.
            let (python, backend_dir) = backend_paths(&app.handle().clone());
            if !python.exists() || !backend_dir.join("app.py").exists() {
                let msg = if !python.exists() {
                    "Bundled Python runtime is missing — please reinstall DevSpace."
                } else {
                    "Bundled backend is missing — please reinstall DevSpace."
                };
                log::error!("{msg} (python={}, backend={})", python.display(), backend_dir.display());
                let _ = app.handle().emit("backend-status", msg);
                return Ok(());
            }

            match spawn_backend(&app.handle().clone(), port) {
                Ok(child) => {
                    // Record the PID so the *next* run can sweep this tree if we
                    // die without cleaning up (the job object's safety net).
                    let _ = std::fs::write(backend_pid_file(), child.id().to_string());
                    // Bind the whole backend tree to a kill-on-close job BEFORE
                    // uvicorn has had time to spawn its MCP-server children, so
                    // those children inherit the job and die with us too.
                    #[cfg(windows)]
                    {
                        if let Some(job) = assign_to_kill_on_close_job(&child) {
                            *app.state::<BackendProcess>()._job.lock().unwrap() = Some(job);
                        }
                    }
                    *app.state::<BackendProcess>().child.lock().unwrap() = Some(child);
                }
                Err(e) => {
                    log::error!("Failed to launch backend: {e}");
                    let _ = app
                        .handle()
                        .emit("backend-status", format!("Could not start local engine: {e}"));
                }
            }

            // Wait for readiness off the UI thread, then navigate the webview.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if wait_for_backend(port, 240, Duration::from_millis(500)) {
                    let url = format!("http://127.0.0.1:{port}/");
                    log::info!("Backend ready, loading {url}");
                    if let Some(window) = handle.get_webview_window("main") {
                        match tauri::Url::parse(&url) {
                            Ok(u) => {
                                if let Err(e) = window.navigate(u) {
                                    log::error!("navigate failed: {e}");
                                }
                            }
                            Err(e) => log::error!("bad backend url {url}: {e}"),
                        }
                    }
                } else {
                    log::error!("Backend never became reachable on port {port}");
                    let _ = handle.emit(
                        "backend-status",
                        "Local engine failed to start \u{2014} check logs.",
                    );
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building DevSpace")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendProcess>() {
                    if let Some(mut child) = state.child.lock().unwrap().take() {
                        log::info!("Shutting down backend (pid {})", child.id());
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
                // Clean shutdown — drop the PID file so the next launch doesn't
                // attempt a (harmless) sweep of an already-dead process.
                let _ = std::fs::remove_file(backend_pid_file());
            }
        });
}
