use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_window_state::StateFlags;

mod backend_manager;
mod backend_transport;
mod conversation_transport;
mod stt_transport;
mod presence;
mod shutdown;
mod spout_presence;

static CLICK_THROUGH: AtomicBool = AtomicBool::new(false);
static CURSOR_TRACKER_RUNNING: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "camelCase")]
struct ScreenBounds {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "camelCase")]
struct GlobalCursorSample {
    available: bool,
    cursor_x: i32,
    cursor_y: i32,
    normalized_x: f64,
    normalized_y: f64,
    virtual_desktop_bounds: ScreenBounds,
    monitor_count: u32,
}

fn main_window(app: &tauri::AppHandle) -> Option<tauri::WebviewWindow> {
    app.get_webview_window("main")
}

fn show_dashboard(app: &tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("dashboard") {
        window.unminimize().map_err(|error| error.to_string())?;
        window.show().map_err(|error| error.to_string())?;
        // Windows can persist the iconic (-32000, -32000) position for a
        // minimized webview. Centering after unminimize keeps a deliberate
        // application launch inside the visible work area.
        window.center().map_err(|error| error.to_string())?;
        window.set_focus().map_err(|error| error.to_string())?;
        return Ok(());
    }

    let window = WebviewWindowBuilder::new(app, "dashboard", WebviewUrl::App("index.html".into()))
        .title("Kazumi")
        .inner_size(1280.0, 820.0)
        .min_inner_size(900.0, 620.0)
        .center()
        .visible(true)
        .build()
        .map_err(|error| error.to_string())?;
    window.unminimize().map_err(|error| error.to_string())?;
    window.show().map_err(|error| error.to_string())?;
    window.center().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

#[tauri::command]
fn open_dashboard(app: tauri::AppHandle) -> Result<(), String> {
    show_dashboard(&app)
}

#[tauri::command]
fn quit_kazumi(app: tauri::AppHandle) -> bool {
    shutdown::request_app_shutdown(app, shutdown::ShutdownReason::UiExit)
}

#[tauri::command]
async fn backend_request(
    request: backend_transport::BackendRequest,
) -> Result<backend_transport::BackendResponse, String> {
    tauri::async_runtime::spawn_blocking(move || backend_transport::execute(request))
        .await
        .map_err(|_| "BACKEND_BRIDGE_TASK_FAILED".to_string())?
}

#[tauri::command]
fn start_conversation_bridge(
    app: tauri::AppHandle,
    bridge: tauri::State<'_, conversation_transport::ConversationBridge>,
) -> bool {
    bridge.start(app)
}

#[tauri::command]
async fn stt_stream_open(window: tauri::WebviewWindow, bridge: tauri::State<'_, stt_transport::SttBridge>,
    stream_id: String, ticket: String, channel: tauri::ipc::Channel<serde_json::Value>) -> Result<(), String> {
    let bridge = bridge.inner().clone();
    let owner = window.label().to_string();
    tauri::async_runtime::spawn_blocking(move || bridge.open(owner, stream_id, ticket, channel))
        .await.map_err(|_| "STT_TASK_FAILED".to_string())?
}

#[tauri::command]
fn stt_stream_audio(window: tauri::WebviewWindow, bridge: tauri::State<'_, stt_transport::SttBridge>,
    stream_id: String, audio: Vec<u8>, end: bool) -> Result<(), String> {
    bridge.send(window.label(), &stream_id, audio, end)
}

#[tauri::command]
async fn stt_stream_close(window: tauri::WebviewWindow, bridge: tauri::State<'_, stt_transport::SttBridge>, stream_id: String) -> Result<(), String> {
    let bridge = bridge.inner().clone();
    let owner = window.label().to_string();
    tauri::async_runtime::spawn_blocking(move || bridge.close(&owner, &stream_id))
        .await.map_err(|_| "STT_TASK_FAILED".to_string())
}

#[tauri::command]
fn presence_show(window: tauri::WebviewWindow) -> PresenceStatusPayload {
    let _ = window.unminimize();
    let _ = window.show();
    if window.is_minimized().unwrap_or(false) {
        log::warn!("presence_show: janela permaneceu minimizada");
    }
    emit_presence_state(&window, "VISIBLE");
    presence_status_payload(&window)
}

#[tauri::command]
fn presence_hide(window: tauri::WebviewWindow) -> PresenceStatusPayload {
    let _ = window.hide();
    emit_presence_state(&window, "HIDDEN");
    presence_status_payload(&window)
}

#[tauri::command]
fn presence_toggle(window: tauri::WebviewWindow) -> PresenceStatusPayload {
    if window.is_visible().unwrap_or(false) {
        presence_hide(window)
    } else {
        presence_show(window)
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct PresenceStatusPayload {
    state: String,
    visible: bool,
    minimized: bool,
}

#[tauri::command]
fn presence_status_command(window: tauri::WebviewWindow) -> PresenceStatusPayload {
    presence_status_payload(&window)
}

fn presence_status_payload(window: &tauri::WebviewWindow) -> PresenceStatusPayload {
    let visible = window.is_visible().unwrap_or(false);
    let minimized = window.is_minimized().unwrap_or(false);
    let state = match (visible, minimized) {
        (true, false) => "VISIBLE",
        (true, true) => "READY",
        (false, _) => "HIDDEN",
    };
    PresenceStatusPayload {
        state: state.to_string(),
        visible,
        minimized,
    }
}

#[tauri::command]
fn vts_presence_configure(
    state: tauri::State<'_, spout_presence::SpoutPresence>,
    config: spout_presence::SpoutPresenceConfig,
) -> Result<spout_presence::SpoutPresenceStatus, String> {
    state.configure(config)
}

#[tauri::command]
fn vts_presence_status(
    state: tauri::State<'_, spout_presence::SpoutPresence>,
) -> spout_presence::SpoutPresenceStatus {
    state.status()
}

#[tauri::command]
fn emit_presence_state(window: &tauri::WebviewWindow, state: &str) {
    let _ = window.emit("kazumi-presence", state);
}

#[tauri::command]
fn set_click_through(window: tauri::WebviewWindow, enabled: bool) -> Result<(), String> {
    CLICK_THROUGH.store(enabled, Ordering::SeqCst);
    window
        .set_ignore_cursor_events(enabled)
        .map_err(|error| error.to_string())
}

#[cfg(windows)]
fn native_cursor_position() -> Option<(i32, i32, ScreenBounds, u32)> {
    use windows_sys::Win32::Foundation::POINT;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetCursorPos, GetSystemMetrics, SM_CMONITORS, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN,
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
    };

    let mut point = POINT { x: 0, y: 0 };
    if unsafe { GetCursorPos(&mut point) } == 0 {
        return None;
    }
    let bounds = ScreenBounds {
        x: unsafe { GetSystemMetrics(SM_XVIRTUALSCREEN) },
        y: unsafe { GetSystemMetrics(SM_YVIRTUALSCREEN) },
        width: unsafe { GetSystemMetrics(SM_CXVIRTUALSCREEN) }.max(0) as u32,
        height: unsafe { GetSystemMetrics(SM_CYVIRTUALSCREEN) }.max(0) as u32,
    };
    if bounds.width == 0 || bounds.height == 0 {
        return None;
    }
    let monitor_count = unsafe { GetSystemMetrics(SM_CMONITORS) }.max(1) as u32;
    Some((point.x, point.y, bounds, monitor_count))
}

#[cfg(not(windows))]
fn native_cursor_position() -> Option<(i32, i32, ScreenBounds, u32)> {
    None
}

fn normalize_virtual_cursor(cursor_x: i32, cursor_y: i32, bounds: ScreenBounds) -> (f64, f64) {
    let x = ((cursor_x - bounds.x) as f64 / (bounds.width.saturating_sub(1).max(1)) as f64)
        .mul_add(2.0, -1.0)
        .clamp(-1.0, 1.0);
    let y = (1.0
        - (cursor_y - bounds.y) as f64 / (bounds.height.saturating_sub(1).max(1)) as f64 * 2.0)
        .clamp(-1.0, 1.0);
    (x, y)
}

fn start_global_cursor_tracker(window: tauri::WebviewWindow) {
    if CURSOR_TRACKER_RUNNING.swap(true, Ordering::SeqCst) {
        return;
    }
    let spawned = std::thread::Builder::new()
        .name("kazumi-global-cursor".into())
        .spawn(move || {
            let mut unavailable_reported_at: Option<Instant> = None;
            let mut emit_error_reported = false;
            let mut first_sample_reported = false;
            while CURSOR_TRACKER_RUNNING.load(Ordering::SeqCst) {
                let Some((cursor_x, cursor_y, virtual_desktop_bounds, monitor_count)) = native_cursor_position()
                else {
                    if unavailable_reported_at
                        .map(|reported| reported.elapsed() >= Duration::from_secs(1))
                        .unwrap_or(true)
                    {
                        unavailable_reported_at = Some(Instant::now());
                        let empty = ScreenBounds {
                            x: 0,
                            y: 0,
                            width: 0,
                            height: 0,
                        };
                        let _ = window.emit(
                            "kazumi-global-cursor",
                            GlobalCursorSample {
                                available: false,
                                cursor_x: 0,
                                cursor_y: 0,
                                normalized_x: 0.0,
                                normalized_y: 0.0,
                                virtual_desktop_bounds: empty,
                                monitor_count: 0,
                            },
                        );
                    }
                    std::thread::sleep(Duration::from_millis(250));
                    continue;
                };
                unavailable_reported_at = None;
                let (normalized_x, normalized_y) =
                    normalize_virtual_cursor(cursor_x, cursor_y, virtual_desktop_bounds);
                let emitted = window.emit(
                    "kazumi-global-cursor",
                    GlobalCursorSample {
                        available: true,
                        cursor_x,
                        cursor_y,
                        normalized_x,
                        normalized_y,
                        virtual_desktop_bounds,
                        monitor_count,
                    },
                );
                if let Err(error) = emitted {
                    if !emit_error_reported {
                        emit_error_reported = true;
                        log::warn!("Falha ao emitir cursor global: {error}");
                    }
                } else if !first_sample_reported {
                    emit_error_reported = false;
                    first_sample_reported = true;
                    log::info!(
                        "Cursor global ativo (cursor=({}, {}), virtual=({}, {}, {}x{}), monitors={})",
                        cursor_x,
                        cursor_y,
                        virtual_desktop_bounds.x,
                        virtual_desktop_bounds.y,
                        virtual_desktop_bounds.width,
                        virtual_desktop_bounds.height,
                        monitor_count,
                    );
                }
                std::thread::sleep(Duration::from_millis(33));
            }
        });
    if let Err(error) = spawned {
        CURSOR_TRACKER_RUNNING.store(false, Ordering::SeqCst);
        log::error!("Falha ao iniciar cursor global: {error}");
    }
}

pub(crate) fn stop_global_cursor_tracker() {
    CURSOR_TRACKER_RUNNING.store(false, Ordering::SeqCst);
}

pub fn run() {
    tauri::Builder::default()
        .manage(backend_manager::BackendManager::new())
        .manage(conversation_transport::ConversationBridge::new())
        .manage(stt_transport::SttBridge::default())
        .manage(spout_presence::SpoutPresence::new())
        .invoke_handler(tauri::generate_handler![
            set_click_through,
            open_dashboard,
            quit_kazumi,
            backend_request,
            start_conversation_bridge,
            stt_stream_open,
            stt_stream_audio,
            stt_stream_close,
            presence_show,
            presence_hide,
            presence_toggle,
            presence_status_command,
            vts_presence_configure,
            vts_presence_status
        ])
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Err(error) = show_dashboard(app) {
                log::warn!("Falha ao restaurar painel empacotado: {error}");
            }
            if let Some(window) = main_window(app) {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(
            tauri_plugin_window_state::Builder::default()
                // VISIBILITY jamais é restaurada: um encerramento ocultado não pode
                // fazer o Desktop Presence nascer invisível no próximo start.
                .with_state_flags(
                    StateFlags::SIZE
                        | StateFlags::POSITION
                        | StateFlags::MAXIMIZED
                        | StateFlags::FULLSCREEN
                        | StateFlags::DECORATIONS,
                )
                .build(),
        )
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::Builder::new().build())
        .plugin(
            tauri_plugin_log::Builder::new()
                .target(tauri_plugin_log::Target::new(
                    tauri_plugin_log::TargetKind::LogDir {
                        file_name: Some("desktop".into()),
                    },
                ))
                .build(),
        )
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    let show =
                        Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyN);
                    let interactive =
                        Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyI);
                    let interactive_fallback =
                        Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyI);
                    let talk =
                        Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space);
                    let mute =
                        Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyM);
                    if let Some(window) = main_window(app) {
                        if shortcut == &talk {
                            CLICK_THROUGH.store(false, Ordering::SeqCst);
                            let _ = window.set_ignore_cursor_events(false);
                            let _ = window.show();
                            let _ = window.emit(
                                "kazumi-desktop",
                                if event.state() == ShortcutState::Pressed {
                                    "talk-start"
                                } else {
                                    "talk-stop"
                                },
                            );
                            return;
                        }
                        if event.state() != ShortcutState::Pressed {
                            return;
                        }
                        if shortcut == &mute {
                            let _ = window.emit("kazumi-desktop", "mic-toggle");
                            return;
                        }
                        if shortcut == &show {
                            if window.is_visible().unwrap_or(false) {
                                let _ = window.hide();
                            } else {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                        if shortcut == &interactive || shortcut == &interactive_fallback {
                            let ignored = !CLICK_THROUGH.fetch_xor(true, Ordering::SeqCst);
                            let _ = window.set_ignore_cursor_events(ignored);
                            let _ = window.emit(
                                "kazumi-desktop",
                                if ignored {
                                    "click-through"
                                } else {
                                    "interactive"
                                },
                            );
                        }
                    }
                })
                .build(),
        )
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let manager = window
                    .app_handle()
                    .state::<backend_manager::BackendManager>();
                if !manager.is_shutting_down() && !manager.exit_ready() {
                    api.prevent_close();
                    let _ = window.hide();
                    log::info!(
                        "window_close_behavior=HIDE_TO_TRAY label={}",
                        window.label()
                    );
                }
            }
        })
        .setup(|app| {
            let show = MenuItemBuilder::with_id("show", "Mostrar Kazumi").build(app)?;
            let hide = MenuItemBuilder::with_id("hide", "Ocultar Kazumi").build(app)?;
            let interactive =
                MenuItemBuilder::with_id("interactive", "Modo interativo").build(app)?;
            let click = MenuItemBuilder::with_id("click", "Click-through").build(app)?;
            let top = MenuItemBuilder::with_id("top", "Alternar always on top").build(app)?;
            let panel = MenuItemBuilder::with_id("panel", "Abrir painel").build(app)?;
            let talk = MenuItemBuilder::with_id("talk", "Falar com Kazumi").build(app)?;
            let settings = MenuItemBuilder::with_id("settings", "Configurações").build(app)?;
            let listening =
                MenuItemBuilder::with_id("listening", "Listening: ON/OFF").build(app)?;
            let network =
                MenuItemBuilder::with_id("network", "Network Watch: ON/OFF").build(app)?;
            let sentinel =
                MenuItemBuilder::with_id("sentinel", "Sentinel Watch: ON/OFF").build(app)?;
            let sentinel_reconnect =
                MenuItemBuilder::with_id("sentinel-reconnect", "Reconectar Sentinel").build(app)?;
            let sentinel_open =
                MenuItemBuilder::with_id("sentinel-open", "Abrir Sentinel").build(app)?;
            let quiet = MenuItemBuilder::with_id("quiet", "Quiet Mode").build(app)?;
            let reconnect =
                MenuItemBuilder::with_id("reconnect", "Reiniciar conexão").build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Encerrar Kazumi").build(app)?;
            let menu = MenuBuilder::new(app)
                .items(&[
                    &show,
                    &hide,
                    &interactive,
                    &click,
                    &top,
                    &panel,
                    &talk,
                    &listening,
                    &network,
                    &sentinel,
                    &sentinel_reconnect,
                    &sentinel_open,
                    &quiet,
                    &settings,
                    &reconnect,
                    &quit,
                ])
                .build()?;
            TrayIconBuilder::with_id(shutdown::TRAY_ID)
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .on_menu_event(|app, event| {
                    if event.id.as_ref() == "quit" {
                        shutdown::request_app_shutdown(
                            app.clone(),
                            shutdown::ShutdownReason::TrayExit,
                        );
                        return;
                    }
                    if let Some(window) = main_window(app) {
                        match event.id.as_ref() {
                            "show" => {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                            "hide" => {
                                let _ = window.hide();
                            }
                            "interactive" => {
                                CLICK_THROUGH.store(false, Ordering::SeqCst);
                                let _ = window.set_ignore_cursor_events(false);
                                let _ = window.show();
                                let _ = window.emit("kazumi-desktop", "interactive");
                            }
                            "click" => {
                                CLICK_THROUGH.store(true, Ordering::SeqCst);
                                let _ = window.set_ignore_cursor_events(true);
                                let _ = window.emit("kazumi-desktop", "click-through");
                            }
                            "top" => {
                                let next = !window.is_always_on_top().unwrap_or(true);
                                let _ = window.set_always_on_top(next);
                            }
                            "panel" => {
                                if let Err(error) = show_dashboard(app) {
                                    log::warn!("Falha ao abrir painel empacotado: {error}");
                                }
                            }
                            "talk" => {
                                let _ = window.set_ignore_cursor_events(false);
                                let _ = window.show();
                                let _ = window.emit("kazumi-desktop", "talk-menu");
                            }
                            "listening" => {
                                let _ = window.emit("kazumi-desktop", "listening-toggle");
                            }
                            "network" => {
                                let _ = window.emit("kazumi-desktop", "network-toggle");
                            }
                            "sentinel" => {
                                let _ = window.emit("kazumi-desktop", "sentinel-toggle");
                            }
                            "sentinel-reconnect" => {
                                let _ = window.emit("kazumi-desktop", "sentinel-reconnect");
                            }
                            "sentinel-open" => {
                                let _ = window.emit("kazumi-desktop", "sentinel-open");
                            }
                            "quiet" => {
                                let _ = window.emit("kazumi-desktop", "quiet-toggle");
                            }
                            "settings" => {
                                let _ = window.set_ignore_cursor_events(false);
                                let _ = window.show();
                                let _ = window.emit("kazumi-desktop", "settings");
                            }
                            "reconnect" => {
                                let _ = window.emit("kazumi-desktop", "reconnect");
                            }
                            _ => {}
                        }
                    }
                })
                .build(app)?;
            let shortcuts = [
                Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyN),
                Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyI),
                Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space),
                Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyM),
            ];
            // Registro único por processo: limpa qualquer registro residual do
            // plugin antes de registrar, evitando "HotKey already registered".
            let _ = app.global_shortcut().unregister_all();
            for shortcut in shortcuts {
                if let Err(error) = app.global_shortcut().register(shortcut) {
                    let detail = error.to_string();
                    // "HotKey already registered" aqui é conflito de SO: outro
                    // aplicativo (ex.: Discord/Parsec) já reservou o atalho.
                    // Não é inicialização duplicada da KAZUMI; registra como
                    // conflito externo em vez de espalhar o erro bruto.
                    if detail.contains("already registered") {
                        log::info!("Atalho global em uso por outro aplicativo: {shortcut}");
                    } else {
                        log::warn!("Atalho global indisponível: {error}");
                    }
                    if shortcut
                        == Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyI)
                    {
                        let fallback =
                            Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyI);
                        if app.global_shortcut().register(fallback).is_ok() {
                            log::info!("Fallback click-through registrado: Ctrl+Alt+I");
                        }
                    }
                }
            }
            let marker = match app.path().app_local_data_dir() {
                Ok(dir) => dir.join("position-initialized"),
                Err(error) => {
                    log::warn!("app_local_data_dir indisponível: {error}");
                    app.path()
                        .temp_dir()
                        .ok()
                        .map(|dir| dir.join("kazumi-position-initialized"))
                        .unwrap_or_default()
                }
            };
            if !marker.exists() {
                if let Some(window) = app.get_webview_window("main") {
                    // Falhas de monitor/tamanho NUNCA abortam o setup: presença > geometria.
                    match (window.current_monitor(), window.outer_size()) {
                        (Ok(Some(monitor)), Ok(size)) => {
                            let area = monitor.work_area();
                            let x =
                                area.position.x + area.size.width as i32 - size.width as i32 - 16;
                            let y =
                                area.position.y + area.size.height as i32 - size.height as i32 - 12;
                            let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
                        }
                        (monitor, size) => {
                            let monitor_name = match monitor {
                                Ok(Some(item)) => item.name().map(|name| name.to_string()),
                                _ => None,
                            };
                            log::warn!(
                                "Posicionamento inicial adiado (monitor={:?}, size_ok={})",
                                monitor_name,
                                size.is_ok()
                            );
                        }
                    }
                }
                if !marker.as_os_str().is_empty() {
                    let _ = std::fs::create_dir_all(
                        marker.parent().unwrap_or(std::path::Path::new(".")),
                    );
                    let _ = std::fs::write(&marker, b"1");
                }
            }
            // The window-state plugin may restore a previously hidden tray state
            // or an off-screen position. A deliberate app launch must always
            // bring Desktop Presence back, visible inside the work area.
            if let Some(window) = app.get_webview_window("main") {
                if let Err(error) = app.state::<spout_presence::SpoutPresence>().start(&window) {
                    log::error!("Falha ao iniciar VTube Studio Presence: {error}");
                }
                restore_presence(&window);
                start_global_cursor_tracker(window);
            }
            if let Err(error) = show_dashboard(app.handle()) {
                log::warn!("Falha ao abrir painel inicial empacotado: {error}");
            }
            // §5: garante backend KAZUMI saudável (reuso, conflito ou spawn próprio).
            backend_manager::spawn_supervisor(app.handle().clone());
            // A hidden launcher process can briefly propagate Windows'
            // minimized startup state after WebView creation. Restore once
            // more after the event loop is active so the daily-use shortcut
            // always leaves the packaged dashboard visible.
            let dashboard_app = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(650));
                if presence::show_on_start_enabled() {
                    reassert_presence_after_startup(&dashboard_app);
                }
                if let Err(error) = show_dashboard(&dashboard_app) {
                    log::warn!("Falha ao restaurar painel após startup: {error}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("falha ao iniciar KAZUMI Desktop")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = &event {
                if !app_handle
                    .state::<backend_manager::BackendManager>()
                    .exit_ready()
                {
                    api.prevent_exit();
                    shutdown::request_app_shutdown(
                        app_handle.clone(),
                        shutdown::ShutdownReason::OsShutdown,
                    );
                    return;
                }
            }
            if let tauri::RunEvent::Exit = event {
                // Idempotent final guard. The authoritative coordinator has
                // already stopped Presence and the owned backend.
                stop_global_cursor_tracker();
                app_handle.state::<spout_presence::SpoutPresence>().stop();
            }
        });
}

