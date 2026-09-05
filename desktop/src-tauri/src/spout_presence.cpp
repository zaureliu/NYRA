// KAZUMI VTube Studio Presence receiver.
//
// Spout interoperability is implemented from the public wire contract in the
// official leadedge/Spout2 SDK, pinned for this implementation to 2.007.017
// (commit f49e2f469f8cb25f559a6eaa61a3f5b8173fc100). In particular, see
// SpoutSenderNames.{h,cpp}, SpoutSharedMemory.cpp and SpoutFrameCount.cpp.
// No frame is read back to the CPU except the infrequent alpha-validity probe.

#include "spout_presence.hpp"

#define NOMINMAX
#include <windows.h>
#include <d2d1_1.h>
#include <d3d11.h>
#include <dcomp.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;
using Clock = std::chrono::steady_clock;

namespace {

constexpr wchar_t kOverlayClass[] = L"KAZUMI.SpoutPresence.Overlay";
constexpr uint32_t kSenderNameBytes = 256;
constexpr uint32_t kSenderInfoBytes = 280;

enum class RendererState : int32_t {
    VtsOffline = 0,
    VtsDiscovering = 1,
    VtsConnecting = 2,
    VtsWaitingFrames = 3,
    VtsActive = 4,
    VtsDegraded = 5,
    VtsUnavailable = 6,
};

enum class AlphaState : int32_t { Unknown = 0, Valid = 1, Opaque = 2, Empty = 3 };

#pragma pack(push, 1)
struct SharedTextureInfo {
    uint32_t share_handle;
    uint32_t width;
    uint32_t height;
    uint32_t format;
    uint32_t usage;
    uint8_t description[256];
    uint32_t partner_id;
};
#pragma pack(pop)
static_assert(sizeof(SharedTextureInfo) == kSenderInfoBytes, "Spout SharedTextureInfo ABI changed");

struct Config {
    std::string sender = "AUTO";
    float scale = 1.0f;
    float offset_x = 0.0f;
    float offset_y = 0.0f;
    uint32_t watchdog_seconds = 12;

