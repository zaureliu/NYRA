//! Authoritative desktop shutdown coordinator.
//!
//! Every full-exit source delegates here. In particular, the native tray
//! item and the dashboard command share this exact path. Window close remains
//! a separate hide-to-tray policy in `lib.rs`.

use tauri::{AppHandle, Manager};

use crate::{backend_manager, spout_presence, stop_global_cursor_tracker};

pub const TRAY_ID: &str = "nyra-tray";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ShutdownReason {
    TrayExit,
    UiExit,
    OsShutdown,
}

impl ShutdownReason {
    fn label(self) -> &'static str {
        match self {
            Self::TrayExit => "tray_exit",
            Self::UiExit => "ui_exit",
            Self::OsShutdown => "os_shutdown",
        }
    }
}

/// Request one full application exit without blocking the tray/UI callback.
///
/// The one-shot guard lives in `BackendManager`, beside the owned child state,
/// so repeated tray, UI, or OS requests cannot race cleanup or target a reused
/// PID. Tauri exit is requested only after bounded backend shutdown and native
/// Presence cleanup have completed.
pub fn request_app_shutdown(app: AppHandle, reason: ShutdownReason) -> bool {
    if !app
        .state::<backend_manager::BackendManager>()
        .request_exit()
    {
        log::info!(
            "shutdown_request_ignored reason={} state=already_started",
            reason.label()
        );
        return false;
    }

    log::info!("shutdown_requested reason={}", reason.label());
    log::info!("shutdown_started");

    let shutdown_app = app.clone();
    let spawned = std::thread::Builder::new()
        .name("nyra-shutdown".into())
        .spawn(move || {
            stop_global_cursor_tracker();
            shutdown_app.state::<crate::stt_transport::SttBridge>().stop();
            let backend_complete = backend_manager::shutdown_owned(&shutdown_app);
            if !backend_complete {
                log::error!("shutdown_incomplete backend_child_still_alive");
            }

            log::info!("presence_shutdown_started");
            shutdown_app.state::<spout_presence::SpoutPresence>().stop();
            log::info!("presence_shutdown_complete");

            // A stable tray id allows deterministic resource removal before
            // the event loop exits; dropping the returned icon releases its
            // native callbacks and handle.
            drop(shutdown_app.remove_tray_by_id(TRAY_ID));
            log::info!("tray_destroyed");

            let windows = shutdown_app.webview_windows();
            let window_count = windows.len();
            for window in windows.values() {
                if let Err(error) = window.destroy() {
                    log::warn!(
                        "window_destroy_failed label={} error={error}",
                        window.label()
                    );
                }
            }
            log::info!("windows_closed count={window_count}");

            shutdown_app
                .state::<backend_manager::BackendManager>()
                .mark_exit_ready();
            log::info!("tauri_exit_requested");
            log::info!("shutdown_finalizing backend_complete={backend_complete}");
            shutdown_app.exit(0);
        });

    if let Err(error) = spawned {
        // Thread creation failure is exceptionally rare. The Job Object still
        // provides the owned-child fail-safe when the desktop exits.
        log::error!("shutdown_thread_failed error={error}");
        app.state::<backend_manager::BackendManager>()
            .mark_exit_ready();
        app.exit(1);
    }
    true
}

#[cfg(test)]
mod tests {
    use super::ShutdownReason;

    #[test]
    fn shutdown_sources_have_stable_log_labels() {
        assert_eq!(ShutdownReason::TrayExit.label(), "tray_exit");
        assert_eq!(ShutdownReason::UiExit.label(), "ui_exit");
        assert_eq!(ShutdownReason::OsShutdown.label(), "os_shutdown");
    }
}
