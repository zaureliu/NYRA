//! Ownership and lifecycle for the packaged backend.
//!
//! An existing healthy backend is reused but never owned or terminated. A
//! backend spawned here receives a private shutdown token and parent PID, is
//! assigned to a kill-on-close Windows Job Object, and is stopped gracefully
//! before the owned-process-only forced fallback is used.

use serde::Serialize;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};

const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: u16 = 8000;
const HEALTH_TIMEOUT: Duration = Duration::from_millis(900);
const HEALTH_WAIT_LIMIT: Duration = Duration::from_secs(120);
const GRACEFUL_SHUTDOWN_LIMIT: Duration = Duration::from_secs(12);
const FORCED_SHUTDOWN_LIMIT: Duration = Duration::from_secs(4);
const EXIT_CODE_RESTART: i32 = 75;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum GracefulShutdownRequest {
    Failed,
    Sent { acknowledged: bool },
}

impl GracefulShutdownRequest {
    fn was_sent(self) -> bool {
        matches!(self, Self::Sent { .. })
    }
}

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Default)]
pub struct BackendManager {
    child_pid: Mutex<Option<u32>>,
    owner_token: Mutex<Option<String>>,
    job_handle: Mutex<Option<usize>>,
    owned: AtomicBool,
    shutting_down: AtomicBool,
    exit_ready: AtomicBool,
}

impl BackendManager {
    pub const fn new() -> Self {
        Self {
            child_pid: Mutex::new(None),
            owner_token: Mutex::new(None),
            job_handle: Mutex::new(None),
            owned: AtomicBool::new(false),
            shutting_down: AtomicBool::new(false),
            exit_ready: AtomicBool::new(false),
        }
    }

    fn set_child(&self, pid: u32, token: String, job_handle: Option<usize>) {
        *self.child_pid.lock().expect("backend pid lock") = Some(pid);
        *self.owner_token.lock().expect("backend token lock") = Some(token);
        *self.job_handle.lock().expect("backend job lock") = job_handle;
        self.owned.store(true, Ordering::SeqCst);
    }

    fn clear_child(&self, pid: u32) {
        let mut guard = self.child_pid.lock().expect("backend pid lock");
        if *guard != Some(pid) {
            return;
        }
        *guard = None;
        self.owner_token.lock().expect("backend token lock").take();
        self.owned.store(false, Ordering::SeqCst);
        close_job_handle(self.job_handle.lock().expect("backend job lock").take());
    }

    fn owned_child(&self) -> Option<(u32, String)> {
        if !self.owned.load(Ordering::SeqCst) {
            return None;
        }
        let pid = *self.child_pid.lock().ok()?;
        let token = self.owner_token.lock().ok()?.clone()?;
        pid.map(|value| (value, token))
    }

    pub fn request_exit(&self) -> bool {
        !self.shutting_down.swap(true, Ordering::SeqCst)
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
    }

    pub fn exit_ready(&self) -> bool {
        self.exit_ready.load(Ordering::SeqCst)
    }