    bool operator==(const Config& other) const {
        return sender == other.sender
            && scale == other.scale && offset_x == other.offset_x
            && offset_y == other.offset_y && watchdog_seconds == other.watchdog_seconds;
    }
    bool operator!=(const Config& other) const { return !(*this == other); }
};

struct Runtime {
    std::atomic<bool> running{false};
    HWND owner = nullptr;
    HWND overlay = nullptr;
    std::thread worker;
    std::mutex config_mutex;
    Config config;
    std::mutex status_mutex;
    KazumiSpoutStatus status{};
};

Runtime g_runtime;

void copy_text(char* destination, size_t size, const std::string& value) {
    if (!destination || size == 0) return;
    strncpy_s(destination, size, value.c_str(), _TRUNCATE);
}

void publish_status(
    RendererState state,
    AlphaState alpha,
    const std::string& error = {},
    const std::string& sender = {}) {
    std::scoped_lock lock(g_runtime.status_mutex);
    g_runtime.status.state = static_cast<int32_t>(state);
    g_runtime.status.alpha = static_cast<int32_t>(alpha);
    copy_text(g_runtime.status.error, sizeof(g_runtime.status.error), error);
    if (!sender.empty()) copy_text(g_runtime.status.sender, sizeof(g_runtime.status.sender), sender);
}

bool lock_named_mutex(const std::string& name, HANDLE& mutex) {
    mutex = OpenMutexA(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, name.c_str());
    if (!mutex) return true; // Spout 1 senders may not publish a mutex.
    const DWORD result = WaitForSingleObject(mutex, 67);
    return result == WAIT_OBJECT_0 || result == WAIT_ABANDONED;
}

void unlock_named_mutex(HANDLE mutex) {
    if (!mutex) return;
    ReleaseMutex(mutex);
    CloseHandle(mutex);
}

bool read_shared_map(const std::string& name, void* output, size_t output_size) {
    HANDLE map = OpenFileMappingA(FILE_MAP_READ, FALSE, name.c_str());
    if (!map) return false;
    const void* view = MapViewOfFile(map, FILE_MAP_READ, 0, 0, 0);
    if (!view) {
        CloseHandle(map);
        return false;
    }
    HANDLE mutex = nullptr;
    const bool locked = lock_named_mutex(name + "_mutex", mutex);
    if (locked) std::memcpy(output, view, output_size);
    unlock_named_mutex(mutex);
    UnmapViewOfFile(view);
    CloseHandle(map);
    return locked;
}

std::vector<std::string> enumerate_senders() {
    std::vector<std::string> senders;
    HANDLE map = OpenFileMappingA(FILE_MAP_READ, FALSE, "SpoutSenderNames");
    if (!map) return senders;
    const char* view = static_cast<const char*>(MapViewOfFile(map, FILE_MAP_READ, 0, 0, 0));
    if (!view) {
        CloseHandle(map);
        return senders;
    }
    MEMORY_BASIC_INFORMATION memory{};
    VirtualQuery(view, &memory, sizeof(memory));
    const size_t bytes = std::min<size_t>(memory.RegionSize, 64 * kSenderNameBytes);
    HANDLE mutex = nullptr;
    if (lock_named_mutex("SpoutSenderNames_mutex", mutex)) {
        for (size_t offset = 0; offset + kSenderNameBytes <= bytes; offset += kSenderNameBytes) {
            const size_t length = strnlen_s(view + offset, kSenderNameBytes);
            if (length == 0) break;
            senders.emplace_back(view + offset, length);
        }
    }
    unlock_named_mutex(mutex);
    UnmapViewOfFile(view);
    CloseHandle(map);
    return senders;
}

bool is_vtube_sender(const std::string& name) {
    constexpr char prefix[] = "VTubeStudioSpout";
    if (name == prefix) return true;
    if (name.rfind(prefix, 0) != 0) return false;
    const auto suffix = name.substr(sizeof(prefix) - 1);
    return !suffix.empty() && std::all_of(suffix.begin(), suffix.end(), [](unsigned char ch) {
        return ch >= '0' && ch <= '9';
    });
}

std::string select_sender(const std::vector<std::string>& senders, const Config& config) {
    if (!config.sender.empty() && config.sender != "AUTO") {
        const auto found = std::find(senders.begin(), senders.end(), config.sender);
        return found == senders.end() ? std::string{} : *found;
    }
    const auto exact = std::find(senders.begin(), senders.end(), "VTubeStudioSpout");
    if (exact != senders.end()) return *exact;
    const auto variant = std::find_if(senders.begin(), senders.end(), is_vtube_sender);
    return variant == senders.end() ? std::string{} : *variant;
}

std::string utf8_adapter_name(const DXGI_ADAPTER_DESC1& description) {
    char buffer[256]{};
    WideCharToMultiByte(CP_UTF8, 0, description.Description, -1, buffer, 256, nullptr, nullptr);
    return buffer;
}

LRESULT CALLBACK overlay_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
        case WM_NCHITTEST: return HTTRANSPARENT;
        case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
        case WM_ERASEBKGND: return 1;
        case WM_CLOSE: DestroyWindow(hwnd); return 0;
        default: return DefWindowProcW(hwnd, message, wparam, lparam);
    }
}

HWND create_overlay(HWND owner) {
    WNDCLASSEXW window_class{};
    window_class.cbSize = sizeof(window_class);
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.lpfnWndProc = overlay_proc;
    window_class.lpszClassName = kOverlayClass;
    window_class.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
    RegisterClassExW(&window_class);
    return CreateWindowExW(
        WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_NOREDIRECTIONBITMAP,
        kOverlayClass,
        L"KAZUMI VTube Studio Presence",
        WS_CHILD,
        0,
        0,
        1,
        1,
        owner,
        nullptr,
        GetModuleHandleW(nullptr),
        nullptr);
}

bool sync_overlay(HWND owner, HWND overlay, bool show, uint32_t& width, uint32_t& height) {
    if (!IsWindow(owner) || !IsWindow(overlay)) return false;
    if (!IsWindowVisible(owner) || IsIconic(owner) || !show) {
        ShowWindow(overlay, SW_HIDE);
        return true;
    }
    RECT rectangle{};
    if (!GetClientRect(owner, &rectangle)) return false;
    width = static_cast<uint32_t>(std::max<LONG>(1, rectangle.right));
    height = static_cast<uint32_t>(std::max<LONG>(1, rectangle.bottom));
    SetWindowPos(
        overlay,
        HWND_BOTTOM,
        0,
        0,
        static_cast<int>(width),
        static_cast<int>(height),
        SWP_NOACTIVATE | SWP_SHOWWINDOW);
    return true;
}

class ReceiverResources {
public:
    ~ReceiverResources() { reset(); }

