// Grainy GPU develop stages (D3D11 compute). CPU numpy code remains the reference;
// Python compares results and falls back to it whenever this module is unavailable.
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <cstring>
#include <cwchar>
#include <string>
#include <utility>
#include <vector>
#include <cmath>
using Microsoft::WRL::ComPtr;
#define API extern "C" __declspec(dllexport)
static void check(HRESULT h) { if(FAILED(h)) throw h; }

// Keep in sync with luma/native_gpu.py (_ToneParams).
struct ToneParams {
    float gain[4];          // exposure * white balance, per channel
    float shadows, highlights, black, whitesGain;
    float contrast, vignette; unsigned curveCount, flags;   // flags: 1 shadows/highlights, 2 contrast, 4 curve, 8 vignette, 16 HDR
    unsigned width, height; float curveSlope; unsigned localMaps; // HDR: curve slope above its last point (hdr.curve_extended)
    float shoulderKnee, shoulderWhite, shoulderSlope, localFloor; // flag 32: engine.highlight_rolloff
    // flag 64: tone_response.apply (process 3 Highlights/Shadows). The curve buffer then holds, after the
    // curveCount curve points, 257 change values (.x) and localMaps = width<<16|height (a, b) coefficients.
};
static_assert(sizeof(ToneParams)%16==0,"constant buffer alignment");

// Points in the tone stage's curve buffer.
static unsigned tonePoints(const ToneParams* t) {
    if(t->flags&64) return t->curveCount+257+(t->localMaps>>16)*(t->localMaps&65535);
    return (t->flags&4)?t->curveCount:0;
}
static bool toneValid(const ToneParams* t,const float* points) {
    if(t->curveCount>4096||((t->flags&4)&&(!points||t->curveCount<2))) return false;
    if(t->flags&64) {
        unsigned w=t->localMaps>>16,h=t->localMaps&65535;
        if(!points||!w||!h||w>1024||h>1024) return false;
    }
    return true;
}

// Keep in sync with luma/native_gpu.py (_LocalParams).
struct LocalParams {
    float exposureGain, contrast, saturation, shadows;
    float highlights, black, whiteMinusBlack, hueShift;
    float tempTint[4];
    unsigned width, height, flags, pad;      // flags: 1 whites/blacks, 2 hue, 4 HSL
    unsigned curveOffset[4], curveCount[4];  // master, R, G, B; count 0 = unchanged
    float hsl[8][4];                         // raw slider values per colour group
};
static_assert(sizeof(LocalParams)%16==0,"constant buffer alignment");

// Keep in sync with luma/native_gpu.py (_ColorParams).
struct ColorParams {
    float saturation, vibrance; unsigned width, height;
    unsigned flags, pad0, pad1, pad2;      // 1 sat/vibrance, 2 vibrance, 4 HSL mixer, 8 HSV mixer, 16 parametric,
                                           // 32 calibration, 64 grading, 128 camera matrix
    unsigned curveOffset[4], curveCount[4];  // R, G, B, unused
    float hsl[8][4];
    float parametric[4];
    float calibration[3][4];
    float grading[3][4];                   // per-zone RGB offset ((tint-.5)*sat/200+light/400)
    float gradingBalance, pad3, pad4, pad5;
    float matrix[3][4];
    unsigned pointCount, pad6, pad7, pad8;   // point colours (flag 256) in the points buffer
};
static_assert(sizeof(ColorParams)%16==0,"constant buffer alignment");

// Keep in sync with luma/native_gpu.py (_PointParams): one point_color entry.
struct PointParams {
    float reference[4];      // HLS of the picked colour: hue (0..1), saturation, lightness
    float widths[4];         // hue, saturation, lightness ranges (v1: hue width only)
    float amounts[4];        // hue, saturation, lightness sliders
    unsigned flags[4];       // version2, all hues, all saturations, all lightness
};
static_assert(sizeof(PointParams)==64,"point layout");

// Keep in sync with luma/native_gpu.py (_DetailParams). One instance per dispatch.
struct DetailParams {
    unsigned width, height, flags, stage;    // flags: 1 texture, 2 monochrome, 4 sharpen threshold, 8 sharpen masking
    float texture, clarity, sharpen, threshold;
    float maskDivisor, pixelScale, vignette, pad0;
    unsigned radius, kernelOffset, pad1, pad2;
    unsigned channel, lutOffset, spaceOffset, spaceCount;   // bilateral pass
    float scaleIndex, strength, constant, pad3;              // constant: 1 when OpenCV would copy the channel
    float dehaze, defringe, edgeScale; unsigned dehazeRadius; // flags 32 dehaze, 16 defringe
    float vignetteRound, vignetteP, vignetteScale, vignetteExponent;   // engine.vignette_shape
    float vignetteProtect, vignetteAspectX, vignetteAspectY, pad5;
};
static_assert(sizeof(DetailParams)%16==0,"constant buffer alignment");

// Keep in sync with luma/native_gpu.py (_OpticalParams). Constants are float32 values of the numpy scalars.
struct OpticalParams {
    unsigned width, height, pad0, pad1;
    float halfWidth, halfHeight, scale, perspectiveH;
    float perspectiveV, zoom, aspect, shiftX;
    float shiftY, distortion1, distortion2, distortion3;
    float channelScale[4];                  // 1+lateral CA per channel
};
static_assert(sizeof(OpticalParams)%16==0,"constant buffer alignment");

// Keep in sync with luma/native_gpu.py (_RotateParams): Pillow's inverse affine matrix (doubles).
struct RotateParams {
    unsigned width, height, pad0, pad1;
    double matrix[6];
};
static_assert(sizeof(RotateParams)%16==0,"constant buffer alignment");

// Keep in sync with luma/native_gpu.py (_NoiseParams). Channels: 0 = L (luma), 1/2 = a/b (colour).
// L uses the bilateral fields; a/b use processing.chroma_guided (box taps at guidedOffset in the kernels).
struct NoiseParams {
    unsigned active[3]; unsigned diameter;
    float strength[3], sigmaColor[3], sigmaSpace[3];
    unsigned guidedRadius, guidedOffset; float guidedEps, guidedBlend, guidedChromaEps, pad[3];
    // processing.luma_wavelet (detail_version 3) instead of the L bilateral: level kernels and 5x5 box taps.
    unsigned lumaWavelet, waveletOffset[5], boxOffset; float waveletLambda;
};

static const char* common=R"(
StructuredBuffer<float2> curve:register(t1);
precise float toSRGB(precise float a) { a=max(a,0); return a<=.0031308 ? a*12.92 : 1.055*pow(a,1/2.4)-.055; }
precise float toLinear(precise float a) { return a<=.04045 ? a/12.92 : pow(max(a+.055,0)/1.055,2.4); }
precise float interpolate(precise float x,uint first,uint count) {
    // numpy.interp: clamp to end values outside the points, linear between them.
    if(x<=curve[first].x) return curve[first].y;
    for(uint i=first+1;i<first+count;++i) {
        float2 p=curve[i-1],q=curve[i];
        if(x<=q.x) return q.x>p.x ? p.y+(x-p.x)*(q.y-p.y)/(q.x-p.x) : q.y;
    }
    return curve[first+count-1].y;
}
#ifdef EXACT_DIVISION
// numpy's float32 division is correctly rounded; D3D's may be 2.5 ULP off. Choose the neighbour of the
// hardware quotient with the smallest exact residual a-q*b (float products are exact in double).
precise float exactDiv(precise float a,precise float b) {
    precise float q=a/b;
    if(!(abs(q)<3.0e38)||b==0) return q;
    double residual=abs((double)a-(double)q*(double)b);
    if(residual<abs((double)asfloat(asint(q)+1)-(double)q)*abs((double)b)*0.5) return q;
    precise float best=q;double bestError=residual;
    [unroll] for(int k=-3;k<=3;++k) {
        if(k==0) continue;
        precise float candidate=asfloat(asint(q)+k);
        double error=abs((double)a-(double)candidate*(double)b);
        if(error<bestError) {best=candidate;bestError=error;}
    }
    return best;
}
#else
precise float exactDiv(precise float a,precise float b) { return a/b; }
#endif
// OpenCV float RGB<->HLS (hue in degrees), as used by processing.rgb_to_hsl/hsl_to_rgb.
precise float3 rgbToHls(precise float3 c) {
    c=saturate(c);
    float vmax=max(max(c.r,c.g),c.b),vmin=min(min(c.r,c.g),c.b),diff=vmax-vmin;
    precise float l=(vmax+vmin)*.5,h=0,s=0;
    if(diff>1.1920929e-7) {
        s=l<.5 ? exactDiv(diff,vmax+vmin) : exactDiv(diff,2-vmax-vmin);
        float d=exactDiv(60,diff);
        h=vmax==c.r ? (c.g-c.b)*d : vmax==c.g ? (c.b-c.r)*d+120 : (c.r-c.g)*d+240;
        if(h<0) h+=360;
    }
    return float3(h,l,s);
}
precise float3 hlsToRgb(precise float h,precise float l,precise float s) {
    if(s==0) return l;
    precise float p2=l<=.5 ? l*(1+s) : l+s-l*s, p1=2*l-p2;
    h*=6.0/360.0;
    if(h<0) h+=6; else if(h>=6) h-=6;
    int sector=(int)floor(h); h-=sector; if(sector<0||sector>5){sector=0;h=0;}
    float tab[4]={p2,p1,p1+(p2-p1)*(1-h),p1+(p2-p1)*h};
    static const uint3 order[6]={uint3(1,3,0),uint3(1,0,2),uint3(3,0,1),uint3(0,2,1),uint3(0,1,3),uint3(2,1,0)};
    uint3 o=order[sector];   // b, g, r indices
    return float3(tab[o.z],tab[o.y],tab[o.x]);
}
precise float wrap(precise float x) { return x-floor(x); }
static const float centers[8]={0,1.0/12,1.0/6,1.0/3,.5,2.0/3,.75,5.0/6};
// processing.mix_hsl
precise float3 mixHsl(precise float3 a,float4 groups[8]) {
    precise float3 hls=rgbToHls(a);
    precise float h=exactDiv(hls.x,360),dh=0,ds=0,dl=0;
    [unroll] for(uint g=0;g<8;++g) {
        if(groups[g].x==0&&groups[g].y==0&&groups[g].z==0) continue;
        precise float w=max(0,1-abs(wrap(h-centers[g]+.5)-.5)/.125); w*=w;
        dh+=w*groups[g].x/600; ds+=w*groups[g].y/100; dl+=w*groups[g].z/200;
    }
    return hlsToRgb(wrap(h+dh)*360,saturate(hls.y+dl),saturate(hls.z*(1+ds)));
}
// engine.rgb_to_hsv / hsv_to_rgb and the HSV mixer in engine._develop_color
precise float3 mixHsv(precise float3 a,float4 groups[8]) {
    a=saturate(a);
    precise float hi=max(max(a.r,a.g),a.b),lo=min(min(a.r,a.g),a.b),d=hi-lo,safe=max(d,1e-7);
    precise float hue=0;
    if(hi==a.b) hue=((a.r-a.g)/safe+4)/6;
    if(hi==a.g) hue=((a.b-a.r)/safe+2)/6;
    if(hi==a.r) hue=((a.g-a.b)/safe+0)/6;
    precise float h=wrap(hue),sat=hi>0 ? d/max(hi,1e-7) : 0,val=hi,dh=0,ds=0,dv=0;
    [unroll] for(uint g=0;g<8;++g) {
        if(groups[g].x==0&&groups[g].y==0&&groups[g].z==0) continue;
        precise float w=max(0,1-abs(wrap(h-centers[g]+.5)-.5)/(1.0/8)); w*=w;
        dh+=w*groups[g].x/600; ds+=w*groups[g].y/100; dv+=w*groups[g].z/200;
    }
    h=wrap(h+dh); sat=saturate(sat*(1+ds)); val=saturate(val+dv);
    uint i=((uint)max(floor(h*6),0))%6;
    precise float f=h*6-floor(h*6),p=val*(1-sat),q=val*(1-f*sat),t=val*(1-(1-f)*sat);
    float3 combos[6]={float3(val,t,p),float3(q,val,p),float3(p,val,t),float3(p,q,val),float3(t,p,val),float3(val,p,q)};
    return combos[i];
}
)";

