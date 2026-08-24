use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_window_state::{StateFlags, WindowExt};

mod backend_manager;
mod presence;

static CLICK_THROUGH: AtomicBool = AtomicBool::new(false);

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
    window_bounds: ScreenBounds,
    window_monitor_bounds: ScreenBounds,
    cursor_monitor_bounds: ScreenBounds,
    monitor_changed: bool,
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
        .title("NYRA")
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

fn emit_presence_state(window: &tauri::WebviewWindow, state: &str) {
    let _ = window.emit("nyra-presence", state);
}

#[tauri::command]
fn set_click_through(window: tauri::WebviewWindow, enabled: bool) -> Result<(), String> {
    CLICK_THROUGH.store(enabled, Ordering::SeqCst);
    window
        .set_ignore_cursor_events(enabled)
        .map_err(|error| error.to_string())
}

#[cfg(windows)]
fn native_cursor_position() -> Option<(i32, i32, ScreenBounds)> {
    use std::mem::{size_of, zeroed};
    use windows_sys::Win32::Foundation::POINT;
    use windows_sys::Win32::Graphics::Gdi::{
        GetMonitorInfoW, MonitorFromPoint, MONITORINFO, MONITOR_DEFAULTTONEAREST,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::GetCursorPos;

    let mut point = POINT { x: 0, y: 0 };
    if unsafe { GetCursorPos(&mut point) } == 0 {
        return None;
    }
    let monitor = unsafe { MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST) };
    let mut info: MONITORINFO = unsafe { zeroed() };
    info.cbSize = size_of::<MONITORINFO>() as u32;
    if monitor.is_null() || unsafe { GetMonitorInfoW(monitor, &mut info) } == 0 {
        return None;
    }
    let bounds = ScreenBounds {
        x: info.rcMonitor.left,
        y: info.rcMonitor.top,
        width: (info.rcMonitor.right - info.rcMonitor.left).max(0) as u32,
        height: (info.rcMonitor.bottom - info.rcMonitor.top).max(0) as u32,
    };
    Some((point.x, point.y, bounds))
}

#[cfg(not(windows))]
fn native_cursor_position() -> Option<(i32, i32, ScreenBounds)> {
    None
}