    void reset() {
        if (frame_semaphore_) CloseHandle(frame_semaphore_);
        frame_semaphore_ = nullptr;
        source_bitmap_.Reset();
        target_bitmap_.Reset();
        local_texture_.Reset();
        shared_texture_.Reset();
        d2d_context_.Reset();
        d2d_device_.Reset();
        d2d_factory_.Reset();
        dcomp_visual_.Reset();
        dcomp_target_.Reset();
        dcomp_device_.Reset();
        swap_chain_.Reset();
        context_.Reset();
        device_.Reset();
        sender_.clear();
        info_ = {};
        adapter_name_.clear();
        target_width_ = target_height_ = 0;
        last_sequence_ = 0;
    }

    bool open(HWND overlay, const std::string& sender, const SharedTextureInfo& info, std::string& error) {
        reset();
        sender_ = sender;
        info_ = info;
        const HANDLE shared_handle = reinterpret_cast<HANDLE>(static_cast<uintptr_t>(info.share_handle));
        ComPtr<IDXGIFactory1> factory;
        HRESULT result = CreateDXGIFactory1(IID_PPV_ARGS(&factory));
        if (FAILED(result)) {
            error = "DXGI_FACTORY_FAILED";
            return false;
        }

        for (UINT index = 0;; ++index) {
            ComPtr<IDXGIAdapter1> adapter;
            if (factory->EnumAdapters1(index, &adapter) == DXGI_ERROR_NOT_FOUND) break;
            D3D_FEATURE_LEVEL level{};
            ComPtr<ID3D11Device> candidate_device;
            ComPtr<ID3D11DeviceContext> candidate_context;
            const D3D_FEATURE_LEVEL levels[] = {
                D3D_FEATURE_LEVEL_11_1,
                D3D_FEATURE_LEVEL_11_0,
                D3D_FEATURE_LEVEL_10_1,
                D3D_FEATURE_LEVEL_10_0,
            };
            result = D3D11CreateDevice(
                adapter.Get(),
                D3D_DRIVER_TYPE_UNKNOWN,
                nullptr,
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                levels,
                ARRAYSIZE(levels),
                D3D11_SDK_VERSION,
                &candidate_device,
                &level,
                &candidate_context);
            if (FAILED(result)) continue;
            ComPtr<ID3D11Texture2D> candidate_texture;
            result = candidate_device->OpenSharedResource(shared_handle, IID_PPV_ARGS(&candidate_texture));
            if (FAILED(result)) continue;
            DXGI_ADAPTER_DESC1 description{};
            adapter->GetDesc1(&description);
            adapter_name_ = utf8_adapter_name(description);
            device_ = candidate_device;
            context_ = candidate_context;
            shared_texture_ = candidate_texture;
            break;
        }
        if (!device_ || !shared_texture_) {
            error = "SPOUT_ADAPTER_NOT_FOUND";
            return false;
        }

        D3D11_TEXTURE2D_DESC source_description{};
        shared_texture_->GetDesc(&source_description);
        source_description.Usage = D3D11_USAGE_DEFAULT;
        source_description.CPUAccessFlags = 0;
        source_description.MiscFlags = 0;
        source_description.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        result = device_->CreateTexture2D(&source_description, nullptr, &local_texture_);
        if (FAILED(result)) {
            error = "SPOUT_LOCAL_TEXTURE_FAILED";
            return false;
        }

        D2D1_FACTORY_OPTIONS factory_options{};
        result = D2D1CreateFactory(
            D2D1_FACTORY_TYPE_SINGLE_THREADED,
            __uuidof(ID2D1Factory1),
            &factory_options,
            reinterpret_cast<void**>(d2d_factory_.GetAddressOf()));
        if (FAILED(result)) {
            error = "D2D_FACTORY_FAILED";
            return false;
        }
        ComPtr<IDXGIDevice> dxgi_device;
        result = device_.As(&dxgi_device);
        if (FAILED(result) || FAILED(d2d_factory_->CreateDevice(dxgi_device.Get(), &d2d_device_))
            || FAILED(d2d_device_->CreateDeviceContext(D2D1_DEVICE_CONTEXT_OPTIONS_NONE, &d2d_context_))) {
            error = "D2D_DEVICE_FAILED";
            return false;
        }

        ComPtr<IDXGISurface> source_surface;
        result = local_texture_.As(&source_surface);
        if (FAILED(result)) {
            error = "SPOUT_SOURCE_SURFACE_FAILED";
            return false;
        }
        D2D1_BITMAP_PROPERTIES1 source_properties{};
        source_properties.pixelFormat.format = static_cast<DXGI_FORMAT>(info.format);
        source_properties.pixelFormat.alphaMode = D2D1_ALPHA_MODE_STRAIGHT;
        source_properties.dpiX = 96.0f;
        source_properties.dpiY = 96.0f;
        source_properties.bitmapOptions = D2D1_BITMAP_OPTIONS_NONE;
        result = d2d_context_->CreateBitmapFromDxgiSurface(
            source_surface.Get(), &source_properties, &source_bitmap_);
        if (FAILED(result)) {
            // Some drivers expose shared content as premultiplied only.
            source_properties.pixelFormat.alphaMode = D2D1_ALPHA_MODE_PREMULTIPLIED;
            result = d2d_context_->CreateBitmapFromDxgiSurface(
                source_surface.Get(), &source_properties, &source_bitmap_);
        }
        if (FAILED(result)) {
            error = "SPOUT_SOURCE_BITMAP_FAILED";
            return false;
        }

        frame_semaphore_ = OpenSemaphoreA(
            SYNCHRONIZE | SEMAPHORE_MODIFY_STATE,
            FALSE,
            (sender + "_Count_Semaphore").c_str());
        uint32_t width = 1, height = 1;
        RECT rectangle{};
        if (GetWindowRect(overlay, &rectangle)) {
            width = static_cast<uint32_t>(std::max<LONG>(1, rectangle.right - rectangle.left));
            height = static_cast<uint32_t>(std::max<LONG>(1, rectangle.bottom - rectangle.top));
        }
        if (!create_composition_target(overlay, width, height, error)) return false;

        {
            std::scoped_lock lock(g_runtime.status_mutex);
            g_runtime.status.adapter_match = 1;
            g_runtime.status.format = static_cast<int32_t>(info.format);
            g_runtime.status.width = info.width;
            g_runtime.status.height = info.height;
            g_runtime.status.memory_bytes = static_cast<uint64_t>(info.width) * info.height * 4ULL * 3ULL;
            copy_text(g_runtime.status.sender, sizeof(g_runtime.status.sender), sender);
            copy_text(g_runtime.status.sender_adapter, sizeof(g_runtime.status.sender_adapter), adapter_name_);
            copy_text(g_runtime.status.receiver_adapter, sizeof(g_runtime.status.receiver_adapter), adapter_name_);
        }
        return true;
    }