static const char* toneShader=R"(
cbuffer Params:register(b0) {
    float4 gain; float shadows; float highlights; float black; float whitesGain;
    float contrast; float vignette; uint curveCount; uint flags; uint width; uint height; float curveSlope; uint localMaps;
    float shoulderKnee; float shoulderWhite; float shoulderSlope; float localFloor;
};
StructuredBuffer<float> source:register(t0);
RWStructuredBuffer<float> result:register(u0);
[numthreads(16,16,1)]
void main(uint3 id:SV_DispatchThreadID) {
    if(id.x>=width||id.y>=height) return;
    uint index=(id.y*width+id.x)*3;
    precise float3 a=float3(source[index],source[index+1],source[index+2]);
    a*=gain.w; a*=gain.xyz;
    if(flags&8) {
        float x=width>1 ? -1+2*(float)id.x/(width-1) : -1, y=height>1 ? -1+2*(float)id.y/(height-1) : -1;
        a*=max(.1,1+(x*x+y*y)*vignette/100);
    }
    if(flags&32) {                          // engine.highlight_rolloff
        [unroll] for(uint c=0;c<3;++c) {
            precise float v=a[c];
            if(v>shoulderKnee) {
                precise float u=(v-shoulderKnee)/(shoulderWhite-shoulderKnee);
                v=shoulderKnee+(1-shoulderKnee)*(shoulderSlope*u/(1+(shoulderSlope-1)*u));
            }
            if(c==0) a.x=v; else if(c==1) a.y=v; else a.z=v;
        }
    }
    a=float3(toSRGB(a.x),toSRGB(a.y),toSRGB(a.z));
    if(flags&64) {                          // tone_response.apply: the change follows the base, detail is kept
        uint mapWidth=localMaps>>16, mapHeight=localMaps&65535, table=curveCount, maps=curveCount+257;
        precise float luma=dot(a,float3(.2126,.7152,.0722));
        float sy=clamp((id.y+.5)*mapHeight/height-.5,0,mapHeight-1), sx=clamp((id.x+.5)*mapWidth/width-.5,0,mapWidth-1);
        uint y0=(uint)sy, x0=(uint)sx, y1=min(y0+1,mapHeight-1), x1=min(x0+1,mapWidth-1);
        float ty=sy-y0, tx=sx-x0;
        float2 upper=lerp(curve[maps+y0*mapWidth+x0],curve[maps+y0*mapWidth+x1],tx);
        float2 lower=lerp(curve[maps+y1*mapWidth+x0],curve[maps+y1*mapWidth+x1],tx);
        float2 k=lerp(upper,lower,ty);
        float position=saturate(k.x*luma+k.y)*256;
        uint i=min((uint)position,255u);
        float change=lerp(curve[table+i].x,curve[table+i+1].x,position-i);
        change=clamp(change,-max(luma,0)*.5,max(1-luma,0)*.5);     // tone_response.REACH: at most half way to black or white
        float target=max(luma+change,0);
        a=(a*target+localFloor*(a+target-luma))/(luma+localFloor);
    } else if(flags&1) {
        precise float luma=dot(a,float3(.2126,.7152,.0722));
        precise float low=saturate(1-luma); low=low*low*low;
        precise float high=saturate(luma); high=high*high*high;
        a+=shadows/180*low*saturate(luma*4)+highlights/180*high*saturate((1-luma)*4);
    }
    if(black<0) a=max(a+black,0)/(1+black);
    else if(black>0) a=a*(1-black)+black;
    a*=whitesGain;
    if(flags&2) a=(a-.5)*contrast+.5;
    if(flags&16) {                          // HDR: hdr.curve_extended, then only negative values are clipped
        if(flags&4) {
            float2 last=curve[curveCount-1];
            [unroll] for(uint c=0;c<3;++c) {
                precise float x=a[c],y=x>last.x ? last.y+(x-last.x)*curveSlope : interpolate(x,0,curveCount);
                if(c==0) a.x=y; else if(c==1) a.y=y; else a.z=y;
            }
        }
        a=max(a,0);
    } else {
        if(flags&4) a=float3(interpolate(saturate(a.x),0,curveCount),interpolate(saturate(a.y),0,curveCount),interpolate(saturate(a.z),0,curveCount));
        a=saturate(a);
    }
    result[index]=a.x; result[index+1]=a.y; result[index+2]=a.z;
}
)";

// Mirrors processing._local_region for masks without point colour, clarity, detail or HDR.
static const char* localShader=R"(
cbuffer Params:register(b0) {
    float exposureGain; float contrast; float saturation; float shadows;
    float highlights; float black; float whiteMinusBlack; float hueShift;
    float4 tempTint; uint width; uint height; uint flags; uint pad;
    uint4 curveOffset; uint4 curveCount; float4 hsl[8];
};
StructuredBuffer<float> source:register(t0);
StructuredBuffer<float> weights:register(t2);
RWStructuredBuffer<float> result:register(u0);
[numthreads(16,16,1)]
void main(uint3 id:SV_DispatchThreadID) {
    if(id.x>=width||id.y>=height) return;
    uint pixel=id.y*width+id.x,index=pixel*3;
    precise float3 region=float3(source[index],source[index+1],source[index+2]);
    precise float3 a=float3(toLinear(region.x),toLinear(region.y),toLinear(region.z))*exposureGain;
    a=float3(toSRGB(a.x),toSRGB(a.y),toSRGB(a.z));
    a=(a-.5)*contrast+.5;
    a+=tempTint.xyz;
    precise float gray=dot(a,float3(.2126,.7152,.0722));
    a=gray+(a-gray)*saturation;
    precise float dark=1-saturate(gray),light=saturate(gray);
    a+=shadows*(dark*dark);
    a+=highlights*(light*light);
    if(flags&1) a=a*whiteMinusBlack+black;
    if(curveCount.x) a=float3(interpolate(a.x,curveOffset.x,curveCount.x),interpolate(a.y,curveOffset.x,curveCount.x),interpolate(a.z,curveOffset.x,curveCount.x));
    if(curveCount.y) a.x=interpolate(a.x,curveOffset.y,curveCount.y);
    if(curveCount.z) a.y=interpolate(a.y,curveOffset.z,curveCount.z);
    if(curveCount.w) a.z=interpolate(a.z,curveOffset.w,curveCount.w);
    if(flags&2) {
        precise float3 hls=rgbToHls(a);
        a=hlsToRgb(wrap(hls.x/360+hueShift)*360,saturate(hls.y),saturate(hls.z));
    }
    if(flags&4) a=mixHsl(a,hsl);
    precise float weight=weights[pixel];
    a=region*(1-weight)+saturate(a)*weight;
    result[index]=a.x; result[index+1]=a.y; result[index+2]=a.z;
}
)";

// Mirrors engine._develop_color + processing.color_tools without point colours or photo filters.
static const char* colorShader=R"(
cbuffer Params:register(b0) {
    float saturation; float vibrance; uint width; uint height;
    uint flags; uint3 pad0;
    uint4 curveOffset; uint4 curveCount; float4 hsl[8];
    float4 parametric; float4 calibration[3]; float4 grading[3];
    float gradingBalance; float3 pad1; float4 cameraMatrix[3];
    uint pointCount; uint3 pad2;
};
struct Point { float4 reference; float4 widths; float4 amounts; uint4 flags; };
StructuredBuffer<Point> points:register(t2);
StructuredBuffer<float> source:register(t0);
RWStructuredBuffer<float> result:register(u0);
static const float3 lumaWeights=float3(.2126,.7152,.0722);
static const float parametricCenters[4]={.125,.375,.625,.875};
static const float calibrationCenters[3]={0,1.0/3,2.0/3};
[numthreads(16,16,1)]
void main(uint3 id:SV_DispatchThreadID) {
    if(id.x>=width||id.y>=height) return;
    uint index=(id.y*width+id.x)*3;
    precise float3 a=float3(source[index],source[index+1],source[index+2]);
    if(flags&1) {
        precise float gray=dot(a,lumaWeights),factor=saturation;
        if(flags&2) factor=factor+vibrance*(1-(max(max(a.r,a.g),a.b)-min(min(a.r,a.g),a.b)));
        a=gray+(a-gray)*factor;
    }
    if(flags&4) a=mixHsl(a,hsl);
    else if(flags&8) a=mixHsv(a,hsl);
    if(curveCount.x) a.x=interpolate(a.x,curveOffset.x,curveCount.x);
    if(curveCount.y) a.y=interpolate(a.y,curveOffset.y,curveCount.y);
    if(curveCount.z) a.z=interpolate(a.z,curveOffset.z,curveCount.z);
    if(flags&16) {
        precise float lum=saturate(dot(a,lumaWeights)),delta=0;
        [unroll] for(uint i=0;i<4;++i) { precise float w=max(0,1-abs(lum-parametricCenters[i])/.375); delta+=w*w*parametric[i]/400; }
        a+=delta;
    }
    if(flags&32) {
        precise float3 hls=rgbToHls(a);
        precise float h=hls.x/360,dh=0,ds=0;
        [unroll] for(uint i=0;i<3;++i) {
            if(calibration[i].x==0&&calibration[i].y==0) continue;
            precise float w=max(0,1-abs(wrap(h-calibrationCenters[i]+.5)-.5)/.25);
            dh+=w*calibration[i].x/600; ds+=w*calibration[i].y/100;
        }
        a=hlsToRgb(wrap(h+dh)*360,saturate(hls.y),saturate(hls.z*(1+ds)));
    }
    if(flags&256) {                       // point_color.apply
        for(uint i=0;i<pointCount;++i) {
            Point p=points[i];
            precise float3 hls=rgbToHls(a);
            precise float h=exactDiv(hls.x,360),l=hls.y,sat=hls.z,w;
            if(p.flags.x) {
                precise float hue=p.flags.y ? 1 : saturate(1-exactDiv(abs(wrap(h-p.reference.x+.5)-.5),p.widths.x));
                precise float saturation=p.flags.z ? 1 : saturate(1-exactDiv(abs(sat-p.reference.y),p.widths.y));
                precise float lightness=p.flags.w ? 1 : saturate(1-exactDiv(abs(l-p.reference.z),p.widths.z));
                w=(hue*saturation)*lightness;
            } else {
                w=saturate(1-exactDiv(abs(wrap(h-p.reference.x+.5)-.5),p.widths.x));
                w*=saturate(1-exactDiv(abs(sat-p.reference.y),.6));
            }
            precise float3 changed=hlsToRgb(wrap(h+(w*p.amounts.x)/600)*360,saturate(l+(w*p.amounts.z)/200),saturate(sat*(1+(w*p.amounts.y)/100)));
            if(!p.flags.x||w>0) a=changed;
        }
    }
    if(flags&64) {
        precise float l=saturate(dot(a,lumaWeights)+gradingBalance);
        a+=(1-l)*(1-l)*grading[0].xyz;
        a+=4*l*(1-l)*grading[1].xyz;
        a+=l*l*grading[2].xyz;
    }
    if(flags&128) a=float3(dot(a,cameraMatrix[0].xyz),dot(a,cameraMatrix[1].xyz),dot(a,cameraMatrix[2].xyz));
    a=saturate(a);
    result[index]=a.x; result[index+1]=a.y; result[index+2]=a.z;
}
)";

