#pragma once

#include <cstdint>

// C ABI kept intentionally small so the official Tauri Presence owns exactly
// one native receiver without a sidecar process or a second taskbar window.
extern "C" {

struct NyraSpoutStatus {
    int32_t state;
    int32_t alpha;
    int32_t adapter_match;
    int32_t format;
    uint32_t width;
    uint32_t height;
    double sender_fps;
    double receiver_fps;
    uint64_t frame_count;
    uint64_t dropped_frames;
    uint64_t last_frame_age_ms;
    uint64_t memory_bytes;
    char sender[256];
    char sender_adapter[256];
    char receiver_adapter[256];
    char error[128];
};

bool nyra_spout_start(void* owner_hwnd);
void nyra_spout_stop();
void nyra_spout_configure(
    const char* mode,
    const char* sender,
    float scale,
    float offset_x,
    float offset_y,
    uint32_t watchdog_seconds);
void nyra_spout_get_status(NyraSpoutStatus* status);
void nyra_spout_set_internal_visible(bool visible);

}