    bool metadata_matches(const SharedTextureInfo& info) const {
        return info.share_handle == info_.share_handle && info.width == info_.width
            && info.height == info_.height && info.format == info_.format;
    }

    uint64_t frame_sequence(bool& reliable) {
        reliable = frame_semaphore_ != nullptr;
        if (!frame_semaphore_) return ++synthetic_sequence_;
        LONG previous = 0;
        const DWORD wait = WaitForSingleObject(frame_semaphore_, 0);
        if (wait != WAIT_OBJECT_0) return last_sequence_;
        if (!ReleaseSemaphore(frame_semaphore_, 1, &previous)) return last_sequence_;
        return static_cast<uint64_t>(std::max<LONG>(0, previous));
    }

    bool copy_frame(std::string& error) {
        HANDLE access_mutex = nullptr;
        if (!lock_named_mutex(sender_ + "_SpoutAccessMutex", access_mutex)) {
            error = "SPOUT_TEXTURE_LOCK_TIMEOUT";
            return false;
        }
        context_->CopyResource(local_texture_.Get(), shared_texture_.Get());
        unlock_named_mutex(access_mutex);
        return true;
    }

    AlphaState validate_alpha() {
        if (info_.format != DXGI_FORMAT_R8G8B8A8_UNORM
            && info_.format != DXGI_FORMAT_R8G8B8A8_UNORM_SRGB
            && info_.format != DXGI_FORMAT_B8G8R8A8_UNORM
            && info_.format != DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) {
            return AlphaState::Opaque;
        }
        D3D11_TEXTURE2D_DESC description{};
        local_texture_->GetDesc(&description);
        description.Usage = D3D11_USAGE_STAGING;
        description.BindFlags = 0;
        description.MiscFlags = 0;
        description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        ComPtr<ID3D11Texture2D> staging;
        if (FAILED(device_->CreateTexture2D(&description, nullptr, &staging))) return AlphaState::Unknown;
        context_->CopyResource(staging.Get(), local_texture_.Get());
        D3D11_MAPPED_SUBRESOURCE mapped{};
        if (FAILED(context_->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) return AlphaState::Unknown;
        uint64_t samples = 0, transparent = 0, visible = 0;
        const uint32_t step_x = std::max<uint32_t>(1, info_.width / 128);
        const uint32_t step_y = std::max<uint32_t>(1, info_.height / 128);
        for (uint32_t y = 0; y < info_.height; y += step_y) {
            const auto* row = static_cast<const uint8_t*>(mapped.pData) + y * mapped.RowPitch;
            for (uint32_t x = 0; x < info_.width; x += step_x) {
                const uint8_t alpha = row[x * 4 + 3];
                ++samples;
                if (alpha < 245) ++transparent;
                if (alpha > 10) ++visible;
            }
        }
        context_->Unmap(staging.Get(), 0);
        if (samples == 0 || visible * 100 < samples) return AlphaState::Empty;
        if (transparent * 100 >= samples) return AlphaState::Valid;
        return AlphaState::Opaque;
    }

    bool render(HWND overlay, uint32_t width, uint32_t height, const Config& config, std::string& error) {
        if (width != target_width_ || height != target_height_) {
            if (!resize_target(overlay, width, height, error)) return false;
        }
        d2d_context_->SetTarget(target_bitmap_.Get());
        d2d_context_->BeginDraw();
        d2d_context_->Clear(D2D1::ColorF(0.0f, 0.0f, 0.0f, 0.0f));
        const float base = std::min(
            static_cast<float>(width) / std::max(1u, info_.width),
            static_cast<float>(height) / std::max(1u, info_.height));
        const float fit = std::max(0.1f, base * config.scale);
        const float draw_width = info_.width * fit;
        const float draw_height = info_.height * fit;
        const float left = (width - draw_width) * 0.5f + config.offset_x * width;
        const float top = (height - draw_height) * 0.5f + config.offset_y * height;
        const D2D1_RECT_F destination{left, top, left + draw_width, top + draw_height};
        const D2D1_RECT_F source{0.0f, 0.0f, static_cast<float>(info_.width), static_cast<float>(info_.height)};
        d2d_context_->DrawBitmap(
            source_bitmap_.Get(),
            destination,
            1.0f,
            D2D1_INTERPOLATION_MODE_HIGH_QUALITY_CUBIC,
            source);
        const HRESULT draw = d2d_context_->EndDraw();
        if (FAILED(draw)) {
            error = "SPOUT_DRAW_FAILED";
            return false;
        }
        const HRESULT present = swap_chain_->Present(1, 0);
        if (FAILED(present)) {
            error = "SPOUT_PRESENT_FAILED";
            return false;
        }
        return true;
    }

    uint64_t last_sequence() const { return last_sequence_; }
    void set_last_sequence(uint64_t value) { last_sequence_ = value; }
    const std::string& adapter_name() const { return adapter_name_; }

private:
    bool create_composition_target(
        HWND overlay,
        uint32_t width,
        uint32_t height,
        std::string& error) {
        ComPtr<IDXGIDevice> dxgi_device;
        ComPtr<IDXGIAdapter> adapter;
        ComPtr<IDXGIFactory2> factory;
        if (FAILED(device_.As(&dxgi_device))
            || FAILED(dxgi_device->GetAdapter(&adapter))
            || FAILED(adapter->GetParent(IID_PPV_ARGS(&factory)))) {
            error = "DXGI_COMPOSITION_DEVICE_FAILED";
            return false;
        }
        DXGI_SWAP_CHAIN_DESC1 description{};
        description.Width = width;
        description.Height = height;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1;
        description.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        description.BufferCount = 2;
        description.Scaling = DXGI_SCALING_STRETCH;
        description.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
        description.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
        if (FAILED(factory->CreateSwapChainForComposition(device_.Get(), &description, nullptr, &swap_chain_))) {
            error = "DXGI_COMPOSITION_SWAPCHAIN_FAILED";
            return false;
        }
        if (FAILED(DCompositionCreateDevice(
                dxgi_device.Get(), __uuidof(IDCompositionDevice),
                reinterpret_cast<void**>(dcomp_device_.GetAddressOf())))
            || FAILED(dcomp_device_->CreateTargetForHwnd(overlay, TRUE, &dcomp_target_))
            || FAILED(dcomp_device_->CreateVisual(&dcomp_visual_))
            || FAILED(dcomp_visual_->SetContent(swap_chain_.Get()))
            || FAILED(dcomp_target_->SetRoot(dcomp_visual_.Get()))
            || FAILED(dcomp_device_->Commit())) {
            error = "DIRECT_COMPOSITION_FAILED";
            return false;
        }
        return create_target_bitmap(width, height, error);
    }

    bool create_target_bitmap(uint32_t width, uint32_t height, std::string& error) {
        ComPtr<IDXGISurface> surface;
        if (FAILED(swap_chain_->GetBuffer(0, IID_PPV_ARGS(&surface)))) {
            error = "SPOUT_BACKBUFFER_FAILED";
            return false;
        }
        D2D1_BITMAP_PROPERTIES1 properties{};
        properties.pixelFormat.format = DXGI_FORMAT_B8G8R8A8_UNORM;
        properties.pixelFormat.alphaMode = D2D1_ALPHA_MODE_PREMULTIPLIED;
        properties.dpiX = 96.0f;
        properties.dpiY = 96.0f;
        properties.bitmapOptions = D2D1_BITMAP_OPTIONS_TARGET | D2D1_BITMAP_OPTIONS_CANNOT_DRAW;
        if (FAILED(d2d_context_->CreateBitmapFromDxgiSurface(surface.Get(), &properties, &target_bitmap_))) {
            error = "SPOUT_TARGET_BITMAP_FAILED";
            return false;
        }
        target_width_ = width;
        target_height_ = height;
        return true;
    }

    bool resize_target(HWND, uint32_t width, uint32_t height, std::string& error) {
        d2d_context_->SetTarget(nullptr);
        target_bitmap_.Reset();
        const HRESULT resized = swap_chain_->ResizeBuffers(2, width, height, DXGI_FORMAT_B8G8R8A8_UNORM, 0);
        if (FAILED(resized)) {
            error = "SPOUT_TARGET_RESIZE_FAILED";
            return false;
        }
        return create_target_bitmap(width, height, error);
    }

    std::string sender_;
    SharedTextureInfo info_{};
    std::string adapter_name_;
    HANDLE frame_semaphore_ = nullptr;
    uint64_t last_sequence_ = 0;
    uint64_t synthetic_sequence_ = 0;
    uint32_t target_width_ = 0;
    uint32_t target_height_ = 0;
    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> context_;
    ComPtr<ID3D11Texture2D> shared_texture_;
    ComPtr<ID3D11Texture2D> local_texture_;
    ComPtr<ID2D1Factory1> d2d_factory_;
    ComPtr<ID2D1Device> d2d_device_;
    ComPtr<ID2D1DeviceContext> d2d_context_;
    ComPtr<ID2D1Bitmap1> source_bitmap_;
    ComPtr<ID2D1Bitmap1> target_bitmap_;
    ComPtr<IDXGISwapChain1> swap_chain_;
    ComPtr<IDCompositionDevice> dcomp_device_;
    ComPtr<IDCompositionTarget> dcomp_target_;
    ComPtr<IDCompositionVisual> dcomp_visual_;
};

void clear_receiver_status() {
    std::scoped_lock lock(g_runtime.status_mutex);
    g_runtime.status.alpha = static_cast<int32_t>(AlphaState::Unknown);
    g_runtime.status.adapter_match = 0;
    g_runtime.status.format = 0;
    g_runtime.status.width = 0;
    g_runtime.status.height = 0;
    g_runtime.status.sender_fps = 0.0;
    g_runtime.status.receiver_fps = 0.0;
    g_runtime.status.frame_count = 0;
    g_runtime.status.dropped_frames = 0;
    g_runtime.status.last_frame_age_ms = 0;
    g_runtime.status.memory_bytes = 0;
    g_runtime.status.sender[0] = 0;
    g_runtime.status.sender_adapter[0] = 0;
    g_runtime.status.receiver_adapter[0] = 0;
}

void publish_unavailable(RendererState state, AlphaState alpha, const std::string& error) {
    publish_status(state, alpha, error);
}

void worker_loop() {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    g_runtime.overlay = create_overlay(g_runtime.owner);
    if (!g_runtime.overlay) {
        publish_status(RendererState::VtsDegraded, AlphaState::Unknown, "SPOUT_OVERLAY_CREATE_FAILED");
        g_runtime.running.store(false);
        CoUninitialize();
        return;
    }

    ReceiverResources receiver;
    Config active_config;
    std::string selected;
    AlphaState alpha = AlphaState::Unknown;
    auto last_discovery = Clock::time_point{};
    auto last_metadata = Clock::time_point{};
    auto last_alpha_probe = Clock::time_point{};
    auto last_frame = Clock::now();
    auto fps_window = Clock::now();
    uint64_t fps_frames = 0;
    uint64_t sender_window_start = 0;
    uint64_t valid_frames = 0;
    uint32_t overlay_width = 1, overlay_height = 1;
    bool was_active = false;

    publish_status(RendererState::VtsDiscovering, AlphaState::Unknown);
    while (g_runtime.running.load()) {
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }

        Config config;
        {
            std::scoped_lock lock(g_runtime.config_mutex);
            config = g_runtime.config;
        }
        if (config != active_config) {
            active_config = config;
            if (was_active) publish_unavailable(RendererState::VtsDiscovering, alpha, {});
            receiver.reset();
            selected.clear();
            alpha = AlphaState::Unknown;
            valid_frames = 0;
            clear_receiver_status();
            ShowWindow(g_runtime.overlay, SW_HIDE);
            publish_status(RendererState::VtsDiscovering, AlphaState::Unknown);
        }

        const auto now = Clock::now();
        if (selected.empty() && now - last_discovery >= std::chrono::milliseconds(750)) {
            last_discovery = now;
            const auto senders = enumerate_senders();
            selected = select_sender(senders, config);
            if (selected.empty()) {
                publish_status(
                    was_active ? RendererState::VtsDegraded : RendererState::VtsUnavailable,
                    AlphaState::Unknown,
                    "SPOUT_SENDER_NOT_FOUND");
                sync_overlay(g_runtime.owner, g_runtime.overlay, false, overlay_width, overlay_height);
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                continue;
            }
            publish_status(RendererState::VtsConnecting, AlphaState::Unknown, {}, selected);
            SharedTextureInfo info{};
            std::string error;
            if (!read_shared_map(selected, &info, sizeof(info))
                || info.share_handle == 0 || info.width == 0 || info.height == 0) {
                selected.clear();
                publish_status(RendererState::VtsDegraded, AlphaState::Unknown, "SPOUT_SENDER_METADATA_INVALID");
                continue;
            }
            if (!receiver.open(g_runtime.overlay, selected, info, error)) {
                selected.clear();
                publish_status(RendererState::VtsDegraded, AlphaState::Unknown, error);
                continue;
            }
            last_frame = now;
            last_metadata = now;
            last_alpha_probe = Clock::time_point{};
            fps_window = now;
            fps_frames = 0;
            sender_window_start = 0;
            publish_status(RendererState::VtsWaitingFrames, AlphaState::Unknown, {}, selected);
        }

        if (selected.empty()) continue;

        if (now - last_metadata >= std::chrono::milliseconds(750)) {
            last_metadata = now;
            SharedTextureInfo current{};
            if (!read_shared_map(selected, &current, sizeof(current)) || !receiver.metadata_matches(current)) {
                publish_unavailable(RendererState::VtsDegraded, alpha, "SPOUT_SENDER_LOST");
                receiver.reset();
                selected.clear();
                alpha = AlphaState::Unknown;
                valid_frames = 0;
                ShowWindow(g_runtime.overlay, SW_HIDE);
                continue;
            }
        }

        bool reliable_sequence = false;
        const uint64_t sequence = receiver.frame_sequence(reliable_sequence);
        if (reliable_sequence && sequence == receiver.last_sequence()) {
            const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_frame).count();
            {
                std::scoped_lock lock(g_runtime.status_mutex);
                g_runtime.status.last_frame_age_ms = static_cast<uint64_t>(std::max<int64_t>(0, age));
            }
            if (age > static_cast<int64_t>(config.watchdog_seconds) * 1000) {
                publish_unavailable(RendererState::VtsDegraded, alpha, "SPOUT_FRAME_TIMEOUT");
                receiver.reset();
                selected.clear();
                alpha = AlphaState::Unknown;
                valid_frames = 0;
                ShowWindow(g_runtime.overlay, SW_HIDE);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(4));
            continue;
        }