// Separable Gaussian passes matching cv2.GaussianBlur(BORDER_REFLECT_101) and the per-pixel
// combine steps of engine._develop_detail. Buffers hold interleaved float RGB.
static const char* detailShader=R"(
cbuffer Params:register(b0) {
    uint width; uint height; uint flags; uint stage;
    float textureAmount; float clarityAmount; float sharpenAmount; float threshold;
    float maskDivisor; float pixelScale; float vignette; float pad0;
    uint radius; uint kernelOffset; uint2 pad1;
    uint channel; uint lutOffset; uint spaceOffset; uint spaceCount;
    float scaleIndex; float strength; float constant; float pad2;
    float dehazeAmount; float defringeAmount; float edgeScale; uint dehazeRadius;
    float vignetteRound; float vignetteP; float vignetteScale; float vignetteExponent;
    float vignetteProtect; float vignetteAspectX; float vignetteAspectY; float pad5;
};
StructuredBuffer<float> source:register(t0);
StructuredBuffer<float> blurred:register(t2);
StructuredBuffer<float> taps:register(t3);
StructuredBuffer<float> lut:register(t4);
StructuredBuffer<float4> space:register(t5);
StructuredBuffer<float> extra0:register(t6);
StructuredBuffer<float> extra1:register(t7);
StructuredBuffer<float> extra2:register(t8);
RWStructuredBuffer<float> result:register(u0);
RWStructuredBuffer<uint> extremes:register(u1);
static const float3 lumaWeights=float3(.2126,.7152,.0722);
// processing._rgb_to_lab / _lab_to_rgb (exact CIE formulas, D65).
precise float labF(precise float t) { return t>.008856 ? pow(t,1.0/3) : 7.787*t+16.0/116; }
precise float labInverse(precise float t) { return t<=.20689303 ? (t-16.0/116)/7.787 : t*t*t; }
precise float3 rgbToLab(precise float3 c) {
    c=saturate(c);
    c=float3(toLinear(c.x),toLinear(c.y),toLinear(c.z));
    precise float x=(.412453*c.r+.357580*c.g+.180423*c.b)/.950456;
    precise float y=.212671*c.r+.715160*c.g+.072169*c.b;
    precise float z=(.019334*c.r+.119193*c.g+.950227*c.b)/1.088754;
    precise float fx=labF(x),fy=labF(y),fz=labF(z);
    return float3(y>.008856 ? 116*fy-16 : 903.3*y,500*(fx-fy),200*(fy-fz));
}
precise float3 labToRgb(precise float3 lab) {
    bool low=lab.x<=7.9996248;
    precise float fy=low ? 7.787*(lab.x/903.3)+16.0/116 : (lab.x+16)/116;
    precise float y=low ? lab.x/903.3 : fy*fy*fy;
    precise float x=labInverse(fy+lab.y/500),z=labInverse(fy-lab.z/200);
    precise float3 c=saturate(float3(3.07993494*x-1.53715152*y-.542783419*z,
                                     -.921234183*x+1.87599*y+.0452441813*z,
                                     .052889682*x-.204041338*y+1.15115166*z));
    return float3(toSRGB(c.x),toSRGB(c.y),toSRGB(c.z));
}
// Sortable unsigned encoding so InterlockedMin/Max order floats correctly.
uint orderKey(float v) { uint u=asuint(v); return (u&0x80000000u) ? ~u : (u|0x80000000u); }
groupshared float3 lowest[256];
groupshared float3 highest[256];
groupshared float3 tile[16][16];
int reflect101(int p,int n) {
    if(n==1) return 0;
    [loop] while(p<0||p>=n) p = p<0 ? -p : 2*n-2-p;
    return p;
}
float3 load(StructuredBuffer<float> b,int x,int y) { uint i=(y*width+x)*3; return float3(b[i],b[i+1],b[i+2]); }
float gray(int x,int y) { return dot(load(source,reflect101(x,width),reflect101(y,height)),lumaWeights); }
float channelMax(int x,int y) { float3 v=load(source,reflect101(x,width),reflect101(y,height)); return max(max(v.r,v.g),v.b); }
// cv2.medianBlur(ksize=5) of one channel of `source` (BORDER_REPLICATE): 13th smallest of 25.
float median5(int x,int y,uint c) {
    float v[25];
    [unroll] for(int j=0;j<5;++j) [unroll] for(int i=0;i<5;++i)
        v[j*5+i]=source[(clamp(y+j-2,0,(int)height-1)*width+clamp(x+i-2,0,(int)width-1))*3+c];
    [unroll] for(int k=0;k<13;++k) [unroll] for(int n=k+1;n<25;++n) { float lo=min(v[k],v[n]); v[n]=max(v[k],v[n]); v[k]=lo; }
    return v[12];
}
void store(uint3 id,float3 a) { uint i=(id.y*width+id.x)*3; result[i]=a.x; result[i+1]=a.y; result[i+2]=a.z; }
// luma_wavelet noise statistics in `extremes` (uints): level l at 8+32*l: [0,1] sum |d| (64-bit, 1/4096
// units), [2] count, then for bins 0-7 and 8 (all) [3+3b,4+3b] clipped sum, [5+3b] count.
groupshared uint waveletSums[18];
void add64(uint index,uint value) {
    uint old; InterlockedAdd(extremes[index],value,old);
    if(old>0xffffffffu-value) InterlockedAdd(extremes[index+1],1);
}
float read64(uint index) { return extremes[index+1]*4294967296.0+extremes[index]; }
float waveletProfile(uint level,float guide) {          // np.interp of the per-bin noise over bin centres
    uint base=8+32*min(level,2u); uint total=extremes[base+2]; uint minimum=max(256u,total/2000);
    float overall=read64(base+3+3*8)/4096/max(extremes[base+5+3*8],1u);
    float values[8];
    [unroll] for(uint b=0;b<8;++b) {
        uint n=extremes[base+5+3*b];
        values[b]=n>minimum ? read64(base+3+3*b)/4096/n : overall;
    }
    float t=guide/12.5-.5,v;
    if(t<=0) v=values[0]; else if(t>=7) v=values[7];
    else { int i=(int)floor(t); float f=t-i; v=values[i]+(values[i+1]-values[i])*f; }
    return level>2 ? v*pow(.5,level-2) : v;
}
[numthreads(16,16,1)]
void main(uint3 id:SV_DispatchThreadID,uint slot:SV_GroupIndex) {
    if(stage==27||stage==28) {              // luma_wavelet statistics of level `channel` (source: P, blurred: S)
        bool inside=id.x<width&&id.y<height;
        if(slot<18) waveletSums[slot]=0;
        GroupMemoryBarrierWithGroupSync();
        uint base=8+32*channel;
        if(inside) {
            float3 p=load(source,id.x,id.y),s=load(blurred,id.x,id.y);
            float d=abs(p.x-s.x);
            if(stage==27) { InterlockedAdd(waveletSums[0],(uint)(d*4096+.5)); InterlockedAdd(waveletSums[1],1); }
            else {
                float mean=read64(base)/4096/max(extremes[base+2],1u);
                uint c=(uint)(min(d,2.5*mean)*4096+.5);
                float guide=channel==0 ? s.x : p.z;
                uint b=(uint)clamp((int)(guide*.08),0,7);
                InterlockedAdd(waveletSums[b],c);InterlockedAdd(waveletSums[9+b],1);
                InterlockedAdd(waveletSums[8],c);InterlockedAdd(waveletSums[17],1);
            }
        }
        GroupMemoryBarrierWithGroupSync();
        if(slot==0) {
            if(stage==27) { add64(base,waveletSums[0]); InterlockedAdd(extremes[base+2],waveletSums[1]); }
            else [unroll] for(uint b=0;b<9;++b) { add64(base+3+3*b,waveletSums[b]); InterlockedAdd(extremes[base+5+3*b],waveletSums[9+b]); }
        }
        return;
    }
    if(stage==9) {                         // per-channel min/max of the Lab frame (group reduction, then atomics)
        bool inside=id.x<width&&id.y<height;
        float3 v=inside ? load(source,id.x,id.y) : 0;
        lowest[slot]=inside ? v : 3.402823e38; highest[slot]=inside ? v : -3.402823e38;
        GroupMemoryBarrierWithGroupSync();
        [unroll] for(uint step=128;step>0;step>>=1) {
            if(slot<step) { lowest[slot]=min(lowest[slot],lowest[slot+step]); highest[slot]=max(highest[slot],highest[slot+step]); }
            GroupMemoryBarrierWithGroupSync();
        }
        if(slot==0) {
            [unroll] for(uint c=0;c<3;++c) { InterlockedMin(extremes[c],orderKey(lowest[0][c])); InterlockedMax(extremes[3+c],orderKey(highest[0][c])); }
        }
        return;
    }
    if(stage==0||stage==1) {                 // horizontal / vertical Gaussian
        // The group loads its input window 16 pixels at a time into groupshared memory; every pixel is
        // read from the frame ~(16+2r)/16 times instead of 2r+1. Taps are summed in the same order (t=0..2r).
        bool horizontal=stage==0;
        uint tx=slot%16,ty=slot/16;
        int x=id.x,y=id.y,r=radius;
        int own=horizontal ? x : y;
        int origin=own-(int)(horizontal ? tx : ty)-r;          // first input index of the group's window
        precise float3 sum=0;
        for(int block=0;block<16+2*r;block+=16) {
            int p=origin+block+(int)(horizontal ? tx : ty);
            int lx=horizontal ? reflect101(p,width) : min(x,(int)width-1);
            int ly=horizontal ? min(y,(int)height-1) : reflect101(p,height);
            tile[ty][tx]=load(source,lx,ly);
            GroupMemoryBarrierWithGroupSync();
            [unroll] for(int k=0;k<16;++k) {
                int t=origin+block+k-own+r;
                if(t>=0&&t<=2*r) sum+=(horizontal ? tile[ty][k] : tile[k][tx])*taps[kernelOffset+t];
            }
            GroupMemoryBarrierWithGroupSync();
        }
        if(id.x<width&&id.y<height) store(id,sum);
        return;
    }
    if(id.x>=width||id.y>=height) return;
    int x=id.x,y=id.y;
    precise float3 a=load(source,x,y);
    if(stage==2) {                          // end of detail_tools: texture, clip; then monochrome
        if(flags&1) a+=(a-load(blurred,x,y))*textureAmount;
        if(flags&16) {store(id,a);return;}  // defringe (stage 14) runs before the clip
        a=saturate(a);
        if(flags&2) a=dot(a,lumaWeights);
    } else if(stage==3) {                   // clarity
        a+=(a-load(blurred,x,y))*clarityAmount;
    } else if(stage==4) {                   // sharpen
        precise float3 residual=a-load(blurred,x,y);
        if(flags&64) residual=dot(residual,lumaWeights);    // detail_version 2: luminance only
        if(flags&4) residual*=saturate(abs(residual)/threshold);
        if(flags&8) {
            precise float dx=(gray(x+1,y-1)-gray(x-1,y-1))+2*(gray(x+1,y)-gray(x-1,y))+(gray(x+1,y+1)-gray(x-1,y+1));
            precise float dy=(gray(x-1,y+1)-gray(x-1,y-1))+2*(gray(x,y+1)-gray(x,y-1))+(gray(x+1,y+1)-gray(x+1,y-1));
            residual*=saturate(sqrt(dx*dx+dy*dy)*pixelScale/maskDivisor);
        }
        a=a+residual*sharpenAmount;
    } else if(stage==6) {                   // RGB -> Lab
        a=rgbToLab(a);
    } else if(stage==7) {                   // cv2.bilateralFilter (float LUT method) on one Lab channel, then blend
        precise float centre=a[channel],filtered=centre;
        if(constant==0) {
            precise float sum=0,wsum=0;
            for(uint k=0;k<spaceCount;++k) {
                float4 o=space[spaceOffset+k];
                float value=load(source,reflect101(x+(int)o.y,width),reflect101(y+(int)o.x,height))[channel];
                precise float alpha=abs(value-centre)*scaleIndex;
                int idx=(int)floor(alpha); alpha-=idx;
                precise float w=o.z*(lut[lutOffset+idx]+alpha*(lut[lutOffset+idx+1]-lut[lutOffset+idx]));
                sum+=value*w; wsum+=w;
            }
            filtered=sum/wsum;
        }
        precise float blended=centre*(1-strength)+filtered*strength;
        if(channel==0) a.x=blended; else if(channel==1) a.y=blended; else a.z=blended;
    } else if(stage==17) {                  // chroma_guided guide: (L/100, median5(a)/100, median5(b)/100)
        a=float3(a.x/100,median5(x,y,1)/100,median5(x,y,2)/100);
    } else if(stage==18) {                  // guide products I*G (source: guide)
        a=a.x*a;
    } else if(stage==19) {                  // guide products (ga*ga, ga*gb, gb*gb)
        a=float3(a.y*a.y,a.y*a.z,a.z*a.z);
    } else if(stage==20) {                  // inverse of the regularised guide covariance, row `channel` (0: i11 i12 i13, 1: i22 i23 i33)
        precise float3 m=a,b=load(blurred,x,y),c=load(extra0,x,y);  // means, box(I*G), box(ga*ga,ga*gb,gb*gb)
        precise float a11=b.x-m.x*m.x+scaleIndex,a12=b.y-m.x*m.y,a13=b.z-m.x*m.z;   // scaleIndex/constant: eps of L / chroma
        precise float a22=c.x-m.y*m.y+constant,a23=c.y-m.y*m.z,a33=c.z-m.z*m.z+constant;
        precise float i11=a22*a33-a23*a23,i12=a13*a23-a12*a33,i13=a12*a23-a13*a22;
        precise float i22=a11*a33-a13*a13,i23=a13*a12-a11*a23,i33=a11*a22-a12*a12;
        precise float det=a11*i11+a12*i12+a13*i13;
        a=channel==0 ? float3(i11,i12,i13)/det : float3(i22,i23,i33)/det;
    } else if(stage==21) {                  // products (p, I*p, ga*p) for chroma channel `channel` (blurred: guide)
        precise float3 g=load(blurred,x,y); precise float p=a[channel];
        a=float3(p,g.x*p,g.y*p);
    } else if(stage==22) {                  // product gb*p
        a=float3(load(blurred,x,y).z*a[channel],0,0);
    } else if(stage==23) {                  // linear coefficients A = inverse * cov(G,p) (source: guide means)
        precise float3 m=a,d=load(blurred,x,y),i1=load(extra1,x,y),i2=load(extra2,x,y);
        precise float c0=d.y-m.x*d.x,c1=d.z-m.y*d.x,c2=extra0[(y*width+x)*3]-m.z*d.x;
        a=float3(i1.x*c0+i1.y*c1+i1.z*c2,i1.y*c0+i2.x*c1+i2.y*c2,i1.z*c0+i2.y*c1+i2.z*c2);
    } else if(stage==24) {                  // offset B = mean(p) - A.mean(G) (source: guide means, blurred: A, extra0: means of p products)
        precise float3 k=load(blurred,x,y);
        a=float3(extra0[(y*width+x)*3]-(k.x*a.x+k.y*a.y+k.z*a.z),0,0);
    } else if(stage==25) {                  // q = mean(A).G + mean(B), blended into the channel (blurred: guide)
        precise float3 g=load(blurred,x,y),k=load(extra0,x,y);
        precise float p=a[channel],q=k.x*g.x+k.y*g.y+k.z*g.z+extra1[(y*width+x)*3];
        precise float blended=p+(q-p)*strength;
        if(channel==1) a.y=blended; else a.z=blended;
    } else if(stage==26) {                  // luma_wavelet start: P = (L, output 0, guide 0)
        a=float3(a.x,0,0);
    } else if(stage==29) {                  // luma_wavelet level energy input d*d (source: P, blurred: S)
        precise float d=a.x-load(blurred,x,y).x;
        a=float3(d*d,0,0);
    } else if(stage==30) {                  // luma_wavelet level: P' = (smooth, output + d*gain, guide)
        precise float3 s=load(blurred,x,y);
        precise float d=a.x-s.x,energy=load(extra0,x,y).x;
        float guide=channel==0 ? s.x : a.z;
        precise float sigma=waveletProfile(channel,guide)*1.25;
        precise float gain=energy/(energy+scaleIndex*sigma*sigma+1e-12);  // scaleIndex carries lambda
        a=float3(s.x,a.y+d*gain,guide);
    } else if(stage==31) {                  // luma_wavelet end: L = output + coarsest smooth (blurred: P)
        precise float3 w=load(blurred,x,y);
        a.x=w.y+w.x;
    } else if(stage==8) {                   // Lab -> RGB (clipped)
        a=labToRgb(a);
    } else if(stage==10) {                  // dehaze: per-pixel channel minimum
        a=min(min(a.r,a.g),a.b);
    } else if(stage==11||stage==12) {       // cv2.erode with a square kernel: separable min, outside ignored
        precise float m=a.x;
        for(int o=-(int)dehazeRadius;o<=(int)dehazeRadius;++o) {
            int sx=stage==11 ? x+o : x,sy=stage==12 ? y+o : y;
            if(sx>=0&&sy>=0&&sx<(int)width&&sy<(int)height) m=min(m,source[(sy*width+sx)*3]);
        }
        a=m;
    } else if(stage==13) {                  // dehaze: a=(a-1)/clip(1-k*dark,.2,1.8)+1
        precise float transmission=clamp(1-dehazeAmount*load(blurred,x,y).x,.2,1.8);
        a=(a-1)/transmission+1;
    } else if(stage==15) {                  // develop's final clip (resident previews)
        a=saturate(a);
    } else if(stage==16) {                  // engine._add_grain: one float per pixel in `blurred`
        a+=blurred[y*width+x];
    } else if(stage==14) {                  // defringe, then detail_tools' clip and monochrome
        precise float purple=(a.r+a.b)/2-a.g;
        precise float lap=channelMax(x-1,y)+channelMax(x+1,y)+channelMax(x,y-1)+channelMax(x,y+1)-4*channelMax(x,y);
        precise float edge=abs(lap)*edgeScale;
        precise float weight=(saturate(purple*6)*saturate(edge*3))*defringeAmount/100;
        precise float mean=((a.r+a.g)+a.b)/3;
        a=a*(1-weight)+mean*weight;
        a=saturate(a);
        if(flags&2) a=dot(a,lumaWeights);
    } else if(stage==5) {                   // vignette
        float u=width>1 ? -1+2*(float)x/(width-1) : -1, v=height>1 ? -1+2*(float)y/(height-1) : -1;
        if(vignetteRound!=0) { u*=1+vignetteRound*(vignetteAspectX-1); v*=1+vignetteRound*(vignetteAspectY-1); }
        precise float d2=vignetteP==2 ? u*u+v*v : pow(pow(abs(u),vignetteP)+pow(abs(v),vignetteP),2/vignetteP);
        if(vignetteScale!=1) d2*=vignetteScale;
        d2=clamp(d2,0,2);
        if(vignetteExponent!=1) d2=2*pow(d2/2,vignetteExponent);
        precise float darken=d2*vignette;
        if(vignetteProtect!=0) { float l=saturate((dot(a,lumaWeights)-.5)/.5); darken*=1-vignetteProtect*l*l; }
        a*=1-darken;
    }
    store(id,a);
}
)";