    pub fn mark_exit_ready(&self) {
        self.exit_ready.store(true, Ordering::SeqCst);
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendStatePayload {
    state: &'static str,
    detail: String,
}

fn emit_state(app: &AppHandle, state: &'static str, detail: impl Into<String>) {
    let detail = detail.into();
    let _ = app.emit(
        "kazumi-backend",
        BackendStatePayload {
            state,
            detail: detail.clone(),
        },
    );
    log::info!("backend_state={state} detail={detail}");
}

fn probe_backend() -> Option<bool> {
    let mut stream = TcpStream::connect((BACKEND_HOST, BACKEND_PORT)).ok()?;
    stream
        .set_read_timeout(Some(HEALTH_TIMEOUT))
        .and_then(|_| stream.set_write_timeout(Some(HEALTH_TIMEOUT)))
        .ok()?;
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: {BACKEND_HOST}:{BACKEND_PORT}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    Some(response.contains("\"character\":\"KAZUMI\""))
}

fn backend_exe_path(app: &AppHandle) -> Option<std::path::PathBuf> {
    let resource_dir = app.path().resource_dir().ok()?;
    let candidate = resource_dir
        .join("backend-runtime")
        .join("kazumi-backend.exe");
    candidate.is_file().then_some(candidate)
}

fn owner_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| "owner token generation failed".to_string())?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn spawn_backend(app: &AppHandle, token: &str) -> Result<Child, String> {
    let exe = backend_exe_path(app)
        .ok_or_else(|| "BACKEND_NOT_PACKAGED (resource backend-runtime ausente)".to_string())?;
    let working_dir = exe.parent().map(std::path::Path::to_path_buf);
    let mut command = Command::new(&exe);
    command
        .env("KAZUMI_FROZEN", "1")
        .env("KAZUMI_BACKEND_OWNED", "1")
        .env("KAZUMI_PARENT_PID", std::process::id().to_string())
        .env("KAZUMI_OWNER_TOKEN", token);
    if let Some(dir) = working_dir {
        command.current_dir(dir);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
        .spawn()
        .map_err(|error| format!("spawn falhou: {error}"))
}

fn await_health(deadline: Instant) -> bool {
    while Instant::now() < deadline {
        if probe_backend() == Some(true) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(700));
    }
    false
}

pub fn spawn_supervisor(app: AppHandle) {
    std::thread::Builder::new()
        .name("kazumi-backend-supervisor".into())
        .spawn(move || supervise(app))
        .map_err(|error| log::error!("supervisor thread falhou: {error}"))
        .ok();
}

fn supervise(app: AppHandle) {
    match probe_backend() {
        Some(true) => {
            emit_state(&app, "READY", "backend existente reutilizado (não-owned)");
            return;
        }
        Some(false) => {
            emit_state(
                &app,
                "BACKEND_PORT_CONFLICT",
                "porta 8000 ocupada por processo externo; nenhum processo foi encerrado",
            );
            return;
        }
        None => {}
    }

    emit_state(&app, "STARTING", "iniciando backend empacotado owned");
    let token = match owner_token() {
        Ok(value) => value,
        Err(detail) => {
            emit_state(&app, "BACKEND_START_FAILED", detail);
            return;
        }
    };
    let mut child = match spawn_backend(&app, &token) {
        Ok(child) => child,
        Err(detail) => {
            emit_state(&app, "BACKEND_NOT_PACKAGED", detail);
            return;
        }
    };
    let pid = child.id();
    let job_handle = create_kill_on_close_job(&child)
        .map_err(|error| log::warn!("backend_job_object_unavailable reason={error}"))
        .ok();
    app.state::<BackendManager>()
        .set_child(pid, token, job_handle);

    if !await_health(Instant::now() + HEALTH_WAIT_LIMIT) {
        emit_state(
            &app,
            "BACKEND_START_TIMEOUT",
            "backend não ficou saudável dentro do timeout",
        );
        force_owned_process(&app, pid, "startup_timeout");
        let _ = child.wait();
        app.state::<BackendManager>().clear_child(pid);
        return;
    }
    emit_state(
        &app,
        "READY",
        format!("backend próprio saudável (pid {pid})"),
    );

    let status = child.wait();
    let code = status.ok().and_then(|exit| exit.code());
    app.state::<BackendManager>().clear_child(pid);
    if app.state::<BackendManager>().is_shutting_down() {
        log::info!("backend_child_exited pid={pid} code={code:?}");
        return;
    }
    if code == Some(EXIT_CODE_RESTART) {
        emit_state(
            &app,
            "RESTARTING",
            "restart completo pedido; iniciando nova sessão",
        );
        std::thread::sleep(Duration::from_millis(1200));
        supervise(app);
        return;
    }
    emit_state(
        &app,
        "BACKEND_EXITED",
        format!("backend saiu de forma inesperada (code={code:?}); auto-restart desabilitado"),
    );
}

pub fn shutdown_owned(app: &AppHandle) -> bool {
    let manager = app.state::<BackendManager>();
    manager.shutting_down.store(true, Ordering::SeqCst);
    let Some((pid, token)) = manager.owned_child() else {
        log::info!("backend_child_shutdown_skipped ownership=non-owned");
        return true;
    };
    log::info!("backend_child_shutdown_requested pid={pid}");
    log::info!("backend_graceful_shutdown_requested pid={pid}");
    let request = request_graceful_shutdown(&token);
    if request.was_sent() && wait_for_exit(pid, GRACEFUL_SHUTDOWN_LIMIT) {
        log::info!("backend_child_exited pid={pid} mode=graceful");
        log::info!("backend_exited_gracefully pid={pid}");
        return true;
    }
    let reason = if request.was_sent() {
        "graceful_timeout"
    } else {
        "graceful_signal_failed"
    };
    log::warn!("backend_forced_termination pid={pid} reason={reason}");
    log::warn!("backend_force_terminated pid={pid} reason={reason}");
    force_owned_process(app, pid, reason);
    if wait_for_exit(pid, FORCED_SHUTDOWN_LIMIT) {
        log::info!("backend_child_exited pid={pid} mode=forced");
        true
    } else {
        log::error!("backend_child_exit_unconfirmed pid={pid}");
        false
    }
}

fn request_graceful_shutdown(token: &str) -> GracefulShutdownRequest {
    let Ok(mut stream) = TcpStream::connect_timeout(
        &format!("{BACKEND_HOST}:{BACKEND_PORT}")
            .parse()
            .expect("fixed backend address"),
        Duration::from_secs(1),
    ) else {
        return GracefulShutdownRequest::Failed;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let request = format!(
        "POST /internal/owned-shutdown HTTP/1.1\r\nHost: {BACKEND_HOST}:{BACKEND_PORT}\r\nX-KAZUMI-Owner-Token: {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return GracefulShutdownRequest::Failed;
    }
    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);
    let acknowledged = response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.1 202");
    log::info!("backend_graceful_shutdown_signal_sent acknowledged={acknowledged}");
    // Uvicorn may begin handling SIGINT before its loopback response is fully
    // flushed. A successfully written, authenticated request must still get
    // the bounded graceful window before the owned-only forced fallback.
    GracefulShutdownRequest::Sent { acknowledged }
}

fn wait_for_exit(pid: u32, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !process_is_alive(pid) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    !process_is_alive(pid)
}

#[cfg(windows)]
fn process_is_alive(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return false;
        }
        let mut code = 0u32;
        let ok = GetExitCodeProcess(process, &mut code) != 0;
        CloseHandle(process);
        ok && code == STILL_ACTIVE as u32
    }
}