fn start_global_cursor_tracker(window: tauri::WebviewWindow) {
    let _ = std::thread::Builder::new()
        .name("nyra-global-cursor".into())
        .spawn(move || {
            let mut previous: Option<(i32, i32)> = None;
            let mut unavailable_reported_at: Option<Instant> = None;
            let mut window_error_reported = false;
            let mut first_sample_reported = false;
            loop {
                if !window.is_visible().unwrap_or(false) {
                    std::thread::sleep(Duration::from_millis(250));
                    continue;
                }
                let Some((cursor_x, cursor_y, cursor_monitor_bounds)) = native_cursor_position()
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
                            "nyra-global-cursor",
                            GlobalCursorSample {
                                available: false,
                                cursor_x: 0,
                                cursor_y: 0,
                                normalized_x: 0.0,
                                normalized_y: 0.0,
                                window_bounds: empty,
                                window_monitor_bounds: empty,
                                cursor_monitor_bounds: empty,
                                monitor_changed: false,
                            },
                        );
                    }
                    std::thread::sleep(Duration::from_millis(250));
                    continue;
                };
                unavailable_reported_at = None;
                if previous == Some((cursor_x, cursor_y)) {
                    std::thread::sleep(Duration::from_millis(33));
                    continue;
                }
                previous = Some((cursor_x, cursor_y));

                let position = match window.outer_position() {
                    Ok(value) => value,
                    Err(error) => {
                        if !window_error_reported {
                            log::warn!("Global cursor sem posição da janela: {error}");
                            window_error_reported = true;
                        }
                        std::thread::sleep(Duration::from_millis(33));
                        continue;
                    }
                };
                let size = match window.outer_size() {
                    Ok(value) => value,
                    Err(error) => {
                        if !window_error_reported {
                            log::warn!("Global cursor sem tamanho da janela: {error}");
                            window_error_reported = true;
                        }
                        std::thread::sleep(Duration::from_millis(33));
                        continue;
                    }
                };
                window_error_reported = false;
                let window_bounds = ScreenBounds {
                    x: position.x,
                    y: position.y,
                    width: size.width,
                    height: size.height,
                };
                let window_monitor_bounds = window
                    .current_monitor()
                    .ok()
                    .flatten()
                    .map(|monitor| {
                        let position = monitor.position();
                        let size = monitor.size();
                        ScreenBounds {
                            x: position.x,
                            y: position.y,
                            width: size.width,
                            height: size.height,
                        }
                    })
                    .unwrap_or(cursor_monitor_bounds);
                let avatar_x = window_bounds.x as f64 + window_bounds.width as f64 * 0.5;
                let avatar_y = window_bounds.y as f64 + window_bounds.height as f64 * 0.43;
                let half_width = (window_monitor_bounds.width as f64 * 0.5).max(1.0);
                let half_height = (window_monitor_bounds.height as f64 * 0.5).max(1.0);
                let normalized_x = ((cursor_x as f64 - avatar_x) / half_width).clamp(-1.0, 1.0);
                let normalized_y = ((cursor_y as f64 - avatar_y) / half_height).clamp(-1.0, 1.0);
                let monitor_changed = cursor_monitor_bounds.x != window_monitor_bounds.x
                    || cursor_monitor_bounds.y != window_monitor_bounds.y
                    || cursor_monitor_bounds.width != window_monitor_bounds.width
                    || cursor_monitor_bounds.height != window_monitor_bounds.height;
                let emitted = window.emit(
                    "nyra-global-cursor",
                    GlobalCursorSample {
                        available: true,
                        cursor_x,
                        cursor_y,
                        normalized_x,
                        normalized_y,
                        window_bounds,
                        window_monitor_bounds,
                        cursor_monitor_bounds,
                        monitor_changed,
                    },
                );
                if let Err(error) = emitted {
                    log::warn!("Falha ao emitir cursor global: {error}");
                } else if !first_sample_reported {
                    first_sample_reported = true;
                    log::info!(
                        "Cursor global ativo (cursor=({}, {}), monitor=({}, {}, {}x{}))",
                        cursor_x,
                        cursor_y,
                        cursor_monitor_bounds.x,
                        cursor_monitor_bounds.y,
                        cursor_monitor_bounds.width,
                        cursor_monitor_bounds.height,
                    );
                }
                std::thread::sleep(Duration::from_millis(33));
            }
        });
}