// processing.optical_geometry: inverse radial distortion/perspective map + cv2.remap(INTER_LINEAR,
// BORDER_CONSTANT) as exact float bilinear with zero outside the frame.
static const char* opticalShader=R"(
cbuffer Params:register(b0) {
    uint width; uint height; uint2 pad0;
    float halfWidth; float halfHeight; float scale; float perspectiveH;
    float perspectiveV; float zoom; float aspect; float shiftX;
    float shiftY; float distortion1; float distortion2; float distortion3;
    float4 channelScale;
};
StructuredBuffer<float> source:register(t0);
RWStructuredBuffer<float> result:register(u0);
float tap(int x,int y,uint c) { return x>=0&&y>=0&&x<(int)width&&y<(int)height ? source[(y*width+x)*3+c] : 0; }
// numpy divides float32 with IEEE round-to-nearest; D3D's float div may be 2.5 ULP off, which moves
// sample positions. Pick the neighbour of the hardware quotient with the smallest exact residual
// a-q*b (exact in double: float products fit in 48 bits), i.e. the correctly rounded quotient.
precise float div(precise float a,precise float b) {
    precise float q=a/b;
    if(!(abs(q)<3.0e38)||b==0) return q;
    double residual=abs((double)a-(double)q*(double)b);
    // Already correctly rounded when the residual is within half an ULP of q (the common case).
    double halfUlp=abs((double)asfloat(asint(q)+1)-(double)q)*abs((double)b)*0.5;
    if(residual<halfUlp) return q;
    precise float best=q;double bestError=residual;
    [unroll] for(int k=-3;k<=3;++k) {
        if(k==0) continue;
        precise float candidate=asfloat(asint(q)+k);
        double error=abs((double)a-(double)candidate*(double)b);
        if(error<bestError) {best=candidate;bestError=error;}
    }
    return best;
}
precise float sampleChannel(precise float mx,precise float my,uint c) {
    precise float fx0=floor(mx),fy0=floor(my);int x0=(int)fx0,y0=(int)fy0;
    precise float fx=mx-fx0,fy=my-fy0;
    return tap(x0,y0,c)*((1-fx)*(1-fy))+tap(x0+1,y0,c)*(fx*(1-fy))+tap(x0,y0+1,c)*((1-fx)*fy)+tap(x0+1,y0+1,c)*(fx*fy);
}
[numthreads(16,16,1)]
void main(uint3 id:SV_DispatchThreadID) {
    if(id.x>=width||id.y>=height) return;
    precise float u=div((float)id.x-halfWidth,scale),v=div((float)id.y-halfHeight,scale);
    precise float denominator=max(.2,(1+perspectiveH*u)+perspectiveV*v);
    u=div(div(div(u,denominator)+shiftX,zoom),aspect);
    v=div(div(v,denominator)+shiftY,zoom);
    precise float r2=u*u+v*v;
    precise float factor=((1+distortion1*r2)+(distortion2*r2)*r2)+((distortion3*r2)*r2)*r2;
    uint index=(id.y*width+id.x)*3;
    [unroll] for(uint c=0;c<3;++c) {
        precise float mx=((u*factor)*channelScale[c])*scale+halfWidth;
        precise float my=((v*factor)*channelScale[c])*scale+halfHeight;
        result[index+c]=sampleChannel(mx,my,c);
    }
}
)";

// PIL Image.rotate(angle, BICUBIC, expand=False) on float32 ('F') channels: Geometry.c's
// affine_transform + bicubic_filter32F, including its edge handling. Needs double support.
static const char* rotateShader=R"(
cbuffer Params:register(b0) {
    uint width; uint height; uint2 pad0;
    double2 m01; double2 m23; double2 m45;
};
StructuredBuffer<float> source:register(t0);
RWStructuredBuffer<float> result:register(u0);
double cubic(double v1,double v2,double v3,double v4,double d) {
    double p1=v2,p2=-v1+v3,p3=2*(v1-v2)+v3-v4,p4=-v1+v2-v3+v4;
    return p1+d*(p2+d*(p3+d*p4));
}
// Base SM5 doubles lack int<->double conversion; frame coordinates are exact in float.
double toDouble(int i) { return (double)(float)i; }
int floorInt(double x) {
    int i=(int)floor((float)x);
    if(toDouble(i)>x) i--; else if(toDouble(i+1)<=x) i++;
    return i;
}
int clampX(int x) { return x<0 ? 0 : x>=(int)width ? (int)width-1 : x; }
double rowValue(int y,int x,double dx,uint c) {
    double v0=(double)source[(y*width+clampX(x))*3+c],v1=(double)source[(y*width+clampX(x+1))*3+c];
    double v2=(double)source[(y*width+clampX(x+2))*3+c],v3=(double)source[(y*width+clampX(x+3))*3+c];
    return cubic(v0,v1,v2,v3,dx);
}
[numthreads(16,16,1)]
void main(uint3 id:SV_DispatchThreadID) {
    if(id.x>=width||id.y>=height) return;
    double xo=toDouble((int)id.x)+0.5,yo=toDouble((int)id.y)+0.5;
    double xin=m01.x*xo+m01.y*yo+m23.x,yin=m23.y*xo+m45.x*yo+m45.y;
    uint index=(id.y*width+id.x)*3;
    if(xin<0||xin>=toDouble((int)width)||yin<0||yin>=toDouble((int)height)) {result[index]=0;result[index+1]=0;result[index+2]=0;return;}
    xin-=0.5;yin-=0.5;
    int x=floorInt(xin),y=floorInt(yin);double dx=xin-toDouble(x),dy=yin-toDouble(y);x--;y--;
    int h=(int)height;int y0=y<0 ? 0 : y>=h ? h-1 : y;
    [unroll] for(uint c=0;c<3;++c) {
        double v1=rowValue(y0,x,dx,c);
        double v2=(y+1>=0&&y+1<h) ? rowValue(y+1,x,dx,c) : v1;
        double v3=(y+2>=0&&y+2<h) ? rowValue(y+2,x,dx,c) : v2;
        double v4=(y+3>=0&&y+3<h) ? rowValue(y+3,x,dx,c) : v3;
        result[index+c]=(float)cubic(v1,v2,v3,v4,dy);
    }
}
)";

