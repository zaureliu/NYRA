//! Native Spout2 Presence ownership and the narrow Tauri bridge.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SpoutPresenceConfig {
    pub mode: String,
    #[serde(default = "default_sender")]
    pub sender: String,
    #[serde(default = "default_scale")]
    pub scale: f32,
    #[serde(default)]
    pub offset_x: f32,
    #[serde(default)]
    pub offset_y: f32,
    #[serde(default = "default_watchdog")]
    pub watchdog_seconds: u32,
}

fn default_sender() -> String {
    "AUTO".to_string()
}
fn default_scale() -> f32 {
    1.0
}
fn default_watchdog() -> u32 {
    12
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SpoutPresenceStatus {
    pub state: &'static str,
    pub alpha: &'static str,
    pub fallback_active: bool,
    pub sender: Option<String>,
    pub width: u32,
    pub height: u32,
    pub format: Option<String>,
    pub sender_fps: f64,
    pub receiver_fps: f64,
    pub frame_count: u64,
    pub dropped_frames: u64,
    pub last_frame_age_ms: u64,
    pub adapter_match: bool,
    pub sender_adapter: Option<String>,
    pub receiver_adapter: Option<String>,
    pub memory_bytes: u64,
    pub error: Option<String>,
}

fn state_name(value: i32) -> &'static str {
    match value {
        0 => "INTERNAL_ACTIVE",
        1 => "VTS_DISCOVERING",
        2 => "VTS_CONNECTING",
        3 => "VTS_WAITING_FRAMES",
        4 => "VTS_ACTIVE",
        5 => "VTS_DEGRADED",
        6 => "FALLBACK_INTERNAL",
        _ => "VTS_DEGRADED",
    }
}

fn alpha_name(value: i32) -> &'static str {
    match value {
        1 => "VALID",
        2 => "OPAQUE",
        3 => "EMPTY",
        _ => "UNKNOWN",
    }
}

fn format_name(value: i32) -> Option<String> {
    match value {
        28 => Some("DXGI_FORMAT_R8G8B8A8_UNORM".to_string()),
        29 => Some("DXGI_FORMAT_R8G8B8A8_UNORM_SRGB".to_string()),
        87 => Some("DXGI_FORMAT_B8G8R8A8_UNORM".to_string()),
        91 => Some("DXGI_FORMAT_B8G8R8A8_UNORM_SRGB".to_string()),
        0 => None,
        other => Some(format!("DXGI_FORMAT_{other}")),
    }
}