        std::string error;
        if (!receiver.copy_frame(error)) {
            publish_unavailable(RendererState::VtsDegraded, alpha, error);
            ShowWindow(g_runtime.overlay, SW_HIDE);
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }
        if (reliable_sequence && receiver.last_sequence() > 0 && sequence > receiver.last_sequence() + 1) {
            std::scoped_lock lock(g_runtime.status_mutex);
            g_runtime.status.dropped_frames += sequence - receiver.last_sequence() - 1;
        }
        receiver.set_last_sequence(sequence);
        last_frame = now;
        ++fps_frames;
        ++valid_frames;

        const auto alpha_interval = alpha == AlphaState::Valid ? std::chrono::seconds(5) : std::chrono::seconds(2);
        if (now - last_alpha_probe >= alpha_interval) {
            last_alpha_probe = now;
            const AlphaState previous_alpha = alpha;
            alpha = receiver.validate_alpha();
            {
                std::scoped_lock lock(g_runtime.status_mutex);
                g_runtime.status.alpha = static_cast<int32_t>(alpha);
            }
            if (previous_alpha == AlphaState::Valid && alpha != AlphaState::Valid) {
                publish_unavailable(
                    RendererState::VtsDegraded,
                    alpha,
                    alpha == AlphaState::Opaque ? "SPOUT_ALPHA_OPAQUE" : "SPOUT_FRAME_EMPTY");
                ShowWindow(g_runtime.overlay, SW_HIDE);
            }
        }