struct Device {
    ComPtr<ID3D11Device> device; ComPtr<ID3D11DeviceContext> context;
    ComPtr<ID3D11ComputeShader> tone, local, color, detail, optical, rotate;
    ComPtr<ID3D11Buffer> toneParams, localParams, colorParams, detailParams, opticalParams, rotateParams;
    // Read/write buffers for multi-pass stages (ping-pong image, blur scratch, blurred copy, kernels).
    UINT workCapacity=0, kernelCapacity=0, lutCapacity=0, spaceCapacity=0;
    // work[4..7] exist only while processing.chroma_guided runs (guidedCapacity floats each).
    UINT guidedCapacity=0;
    ComPtr<ID3D11Buffer> work[8], kernels; ComPtr<ID3D11ShaderResourceView> workView[8], kernelView;
    ComPtr<ID3D11UnorderedAccessView> workAccess[8];
    ComPtr<ID3D11Buffer> luts, spaces, extremes, extremesStaging, points;
    ComPtr<ID3D11ShaderResourceView> pointView; UINT pointCapacity=0;
    ComPtr<ID3D11ShaderResourceView> lutView, spaceView; ComPtr<ID3D11UnorderedAccessView> extremesAccess;
    // Buffers are reused while frames do not grow.
    UINT capacity=0, curveCapacity=0, maskCapacity=0;
    ComPtr<ID3D11Buffer> input, output, staging, curve, mask;
    ComPtr<ID3D11ShaderResourceView> inputView, curveView, maskView; ComPtr<ID3D11UnorderedAccessView> outputView;
    // Resident frames: stage results the preview cache keeps on the device between calls.
    static const int slotCount=32;
    ComPtr<ID3D11Buffer> slots[slotCount]; ComPtr<ID3D11ShaderResourceView> slotViews[slotCount]; UINT slotCapacity[slotCount]={};
    unsigned long long memory=0;   // dedicated video memory in bytes
    wchar_t name[128]={0};
};

static ComPtr<ID3D11Buffer> buffer(ID3D11Device* d, UINT count, UINT stride, UINT bind, D3D11_USAGE usage=D3D11_USAGE_DEFAULT, UINT cpu=0) {
    D3D11_BUFFER_DESC desc{};desc.ByteWidth=count*stride;desc.Usage=usage;desc.BindFlags=bind;desc.CPUAccessFlags=cpu;
    if(bind){desc.MiscFlags=D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;desc.StructureByteStride=stride;}
    ComPtr<ID3D11Buffer> b;check(d->CreateBuffer(&desc,nullptr,&b));return b;
}

static void reserve(Device& g, UINT floats) {
    if(floats<=g.capacity) return;
    g.inputView.Reset();g.outputView.Reset();
    g.input=buffer(g.device.Get(),floats,4,D3D11_BIND_SHADER_RESOURCE);
    g.output=buffer(g.device.Get(),floats,4,D3D11_BIND_UNORDERED_ACCESS);
    g.staging=buffer(g.device.Get(),floats,4,0,D3D11_USAGE_STAGING,D3D11_CPU_ACCESS_READ);
    check(g.device->CreateShaderResourceView(g.input.Get(),nullptr,&g.inputView));
    check(g.device->CreateUnorderedAccessView(g.output.Get(),nullptr,&g.outputView));
    g.capacity=floats;
}

static void reserveCurve(Device& g, UINT points) {
    points=points<2?2:points;
    if(points<=g.curveCapacity) return;
    g.curveView.Reset();
    g.curve=buffer(g.device.Get(),points,8,D3D11_BIND_SHADER_RESOURCE);
    check(g.device->CreateShaderResourceView(g.curve.Get(),nullptr,&g.curveView));
    g.curveCapacity=points;
}

static void reserveMask(Device& g, UINT count) {
    if(count<=g.maskCapacity) return;
    g.maskView.Reset();
    g.mask=buffer(g.device.Get(),count,4,D3D11_BIND_SHADER_RESOURCE);
    check(g.device->CreateShaderResourceView(g.mask.Get(),nullptr,&g.maskView));
    g.maskCapacity=count;
}

static ComPtr<ID3D11ComputeShader> compile(ID3D11Device* device, const char* body, const char* name, const char* prefix="") {
    std::string text=std::string(prefix)+common+body;
    ComPtr<ID3DBlob> code,errors;
    auto compiled=D3DCompile(text.data(),text.size(),name,nullptr,nullptr,"main","cs_5_0",
        D3DCOMPILE_OPTIMIZATION_LEVEL3|D3DCOMPILE_IEEE_STRICTNESS,0,&code,&errors);
    if(FAILED(compiled)) {
        if(errors) OutputDebugStringA(static_cast<const char*>(errors->GetBufferPointer()));
        throw compiled;
    }
    ComPtr<ID3D11ComputeShader> shader;
    check(device->CreateComputeShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&shader));
    return shader;
}

static ComPtr<ID3D11ComputeShader> create(ID3D11Device* device, const unsigned char* code, size_t size) {
    ComPtr<ID3D11ComputeShader> shader;
    check(device->CreateComputeShader(code,size,nullptr,&shader));
    return shader;
}

// Release builds embed bytecode generated by tools/build_native_gpu.py; other builds compile at runtime.
#ifdef GRAINY_PRECOMPILED_SHADERS
#include "gpu_shaders.h"
#define SHADER(name) create(g->device.Get(),name##Code,sizeof(name##Code))
#define EXACT_COLOR_SHADER() create(g->device.Get(),colorShaderExactCode,sizeof(colorShaderExactCode))
#else
#define SHADER(name) compile(g->device.Get(),name,#name)
#define EXACT_COLOR_SHADER() compile(g->device.Get(),colorShader,"colorShaderExact","#define EXACT_DIVISION 1\n")
#endif

static ComPtr<ID3D11Buffer> constants(ID3D11Device* device, UINT size) {
    D3D11_BUFFER_DESC cb{};cb.ByteWidth=size;cb.Usage=D3D11_USAGE_DEFAULT;cb.BindFlags=D3D11_BIND_CONSTANT_BUFFER;
    ComPtr<ID3D11Buffer> b;check(device->CreateBuffer(&cb,nullptr,&b));return b;
}

API int grainy_gpu_abi(){return 17;}

API void* grainy_gpu_create(int warp,int* error,wchar_t* name,unsigned nameLength) {
    auto g=new(std::nothrow) Device();
    if(!g){if(error)*error=E_OUTOFMEMORY;return nullptr;}
    try {
        ComPtr<IDXGIAdapter1> adapter;
        if(!warp) {
            ComPtr<IDXGIFactory6> factory;
            if(SUCCEEDED(CreateDXGIFactory2(0,IID_PPV_ARGS(&factory))))
                factory->EnumAdapterByGpuPreference(0,DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE,IID_PPV_ARGS(&adapter));
        }
        D3D_FEATURE_LEVEL levels[]={D3D_FEATURE_LEVEL_11_1,D3D_FEATURE_LEVEL_11_0};
        auto type=warp?D3D_DRIVER_TYPE_WARP:adapter?D3D_DRIVER_TYPE_UNKNOWN:D3D_DRIVER_TYPE_HARDWARE;
        check(D3D11CreateDevice(warp?nullptr:adapter.Get(),type,nullptr,0,levels,2,D3D11_SDK_VERSION,&g->device,nullptr,&g->context));
        ComPtr<IDXGIDevice> dxgi;ComPtr<IDXGIAdapter> used;DXGI_ADAPTER_DESC desc{};
        if(SUCCEEDED(g->device.As(&dxgi))&&SUCCEEDED(dxgi->GetAdapter(&used))&&SUCCEEDED(used->GetDesc(&desc)))
            {wcsncpy_s(g->name,desc.Description,_TRUNCATE);g->memory=desc.DedicatedVideoMemory;}
        g->tone=SHADER(toneShader);
        g->local=SHADER(localShader);
        g->color=SHADER(colorShader);
        g->colorParams=constants(g->device.Get(),sizeof(ColorParams));
        g->detail=SHADER(detailShader);
        g->detailParams=constants(g->device.Get(),sizeof(DetailParams));
        g->opticalParams=constants(g->device.Get(),sizeof(OpticalParams));
        g->rotateParams=constants(g->device.Get(),sizeof(RotateParams));
        // The rotation shader uses doubles like PIL; without device support it stays unavailable.
        D3D11_FEATURE_DATA_DOUBLES doubles{};
        if(SUCCEEDED(g->device->CheckFeatureSupport(D3D11_FEATURE_DOUBLES,&doubles,sizeof(doubles)))&&doubles.DoublePrecisionFloatShaderOps) {
            try {g->rotate=SHADER(rotateShader);} catch(HRESULT) {g->rotate.Reset();}
            try {g->optical=SHADER(opticalShader);} catch(HRESULT) {g->optical.Reset();}
            try {g->color=EXACT_COLOR_SHADER();} catch(HRESULT) {}   // keeps the plain colour shader
        }
        g->toneParams=constants(g->device.Get(),sizeof(ToneParams));
        g->localParams=constants(g->device.Get(),sizeof(LocalParams));
        if(name&&nameLength) wcsncpy_s(name,nameLength,g->name,_TRUNCATE);
        if(error)*error=S_OK;
        return g;
    } catch(HRESULT h) {delete g;if(error)*error=h;return nullptr;}
    catch(...) {delete g;if(error)*error=E_FAIL;return nullptr;}
}

API void grainy_gpu_destroy(void* handle){delete static_cast<Device*>(handle);}

// Upload, dispatch one shader over width x height and read the float3 result back.
static void run(Device& g, ID3D11ComputeShader* shader, ID3D11Buffer* params, const void* paramData,
                const float* input, float* output, UINT width, UINT height, UINT curvePoints, const float* curveData,
                const float* mask) {
    unsigned long long floats=3ull*width*height;
    reserve(g,static_cast<UINT>(floats));reserveCurve(g,curvePoints);
    D3D11_BOX box{0,0,0,static_cast<UINT>(floats*4),1,1};
    g.context->UpdateSubresource(g.input.Get(),0,&box,input,0,0);
    if(curvePoints){D3D11_BOX c{0,0,0,curvePoints*8,1,1};g.context->UpdateSubresource(g.curve.Get(),0,&c,curveData,0,0);}
    if(mask){reserveMask(g,width*height);D3D11_BOX m{0,0,0,width*height*4,1,1};g.context->UpdateSubresource(g.mask.Get(),0,&m,mask,0,0);}
    g.context->UpdateSubresource(params,0,nullptr,paramData,0,0);
    ID3D11ShaderResourceView* views[]={g.inputView.Get(),g.curveView.Get(),mask?g.maskView.Get():nullptr};
    ID3D11UnorderedAccessView* uav=g.outputView.Get();
    g.context->CSSetShader(shader,nullptr,0);
    g.context->CSSetShaderResources(0,3,views);g.context->CSSetUnorderedAccessViews(0,1,&uav,nullptr);
    g.context->CSSetConstantBuffers(0,1,&params);
    g.context->Dispatch((width+15)/16,(height+15)/16,1);
    ID3D11ShaderResourceView* noViews[3]={};ID3D11UnorderedAccessView* noUav=nullptr;
    g.context->CSSetShaderResources(0,3,noViews);g.context->CSSetUnorderedAccessViews(0,1,&noUav,nullptr);
    g.context->CopySubresourceRegion(g.staging.Get(),0,0,0,0,g.output.Get(),0,&box);
    D3D11_MAPPED_SUBRESOURCE mapped{};
    check(g.context->Map(g.staging.Get(),0,D3D11_MAP_READ,0,&mapped));
    std::memcpy(output,mapped.pData,static_cast<size_t>(floats*4));
    g.context->Unmap(g.staging.Get(),0);
}

