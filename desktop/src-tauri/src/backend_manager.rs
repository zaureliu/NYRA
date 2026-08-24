//! Ciclo de vida do backend standalone empacotado (packaging fix §5-§7).
//!
//! Fluxo do supervisor (thread dedicada):
//!   * `/health` responde com character=NYRA -> REUTILIZA (nunca mata);
//!   * porta 8000 ocupada por processo externo -> `BACKEND_PORT_CONFLICT`
//!     (NÃO matar);
//!   * porta livre -> inicia `backend-runtime\nyra-backend.exe` (resource),
//!     espera `/health` com timeout limitado e reporta estados por evento.
//!
//! Ownership: se o Tauri iniciou o backend, ele é NYRA-owned — ao sair
//! (`RunEvent::Exit`, tray "Encerrar NYRA", X da última janela) a árvore do
//! processo é encerrada e a porta liberada. Processos externos (Ollama,
//! navegador, VS Code) nunca são tocados.
//!
//! Restart limpo (§7): `run_backend.py` sai com código 75 quando o operador
//! pediu "Reiniciar NYRA completamente" — o supervisor relança o backend
//! sozinho (nova sessão, `/health` PASS). Shutdown intencional NÃO relança.

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
const EXIT_CODE_RESTART: i32 = 75;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Default)]
pub struct BackendManager {
    child_pid: Mutex<Option<u32>>,
    owned: AtomicBool,
    shutting_down: AtomicBool,
}

impl BackendManager {
    pub const fn new() -> Self {
        Self {
            child_pid: Mutex::new(None),
            owned: AtomicBool::new(false),
            shutting_down: AtomicBool::new(false),
        }
    }

    pub fn mark_shutting_down(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
    }

    fn set_child(&self, pid: Option<u32>, owned: bool) {
        if let Ok(mut guard) = self.child_pid.lock() {
            *guard = pid;
        }
        self.owned.store(owned, Ordering::SeqCst);
    }

    fn pid(&self) -> Option<u32> {
        self.child_pid.lock().ok().and_then(|mut guard| guard.take())
    }

    fn is_owned(&self) -> bool {
        self.owned.load(Ordering::SeqCst)
    }

    fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
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
        "nyra-backend",
        BackendStatePayload {
            state,
            detail: detail.clone(),
        },
    );
    log::info!("backend_state={state} detail={detail}");
}

/// Sonda mínima HTTP/1.1 sobre TCP puro (sem dependências novas):
/// `None` = porta livre/inacessível; `Some(true)` = backend NYRA válido;
/// `Some(false)` = algo ocupa a porta mas NÃO é um backend NYRA válido.
fn probe_backend() -> Option<bool> {
    let mut stream = TcpStream::connect((BACKEND_HOST, BACKEND_PORT)).ok()?;
    stream
        .set_read_timeout(Some(HEALTH_TIMEOUT))
        .and_then(|_| stream.set_write_timeout(Some(HEALTH_TIMEOUT)))
        .ok()?;
    let request = format!("GET /health HTTP/1.1\r\nHost: {BACKEND_HOST}:{BACKEND_PORT}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    Some(response.contains("\"character\":\"NYRA\""))
}

fn backend_exe_path(app: &AppHandle) -> Option<std::path::PathBuf> {
    let resource_dir = app.path().resource_dir().ok()?;
    let candidate = resource_dir.join("backend-runtime").join("nyra-backend.exe");
    candidate.is_file().then_some(candidate)
}

fn spawn_backend(app: &AppHandle) -> Result<Child, String> {
    let exe = backend_exe_path(app)
        .ok_or_else(|| "BACKEND_NOT_PACKAGED (resource backend-runtime ausente)".to_string())?;
    let working_dir = exe.parent().map(std::path::Path::to_path_buf);
    let mut command = Command::new(&exe);
    command.env("NYRA_FROZEN", "1");
    if let Some(dir) = working_dir {
        command.current_dir(dir);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command.spawn().map_err(|error| format!("spawn falhou: {error}"))
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

/// Thread-supervisora: garante um backend NYRA saudável enquanto o app vive.
pub fn spawn_supervisor(app: AppHandle) {
    std::thread::Builder::new()
        .name("nyra-backend-supervisor".into())
        .spawn(move || supervise(app))
        .map_err(|error| log::error!("supervisor thread falhou: {error}"))
        .ok();
}

fn supervise(app: AppHandle) {
    match probe_backend() {
        Some(true) => {
            emit_state(&app, "READY", "backend existente reutilizado");
            return; // não é nosso: nada a supervisionar nem a matar
        }
        Some(false) => {
            emit_state(&app, "BACKEND_PORT_CONFLICT",
                "porta 8000 ocupada por processo externo; NYRA não iniciou backend próprio");
            return; // §5: NUNCA matar o processo externo
        }
        None => {} // porta livre: iniciar backend empacotado
    }

    emit_state(&app, "STARTING", "iniciando nyra-backend empacotado");
    let mut child = match spawn_backend(&app) {
        Ok(child) => child,
        Err(detail) => {
            emit_state(&app, "BACKEND_NOT_PACKAGED", detail);
            return;
        }
    };
    let pid = child.id();
    app.state::<BackendManager>().set_child(Some(pid), true);

    if !await_health(Instant::now() + HEALTH_WAIT_LIMIT) {
        emit_state(&app, "BACKEND_START_TIMEOUT",
            "backend não ficou saudável dentro do timeout");
        let _ = child.kill();
        app.state::<BackendManager>().set_child(None, false);
        return;
    }
    emit_state(&app, "READY", format!("backend próprio saudável (pid {pid})"));

    // Observa o processo enquanto o app existir.
    let status = child.wait();
    app.state::<BackendManager>().set_child(None, false);
    let code = status.ok().and_then(|exit| exit.code());
    if app.state::<BackendManager>().is_shutting_down() {
        log::info!("backend owned encerrou durante shutdown (code={code:?})");
        return; // shutdown intencional: NÃO relançar (§6/§8)
    }
    if code == Some(EXIT_CODE_RESTART) {
        emit_state(&app, "RESTARTING", "restart completo pedido; nova sessão do backend");
        std::thread::sleep(Duration::from_millis(1200)); // dá tempo da porta fechar
        supervise(app); // §7: abre nova sessão do backend sozinho
        return;
    }
    emit_state(&app, "BACKEND_EXITED",
        format!("backend saiu de forma inesperada (code={code:?}); auto-restart desabilitado no pacote"));
}

/// Cleanup no fim do processo Tauri (§6): encerra apenas o backend OWNED.
pub fn shutdown_owned(app: &AppHandle) {
    let manager = app.state::<BackendManager>();
    manager.mark_shutting_down();
    if !manager.is_owned() {
        log::info!("shutdown: backend não é NYRA-owned; nada a encerrar");
        return;
    }
    let Some(pid) = manager.pid() else {
        return;
    };
    log::info!("shutdown: encerrando backend owned pid={pid}");
    #[cfg(windows)]
    {
        // /T encerra a árvore (filhos), /F força. Apenas NOSSO pid.
        use std::os::windows::process::CommandExt;

        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("kill").args(["-9", &pid.to_string()]).status();
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        match TcpStream::connect((BACKEND_HOST, BACKEND_PORT)) {
            Ok(_) => std::thread::sleep(Duration::from_millis(400)),
            Err(_) => {
                log::info!("shutdown: porta 8000 livre confirmada");
                return;
            }
        }
    }
    log::warn!("shutdown: porta 8000 ainda ocupada após timeout");
}