        sync_overlay(g_runtime.owner, g_runtime.overlay,
                     alpha == AlphaState::Valid && valid_frames >= 3,
                     overlay_width, overlay_height);
        if (!receiver.render(g_runtime.overlay, overlay_width, overlay_height, config, error)) {
            publish_unavailable(RendererState::VtsDegraded, alpha, error);
            receiver.reset();
            selected.clear();
            alpha = AlphaState::Unknown;
            valid_frames = 0;
            ShowWindow(g_runtime.overlay, SW_HIDE);
            continue;
        }

        if (alpha == AlphaState::Valid && valid_frames >= 3) {
            if (!was_active) was_active = true;
            publish_status(RendererState::VtsActive, alpha, {}, selected);
        } else if (alpha == AlphaState::Opaque) {
            ShowWindow(g_runtime.overlay, SW_HIDE);
            publish_status(RendererState::VtsDegraded, alpha, "SPOUT_ALPHA_OPAQUE", selected);
        } else if (alpha == AlphaState::Empty) {
            ShowWindow(g_runtime.overlay, SW_HIDE);
            publish_status(RendererState::VtsWaitingFrames, alpha, "SPOUT_FRAME_EMPTY", selected);
        } else {
            publish_status(RendererState::VtsWaitingFrames, alpha, {}, selected);
        }