static bool validSize(UINT width, UINT height) {
    return width&&height&&3ull*width*height<=0x3fffffffull;
}

// Tone stage for a contiguous float32 RGB frame. input and output may be the same pointer.
API int grainy_gpu_tone(void* handle,const float* input,float* output,const ToneParams* params,const float* curvePoints) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)) return E_INVALIDARG;
    if(!toneValid(params,curvePoints)) return E_INVALIDARG;
    try {
        run(*g,g->tone.Get(),g->toneParams.Get(),params,input,output,params->width,params->height,
            tonePoints(params),curvePoints,nullptr);
        return S_OK;
    } catch(HRESULT h){return h;} catch(...){return E_FAIL;}
}

// Local adjustment of one mask region, blended by its float weights (width x height).
API int grainy_gpu_local(void* handle,const float* region,const float* mask,float* output,const LocalParams* params,
                         const float* curvePoints,unsigned curveTotal) {
    auto g=static_cast<Device*>(handle);
    if(!g||!region||!mask||!output||!params||!validSize(params->width,params->height)||curveTotal>4096) return E_INVALIDARG;
    for(int c=0;c<4;++c) {
        if(params->curveCount[c]==0) continue;
        if(params->curveCount[c]<2||params->curveOffset[c]+params->curveCount[c]>curveTotal||!curvePoints) return E_INVALIDARG;
    }
    try {
        run(*g,g->local.Get(),g->localParams.Get(),params,region,output,params->width,params->height,curveTotal,curvePoints,mask);
        return S_OK;
    } catch(HRESULT h){return h;} catch(...){return E_FAIL;}
}


static void reserveWork(Device& g, UINT floats, UINT kernelFloats) {
    if(floats>g.workCapacity) {
        for(int i=0;i<4;++i) {
            g.workView[i].Reset();g.workAccess[i].Reset();
            g.work[i]=buffer(g.device.Get(),floats,4,D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_UNORDERED_ACCESS);
            check(g.device->CreateShaderResourceView(g.work[i].Get(),nullptr,&g.workView[i]));
            check(g.device->CreateUnorderedAccessView(g.work[i].Get(),nullptr,&g.workAccess[i]));
        }
        g.workCapacity=floats;
    }
    if(floats>g.capacity) reserve(g,floats);   // staging buffer for the readback
    kernelFloats=kernelFloats<1?1:kernelFloats;
    if(kernelFloats>g.kernelCapacity) {
        g.kernelView.Reset();
        g.kernels=buffer(g.device.Get(),kernelFloats,4,D3D11_BIND_SHADER_RESOURCE);
        check(g.device->CreateShaderResourceView(g.kernels.Get(),nullptr,&g.kernelView));
        g.kernelCapacity=kernelFloats;
    }
}

// One detail dispatch: source/blurred/extras are work indices (or -1), target receives the result (-1: none).
static void detailPassView(Device& g, DetailParams params, UINT pass, int source, ID3D11ShaderResourceView* blurred, int target,
                           int extra0=-1, int extra1=-1, int extra2=-1) {
    params.stage=pass;
    g.context->UpdateSubresource(g.detailParams.Get(),0,nullptr,&params,0,0);
    auto view=[&](int i){return i>=0?g.workView[i].Get():nullptr;};
    ID3D11ShaderResourceView* views[9]={g.workView[source].Get(),nullptr,blurred,
                                        g.kernelView.Get(),g.lutView.Get(),g.spaceView.Get(),view(extra0),view(extra1),view(extra2)};
    ID3D11UnorderedAccessView* uavs[2]={target>=0?g.workAccess[target].Get():nullptr,g.extremesAccess.Get()};
    ID3D11Buffer* cb=g.detailParams.Get();
    g.context->CSSetShader(g.detail.Get(),nullptr,0);
    g.context->CSSetShaderResources(0,9,views);g.context->CSSetUnorderedAccessViews(0,2,uavs,nullptr);
    g.context->CSSetConstantBuffers(0,1,&cb);
    g.context->Dispatch((params.width+15)/16,(params.height+15)/16,1);
    ID3D11ShaderResourceView* none[9]={};ID3D11UnorderedAccessView* noUav[2]={};
    g.context->CSSetShaderResources(0,9,none);g.context->CSSetUnorderedAccessViews(0,2,noUav,nullptr);
}

static void detailPass(Device& g, DetailParams params, UINT pass, int source, int blurred, int target,
                       int extra0=-1, int extra1=-1, int extra2=-1) {
    detailPassView(g,params,pass,source,blurred>=0?g.workView[blurred].Get():nullptr,target,extra0,extra1,extra2);
}

static void reserveNoise(Device& g, UINT lutFloats, UINT spaceEntries) {
    if(!g.extremes) {
        g.extremes=buffer(g.device.Get(),128,4,D3D11_BIND_UNORDERED_ACCESS);   // 0-5 extremes, 8+ luma_wavelet statistics
        g.extremesStaging=buffer(g.device.Get(),128,4,0,D3D11_USAGE_STAGING,D3D11_CPU_ACCESS_READ);
        check(g.device->CreateUnorderedAccessView(g.extremes.Get(),nullptr,&g.extremesAccess));
    }
    lutFloats=lutFloats<1?1:lutFloats; spaceEntries=spaceEntries<1?1:spaceEntries;
    if(lutFloats>g.lutCapacity) {
        g.lutView.Reset();g.luts=buffer(g.device.Get(),lutFloats,4,D3D11_BIND_SHADER_RESOURCE);
        check(g.device->CreateShaderResourceView(g.luts.Get(),nullptr,&g.lutView));g.lutCapacity=lutFloats;
    }
    if(spaceEntries>g.spaceCapacity) {
        g.spaceView.Reset();g.spaces=buffer(g.device.Get(),spaceEntries,16,D3D11_BIND_SHADER_RESOURCE);
        check(g.device->CreateShaderResourceView(g.spaces.Get(),nullptr,&g.spaceView));g.spaceCapacity=spaceEntries;
    }
}

static float decodeKey(UINT u) { u=(u&0x80000000u)?(u&0x7fffffffu):~u; float f; std::memcpy(&f,&u,4); return f; }

// processing.chroma_guided on the Lab frame in work[current]: a colour-guided filter (He et al.) with the
// guide (L, median5 a, median5 b). Box means (cv2.boxFilter, REFLECT_101) run through the separable blur
// passes with uniform taps. Needs four more frame buffers (work[4..7]); large frames release them after.
// The result ends in current/other (0/1) because the following detail passes use 2 and 3 as scratch.
static void reserveGuided(Device& g, UINT floats) {
    if(floats<=g.guidedCapacity) return;
    for(int i=4;i<8;++i) {
        g.workView[i].Reset();g.workAccess[i].Reset();g.work[i].Reset();
        g.work[i]=buffer(g.device.Get(),floats,4,D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_UNORDERED_ACCESS);
        check(g.device->CreateShaderResourceView(g.work[i].Get(),nullptr,&g.workView[i]));
        check(g.device->CreateUnorderedAccessView(g.work[i].Get(),nullptr,&g.workAccess[i]));
    }
    g.guidedCapacity=floats;
}

static void freeGuided(Device& g) {
    for(int i=4;i<8;++i){g.workView[i].Reset();g.workAccess[i].Reset();g.work[i].Reset();}
    g.guidedCapacity=0;
}

// The four frame-sized buffers stay allocated between calls: a batch export of 24 MP photos spent a third
// of its GPU time creating and freeing 1.2 GB for every photo. grainy_gpu_trim frees them (the caller
// does that after a few idle seconds). Frames above ~2.8 MP whose buffers would take more than a quarter
// of the video memory are freed at once, as before.
static void releaseGuided(Device& g, UINT floats) {
    if(floats>(1u<<23)&&(g.memory==0||16ull*floats>g.memory/4)) freeGuided(g);
}

static void chromaGuided(Device& g, const DetailParams& base, const NoiseParams& noise, UINT floats, int& current, int& other) {
    reserveGuided(g,floats);
    DetailParams p=base;p.scaleIndex=noise.guidedEps;p.constant=noise.guidedChromaEps;p.strength=noise.guidedBlend;
    DetailParams box=base;box.radius=noise.guidedRadius;box.kernelOffset=noise.guidedOffset;
    auto boxFilter=[&](int target,int scratch){detailPass(g,box,0,target,-1,scratch);detailPass(g,box,1,scratch,-1,target);};
    const int guide=4,means=5,cov=6,inverse1=7,inverse2=2;
    detailPass(g,p,17,current,-1,guide);
    detailPass(g,box,0,guide,-1,2);detailPass(g,box,1,2,-1,means);        // means of G
    detailPass(g,p,18,guide,-1,3);boxFilter(3,2);                        // box(I*G) in 3
    detailPass(g,p,19,guide,-1,cov);boxFilter(cov,2);                    // box(ga*ga, ga*gb, gb*gb) in cov
    p.channel=0;detailPass(g,p,20,means,3,inverse1,cov);
    p.channel=1;detailPass(g,p,20,means,3,inverse2,cov);
    int spare[3]={other,3,cov};
    for(UINT c=1;c<3;++c) {
        if(!noise.active[c]) continue;
        int d=spare[0],e=spare[1],k=spare[2];
        p.channel=c;
        detailPass(g,p,21,current,guide,d);boxFilter(d,k);
        detailPass(g,p,22,current,guide,e);boxFilter(e,k);
        detailPass(g,p,23,means,d,k,e,inverse1,inverse2);                   // A
        detailPass(g,p,24,means,k,e,d);                                     // B (reads mean p from d)
        boxFilter(k,d);boxFilter(e,d);
        detailPass(g,p,25,current,guide,d,k,e);
        spare[0]=current;current=d;
    }
    other=current==0?1:0;
}

// processing.luma_wavelet on L of the Lab frame in work[current]; the result ends in current/other (0/1).
static void lumaWavelet(Device& g, const DetailParams& base, const NoiseParams& noise, UINT floats, int& current, int& other) {
    reserveGuided(g,floats);
    std::vector<UINT> zeros(128,0);
    g.context->UpdateSubresource(g.extremes.Get(),0,nullptr,zeros.data(),0,0);
    DetailParams p=base;p.scaleIndex=noise.waveletLambda;
    DetailParams box=base;box.radius=2;box.kernelOffset=noise.boxOffset;
    int P=4,next=5,S=6,E=7,scratch=2;
    detailPass(g,p,26,current,-1,P);
    for(UINT level=0;level<5;++level) {
        DetailParams k=base;k.radius=2u<<level;k.kernelOffset=noise.waveletOffset[level];
        detailPass(g,k,0,P,-1,scratch);detailPass(g,k,1,scratch,-1,S);
        p.channel=level;
        if(level<3) {detailPass(g,p,27,P,S,-1);detailPass(g,p,28,P,S,-1);}
        detailPass(g,p,29,P,S,E);detailPass(g,box,0,E,-1,scratch);detailPass(g,box,1,scratch,-1,E);
        detailPass(g,p,30,P,S,next,E);std::swap(P,next);
    }
    detailPass(g,p,31,current,P,other);std::swap(current,other);
}

