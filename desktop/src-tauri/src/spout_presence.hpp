#pragma once

#include <cstdint>

// C ABI kept intentionally small so the official Tauri Presence owns exactly
// one native receiver without a sidecar process or a second taskbar window.
extern "C" {

struct KazumiSpoutStatus {
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

bool kazumi_spout_start(void* owner_hwnd);
void kazumi_spout_stop();
void kazumi_spout_configure(
    const char* sender,
    float scale,
    float offset_x,
    float offset_y,
    uint32_t watchdog_seconds);
void kazumi_spout_get_status(KazumiSpoutStatus* status);

}
