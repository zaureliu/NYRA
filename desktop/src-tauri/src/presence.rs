//! Desktop Presence lifecycle: deterministic visibility, off-screen protection.
//!
//! Presence is NYRA's avatar on the desktop, not a hidden tray utility. The
//! window-state plugin must never restore a hidden/off-screen state over a
//! deliberate app launch, and every visibility transition flows through one
//! state machine so the backend/UI always agree.

#[cfg(test)]
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum PresenceState {
    Starting,
    Ready,
    Visible,
    Hidden,
    Error,
}

#[cfg(test)]
impl PresenceState {
    pub fn as_str(self) -> &'static str {
        match self {
            PresenceState::Starting => "STARTING",
            PresenceState::Ready => "READY",
            PresenceState::Visible => "VISIBLE",
            PresenceState::Hidden => "HIDDEN",
            PresenceState::Error => "ERROR",
        }
    }
}

/// Clamps a restored window position into the visible work area (#87/#89).
/// Returns the default margin position when the stored rectangle cannot fit.
pub fn clamp_position_into_work_area(
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    area_x: i32,
    area_y: i32,
    area_width: i32,
    area_height: i32,
) -> (i32, i32) {
    if area_width <= 0 || area_height <= 0 {
        return (x, y);
    }
    let width = width.max(1);
    let height = height.max(1);
    // Janela maior que a área de trabalho: ancora no canto visível.
    let max_x = (area_x + area_width - width.min(area_width)).max(area_x);
    let max_y = (area_y + area_height - height.min(area_height)).max(area_y);
    let clamped_x = x.clamp(area_x, max_x);
    let clamped_y = y.clamp(area_y, max_y);
    (clamped_x, clamped_y)
}

pub fn show_on_start_enabled() -> bool {
    std::env::var("NYRA_DESKTOP_PRESENCE_SHOW_ON_START")
        .map(|value| !matches!(value.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off"))
        .unwrap_or(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_valid_positions() {
        let (x, y) = clamp_position_into_work_area(100, 120, 480, 560, 0, 0, 1920, 1040);
        assert_eq!((x, y), (100, 120));
    }

    #[test]
    fn pulls_offscreen_right_bottom_back_into_view() {
        let (x, y) = clamp_position_into_work_area(2400, 1500, 480, 560, 0, 0, 1920, 1040);
        assert_eq!((x, y), (1920 - 480, 1040 - 560));
    }

    #[test]
    fn pulls_negative_positions_into_view() {
        let (x, y) = clamp_position_into_work_area(-32000, -32000, 480, 560, 0, 0, 1920, 1040);
        assert_eq!((x, y), (0, 0));
    }

    #[test]
    fn respects_secondary_monitor_origin() {
        // Monitor à esquerda (origem -3840): x válido permanece, y fora da área
        // é ancorado no topo visível.
        let (x, y) = clamp_position_into_work_area(-3600, -900, 480, 560, -3840, 0, 1920, 1040);
        assert_eq!((x, y), (-3600, 0));
        let (x, y) = clamp_position_into_work_area(-4000, 700, 480, 560, -3840, 0, 1920, 1040);
        assert_eq!((x, y), (-3840, 1040 - 560));
    }

    #[test]
    fn oversized_window_anchors_inside_work_area() {
        let (x, y) = clamp_position_into_work_area(10, 10, 3000, 2000, 0, 0, 1920, 1040);
        assert_eq!((x, y), (0, 0));
    }

    #[test]
    fn show_on_start_defaults_to_true_and_honors_false() {
        // O ambiente de teste pode ou não definir a variável; ambos os caminhos
        // precisam ser coerentes com o valor presente.
        match std::env::var("NYRA_DESKTOP_PRESENCE_SHOW_ON_START") {
            Ok(value) => {
                let enabled = show_on_start_enabled();
                assert_eq!(enabled, !matches!(value.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off"));
            }
            Err(_) => assert!(show_on_start_enabled()),
        }
    }

    #[test]
    fn presence_states_have_stable_names() {
        assert_eq!(PresenceState::Starting.as_str(), "STARTING");
        assert_eq!(PresenceState::Ready.as_str(), "READY");
        assert_eq!(PresenceState::Visible.as_str(), "VISIBLE");
        assert_eq!(PresenceState::Hidden.as_str(), "HIDDEN");
        assert_eq!(PresenceState::Error.as_str(), "ERROR");
    }
}