pub fn run() {
    tauri::Builder::default()
        .manage(backend_manager::BackendManager::new())
        .invoke_handler(tauri::generate_handler![
            set_click_through,
            open_dashboard,
            presence_show,
            presence_hide,
            presence_toggle,
            presence_status_command
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
                .with_state_flags(StateFlags::SIZE | StateFlags::POSITION | StateFlags::MAXIMIZED | StateFlags::FULLSCREEN | StateFlags::DECORATIONS)
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
                                "nyra-desktop",
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
                            let _ = window.emit("nyra-desktop", "mic-toggle");
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
                                "nyra-desktop",
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
        .setup(|app| {
            let show = MenuItemBuilder::with_id("show", "Mostrar NYRA").build(app)?;
            let hide = MenuItemBuilder::with_id("hide", "Ocultar NYRA").build(app)?;
            let interactive =
                MenuItemBuilder::with_id("interactive", "Modo interativo").build(app)?;
            let click = MenuItemBuilder::with_id("click", "Click-through").build(app)?;
            let top = MenuItemBuilder::with_id("top", "Alternar always on top").build(app)?;
            let panel = MenuItemBuilder::with_id("panel", "Abrir painel").build(app)?;
            let talk = MenuItemBuilder::with_id("talk", "Falar com NYRA").build(app)?;
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
            let quit = MenuItemBuilder::with_id("quit", "Encerrar NYRA").build(app)?;
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
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .on_menu_event(|app, event| {
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
                                let _ = window.emit("nyra-desktop", "interactive");
                            }
                            "click" => {
                                CLICK_THROUGH.store(true, Ordering::SeqCst);
                                let _ = window.set_ignore_cursor_events(true);
                                let _ = window.emit("nyra-desktop", "click-through");
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
                                let _ = window.emit("nyra-desktop", "talk-menu");
                            }
                            "listening" => {
                                let _ = window.emit("nyra-desktop", "listening-toggle");
                            }
                            "network" => {
                                let _ = window.emit("nyra-desktop", "network-toggle");
                            }
                            "sentinel" => {
                                let _ = window.emit("nyra-desktop", "sentinel-toggle");
                            }
                            "sentinel-reconnect" => {
                                let _ = window.emit("nyra-desktop", "sentinel-reconnect");
                            }
                            "sentinel-open" => {
                                let _ = window.emit("nyra-desktop", "sentinel-open");
                            }
                            "quiet" => {
                                let _ = window.emit("nyra-desktop", "quiet-toggle");
                            }
                            "settings" => {
                                let _ = window.set_ignore_cursor_events(false);
                                let _ = window.show();
                                let _ = window.emit("nyra-desktop", "settings");
                            }
                            "reconnect" => {
                                let _ = window.emit("nyra-desktop", "reconnect");
                            }
                            "quit" => {
                                // §6: marca shutdown ANTES de sair para o
                                // supervisor não relançar o backend owned.
                                app.state::<backend_manager::BackendManager>()
                                    .mark_shutting_down();
                                app.exit(0);
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
            for shortcut in shortcuts {
                if let Err(error) = app.global_shortcut().register(shortcut) {
                    log::warn!("Atalho global indisponível: {error}");
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
                    app.path().temp_dir().ok().map(|dir| dir.join("nyra-position-initialized")).unwrap_or_default()
                }
            };
            if !marker.exists() {
                if let Some(window) = app.get_webview_window("main") {
                    // Falhas de monitor/tamanho NUNCA abortam o setup: presença > geometria.
                    match (window.current_monitor(), window.outer_size()) {
                        (Ok(Some(monitor)), Ok(size)) => {
                            let area = monitor.work_area();
                            let x = area.position.x + area.size.width as i32 - size.width as i32 - 16;
                            let y = area.position.y + area.size.height as i32 - size.height as i32 - 12;
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
                    let _ = std::fs::create_dir_all(marker.parent().unwrap_or(std::path::Path::new(".")));
                    let _ = std::fs::write(&marker, b"1");
                }
            }
            // The window-state plugin may restore a previously hidden tray state
            // or an off-screen position. A deliberate app launch must always
            // bring Desktop Presence back, visible inside the work area.
            if let Some(window) = app.get_webview_window("main") {
                restore_presence(&window);
                start_global_cursor_tracker(window);
            }
            if let Err(error) = show_dashboard(app.handle()) {
                log::warn!("Falha ao abrir painel inicial empacotado: {error}");
            }
            // §5: garante backend NYRA saudável (reuso, conflito ou spawn próprio).
            backend_manager::spawn_supervisor(app.handle().clone());
            // A hidden launcher process can briefly propagate Windows'
            // minimized startup state after WebView creation. Restore once
            // more after the event loop is active so the daily-use shortcut
            // always leaves the packaged dashboard visible.
            let dashboard_app = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(650));
                if presence::show_on_start_enabled() {
                    if let Some(window) = dashboard_app.get_webview_window("main") {
                        restore_presence(&window);
                    }
                }
                if let Err(error) = show_dashboard(&dashboard_app) {
                    log::warn!("Falha ao restaurar painel apÃ³s startup: {error}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("falha ao iniciar NYRA Desktop")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                // §6: backend owned é encerrado e a porta 8000 liberada.
                backend_manager::shutdown_owned(app_handle);
            }
        });
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
                log::info!("Presença fora da tela reposicionada: ({}, {}) -> ({}, {})", position.x, position.y, x, y);
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