/// Reafirmação pós-startup (idempotente): só atua/loga se a janela terminou
/// oculta ou minimizada. Evita o "Desktop Presence inicializado" duplicado.
fn reassert_presence_after_startup(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let visible = window.is_visible().unwrap_or(false);
    let minimized = window.is_minimized().unwrap_or(false);
    if visible && !minimized {
        return;
    }
    log::info!(
        "Desktop Presence reafirmado pós-startup (visible={visible}, minimized={minimized})"
    );
    restore_presence(&window);
}

fn restore_presence(window: &tauri::WebviewWindow) {
    let _ = window.unminimize();
    // Proteção off-screen: posição restaurada pelo plugin é validada contra a
    // área de trabalho do monitor atual (#87/#88/#89).
    if let (Ok(position), Ok(size)) = (window.outer_position(), window.outer_size()) {
        if let Ok(Some(monitor)) = window.current_monitor() {
            let area = monitor.work_area();
            let (x, y) = presence::clamp_position_into_work_area(
                position.x,
                position.y,
                size.width as i32,
                size.height as i32,
                area.position.x,
                area.position.y,
                area.size.width as i32,
                area.size.height as i32,
            );
            if (x, y) != (position.x, position.y) {
                log::info!(
                    "Presença fora da tela reposicionada: ({}, {}) -> ({}, {})",
                    position.x,
                    position.y,
                    x,
                    y
                );
                let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
            }
        }
    }
    if let Err(error) = window.show() {
        log::error!("Falha ao mostrar Desktop Presence: {error}");
    }
    if let Err(error) = window.set_always_on_top(true) {
        log::warn!("Falha ao restaurar always-on-top: {error}");
    }
    emit_presence_state(window, "VISIBLE");
    log::info!(
        "Desktop Presence inicializado (visible={:?}, minimized={:?})",
        window.is_visible(),
        window.is_minimized()
    );
}

#[cfg(test)]
mod cursor_tests {
    use super::{normalize_virtual_cursor, ScreenBounds};

    #[test]
    fn virtual_desktop_center_is_neutral_and_axes_are_correct() {
        let bounds = ScreenBounds {
            x: -1920,
            y: 0,
            width: 3841,
            height: 1081,
        };
        assert_eq!(normalize_virtual_cursor(0, 540, bounds), (0.0, 0.0));
        assert_eq!(normalize_virtual_cursor(-1920, 0, bounds), (-1.0, 1.0));
        assert_eq!(normalize_virtual_cursor(1920, 1080, bounds), (1.0, -1.0));
    }

    #[test]
    fn virtual_desktop_normalization_clamps_outside_coordinates() {
        let bounds = ScreenBounds {
            x: 0,
            y: 0,
            width: 1920,
            height: 1080,
        };
        assert_eq!(normalize_virtual_cursor(-500, -500, bounds), (-1.0, 1.0));
        assert_eq!(normalize_virtual_cursor(5000, 5000, bounds), (1.0, -1.0));
    }
}
