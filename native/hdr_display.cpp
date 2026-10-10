// Grainy HDR viewport. The OS performs the scRGB-to-monitor conversion.
// No display configuration or calibration is ever changed here.
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <vector>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>
using Microsoft::WRL::ComPtr;
#define API extern "C" __declspec(dllexport)
struct Info { unsigned size, supported, active, bits; float white, peak, full, minimum; wchar_t name[32]; };
static void check(HRESULT h) { if(FAILED(h)) throw h; }
static ComPtr<IDXGIAdapter1> output(HWND hwnd, Info& info) {
    info={sizeof(Info),0,0,8,80,80,80,0,{0}};
    ComPtr<IDXGIFactory1> factory;check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)));
    auto monitor=MonitorFromWindow(hwnd,MONITOR_DEFAULTTONEAREST);
    for(UINT a=0;;++a) {
        ComPtr<IDXGIAdapter1> adapter;auto adapterResult=factory->EnumAdapters1(a,&adapter);if(adapterResult==DXGI_ERROR_NOT_FOUND) break;check(adapterResult);
        for(UINT o=0;;++o) {
            ComPtr<IDXGIOutput> out;auto outputResult=adapter->EnumOutputs(o,&out);if(outputResult==DXGI_ERROR_NOT_FOUND) break;check(outputResult);
            DXGI_OUTPUT_DESC desc{};check(out->GetDesc(&desc));if(desc.Monitor!=monitor) continue;
            wcsncpy_s(info.name,desc.DeviceName,_TRUNCATE);
            ComPtr<IDXGIOutput6> six;
            if(SUCCEEDED(out.As(&six))) {
                DXGI_OUTPUT_DESC1 d{};check(six->GetDesc1(&d));info.bits=d.BitsPerColor;
                info.active=d.ColorSpace==DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
                info.supported=info.active;info.peak=d.MaxLuminance;info.full=d.MaxFullFrameLuminance;info.minimum=d.MinLuminance;
            }
            for(int retry=0;retry<3;++retry) {
                UINT pc=0,mc=0;if(GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS,&pc,&mc)!=ERROR_SUCCESS) break;
                std::vector<DISPLAYCONFIG_PATH_INFO> paths(pc);std::vector<DISPLAYCONFIG_MODE_INFO> modes(mc);
                auto result=QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS,&pc,paths.data(),&mc,modes.data(),nullptr);
                if(result==ERROR_INSUFFICIENT_BUFFER) continue;if(result!=ERROR_SUCCESS) break;
                for(UINT i=0;i<pc;++i) {
                    const auto& p=paths[i];DISPLAYCONFIG_SOURCE_DEVICE_NAME source{};
                    source.header={DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,sizeof(source),p.sourceInfo.adapterId,p.sourceInfo.id};
                    if(DisplayConfigGetDeviceInfo(&source.header)!=ERROR_SUCCESS || wcscmp(source.viewGdiDeviceName,info.name)) continue;
                    DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2 advanced{};
                    advanced.header={DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2,sizeof(advanced),p.targetInfo.adapterId,p.targetInfo.id};
                    if(DisplayConfigGetDeviceInfo(&advanced.header)==ERROR_SUCCESS) {
                        info.supported=advanced.highDynamicRangeSupported;
                        info.active=advanced.activeColorMode==DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR;
                    }
                    DISPLAYCONFIG_SDR_WHITE_LEVEL white{};
                    white.header={DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,sizeof(white),p.targetInfo.adapterId,p.targetInfo.id};
                    if(DisplayConfigGetDeviceInfo(&white.header)==ERROR_SUCCESS && white.SDRWhiteLevel>0)
                        info.white=80.f*white.SDRWhiteLevel/1000.f;
                    break;
                }
                break;
            }
            if(!info.active) info.white=80.f;
            // Unknown peak must not be advertised as measured HDR headroom.
            if(!std::isfinite(info.peak)||info.peak<info.white)info.peak=info.white;
            return adapter;
        }
    }
    throw HRESULT(DXGI_ERROR_NOT_FOUND);
}
API int grainy_hdr_abi(){return 1;}
API int grainy_hdr_probe(void* hwnd,Info* info) {
    if(!info||info->size!=sizeof(Info))return E_INVALIDARG;
    try {output(static_cast<HWND>(hwnd),*info);return S_OK;}catch(HRESULT h){return h;}catch(...){return E_FAIL;}
}
static const char* shader=R"(
Texture2D photo:register(t0); Texture2D overlay:register(t1); SamplerState samp:register(s0);
cbuffer Params:register(b0) { float4 bounds; float2 viewport; float whiteScale; float peak; };
struct Vertex { float4 position:SV_POSITION; float2 uv:TEXCOORD0; };
Vertex vs(uint id:SV_VertexID) {
    Vertex v;v.uv=float2((id<<1)&2,id&2);v.position=float4(v.uv*float2(2,-2)+float2(-1,1),0,1);return v;
}
float3 decodeSRGB(float3 c) { return lerp(c/12.92,pow(max((c+.055)/1.055,0),2.4),step(.04045,c)); }
float4 ps(Vertex v):SV_TARGET {
    float2 p=v.uv*viewport;float2 uv=(p-bounds.xy)/bounds.zw;
    float3 rgb=decodeSRGB(float3(16,18,21)/255.0);
    if(all(uv>=0)&&all(uv<=1))rgb=photo.Sample(samp,uv).rgb;
    // Clip only for this display. Extended source and export data stay intact.
    rgb=min(rgb,peak)*whiteScale;
    float4 over=overlay.Sample(samp,v.uv);rgb=lerp(rgb,decodeSRGB(over.rgb)*whiteScale,over.a);
    return float4(rgb,1);
})";
struct Renderer {
    HWND hwnd{};UINT width=0,height=0,iw=0,ih=0;DWORD owner=GetCurrentThreadId();
    ComPtr<ID3D11Device> device;ComPtr<ID3D11DeviceContext> context;
    ComPtr<IDXGISwapChain3> swap;ComPtr<ID3D11RenderTargetView> target;
    ComPtr<ID3D11VertexShader> vs;ComPtr<ID3D11PixelShader> ps;
    ComPtr<ID3D11SamplerState> sampler;ComPtr<ID3D11Buffer> params;
    ComPtr<ID3D11Texture2D> photo,overlay;ComPtr<ID3D11ShaderResourceView> photoView,overlayView;
    ComPtr<ID3D11Texture2D> readback;
    Renderer(HWND h,bool warp):hwnd(h) {
        Info info{};auto adapter=output(h,info);D3D_FEATURE_LEVEL level{};
        check(D3D11CreateDevice(warp?nullptr:adapter.Get(),warp?D3D_DRIVER_TYPE_WARP:D3D_DRIVER_TYPE_UNKNOWN,
            nullptr,D3D11_CREATE_DEVICE_BGRA_SUPPORT,nullptr,0,D3D11_SDK_VERSION,&device,&level,&context));
        ComPtr<IDXGIDevice> dxgi;check(device.As(&dxgi));ComPtr<IDXGIAdapter> actual;check(dxgi->GetAdapter(&actual));
        ComPtr<IDXGIFactory2> factory;check(actual->GetParent(IID_PPV_ARGS(&factory)));
        DXGI_SWAP_CHAIN_DESC1 desc{};desc.Width=desc.Height=1;desc.Format=DXGI_FORMAT_R16G16B16A16_FLOAT;
        desc.SampleDesc.Count=1;desc.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT;desc.BufferCount=2;
        desc.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;desc.Scaling=DXGI_SCALING_STRETCH;desc.AlphaMode=DXGI_ALPHA_MODE_IGNORE;
        ComPtr<IDXGISwapChain1> first;check(factory->CreateSwapChainForHwnd(device.Get(),h,&desc,nullptr,nullptr,&first));
        check(first.As(&swap));check(factory->MakeWindowAssociation(h,DXGI_MWA_NO_ALT_ENTER));
        UINT support=0;check(swap->CheckColorSpaceSupport(DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709,&support));
        if(!(support&DXGI_SWAP_CHAIN_COLOR_SPACE_SUPPORT_FLAG_PRESENT))throw HRESULT(DXGI_ERROR_UNSUPPORTED);
        check(swap->SetColorSpace1(DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709));
        ComPtr<ID3DBlob> code,error;
        check(D3DCompile(shader,strlen(shader),nullptr,nullptr,nullptr,"vs","vs_4_0",D3DCOMPILE_OPTIMIZATION_LEVEL3,0,&code,&error));
        check(device->CreateVertexShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&vs));code.Reset();error.Reset();
        check(D3DCompile(shader,strlen(shader),nullptr,nullptr,nullptr,"ps","ps_4_0",D3DCOMPILE_OPTIMIZATION_LEVEL3,0,&code,&error));
        check(device->CreatePixelShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&ps));
        D3D11_SAMPLER_DESC sd{};sd.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;sd.AddressU=sd.AddressV=sd.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;sd.MaxLOD=D3D11_FLOAT32_MAX;
        check(device->CreateSamplerState(&sd,&sampler));
        D3D11_BUFFER_DESC bd{};bd.ByteWidth=32;bd.Usage=D3D11_USAGE_DEFAULT;bd.BindFlags=D3D11_BIND_CONSTANT_BUFFER;
        check(device->CreateBuffer(&bd,nullptr,&params));
    }
    ~Renderer(){if(context){context->ClearState();context->Flush();}}
    void thread(){if(GetCurrentThreadId()!=owner)throw HRESULT(RPC_E_WRONG_THREAD);}
    void texture(UINT w,UINT h,DXGI_FORMAT format,ComPtr<ID3D11Texture2D>& tex,ComPtr<ID3D11ShaderResourceView>& view) {
        view.Reset();tex.Reset();D3D11_TEXTURE2D_DESC d{};d.Width=w;d.Height=h;d.MipLevels=d.ArraySize=1;
        d.Format=format;d.SampleDesc.Count=1;d.Usage=D3D11_USAGE_DEFAULT;d.BindFlags=D3D11_BIND_SHADER_RESOURCE;
        check(device->CreateTexture2D(&d,nullptr,&tex));check(device->CreateShaderResourceView(tex.Get(),nullptr,&view));
    }
    void upload(const float* pixels,UINT w,UINT h) {
        thread();if(!pixels||!w||!h||w>16384||h>16384)throw HRESULT(E_INVALIDARG);
        if(w!=iw||h!=ih){texture(w,h,DXGI_FORMAT_R32G32B32A32_FLOAT,photo,photoView);iw=w;ih=h;}
        context->UpdateSubresource(photo.Get(),0,nullptr,pixels,w*16,0);
    }
    void draw(UINT w,UINT h,const float* rectangle,const unsigned char* rgba,UINT stride,float white,float maxNits,bool present) {
        thread();if(!photo||!w||!h||w>16384||h>16384||!rectangle||!rgba||stride<w*4||white<=0||maxNits<white)throw HRESULT(E_INVALIDARG);
        if(w!=width||h!=height) {
            context->OMSetRenderTargets(0,nullptr,nullptr);target.Reset();readback.Reset();
            check(swap->ResizeBuffers(0,w,h,DXGI_FORMAT_UNKNOWN,0));
            ComPtr<ID3D11Texture2D> back;check(swap->GetBuffer(0,IID_PPV_ARGS(&back)));
            check(device->CreateRenderTargetView(back.Get(),nullptr,&target));
            texture(w,h,DXGI_FORMAT_R8G8B8A8_UNORM,overlay,overlayView);width=w;height=h;
        }
        context->UpdateSubresource(overlay.Get(),0,nullptr,rgba,stride,0);
        float constants[8]={rectangle[0],rectangle[1],rectangle[2],rectangle[3],float(w),float(h),white/80.f,maxNits/white};
        context->UpdateSubresource(params.Get(),0,nullptr,constants,0,0);
        auto rt=target.Get();context->OMSetRenderTargets(1,&rt,nullptr);
        D3D11_VIEWPORT vp{0,0,float(w),float(h),0,1};context->RSSetViewports(1,&vp);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        context->VSSetShader(vs.Get(),nullptr,0);context->PSSetShader(ps.Get(),nullptr,0);
        auto cb=params.Get();context->PSSetConstantBuffers(0,1,&cb);
        auto sm=sampler.Get();context->PSSetSamplers(0,1,&sm);
        ID3D11ShaderResourceView* views[]={photoView.Get(),overlayView.Get()};context->PSSetShaderResources(0,2,views);
        context->Draw(3,0);
        ID3D11ShaderResourceView* none[2]={nullptr,nullptr};context->PSSetShaderResources(0,2,none);
        if(present)check(swap->Present(0,0));
    }
};
API void* grainy_hdr_create(void* hwnd,int warp,int* error) {
    if(error)*error=S_OK;
    try {return new Renderer(static_cast<HWND>(hwnd),warp!=0);}catch(HRESULT h){if(error)*error=h;}catch(...){if(error)*error=E_FAIL;}
    return nullptr;
}
API void grainy_hdr_destroy(void* renderer){delete static_cast<Renderer*>(renderer);}
API int grainy_hdr_upload(void* renderer,const float* rgba,unsigned w,unsigned h) {
    if(!renderer)return E_INVALIDARG;
    try{static_cast<Renderer*>(renderer)->upload(rgba,w,h);return S_OK;}catch(HRESULT e){return e;}catch(...){return E_FAIL;}
}
API int grainy_hdr_draw(void* renderer,unsigned w,unsigned h,const float* rect,const unsigned char* overlay,unsigned stride,float white,float peak,int present) {
    if(!renderer)return E_INVALIDARG;
    try{static_cast<Renderer*>(renderer)->draw(w,h,rect,overlay,stride,white,peak,present!=0);return S_OK;}catch(HRESULT e){return e;}catch(...){return E_FAIL;}
}
// Test diagnostic reads our own back buffer only, never another app or desktop.
API int grainy_hdr_readback(void* renderer,unsigned short* rgba,unsigned count) {
    if(!renderer||!rgba)return E_INVALIDARG;
    try {
        auto r=static_cast<Renderer*>(renderer);r->thread();if(count<r->width*r->height*4)return E_INVALIDARG;
        ComPtr<ID3D11Texture2D> back;check(r->swap->GetBuffer(0,IID_PPV_ARGS(&back)));
        if(!r->readback){D3D11_TEXTURE2D_DESC d{};back->GetDesc(&d);d.Usage=D3D11_USAGE_STAGING;d.BindFlags=0;d.CPUAccessFlags=D3D11_CPU_ACCESS_READ;d.MiscFlags=0;check(r->device->CreateTexture2D(&d,nullptr,&r->readback));}
        r->context->CopyResource(r->readback.Get(),back.Get());D3D11_MAPPED_SUBRESOURCE data{};
        check(r->context->Map(r->readback.Get(),0,D3D11_MAP_READ,0,&data));
        for(UINT y=0;y<r->height;++y)memcpy(rgba+y*r->width*4,static_cast<char*>(data.pData)+y*data.RowPitch,r->width*8);
        r->context->Unmap(r->readback.Get(),0);return S_OK;
    }catch(HRESULT e){return e;}catch(...){return E_FAIL;}
}