// processing.detail_tools noise reduction on the GPU: RGB->Lab, chroma_guided on a/b, then
// cv2.bilateralFilter on L with OpenCV's float LUT construction, blend by strength, Lab->RGB.
static void noiseReduction(Device& g, const DetailParams& base, const NoiseParams& noise, int& current, int& other) {
    DetailParams p=base;
    detailPass(g,p,6,current,-1,other);std::swap(current,other);
    UINT floats=base.width*base.height*3;
    if(noise.active[1]||noise.active[2]) chromaGuided(g,p,noise,floats,current,other);
    if(noise.active[0]&&noise.lumaWavelet) {
        reserveNoise(g,1,1);lumaWavelet(g,p,noise,floats,current,other);
        releaseGuided(g,floats);
        detailPass(g,p,8,current,-1,other);std::swap(current,other);
        return;
    }
    releaseGuided(g,floats);
    UINT initial[6]={0xffffffffu,0xffffffffu,0xffffffffu,0,0,0};
    reserveNoise(g,1,1);
    {D3D11_BOX b{0,0,0,sizeof(initial),1,1};g.context->UpdateSubresource(g.extremes.Get(),0,&b,initial,0,0);}
    detailPass(g,p,9,current,-1,-1);
    g.context->CopyResource(g.extremesStaging.Get(),g.extremes.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};UINT keys[6];
    check(g.context->Map(g.extremesStaging.Get(),0,D3D11_MAP_READ,0,&mapped));
    std::memcpy(keys,mapped.pData,sizeof(keys));g.context->Unmap(g.extremesStaging.Get(),0);
    const int bins=1<<12;
    std::vector<float> luts;std::vector<float> spaces;
    UINT lutOffset[3]={},spaceOffset[3]={},spaceCount[3]={};float scale[3]={};UINT constant[3]={};
    int radius=noise.diameter/2; if(radius<1) radius=1;
    for(int c=0;c<1;++c) {
        if(!noise.active[c]) continue;
        double minimum=decodeKey(keys[c]),maximum=decodeKey(keys[3+c]);
        if(std::abs(minimum-maximum)<1.1920929e-7) {constant[c]=1;continue;}
        double sigmaColor=noise.sigmaColor[c]<=0?1:noise.sigmaColor[c],sigmaSpace=noise.sigmaSpace[c]<=0?1:noise.sigmaSpace[c];
        double colorCoeff=-0.5/(sigmaColor*sigmaColor),spaceCoeff=-0.5/(sigmaSpace*sigmaSpace);
        float length=(float)(maximum-minimum);scale[c]=bins/length;
        lutOffset[c]=(UINT)luts.size();float last=1.f;
        for(int i=0;i<bins+2;++i) {
            if(last>0.f){double value=i/scale[c];luts.push_back((float)std::exp(value*value*colorCoeff));last=luts.back();}
            else luts.push_back(0.f);
        }
        spaceOffset[c]=(UINT)(spaces.size()/4);
        for(int i=-radius;i<=radius;++i) for(int j=-radius;j<=radius;++j) {
            double r=std::sqrt((double)i*i+(double)j*j);if(r>radius) continue;
            spaces.insert(spaces.end(),{(float)i,(float)j,(float)std::exp(r*r*spaceCoeff),0.f});
        }
        spaceCount[c]=(UINT)(spaces.size()/4)-spaceOffset[c];
    }
    reserveNoise(g,(UINT)luts.size(),(UINT)(spaces.size()/4));
    if(!luts.empty()){D3D11_BOX b{0,0,0,(UINT)luts.size()*4,1,1};g.context->UpdateSubresource(g.luts.Get(),0,&b,luts.data(),0,0);}
    if(!spaces.empty()){D3D11_BOX b{0,0,0,(UINT)spaces.size()*4,1,1};g.context->UpdateSubresource(g.spaces.Get(),0,&b,spaces.data(),0,0);}
    for(UINT c=0;c<1;++c) {
        if(!noise.active[c]) continue;
        p.channel=c;p.lutOffset=lutOffset[c];p.spaceOffset=spaceOffset[c];p.spaceCount=spaceCount[c];
        p.scaleIndex=scale[c];p.strength=noise.strength[c];p.constant=(float)constant[c];
        detailPass(g,p,7,current,-1,other);std::swap(current,other);
    }
    detailPass(g,p,8,current,-1,other);std::swap(current,other);
}

// Detail stage after the CPU-only filters. radii[3]/offsets[3] describe the texture, clarity and
// sharpen Gaussian kernels in `kernels` (radius 0xffffffff = step disabled).
static bool validDetail(const DetailParams* params,unsigned kernelTotal,const float* kernels,const unsigned* radii,const unsigned* offsets) {
    if(!params||!radii||!offsets||kernelTotal>1u<<20) return false;
    for(int i=0;i<3;++i) if(radii[i]!=0xffffffffu&&(!kernels||offsets[i]+2ull*radii[i]+1>kernelTotal)) return false;
    return true;
}

// Detail passes on the image in work[current]; work 2 = horizontal scratch, 3 = blurred.
static void runDetail(Device* g,const DetailParams* params,const float* kernels,unsigned kernelTotal,
                      const unsigned* radii,const unsigned* offsets,int tools,int vignette,const NoiseParams* noise,
                      int& current,int& other) {
        if(kernelTotal){D3D11_BOX k{0,0,0,kernelTotal*4,1,1};g->context->UpdateSubresource(g->kernels.Get(),0,&k,kernels,0,0);}
        auto blur=[&](int step) {
            DetailParams b=*params;b.radius=radii[step];b.kernelOffset=offsets[step];
            detailPass(*g,b,0,current,-1,2);detailPass(*g,b,1,2,-1,3);
        };
        auto combine=[&](UINT pass,int blurred) { detailPass(*g,*params,pass,current,blurred,other);std::swap(current,other); };
        if(tools&&noise&&(noise->active[0]||noise->active[1]||noise->active[2])) {
            reserveNoise(*g,1,1);
            noiseReduction(*g,*params,*noise,current,other);
        }
        if(tools&&(params->flags&32)) {        // dehaze
            detailPass(*g,*params,10,current,-1,2);detailPass(*g,*params,11,2,-1,3);detailPass(*g,*params,12,3,-1,2);
            combine(13,2);
        }
        if(tools) {
            if(radii[0]!=0xffffffffu) blur(0);
            combine(2,radii[0]!=0xffffffffu?3:-1);
            if(params->flags&16) combine(14,-1);
        } else if(params->flags&2) combine(2,-1);   // CPU ran detail_tools; clip is idempotent
        if(radii[1]!=0xffffffffu) {blur(1);combine(3,3);}
        if(radii[2]!=0xffffffffu) {blur(2);combine(4,3);}
        if(vignette) combine(5,-1);
}

static void readWork(Device* g,int index,float* output,UINT floats) {
    D3D11_BOX box{0,0,0,floats*4,1,1};
    g->context->CopySubresourceRegion(g->staging.Get(),0,0,0,0,g->work[index].Get(),0,&box);
    D3D11_MAPPED_SUBRESOURCE mapped{};
    check(g->context->Map(g->staging.Get(),0,D3D11_MAP_READ,0,&mapped));
    std::memcpy(output,mapped.pData,static_cast<size_t>(floats)*4);
    g->context->Unmap(g->staging.Get(),0);
}

static void writeWork(Device* g,int index,const float* input,UINT floats) {
    D3D11_BOX box{0,0,0,floats*4,1,1};
    g->context->UpdateSubresource(g->work[index].Get(),0,&box,input,0,0);
}

API int grainy_gpu_detail(void* handle,const float* input,float* output,const DetailParams* params,
                          const float* kernels,unsigned kernelTotal,const unsigned* radii,const unsigned* offsets,
                          int tools,int vignette,const NoiseParams* noise) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)||!validDetail(params,kernelTotal,kernels,radii,offsets)) return E_INVALIDARG;
    try {
        UINT floats=3u*params->width*params->height;
        reserveWork(*g,floats,kernelTotal);
        writeWork(g,0,input,floats);
        int current=0,other=1;
        runDetail(g,params,kernels,kernelTotal,radii,offsets,tools,vignette,noise,current,other);
        readWork(g,current,output,floats);
        return S_OK;
    } catch(HRESULT h){return h;} catch(...){return E_FAIL;}
}

static void uploadPoints(Device* g,const PointParams* points,unsigned count) {
    UINT needed=count<1?1:count;
    if(needed>g->pointCapacity) {
        g->pointView.Reset();g->points=buffer(g->device.Get(),needed,sizeof(PointParams),D3D11_BIND_SHADER_RESOURCE);
        check(g->device->CreateShaderResourceView(g->points.Get(),nullptr,&g->pointView));g->pointCapacity=needed;
    }
    if(count){D3D11_BOX b{0,0,0,count*(UINT)sizeof(PointParams),1,1};g->context->UpdateSubresource(g->points.Get(),0,&b,points,0,0);}
}

// A single-pass stage shader (tone/colour) reading work[source] and writing work[target].
static void stagePass(Device* g,ID3D11ComputeShader* shader,ID3D11Buffer* params,const void* paramData,
                      UINT width,UINT height,int source,int target) {
    g->context->UpdateSubresource(params,0,nullptr,paramData,0,0);
    ID3D11ShaderResourceView* views[3]={g->workView[source].Get(),g->curveView.Get(),g->pointView.Get()};
    ID3D11UnorderedAccessView* uav=g->workAccess[target].Get();
    g->context->CSSetShader(shader,nullptr,0);
    g->context->CSSetShaderResources(0,3,views);g->context->CSSetUnorderedAccessViews(0,1,&uav,nullptr);
    g->context->CSSetConstantBuffers(0,1,&params);
    g->context->Dispatch((width+15)/16,(height+15)/16,1);
    ID3D11ShaderResourceView* none[3]={};ID3D11UnorderedAccessView* noUav=nullptr;
    g->context->CSSetShaderResources(0,3,none);g->context->CSSetUnorderedAccessViews(0,1,&noUav,nullptr);
}

// Tone -> colour -> detail with one upload and one readback (engine.develop without a stage cache).
API int grainy_gpu_develop(void* handle,const float* input,float* output,
                           const ToneParams* tone,const float* toneCurve,
                           const ColorParams* color,const float* colorCurves,unsigned colorCurveTotal,const PointParams* points,
                           const DetailParams* detail,const float* kernels,unsigned kernelTotal,
                           const unsigned* radii,const unsigned* offsets,int vignette,const NoiseParams* noise) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!tone||!color||!detail||!validSize(tone->width,tone->height)) return E_INVALIDARG;
    if(color->width!=tone->width||color->height!=tone->height||detail->width!=tone->width||detail->height!=tone->height) return E_INVALIDARG;
    if(!toneValid(tone,toneCurve)||colorCurveTotal>4096) return E_INVALIDARG;
    for(int c=0;c<3;++c) if(color->curveCount[c]&&(color->curveCount[c]<2||color->curveOffset[c]+color->curveCount[c]>colorCurveTotal||!colorCurves)) return E_INVALIDARG;
    if(!validDetail(detail,kernelTotal,kernels,radii,offsets)||color->pointCount>256||(color->pointCount&&!points)) return E_INVALIDARG;
    try {
        uploadPoints(g,points,color->pointCount);
        UINT floats=3u*tone->width*tone->height;
        reserveWork(*g,floats,kernelTotal);
        writeWork(g,0,input,floats);
        int current=0,other=1;
        if(UINT n=tonePoints(tone)){reserveCurve(*g,n);D3D11_BOX c{0,0,0,n*8,1,1};g->context->UpdateSubresource(g->curve.Get(),0,&c,toneCurve,0,0);}
        stagePass(g,g->tone.Get(),g->toneParams.Get(),tone,tone->width,tone->height,current,other);std::swap(current,other);
        if(colorCurveTotal){reserveCurve(*g,colorCurveTotal);D3D11_BOX c{0,0,0,colorCurveTotal*8,1,1};g->context->UpdateSubresource(g->curve.Get(),0,&c,colorCurves,0,0);}
        stagePass(g,g->color.Get(),g->colorParams.Get(),color,tone->width,tone->height,current,other);std::swap(current,other);
        runDetail(g,detail,kernels,kernelTotal,radii,offsets,1,vignette,noise,current,other);
        readWork(g,current,output,floats);
        return S_OK;
    } catch(HRESULT h){return h;} catch(...){return E_FAIL;}
}

static void copyFloats(Device* g,ID3D11Buffer* to,ID3D11Buffer* from,UINT floats) {
    D3D11_BOX box{0,0,0,floats*4,1,1};
    g->context->CopySubresourceRegion(to,0,0,0,0,from,0,&box);
}

static ID3D11Buffer* slotBuffer(Device* g,int slot,UINT floats) {
    if(floats>g->slotCapacity[slot]) {
        g->slotViews[slot].Reset();g->slots[slot].Reset();g->slotCapacity[slot]=0;
        g->slots[slot]=buffer(g->device.Get(),floats,4,D3D11_BIND_SHADER_RESOURCE);
        check(g->device->CreateShaderResourceView(g->slots[slot].Get(),nullptr,&g->slotViews[slot]));
        g->slotCapacity[slot]=floats;
    }
    return g->slots[slot].Get();
}