#[cfg(not(windows))]
fn process_is_alive(pid: u32) -> bool {
    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .status()
        .is_ok_and(|status| status.success())
}

#[cfg(windows)]
fn create_kill_on_close_job(child: &Child) -> Result<usize, String> {
    use std::mem::size_of;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err("create_failed".into());
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const core::ffi::c_void,
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
            || AssignProcessToJobObject(job, child.as_raw_handle() as _) == 0
        {
            CloseHandle(job);
            return Err("configure_or_assign_failed".into());
        }
        log::info!("backend_job_object_assigned pid={}", child.id());
        Ok(job as usize)
    }
}

#[cfg(not(windows))]
fn create_kill_on_close_job(_child: &Child) -> Result<usize, String> {
    Err("unsupported_platform".into())
}

#[cfg(windows)]
fn close_job_handle(handle: Option<usize>) {
    use windows_sys::Win32::Foundation::CloseHandle;
    if let Some(value) = handle {
        unsafe { CloseHandle(value as _) };
    }
}

#[cfg(not(windows))]
fn close_job_handle(_handle: Option<usize>) {}

#[cfg(windows)]
fn force_owned_process(app: &AppHandle, pid: u32, _reason: &str) {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::TerminateJobObject;
    use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};
    let job = app
        .state::<BackendManager>()
        .job_handle
        .lock()
        .ok()
        .and_then(|mut guard| guard.take());
    unsafe {
        if let Some(value) = job {
            let _ = TerminateJobObject(value as _, 1);
            CloseHandle(value as _);
            return;
        }
        let process = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if !process.is_null() {
            let _ = TerminateProcess(process, 1);
            CloseHandle(process);
        }
    }
}

#[cfg(not(windows))]
fn force_owned_process(_app: &AppHandle, pid: u32, _reason: &str) {
    let _ = Command::new("kill").args(["-9", &pid.to_string()]).status();
}

#[cfg(test)]
mod tests {
    use super::{BackendManager, GracefulShutdownRequest};

    #[test]
    fn non_owned_manager_has_no_shutdown_target() {
        let manager = BackendManager::new();
        assert!(manager.owned_child().is_none());
    }

    #[test]
    fn exit_request_is_one_shot() {
        let manager = BackendManager::new();
        assert!(manager.request_exit());
        assert!(!manager.request_exit());
    }

    #[test]
    fn sent_graceful_request_gets_the_bounded_wait_even_without_http_ack() {
        assert!(GracefulShutdownRequest::Sent {
            acknowledged: false
        }
        .was_sent());
        assert!(!GracefulShutdownRequest::Failed.was_sent());
    }
}