        const auto fps_elapsed = std::chrono::duration<double>(now - fps_window).count();
        if (fps_elapsed >= 1.0) {
            const double receiver_fps = fps_frames / fps_elapsed;
            double sender_fps = receiver_fps;
            if (reliable_sequence && sender_window_start > 0 && sequence >= sender_window_start) {
                sender_fps = (sequence - sender_window_start) / fps_elapsed;
            }
            {
                std::scoped_lock lock(g_runtime.status_mutex);
                g_runtime.status.sender_fps = sender_fps;
                g_runtime.status.receiver_fps = receiver_fps;
                g_runtime.status.frame_count = sequence;
                g_runtime.status.last_frame_age_ms = 0;
            }
            sender_window_start = sequence;
            fps_frames = 0;
            fps_window = now;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }

    receiver.reset();
    if (g_runtime.overlay) DestroyWindow(g_runtime.overlay);
    g_runtime.overlay = nullptr;
    CoUninitialize();
}

} // namespace

extern "C" bool kazumi_spout_start(void* owner_hwnd) {
    if (g_runtime.running.exchange(true)) return true;
    g_runtime.owner = static_cast<HWND>(owner_hwnd);
    {
        std::scoped_lock lock(g_runtime.status_mutex);
        g_runtime.status = {};
        g_runtime.status.state = static_cast<int32_t>(RendererState::VtsOffline);
    }
    try {
        g_runtime.worker = std::thread(worker_loop);
        return true;
    } catch (...) {
        g_runtime.running.store(false);
        publish_status(RendererState::VtsDegraded, AlphaState::Unknown, "SPOUT_THREAD_START_FAILED");
        return false;
    }
}

extern "C" void kazumi_spout_stop() {
    if (!g_runtime.running.exchange(false)) return;
    if (g_runtime.worker.joinable()) g_runtime.worker.join();
}

extern "C" void kazumi_spout_configure(
    const char* sender,
    float scale,
    float offset_x,
    float offset_y,
    uint32_t watchdog_seconds) {
    std::scoped_lock lock(g_runtime.config_mutex);
    g_runtime.config.sender = sender && *sender ? sender : "AUTO";
    g_runtime.config.scale = std::clamp(scale, 0.1f, 4.0f);
    g_runtime.config.offset_x = std::clamp(offset_x, -1.0f, 1.0f);
    g_runtime.config.offset_y = std::clamp(offset_y, -1.0f, 1.0f);
    g_runtime.config.watchdog_seconds = std::clamp<uint32_t>(watchdog_seconds, 5, 60);
}

extern "C" void kazumi_spout_get_status(KazumiSpoutStatus* status) {
    if (!status) return;
    std::scoped_lock lock(g_runtime.status_mutex);
    *status = g_runtime.status;
}