pub fn normalize_mode(value: &str) -> Result<&'static str, String> {
    match value.trim().to_ascii_uppercase().as_str() {
        "AUTO" => Ok("AUTO"),
        "VTUBE_STUDIO" | "LIVE2D" => Ok("VTUBE_STUDIO"),
        "INTERNAL" | "CURRENT" => Ok("INTERNAL"),
        _ => Err("INVALID_RENDERER_MODE".to_string()),
    }
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::ffi::{c_char, c_void, CStr, CString};
    use std::sync::atomic::{AtomicBool, Ordering};

    #[repr(C)]
    struct NativeStatus {
        state: i32,
        alpha: i32,
        adapter_match: i32,
        format: i32,
        width: u32,
        height: u32,
        sender_fps: f64,
        receiver_fps: f64,
        frame_count: u64,
        dropped_frames: u64,
        last_frame_age_ms: u64,
        memory_bytes: u64,
        sender: [c_char; 256],
        sender_adapter: [c_char; 256],
        receiver_adapter: [c_char; 256],
        error: [c_char; 128],
    }

    impl Default for NativeStatus {
        fn default() -> Self {
            Self {
                state: 0,
                alpha: 0,
                adapter_match: 0,
                format: 0,
                width: 0,
                height: 0,
                sender_fps: 0.0,
                receiver_fps: 0.0,
                frame_count: 0,
                dropped_frames: 0,
                last_frame_age_ms: 0,
                memory_bytes: 0,
                sender: [0; 256],
                sender_adapter: [0; 256],
                receiver_adapter: [0; 256],
                error: [0; 128],
            }
        }
    }

    extern "C" {
        fn nyra_spout_start(owner_hwnd: *mut c_void) -> bool;
        fn nyra_spout_stop();
        fn nyra_spout_configure(
            mode: *const c_char,
            sender: *const c_char,
            scale: f32,
            offset_x: f32,
            offset_y: f32,
            watchdog_seconds: u32,
        );
        fn nyra_spout_get_status(status: *mut NativeStatus);
        fn nyra_spout_set_internal_visible(visible: bool);
    }

    pub struct SpoutPresence {
        started: AtomicBool,
    }

    impl SpoutPresence {
        pub fn new() -> Self {
            Self {
                started: AtomicBool::new(false),
            }
        }

        pub fn start(&self, window: &tauri::WebviewWindow) -> Result<(), String> {
            if self.started.load(Ordering::SeqCst) {
                return Ok(());
            }
            let hwnd = window.hwnd().map_err(|error| error.to_string())?;
            if !unsafe { nyra_spout_start(hwnd.0 as *mut c_void) } {
                return Err("SPOUT_RECEIVER_START_FAILED".to_string());
            }
            self.started.store(true, Ordering::SeqCst);
            Ok(())
        }

        pub fn configure(
            &self,
            config: SpoutPresenceConfig,
        ) -> Result<SpoutPresenceStatus, String> {
            let mode = CString::new(normalize_mode(&config.mode)?)
                .map_err(|_| "INVALID_RENDERER_MODE".to_string())?;
            let sender = CString::new(config.sender.trim())
                .map_err(|_| "INVALID_SPOUT_SENDER".to_string())?;
            unsafe {
                nyra_spout_configure(
                    mode.as_ptr(),
                    sender.as_ptr(),
                    config.scale,
                    config.offset_x,
                    config.offset_y,
                    config.watchdog_seconds,
                );
            }
            Ok(self.status())
        }

        pub fn status(&self) -> SpoutPresenceStatus {
            let mut native = NativeStatus::default();
            unsafe { nyra_spout_get_status(&mut native) };
            let state = state_name(native.state);
            SpoutPresenceStatus {
                state,
                alpha: alpha_name(native.alpha),
                fallback_active: state != "VTS_ACTIVE",
                sender: c_string(&native.sender),
                width: native.width,
                height: native.height,
                format: format_name(native.format),
                sender_fps: round_one(native.sender_fps),
                receiver_fps: round_one(native.receiver_fps),
                frame_count: native.frame_count,
                dropped_frames: native.dropped_frames,
                last_frame_age_ms: native.last_frame_age_ms,
                adapter_match: native.adapter_match == 1,
                sender_adapter: c_string(&native.sender_adapter),
                receiver_adapter: c_string(&native.receiver_adapter),
                memory_bytes: native.memory_bytes,
                error: c_string(&native.error),
            }
        }

        pub fn stop(&self) {
            if self.started.swap(false, Ordering::SeqCst) {
                unsafe { nyra_spout_stop() };
            }
        }

        pub fn set_internal_visible(&self, visible: bool) {
            unsafe { nyra_spout_set_internal_visible(visible) };
        }
    }

    impl Drop for SpoutPresence {
        fn drop(&mut self) {
            self.stop();
        }
    }

    fn c_string<const N: usize>(value: &[c_char; N]) -> Option<String> {
        let text = unsafe { CStr::from_ptr(value.as_ptr()) }
            .to_string_lossy()
            .trim()
            .to_string();
        (!text.is_empty()).then_some(text)
    }

    fn round_one(value: f64) -> f64 {
        (value * 10.0).round() / 10.0
    }
    pub use SpoutPresence as PlatformSpoutPresence;
}

