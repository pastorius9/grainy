// Desktop Duplication recorder for measuring an app's latency on screen.
// Every desktop update is timestamped (wall clock, 100 ns) with the mean absolute difference of a
// screen rectangle from the previous update, so a driver's command times can be matched to the
// moment the photo on screen stops changing.
//   screen_latency.exe <out.json> <left> <top> <width> <height> <seconds>   (physical pixels)
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <cmath>
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

static double now_epoch() {
    FILETIME ft; GetSystemTimePreciseAsFileTime(&ft);
    ULARGE_INTEGER t; t.LowPart = ft.dwLowDateTime; t.HighPart = ft.dwHighDateTime;
    return (double)(t.QuadPart - 116444736000000000ULL) / 1e7;
}

int main(int argc, char** argv) {
    if (argc < 7) { std::fprintf(stderr, "usage: out.json left top width height seconds\n"); return 2; }
    const char* out = argv[1];
    int left = std::atoi(argv[2]), top = std::atoi(argv[3]), width = std::atoi(argv[4]), height = std::atoi(argv[5]);
    double seconds = std::atof(argv[6]);
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    // The duplicated output must belong to the device's adapter: find the adapter/output whose desktop
    // contains the rectangle (the primary monitor may be driven by a GPU other than the default one).
    IDXGIFactory1* factory = nullptr; CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&factory);
    IDXGIAdapter1* adapter = nullptr; IDXGIOutput* output = nullptr;
    for (UINT a = 0; !output && factory->EnumAdapters1(a, &adapter) != DXGI_ERROR_NOT_FOUND; ++a) {
        DXGI_ADAPTER_DESC1 ad{}; adapter->GetDesc1(&ad);
        IDXGIOutput* o = nullptr;
        for (UINT i = 0; adapter->EnumOutputs(i, &o) != DXGI_ERROR_NOT_FOUND; ++i) {
            DXGI_OUTPUT_DESC od{}; o->GetDesc(&od);
            std::fwprintf(stderr, L"adapter %u '%s' output %u desktop %ld,%ld-%ld,%ld\n", a, ad.Description, i,
                          od.DesktopCoordinates.left, od.DesktopCoordinates.top, od.DesktopCoordinates.right, od.DesktopCoordinates.bottom);
            if (!output && left >= od.DesktopCoordinates.left && top >= od.DesktopCoordinates.top &&
                left + width <= od.DesktopCoordinates.right && top + height <= od.DesktopCoordinates.bottom) {
                output = o; left -= od.DesktopCoordinates.left; top -= od.DesktopCoordinates.top;
            } else o->Release();
        }
        if (!output) adapter->Release();
    }
    if (!output) return 4;
    ID3D11Device* device = nullptr; ID3D11DeviceContext* context = nullptr;
    if (FAILED(D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &device, nullptr, &context))) return 3;
    IDXGIOutput1* output1 = nullptr; output->QueryInterface(__uuidof(IDXGIOutput1), (void**)&output1);
    IDXGIOutputDuplication* dup = nullptr;
    HRESULT duplicated = output1->DuplicateOutput(device, &dup);
    if (FAILED(duplicated)) { std::fprintf(stderr, "DuplicateOutput failed 0x%08lx\n", (unsigned long)duplicated); return 5; }
    D3D11_TEXTURE2D_DESC desc{}; desc.Width = width; desc.Height = height; desc.MipLevels = 1; desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM; desc.SampleDesc.Count = 1; desc.Usage = D3D11_USAGE_STAGING;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ID3D11Texture2D* staging = nullptr; if (FAILED(device->CreateTexture2D(&desc, nullptr, &staging))) return 6;
    std::vector<unsigned char> previous((size_t)width * height * 4, 0), current(previous.size());
    bool first = true;
    FILE* f = std::fopen(out, "w"); if (!f) return 7;
    std::fprintf(f, "{\"rect\":[%d,%d,%d,%d],\"frames\":[", left, top, width, height);
    double stop = now_epoch() + seconds; int count = 0, acquired = 0, timeouts = 0;
    while (now_epoch() < stop) {
        DXGI_OUTDUPL_FRAME_INFO info{}; IDXGIResource* resource = nullptr;
        HRESULT hr = dup->AcquireNextFrame(50, &info, &resource);
        if (hr == DXGI_ERROR_WAIT_TIMEOUT) { ++timeouts; continue; }
        if (FAILED(hr)) { std::fprintf(stderr, "AcquireNextFrame failed 0x%08lx\n", (unsigned long)hr); break; }
        double t = now_epoch(); ++acquired;
        if (info.LastPresentTime.QuadPart != 0) {
            ID3D11Texture2D* frame = nullptr; resource->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&frame);
            D3D11_BOX box{ (UINT)left, (UINT)top, 0, (UINT)(left + width), (UINT)(top + height), 1 };
            context->CopySubresourceRegion(staging, 0, 0, 0, 0, frame, 0, &box);
            frame->Release();
            D3D11_MAPPED_SUBRESOURCE mapped{};
            if (SUCCEEDED(context->Map(staging, 0, D3D11_MAP_READ, 0, &mapped))) {
                for (int y = 0; y < height; ++y)
                    std::memcpy(&current[(size_t)y * width * 4], (unsigned char*)mapped.pData + (size_t)y * mapped.RowPitch, (size_t)width * 4);
                context->Unmap(staging, 0);
                double diff = 0; size_t n = 0;
                if (!first) for (int y = 0; y < height; y += 2) for (int x = 0; x < width; x += 2) {
                    size_t i = ((size_t)y * width + x) * 4;
                    diff += std::abs((int)current[i] - previous[i]) + std::abs((int)current[i + 1] - previous[i + 1]) + std::abs((int)current[i + 2] - previous[i + 2]);
                    n += 3;
                }
                std::fprintf(f, "%s[%.4f,%.4f]", count ? "," : "", t, n ? diff / n : 0.0);
                ++count; first = false; previous.swap(current);
            }
        }
        resource->Release(); dup->ReleaseFrame();
    }
    std::fprintf(f, "]}\n"); std::fclose(f);
    std::printf("%d frames (%d acquired, %d timeouts)\n", count, acquired, timeouts);
    return 0;
}