static void localPass(Device* g,const LocalParams& params,ID3D11ShaderResourceView* mask,int source,int target) {
    g->context->UpdateSubresource(g->localParams.Get(),0,nullptr,&params,0,0);
    ID3D11ShaderResourceView* views[3]={g->workView[source].Get(),g->curveView.Get(),mask};
    ID3D11UnorderedAccessView* uav=g->workAccess[target].Get();ID3D11Buffer* cb=g->localParams.Get();
    g->context->CSSetShader(g->local.Get(),nullptr,0);
    g->context->CSSetShaderResources(0,3,views);g->context->CSSetUnorderedAccessViews(0,1,&uav,nullptr);
    g->context->CSSetConstantBuffers(0,1,&cb);
    g->context->Dispatch((params.width+15)/16,(params.height+15)/16,1);
    ID3D11ShaderResourceView* none[3]={};ID3D11UnorderedAccessView* noUav=nullptr;
    g->context->CSSetShaderResources(0,3,none);g->context->CSSetUnorderedAccessViews(0,1,&noUav,nullptr);
}

static void uploadSlot(Device* g,int slot,const float* data,UINT floats) {
    D3D11_BOX box{0,0,0,floats*4,1,1};
    g->context->UpdateSubresource(slotBuffer(g,slot,floats),0,&box,data,0,0);
}

// engine.develop for a preview cache, from a kept stage result to the developed frame.
// firstStage: 0 tone, 1 colour, 2 detail, 3 local edits. The input is uploaded into inputSlot or (input==nullptr)
// read from it; parameters of skipped stages are unused. keep[0..2] receive the tone, colour and detail+grain
// results (-1: not kept). grainSlot>=0 adds that per-pixel grain field after detail (grain==nullptr: the slot
// already holds it). Then layerCount shape-mask edits (processing._local_region's blend over the whole frame,
// identical to the bounded region) with masks[i] uploaded into maskSlots[i] (nullptr: already there), and the
// final clip when clip!=0. Only the result is read back.
API int grainy_gpu_resident(void* handle,const float* input,int inputSlot,unsigned firstStage,const int* keep,float* output,
                            const ToneParams* tone,const float* toneCurve,
                            const ColorParams* color,const float* colorCurves,unsigned colorCurveTotal,const PointParams* points,
                            const DetailParams* detail,const float* kernels,unsigned kernelTotal,
                            const unsigned* radii,const unsigned* offsets,int vignette,const NoiseParams* noise,
                            const float* grain,int grainSlot,
                            unsigned layerCount,const LocalParams* layers,const float* const* masks,const int* maskSlots,
                            const float* layerCurves,unsigned layerCurveTotal,int clip) {
    auto g=static_cast<Device*>(handle);
    auto slotOk=[](int slot){return slot>=0&&slot<Device::slotCount;};
    if(!g||!output||!keep||!detail||!validSize(detail->width,detail->height)||firstStage>3||!slotOk(inputSlot)) return E_INVALIDARG;
    for(int i=0;i<3;++i) if(keep[i]<-1||keep[i]>=Device::slotCount||keep[i]==inputSlot) return E_INVALIDARG;
    UINT width=detail->width,height=detail->height,pixels=width*height,floats=3u*pixels;
    if(!input&&g->slotCapacity[inputSlot]<floats) return E_INVALIDARG;
    if(firstStage==3&&input) return E_INVALIDARG;
    if(firstStage==0) {
        if(!tone||tone->width!=width||tone->height!=height) return E_INVALIDARG;
        if(!toneValid(tone,toneCurve)) return E_INVALIDARG;
    }
    if(firstStage<=1) {
        if(!color||color->width!=width||color->height!=height||colorCurveTotal>4096) return E_INVALIDARG;
        for(int c=0;c<3;++c) if(color->curveCount[c]&&(color->curveCount[c]<2||color->curveOffset[c]+color->curveCount[c]>colorCurveTotal||!colorCurves)) return E_INVALIDARG;
        if(color->pointCount>256||(color->pointCount&&!points)) return E_INVALIDARG;
    }
    if(firstStage<=2) {
        if(!validDetail(detail,kernelTotal,kernels,radii,offsets)) return E_INVALIDARG;
        if(grainSlot>=0&&(!slotOk(grainSlot)||grainSlot==inputSlot||(!grain&&g->slotCapacity[grainSlot]<pixels))) return E_INVALIDARG;
    }
    if(layerCount>16||(layerCount&&(!layers||!masks||!maskSlots))||layerCurveTotal>4096||(layerCurveTotal&&!layerCurves)) return E_INVALIDARG;
    for(unsigned i=0;i<layerCount;++i) {
        const LocalParams& l=layers[i];
        if(l.width!=width||l.height!=height||!slotOk(maskSlots[i])||maskSlots[i]==inputSlot) return E_INVALIDARG;
        if(!masks[i]&&g->slotCapacity[maskSlots[i]]<pixels) return E_INVALIDARG;
        for(int c=0;c<4;++c) if(l.curveCount[c]&&(l.curveCount[c]<2||l.curveOffset[c]+l.curveCount[c]>layerCurveTotal)) return E_INVALIDARG;
    }
    try {
        reserveWork(*g,floats,kernelTotal);
        ID3D11Buffer* frame=slotBuffer(g,inputSlot,floats);
        if(input) {writeWork(g,0,input,floats);copyFloats(g,frame,g->work[0].Get(),floats);}
        else copyFloats(g,g->work[0].Get(),frame,floats);
        int current=0,other=1;
        if(firstStage==0) {
            if(UINT n=tonePoints(tone)){reserveCurve(*g,n);D3D11_BOX c{0,0,0,n*8,1,1};g->context->UpdateSubresource(g->curve.Get(),0,&c,toneCurve,0,0);}
            stagePass(g,g->tone.Get(),g->toneParams.Get(),tone,width,height,current,other);std::swap(current,other);
            if(keep[0]>=0) copyFloats(g,slotBuffer(g,keep[0],floats),g->work[current].Get(),floats);
        }
        if(firstStage<=1) {
            uploadPoints(g,points,color->pointCount);
            if(colorCurveTotal){reserveCurve(*g,colorCurveTotal);D3D11_BOX c{0,0,0,colorCurveTotal*8,1,1};g->context->UpdateSubresource(g->curve.Get(),0,&c,colorCurves,0,0);}
            stagePass(g,g->color.Get(),g->colorParams.Get(),color,width,height,current,other);std::swap(current,other);
            if(keep[1]>=0) copyFloats(g,slotBuffer(g,keep[1],floats),g->work[current].Get(),floats);
        }
        if(firstStage<=2) {
            runDetail(g,detail,kernels,kernelTotal,radii,offsets,1,vignette,noise,current,other);
            if(grainSlot>=0) {
                if(grain) uploadSlot(g,grainSlot,grain,pixels);
                detailPassView(*g,*detail,16,current,g->slotViews[grainSlot].Get(),other);std::swap(current,other);
            }
            if(keep[2]>=0) copyFloats(g,slotBuffer(g,keep[2],floats),g->work[current].Get(),floats);
        }
        if(layerCount) {
            reserveCurve(*g,layerCurveTotal<1?1:layerCurveTotal);
            if(layerCurveTotal){D3D11_BOX c{0,0,0,layerCurveTotal*8,1,1};g->context->UpdateSubresource(g->curve.Get(),0,&c,layerCurves,0,0);}
            for(unsigned i=0;i<layerCount;++i) {
                if(masks[i]) uploadSlot(g,maskSlots[i],masks[i],pixels);
                else slotBuffer(g,maskSlots[i],pixels);
                localPass(g,layers[i],g->slotViews[maskSlots[i]].Get(),current,other);std::swap(current,other);
            }
        }
        if(clip) {detailPassView(*g,*detail,15,current,nullptr,other);std::swap(current,other);}
        readWork(g,current,output,floats);
        return S_OK;
    } catch(HRESULT h){return h;} catch(...){return E_FAIL;}
}

// Frees one resident slot (-1: all of them).
API int grainy_gpu_release(void* handle,int slot) {
    auto g=static_cast<Device*>(handle);
    if(!g||slot<-1||slot>=Device::slotCount) return E_INVALIDARG;
    for(int i=0;i<Device::slotCount;++i) if(slot<0||slot==i) {g->slotViews[i].Reset();g->slots[i].Reset();g->slotCapacity[i]=0;}
    return S_OK;
}

// Frees the large scratch buffers kept between calls (see releaseGuided). Small ones stay.
API int grainy_gpu_trim(void* handle) {
    auto g=static_cast<Device*>(handle);
    if(!g) return E_INVALIDARG;
    if(g->guidedCapacity>(1u<<23)) freeGuided(*g);
    return S_OK;
}

API unsigned long long grainy_gpu_memory(void* handle) {
    auto g=static_cast<Device*>(handle);
    return g?g->memory:0;
}

// Same-size warp from work[0] to work[1] with one upload and one readback.
static int warp(Device* g,ID3D11ComputeShader* shader,ID3D11Buffer* params,const void* paramData,
                const float* input,float* output,UINT width,UINT height) {
    if(!shader) return E_NOTIMPL;
    try {
        UINT floats=3u*width*height;
        reserveWork(*g,floats,0);
        writeWork(g,0,input,floats);
        g->context->UpdateSubresource(params,0,nullptr,paramData,0,0);
        ID3D11ShaderResourceView* view=g->workView[0].Get();ID3D11UnorderedAccessView* uav=g->workAccess[1].Get();
        g->context->CSSetShader(shader,nullptr,0);
        g->context->CSSetShaderResources(0,1,&view);g->context->CSSetUnorderedAccessViews(0,1,&uav,nullptr);
        g->context->CSSetConstantBuffers(0,1,&params);
        g->context->Dispatch((width+15)/16,(height+15)/16,1);
        ID3D11ShaderResourceView* none=nullptr;ID3D11UnorderedAccessView* noUav=nullptr;
        g->context->CSSetShaderResources(0,1,&none);g->context->CSSetUnorderedAccessViews(0,1,&noUav,nullptr);
        readWork(g,1,output,floats);
        return S_OK;
    } catch(HRESULT h){return h;} catch(...){return E_FAIL;}
}

API int grainy_gpu_optical(void* handle,const float* input,float* output,const OpticalParams* params) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)) return E_INVALIDARG;
    return warp(g,g->optical.Get(),g->opticalParams.Get(),params,input,output,params->width,params->height);
}

API int grainy_gpu_rotate(void* handle,const float* input,float* output,const RotateParams* params) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)) return E_INVALIDARG;
    return warp(g,g->rotate.Get(),g->rotateParams.Get(),params,input,output,params->width,params->height);
}

// Global colour stage for a contiguous float32 RGB frame.
API int grainy_gpu_color(void* handle,const float* input,float* output,const ColorParams* params,const float* curvePoints,unsigned curveTotal,
                         const PointParams* points) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)||curveTotal>4096||params->pointCount>256) return E_INVALIDARG;
    if(params->pointCount&&!points) return E_INVALIDARG;
    for(int c=0;c<3;++c) {
        if(params->curveCount[c]==0) continue;
        if(params->curveCount[c]<2||params->curveOffset[c]+params->curveCount[c]>curveTotal||!curvePoints) return E_INVALIDARG;
    }
    try {
        UINT floats=3u*params->width*params->height;
        reserveWork(*g,floats,0);writeWork(g,0,input,floats);
        reserveCurve(*g,curveTotal);
        if(curveTotal){D3D11_BOX c{0,0,0,curveTotal*8,1,1};g->context->UpdateSubresource(g->curve.Get(),0,&c,curvePoints,0,0);}
        uploadPoints(g,points,params->pointCount);
        stagePass(g,g->color.Get(),g->colorParams.Get(),params,params->width,params->height,0,1);
        readWork(g,1,output,floats);
        return S_OK;
    } catch(HRESULT h){return h;} catch(...){return E_FAIL;}
}