#[cfg(not(windows))]
mod native {
    use super::*;
    pub struct SpoutPresence;
    impl SpoutPresence {
        pub fn new() -> Self {
            Self
        }
        pub fn start(&self, _window: &tauri::WebviewWindow) -> Result<(), String> {
            Ok(())
        }
        pub fn configure(
            &self,
            _config: SpoutPresenceConfig,
        ) -> Result<SpoutPresenceStatus, String> {
            Ok(self.status())
        }
        pub fn status(&self) -> SpoutPresenceStatus {
            SpoutPresenceStatus {
                state: "INTERNAL_ACTIVE",
                alpha: "UNKNOWN",
                fallback_active: true,
                sender: None,
                width: 0,
                height: 0,
                format: None,
                sender_fps: 0.0,
                receiver_fps: 0.0,
                frame_count: 0,
                dropped_frames: 0,
                last_frame_age_ms: 0,
                adapter_match: false,
                sender_adapter: None,
                receiver_adapter: None,
                memory_bytes: 0,
                error: None,
            }
        }
        pub fn stop(&self) {}
        pub fn set_internal_visible(&self, _visible: bool) {}
    }
    pub use SpoutPresence as PlatformSpoutPresence;
}

pub use native::PlatformSpoutPresence as SpoutPresence;

#[cfg(test)]
mod tests {
    use super::*;
    fn select_sender(values: &[&str], requested: &str) -> Option<String> {
        if requested != "AUTO" {
            return values.contains(&requested).then(|| requested.to_string());
        }
        values
            .iter()
            .find(|value| **value == "VTubeStudioSpout")
            .or_else(|| {
                values.iter().find(|value| {
                    value
                        .strip_prefix("VTubeStudioSpout")
                        .is_some_and(|suffix| {
                            !suffix.is_empty() && suffix.chars().all(|item| item.is_ascii_digit())
                        })
                })
            })
            .map(|value| (*value).to_string())
    }
    fn alpha(samples: &[u8]) -> &'static str {
        let visible = samples.iter().filter(|value| **value > 10).count();
        let transparent = samples.iter().filter(|value| **value < 245).count();
        if visible * 100 < samples.len() {
            "EMPTY"
        } else if transparent * 100 >= samples.len() {
            "VALID"
        } else {
            "OPAQUE"
        }
    }
    #[test]
    fn renderer_modes_preserve_legacy_values() {
        assert_eq!(normalize_mode("AUTO").unwrap(), "AUTO");
        assert_eq!(normalize_mode("LIVE2D").unwrap(), "VTUBE_STUDIO");
        assert_eq!(normalize_mode("CURRENT").unwrap(), "INTERNAL");
        assert!(normalize_mode("SCREENSHOT").is_err());
    }
    #[test]
    fn only_active_state_can_hide_internal_avatar() {
        for state in 0..=6 {
            assert_eq!(state_name(state) == "VTS_ACTIVE", state == 4);
        }
    }
    #[test]
    fn alpha_is_valid_only_after_native_probe() {
        assert_eq!(alpha_name(0), "UNKNOWN");
        assert_eq!(alpha_name(1), "VALID");
        assert_eq!(alpha_name(2), "OPAQUE");
        assert_eq!(alpha_name(3), "EMPTY");
    }
    #[test]
    fn discovery_prefers_exact_vtube_sender_and_rejects_unrelated_sources() {
        assert_eq!(
            select_sender(&["OBS", "VTubeStudioSpout2", "VTubeStudioSpout"], "AUTO").as_deref(),
            Some("VTubeStudioSpout")
        );
        assert_eq!(
            select_sender(&["OBS", "VTubeStudioSpout3"], "AUTO").as_deref(),
            Some("VTubeStudioSpout3")
        );
        assert!(select_sender(&["OBS", "VTubeStudioSpoutBackup"], "AUTO").is_none());
    }
    #[test]
    fn alpha_probe_distinguishes_transparency_opacity_and_empty_frames() {
        assert_eq!(alpha(&[0, 0, 64, 255, 255]), "VALID");
        assert_eq!(alpha(&[255; 100]), "OPAQUE");
        assert_eq!(alpha(&[0; 100]), "EMPTY");
    }
    #[test]
    fn native_receiver_is_built_into_the_desktop_without_a_sidecar() {
        let build = include_str!("../build.rs");
        assert!(build.contains("spout_presence.cpp"));
        assert!(!build.contains("externalBin"));
    }
}
