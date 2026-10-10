// Grainy GPU develop stages on macOS (Metal compute): the counterpart of gpu_compute.cpp (D3D11), with
// the same exported functions, parameter structures and pass order, so luma/native_gpu.py drives both.
// CPU numpy code remains the reference; Python falls back to it whenever a call here fails.
//
// Differences from the D3D11 module:
//  - Metal has no double type. Its float division is correctly rounded with fast math off (measured
//    on Apple Silicon), so the D3D "exact division" helpers are plain divisions here. The rotation
//    shader needs doubles for PIL's bicubic and is not provided: grainy_gpu_rotate reports "not
//    implemented" and the CPU rotates.
//  - Passes are queued in one command buffer per call and run in order; anything the CPU writes or
//    reads in between (an upload, the noise statistics) first waits for the queued passes.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <cwchar>
#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

#define API extern "C" __attribute__((visibility("default")))
typedef unsigned int UINT;
// The D3D module's result codes; Python only tests for a negative value.
static const int S_OK=0, E_NOTIMPL=(int)0x80004001, E_FAIL=(int)0x80004005, E_OUTOFMEMORY=(int)0x8007000E, E_INVALIDARG=(int)0x80070057;
static void check(bool ok) { if(!ok) throw E_FAIL; }

// Keep in sync with luma/native_gpu.py (_ToneParams) and native/gpu_compute.cpp.
struct ToneParams {
    float gain[4];
    float shadows, highlights, black, whitesGain;
    float contrast, vignette; unsigned curveCount, flags;
    unsigned width, height; float curveSlope; unsigned localMaps;
    float shoulderKnee, shoulderWhite, shoulderSlope, localFloor;
};
static_assert(sizeof(ToneParams)==80,"tone layout");

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

struct LocalParams {
    float exposureGain, contrast, saturation, shadows;
    float highlights, black, whiteMinusBlack, hueShift;
    float tempTint[4];
    unsigned width, height, flags, pad;
    unsigned curveOffset[4], curveCount[4];
    float hsl[8][4];
};
static_assert(sizeof(LocalParams)==224,"local layout");

struct ColorParams {
    float saturation, vibrance; unsigned width, height;
    unsigned flags, pad0, pad1, pad2;
    unsigned curveOffset[4], curveCount[4];
    float hsl[8][4];
    float parametric[4];
    float calibration[3][4];
    float grading[3][4];
    float gradingBalance, pad3, pad4, pad5;
    float matrix[3][4];
    unsigned pointCount, pad6, pad7, pad8;
};
static_assert(sizeof(ColorParams)==384,"colour layout");

struct PointParams {
    float reference[4];
    float widths[4];
    float amounts[4];
    unsigned flags[4];
};
static_assert(sizeof(PointParams)==64,"point layout");

struct DetailParams {
    unsigned width, height, flags, stage;
    float texture, clarity, sharpen, threshold;
    float maskDivisor, pixelScale, vignette, pad0;
    unsigned radius, kernelOffset, pad1, pad2;
    unsigned channel, lutOffset, spaceOffset, spaceCount;
    float scaleIndex, strength, constant, pad3;
    float dehaze, defringe, edgeScale; unsigned dehazeRadius;
    float vignetteRound, vignetteP, vignetteScale, vignetteExponent;
    float vignetteProtect, vignetteAspectX, vignetteAspectY, pad5;
};
static_assert(sizeof(DetailParams)==144,"detail layout");

struct OpticalParams {
    unsigned width, height, pad0, pad1;
    float halfWidth, halfHeight, scale, perspectiveH;
    float perspectiveV, zoom, aspect, shiftX;
    float shiftY, distortion1, distortion2, distortion3;
    float channelScale[4];
};
static_assert(sizeof(OpticalParams)==80,"optical layout");

struct RotateParams {
    unsigned width, height, pad0, pad1;
    double matrix[6];
};

struct NoiseParams {
    unsigned active[3]; unsigned diameter;
    float strength[3], sigmaColor[3], sigmaSpace[3];
    unsigned guidedRadius, guidedOffset; float guidedEps, guidedBlend, guidedChromaEps, pad[3];
    unsigned lumaWavelet, waveletOffset[5], boxOffset; float waveletLambda;
};
static_assert(sizeof(NoiseParams)==116,"noise layout");

// The shaders. Each statement mirrors the HLSL in gpu_compute.cpp; "contract(off)" keeps a*b+c as a
// separate multiply and add like numpy (Metal fuses them otherwise, even with fast math off).
static const char* shaders=R"METAL(
#include <metal_stdlib>
#pragma clang fp contract(off)
using namespace metal;

struct ToneParams {
    float4 gain; float shadows; float highlights; float black; float whitesGain;
    float contrast; float vignette; uint curveCount; uint flags; uint width; uint height; float curveSlope; uint localMaps;
    float shoulderKnee; float shoulderWhite; float shoulderSlope; float localFloor;
};
struct LocalParams {
    float exposureGain; float contrast; float saturation; float shadows;
    float highlights; float black; float whiteMinusBlack; float hueShift;
    float4 tempTint; uint width; uint height; uint flags; uint pad;
    uint4 curveOffset; uint4 curveCount; float4 hsl[8];
};
struct ColorParams {
    float saturation; float vibrance; uint width; uint height;
    uint flags; uint pad0a; uint pad0b; uint pad0c;
    uint4 curveOffset; uint4 curveCount; float4 hsl[8];
    float4 parametric; float4 calibration[3]; float4 grading[3];
    float gradingBalance; float pad1a; float pad1b; float pad1c; float4 cameraMatrix[3];
    uint pointCount; uint pad2a; uint pad2b; uint pad2c;
};
struct Point { float4 reference; float4 widths; float4 amounts; uint4 flags; };
struct DetailParams {
    uint width; uint height; uint flags; uint stage;
    float textureAmount; float clarityAmount; float sharpenAmount; float threshold;
    float maskDivisor; float pixelScale; float vignette; float pad0;
    uint radius; uint kernelOffset; uint pad1a; uint pad1b;
    uint channel; uint lutOffset; uint spaceOffset; uint spaceCount;
    float scaleIndex; float strength; float constantValue; float pad2;
    float dehazeAmount; float defringeAmount; float edgeScale; uint dehazeRadius;
    float vignetteRound; float vignetteP; float vignetteScale; float vignetteExponent;
    float vignetteProtect; float vignetteAspectX; float vignetteAspectY; float pad5;
};
struct OpticalParams {
    uint width; uint height; uint pad0a; uint pad0b;
    float halfWidth; float halfHeight; float scale; float perspectiveH;
    float perspectiveV; float zoom; float aspect; float shiftX;
    float shiftY; float distortion1; float distortion2; float distortion3;
    float4 channelScale;
};
struct Groups { float4 g[8]; };

static float toSRGB(float a) { a=max(a,0.0f); return a<=.0031308f ? a*12.92f : 1.055f*pow(a,0.4166666666666667f)-.055f; }
static float toLinear(float a) { return a<=.04045f ? a/12.92f : pow(max(a+.055f,0.0f)/1.055f,2.4f); }
static float3 toSRGB3(float3 a) { return float3(toSRGB(a.x),toSRGB(a.y),toSRGB(a.z)); }
static float3 toLinear3(float3 a) { return float3(toLinear(a.x),toLinear(a.y),toLinear(a.z)); }
static float interpolate(const device float2* curve,float x,uint first,uint count) {
    // numpy.interp: clamp to end values outside the points, linear between them.
    if(x<=curve[first].x) return curve[first].y;
    for(uint i=first+1;i<first+count;++i) {
        float2 p=curve[i-1],q=curve[i];
        if(x<=q.x) return q.x>p.x ? p.y+(x-p.x)*(q.y-p.y)/(q.x-p.x) : q.y;
    }
    return curve[first+count-1].y;
}
// OpenCV float RGB<->HLS (hue in degrees), as used by processing.rgb_to_hsl/hsl_to_rgb.
static float3 rgbToHls(float3 c) {
    c=saturate(c);
    float vmax=max(max(c.x,c.y),c.z),vmin=min(min(c.x,c.y),c.z),diff=vmax-vmin;
    float l=(vmax+vmin)*.5f,h=0,s=0;
    if(diff>1.1920929e-7f) {
        s=l<.5f ? diff/(vmax+vmin) : diff/(2-vmax-vmin);
        float d=60/diff;
        h=vmax==c.x ? (c.y-c.z)*d : vmax==c.y ? (c.z-c.x)*d+120 : (c.x-c.y)*d+240;
        if(h<0) h+=360;
    }
    return float3(h,l,s);
}
static float3 hlsToRgb(float h,float l,float s) {
    if(s==0) return float3(l);
    float p2=l<=.5f ? l*(1+s) : l+s-l*s, p1=2*l-p2;
    h*=0.016666666666666666f;
    if(h<0) h+=6; else if(h>=6) h-=6;
    int sector=int(floor(h)); h-=float(sector); if(sector<0||sector>5){sector=0;h=0;}
    float tab[4]={p2,p1,p1+(p2-p1)*(1-h),p1+(p2-p1)*h};
    const uint order[6][3]={{1,3,0},{1,0,2},{3,0,1},{0,2,1},{0,1,3},{2,1,0}};   // b, g, r indices
    return float3(tab[order[sector][2]],tab[order[sector][1]],tab[order[sector][0]]);
}
static float wrap(float x) { return x-floor(x); }
// processing.mix_hsl
static float3 mixHsl(float3 a,Groups groups) {
    const float centers[8]={0,0.08333333333333333f,0.16666666666666666f,0.3333333333333333f,.5f,0.6666666666666666f,.75f,0.8333333333333334f};
    float3 hls=rgbToHls(a);
    float h=hls.x/360,dh=0,ds=0,dl=0;
    for(uint g=0;g<8;++g) {
        float4 v=groups.g[g];
        if(v.x==0&&v.y==0&&v.z==0) continue;
        float w=max(0.0f,1-fabs(wrap(h-centers[g]+.5f)-.5f)/.125f); w*=w;
        dh+=w*v.x/600; ds+=w*v.y/100; dl+=w*v.z/200;
    }
    return hlsToRgb(wrap(h+dh)*360,saturate(hls.y+dl),saturate(hls.z*(1+ds)));
}
// engine.rgb_to_hsv / hsv_to_rgb and the HSV mixer in engine._develop_color
static float3 mixHsv(float3 a,Groups groups) {
    const float centers[8]={0,0.08333333333333333f,0.16666666666666666f,0.3333333333333333f,.5f,0.6666666666666666f,.75f,0.8333333333333334f};
    a=saturate(a);
    float hi=max(max(a.x,a.y),a.z),lo=min(min(a.x,a.y),a.z),d=hi-lo,safe=max(d,1e-7f);
    float hue=0;
    if(hi==a.z) hue=((a.x-a.y)/safe+4)/6;
    if(hi==a.y) hue=((a.z-a.x)/safe+2)/6;
    if(hi==a.x) hue=((a.y-a.z)/safe+0)/6;
    float h=wrap(hue),sat=hi>0 ? d/max(hi,1e-7f) : 0,val=hi,dh=0,ds=0,dv=0;
    for(uint g=0;g<8;++g) {
        float4 v=groups.g[g];
        if(v.x==0&&v.y==0&&v.z==0) continue;
        float w=max(0.0f,1-fabs(wrap(h-centers[g]+.5f)-.5f)/.125f); w*=w;
        dh+=w*v.x/600; ds+=w*v.y/100; dv+=w*v.z/200;
    }
    h=wrap(h+dh); sat=saturate(sat*(1+ds)); val=saturate(val+dv);
    uint i=uint(max(floor(h*6),0.0f))%6;
    float f=h*6-floor(h*6),p=val*(1-sat),q=val*(1-f*sat),t=val*(1-(1-f)*sat);
    float3 combos[6]={float3(val,t,p),float3(q,val,p),float3(p,val,t),float3(p,q,val),float3(t,p,val),float3(val,p,q)};
    return combos[i];
}
static Groups groupsOf(constant float4* hsl) { Groups g; for(uint i=0;i<8;++i) g.g[i]=hsl[i]; return g; }

kernel void toneMain(uint3 id [[thread_position_in_grid]],constant ToneParams& p [[buffer(0)]],
                     const device float* source [[buffer(1)]],const device float2* curve [[buffer(2)]],
                     device float* result [[buffer(3)]]) {
    if(id.x>=p.width||id.y>=p.height) return;
    uint index=(id.y*p.width+id.x)*3;
    float3 a=float3(source[index],source[index+1],source[index+2]);
    a*=p.gain.w; a*=p.gain.xyz;
    if(p.flags&8) {
        float x=p.width>1 ? -1+2*float(id.x)/float(p.width-1) : -1, y=p.height>1 ? -1+2*float(id.y)/float(p.height-1) : -1;
        a*=max(.1f,1+(x*x+y*y)*p.vignette/100);
    }
    if(p.flags&32) {                          // engine.highlight_rolloff
        for(uint c=0;c<3;++c) {
            float v=a[c];
            if(v>p.shoulderKnee) {
                float u=(v-p.shoulderKnee)/(p.shoulderWhite-p.shoulderKnee);
                v=p.shoulderKnee+(1-p.shoulderKnee)*(p.shoulderSlope*u/(1+(p.shoulderSlope-1)*u));
            }
            a[c]=v;
        }
    }
    a=toSRGB3(a);
    if(p.flags&64) {                          // tone_response.apply: the change follows the base, detail is kept
        uint mapWidth=p.localMaps>>16, mapHeight=p.localMaps&65535, table=p.curveCount, maps=p.curveCount+257;
        float luma=dot(a,float3(.2126f,.7152f,.0722f));
        float sy=clamp((float(id.y)+.5f)*float(mapHeight)/float(p.height)-.5f,0.0f,float(mapHeight-1));
        float sx=clamp((float(id.x)+.5f)*float(mapWidth)/float(p.width)-.5f,0.0f,float(mapWidth-1));
        uint y0=uint(sy), x0=uint(sx), y1=min(y0+1,mapHeight-1), x1=min(x0+1,mapWidth-1);
        float ty=sy-float(y0), tx=sx-float(x0);
        float2 upper=mix(curve[maps+y0*mapWidth+x0],curve[maps+y0*mapWidth+x1],tx);
        float2 lower=mix(curve[maps+y1*mapWidth+x0],curve[maps+y1*mapWidth+x1],tx);
        float2 k=mix(upper,lower,ty);
        float position=saturate(k.x*luma+k.y)*256;
        uint i=min(uint(position),255u);
        float change=mix(curve[table+i].x,curve[table+i+1].x,position-float(i));
        change=clamp(change,-max(luma,0.0f)*.5f,max(1.0f-luma,0.0f)*.5f);     // tone_response.REACH: at most half way to black or white
        float target=max(luma+change,0.0f);
        a=(a*target+p.localFloor*(a+target-luma))/(luma+p.localFloor);
    } else if(p.flags&1) {
        float luma=dot(a,float3(.2126f,.7152f,.0722f));
        float low=saturate(1-luma); low=low*low*low;
        float high=saturate(luma); high=high*high*high;
        a+=p.shadows/180*low*saturate(luma*4)+p.highlights/180*high*saturate((1-luma)*4);
    }
    if(p.black<0) a=max(a+p.black,0.0f)/(1+p.black);
    else if(p.black>0) a=a*(1-p.black)+p.black;
    a*=p.whitesGain;
    if(p.flags&2) a=(a-.5f)*p.contrast+.5f;
    if(p.flags&16) {                          // HDR: hdr.curve_extended, then only negative values are clipped
        if(p.flags&4) {
            float2 last=curve[p.curveCount-1];
            for(uint c=0;c<3;++c) {
                float x=a[c];
                a[c]=x>last.x ? last.y+(x-last.x)*p.curveSlope : interpolate(curve,x,0,p.curveCount);
            }
        }
        a=max(a,0.0f);
    } else {
        if(p.flags&4) a=float3(interpolate(curve,saturate(a.x),0,p.curveCount),interpolate(curve,saturate(a.y),0,p.curveCount),interpolate(curve,saturate(a.z),0,p.curveCount));
        a=saturate(a);
    }
    result[index]=a.x; result[index+1]=a.y; result[index+2]=a.z;
}

// Mirrors processing._local_region for masks without point colour, clarity, detail or HDR.
kernel void localMain(uint3 id [[thread_position_in_grid]],constant LocalParams& p [[buffer(0)]],
                      const device float* source [[buffer(1)]],const device float2* curve [[buffer(2)]],
                      const device float* weights [[buffer(3)]],device float* result [[buffer(4)]]) {
    if(id.x>=p.width||id.y>=p.height) return;
    uint pixel=id.y*p.width+id.x,index=pixel*3;
    float3 region=float3(source[index],source[index+1],source[index+2]);
    float3 a=toLinear3(region)*p.exposureGain;
    a=toSRGB3(a);
    a=(a-.5f)*p.contrast+.5f;
    a+=p.tempTint.xyz;
    float gray=dot(a,float3(.2126f,.7152f,.0722f));
    a=gray+(a-gray)*p.saturation;
    float dark=1-saturate(gray),light=saturate(gray);
    a+=p.shadows*(dark*dark);
    a+=p.highlights*(light*light);
    if(p.flags&1) a=a*p.whiteMinusBlack+p.black;
    if(p.curveCount.x) a=float3(interpolate(curve,a.x,p.curveOffset.x,p.curveCount.x),interpolate(curve,a.y,p.curveOffset.x,p.curveCount.x),interpolate(curve,a.z,p.curveOffset.x,p.curveCount.x));
    if(p.curveCount.y) a.x=interpolate(curve,a.x,p.curveOffset.y,p.curveCount.y);
    if(p.curveCount.z) a.y=interpolate(curve,a.y,p.curveOffset.z,p.curveCount.z);
    if(p.curveCount.w) a.z=interpolate(curve,a.z,p.curveOffset.w,p.curveCount.w);
    if(p.flags&2) {
        float3 hls=rgbToHls(a);
        a=hlsToRgb(wrap(hls.x/360+p.hueShift)*360,saturate(hls.y),saturate(hls.z));
    }
    if(p.flags&4) a=mixHsl(a,groupsOf(p.hsl));
    float weight=weights[pixel];
    a=region*(1-weight)+saturate(a)*weight;
    result[index]=a.x; result[index+1]=a.y; result[index+2]=a.z;
}

// Mirrors engine._develop_color + processing.color_tools without photo filters.
kernel void colorMain(uint3 id [[thread_position_in_grid]],constant ColorParams& p [[buffer(0)]],
                      const device float* source [[buffer(1)]],const device float2* curve [[buffer(2)]],
                      const device Point* points [[buffer(3)]],device float* result [[buffer(4)]]) {
    const float3 lumaWeights=float3(.2126f,.7152f,.0722f);
    const float parametricCenters[4]={.125f,.375f,.625f,.875f};
    const float calibrationCenters[3]={0,0.3333333333333333f,0.6666666666666666f};
    if(id.x>=p.width||id.y>=p.height) return;
    uint index=(id.y*p.width+id.x)*3;
    float3 a=float3(source[index],source[index+1],source[index+2]);
    if(p.flags&1) {
        float gray=dot(a,lumaWeights),factor=p.saturation;
        if(p.flags&2) factor=factor+p.vibrance*(1-(max(max(a.x,a.y),a.z)-min(min(a.x,a.y),a.z)));
        a=gray+(a-gray)*factor;
    }
    if(p.flags&4) a=mixHsl(a,groupsOf(p.hsl));
    else if(p.flags&8) a=mixHsv(a,groupsOf(p.hsl));
    if(p.curveCount.x) a.x=interpolate(curve,a.x,p.curveOffset.x,p.curveCount.x);
    if(p.curveCount.y) a.y=interpolate(curve,a.y,p.curveOffset.y,p.curveCount.y);
    if(p.curveCount.z) a.z=interpolate(curve,a.z,p.curveOffset.z,p.curveCount.z);
    if(p.flags&16) {
        float lum=saturate(dot(a,lumaWeights)),delta=0;
        for(uint i=0;i<4;++i) { float w=max(0.0f,1-fabs(lum-parametricCenters[i])/.375f); delta+=w*w*p.parametric[i]/400; }
        a+=delta;
    }
    if(p.flags&32) {
        float3 hls=rgbToHls(a);
        float h=hls.x/360,dh=0,ds=0;
        for(uint i=0;i<3;++i) {
            if(p.calibration[i].x==0&&p.calibration[i].y==0) continue;
            float w=max(0.0f,1-fabs(wrap(h-calibrationCenters[i]+.5f)-.5f)/.25f);
            dh+=w*p.calibration[i].x/600; ds+=w*p.calibration[i].y/100;
        }
        a=hlsToRgb(wrap(h+dh)*360,saturate(hls.y),saturate(hls.z*(1+ds)));
    }
    if(p.flags&256) {                       // point_color.apply
        for(uint i=0;i<p.pointCount;++i) {
            Point q=points[i];
            float3 hls=rgbToHls(a);
            float h=hls.x/360,l=hls.y,sat=hls.z,w;
            if(q.flags.x) {
                float hue=q.flags.y ? 1 : saturate(1-fabs(wrap(h-q.reference.x+.5f)-.5f)/q.widths.x);
                float saturation=q.flags.z ? 1 : saturate(1-fabs(sat-q.reference.y)/q.widths.y);
                float lightness=q.flags.w ? 1 : saturate(1-fabs(l-q.reference.z)/q.widths.z);
                w=(hue*saturation)*lightness;
            } else {
                w=saturate(1-fabs(wrap(h-q.reference.x+.5f)-.5f)/q.widths.x);
                w*=saturate(1-fabs(sat-q.reference.y)/.6f);
            }
            float3 changed=hlsToRgb(wrap(h+(w*q.amounts.x)/600)*360,saturate(l+(w*q.amounts.z)/200),saturate(sat*(1+(w*q.amounts.y)/100)));
            if(!q.flags.x||w>0) a=changed;
        }
    }
    if(p.flags&64) {
        float l=saturate(dot(a,lumaWeights)+p.gradingBalance);
        a+=(1-l)*(1-l)*p.grading[0].xyz;
        a+=4*l*(1-l)*p.grading[1].xyz;
        a+=l*l*p.grading[2].xyz;
    }
    if(p.flags&128) a=float3(dot(a,p.cameraMatrix[0].xyz),dot(a,p.cameraMatrix[1].xyz),dot(a,p.cameraMatrix[2].xyz));
    a=saturate(a);
    result[index]=a.x; result[index+1]=a.y; result[index+2]=a.z;
}

// Separable Gaussian passes matching cv2.GaussianBlur(BORDER_REFLECT_101) and the per-pixel
// combine steps of engine._develop_detail. Buffers hold interleaved float RGB.
// processing._rgb_to_lab / _lab_to_rgb (exact CIE formulas, D65).
static float labF(float t) { return t>.008856f ? pow(t,0.3333333333333333f) : 7.787f*t+0.13793103448275862f; }
static float labInverse(float t) { return t<=.20689303f ? (t-0.13793103448275862f)/7.787f : t*t*t; }
static float3 rgbToLab(float3 c) {
    c=saturate(c);
    c=toLinear3(c);
    float x=(.412453f*c.x+.357580f*c.y+.180423f*c.z)/.950456f;
    float y=.212671f*c.x+.715160f*c.y+.072169f*c.z;
    float z=(.019334f*c.x+.119193f*c.y+.950227f*c.z)/1.088754f;
    float fx=labF(x),fy=labF(y),fz=labF(z);
    return float3(y>.008856f ? 116*fy-16 : 903.3f*y,500*(fx-fy),200*(fy-fz));
}
static float3 labToRgb(float3 lab) {
    bool low=lab.x<=7.9996248f;
    float fy=low ? 7.787f*(lab.x/903.3f)+0.13793103448275862f : (lab.x+16)/116;
    float y=low ? lab.x/903.3f : fy*fy*fy;
    float x=labInverse(fy+lab.y/500),z=labInverse(fy-lab.z/200);
    float3 c=saturate(float3(3.07993494f*x-1.53715152f*y-.542783419f*z,
                             -.921234183f*x+1.87599f*y+.0452441813f*z,
                             .052889682f*x-.204041338f*y+1.15115166f*z));
    return toSRGB3(c);
}
// Sortable unsigned encoding so atomic min/max order floats correctly.
static uint orderKey(float v) { uint u=as_type<uint>(v); return (u&0x80000000u) ? ~u : (u|0x80000000u); }
static int reflect101(int p,int n) {
    if(n==1) return 0;
    while(p<0||p>=n) p = p<0 ? -p : 2*n-2-p;
    return p;
}
static float3 load(const device float* b,uint width,int x,int y) { uint i=(uint(y)*width+uint(x))*3; return float3(b[i],b[i+1],b[i+2]); }
static float grayAt(const device float* source,uint width,uint height,int x,int y) {
    return dot(load(source,width,reflect101(x,int(width)),reflect101(y,int(height))),float3(.2126f,.7152f,.0722f));
}
static float channelMax(const device float* source,uint width,uint height,int x,int y) {
    float3 v=load(source,width,reflect101(x,int(width)),reflect101(y,int(height))); return max(max(v.x,v.y),v.z);
}
// cv2.medianBlur(ksize=5) of one channel of `source` (BORDER_REPLICATE): 13th smallest of 25.
// The D3D shader selects it with 234 exchanges; this network needs 99 for the same value (checked for
// every 0/1 input, which proves it for all inputs). The guide pass is the slowest one on Apple GPUs.
#define S(i,j) { float lo=min(v[i],v[j]); v[j]=max(v[i],v[j]); v[i]=lo; }
static float median5(const device float* source,uint width,uint height,int x,int y,uint c) {
    float v[25];
    for(int j=0;j<5;++j) for(int i=0;i<5;++i)
        v[j*5+i]=source[(uint(clamp(y+j-2,0,int(height)-1))*width+uint(clamp(x+i-2,0,int(width)-1)))*3+c];
    S(0,1) S(3,4) S(2,4) S(2,3) S(6,7) S(5,7) S(5,6) S(9,10) S(8,10) S(8,9) S(12,13)
    S(11,13) S(11,12) S(15,16) S(14,16) S(14,15) S(18,19) S(17,19) S(17,18) S(21,22) S(20,22) S(20,21)
    S(23,24) S(2,5) S(3,6) S(0,6) S(0,3) S(4,7) S(1,7) S(1,4) S(11,14) S(8,14) S(8,11)
    S(12,15) S(9,15) S(9,12) S(13,16) S(10,16) S(10,13) S(20,23) S(17,23) S(17,20) S(21,24) S(18,24)
    S(18,21) S(19,22) S(8,17) S(9,18) S(0,18) S(0,9) S(10,19) S(1,19) S(1,10) S(11,20) S(2,20)
    S(2,11) S(12,21) S(3,21) S(3,12) S(13,22) S(4,22) S(4,13) S(14,23) S(5,23) S(5,14) S(15,24)
    S(6,24) S(6,15) S(7,16) S(7,19) S(13,21) S(15,23) S(7,13) S(7,15) S(1,9) S(3,11) S(5,17)
    S(11,17) S(9,17) S(4,10) S(6,12) S(7,14) S(4,6) S(4,7) S(12,14) S(10,14) S(6,7) S(10,12)
    S(6,10) S(6,17) S(12,17) S(7,17) S(7,10) S(12,18) S(7,12) S(10,18) S(12,20) S(10,20) S(10,12)
    return v[12];
}
#undef S
// luma_wavelet noise statistics in `extremes` (uints): level l at 8+32*l: [0,1] sum |d| (64-bit, 1/4096
// units), [2] count, then for bins 0-7 and 8 (all) [3+3b,4+3b] clipped sum, [5+3b] count.
static uint word(device atomic_uint* e,uint index) { return atomic_load_explicit(&e[index],memory_order_relaxed); }
static void add64(device atomic_uint* e,uint index,uint value) {
    uint old=atomic_fetch_add_explicit(&e[index],value,memory_order_relaxed);
    if(old>0xffffffffu-value) atomic_fetch_add_explicit(&e[index+1],1u,memory_order_relaxed);
}
static float read64(device atomic_uint* e,uint index) { return float(word(e,index+1))*4294967296.0f+float(word(e,index)); }
static float waveletProfile(device atomic_uint* e,uint level,float guide) {          // np.interp of the per-bin noise over bin centres
    uint base=8+32*min(level,2u); uint total=word(e,base+2); uint minimum=max(256u,total/2000);
    float overall=read64(e,base+3+3*8)/4096/float(max(word(e,base+5+3*8),1u));
    float values[8];
    for(uint b=0;b<8;++b) {
        uint n=word(e,base+5+3*b);
        values[b]=n>minimum ? read64(e,base+3+3*b)/4096/float(n) : overall;
    }
    float t=guide/12.5f-.5f,v;
    if(t<=0) v=values[0]; else if(t>=7) v=values[7];
    else { int i=int(floor(t)); float f=t-float(i); v=values[i]+(values[i+1]-values[i])*f; }
    return level>2 ? v*pow(.5f,float(level-2)) : v;
}

kernel void detailMain(uint3 id [[thread_position_in_grid]],uint slot [[thread_index_in_threadgroup]],
                       constant DetailParams& p [[buffer(0)]],const device float* source [[buffer(1)]],
                       const device float* blurred [[buffer(2)]],const device float* taps [[buffer(3)]],
                       const device float* lut [[buffer(4)]],const device float4* space [[buffer(5)]],
                       const device float* extra0 [[buffer(6)]],const device float* extra1 [[buffer(7)]],
                       const device float* extra2 [[buffer(8)]],device float* result [[buffer(9)]],
                       device atomic_uint* extremes [[buffer(10)]]) {
    threadgroup float3 lowest[256];
    threadgroup float3 highest[256];
    threadgroup float3 tile[16][16];
    threadgroup atomic_uint waveletSums[18];
    const float3 lumaWeights=float3(.2126f,.7152f,.0722f);
    const uint width=p.width,height=p.height,stage=p.stage,flags=p.flags,channel=p.channel;
    if(stage==27||stage==28) {              // luma_wavelet statistics of level `channel` (source: P, blurred: S)
        bool inside=id.x<width&&id.y<height;
        if(slot<18) atomic_store_explicit(&waveletSums[slot],0u,memory_order_relaxed);
        threadgroup_barrier(mem_flags::mem_threadgroup);
        uint base=8+32*channel;
        if(inside) {
            float3 q=load(source,width,int(id.x),int(id.y)),s=load(blurred,width,int(id.x),int(id.y));
            float d=fabs(q.x-s.x);
            if(stage==27) {
                atomic_fetch_add_explicit(&waveletSums[0],uint(d*4096+.5f),memory_order_relaxed);
                atomic_fetch_add_explicit(&waveletSums[1],1u,memory_order_relaxed);
            } else {
                float mean=read64(extremes,base)/4096/float(max(word(extremes,base+2),1u));
                uint c=uint(min(d,2.5f*mean)*4096+.5f);
                float guide=channel==0 ? s.x : q.z;
                uint b=uint(clamp(int(guide*.08f),0,7));
                atomic_fetch_add_explicit(&waveletSums[b],c,memory_order_relaxed);
                atomic_fetch_add_explicit(&waveletSums[9+b],1u,memory_order_relaxed);
                atomic_fetch_add_explicit(&waveletSums[8],c,memory_order_relaxed);
                atomic_fetch_add_explicit(&waveletSums[17],1u,memory_order_relaxed);
            }
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
        if(slot==0) {
            if(stage==27) {
                add64(extremes,base,atomic_load_explicit(&waveletSums[0],memory_order_relaxed));
                atomic_fetch_add_explicit(&extremes[base+2],atomic_load_explicit(&waveletSums[1],memory_order_relaxed),memory_order_relaxed);
            } else for(uint b=0;b<9;++b) {
                add64(extremes,base+3+3*b,atomic_load_explicit(&waveletSums[b],memory_order_relaxed));
                atomic_fetch_add_explicit(&extremes[base+5+3*b],atomic_load_explicit(&waveletSums[9+b],memory_order_relaxed),memory_order_relaxed);
            }
        }
        return;
    }
    if(stage==9) {                         // per-channel min/max of the Lab frame (group reduction, then atomics)
        bool inside=id.x<width&&id.y<height;
        float3 v=inside ? load(source,width,int(id.x),int(id.y)) : float3(0);
        lowest[slot]=inside ? v : float3(3.402823e38f); highest[slot]=inside ? v : float3(-3.402823e38f);
        threadgroup_barrier(mem_flags::mem_threadgroup);
        for(uint step=128;step>0;step>>=1) {
            if(slot<step) { lowest[slot]=min(lowest[slot],lowest[slot+step]); highest[slot]=max(highest[slot],highest[slot+step]); }
            threadgroup_barrier(mem_flags::mem_threadgroup);
        }
        if(slot==0) {
            for(uint c=0;c<3;++c) {
                atomic_fetch_min_explicit(&extremes[c],orderKey(lowest[0][c]),memory_order_relaxed);
                atomic_fetch_max_explicit(&extremes[3+c],orderKey(highest[0][c]),memory_order_relaxed);
            }
        }
        return;
    }
    if(stage==0||stage==1) {                 // horizontal / vertical Gaussian
        // The group loads its input window 16 pixels at a time into threadgroup memory; every pixel is
        // read from the frame ~(16+2r)/16 times instead of 2r+1. Taps are summed in the same order (t=0..2r).
        bool horizontal=stage==0;
        uint tx=slot%16,ty=slot/16;
        int x=int(id.x),y=int(id.y),r=int(p.radius);
        int own=horizontal ? x : y;
        int origin=own-int(horizontal ? tx : ty)-r;          // first input index of the group's window
        float3 sum=float3(0);
        for(int block=0;block<16+2*r;block+=16) {
            int q=origin+block+int(horizontal ? tx : ty);
            int lx=horizontal ? reflect101(q,int(width)) : min(x,int(width)-1);
            int ly=horizontal ? min(y,int(height)-1) : reflect101(q,int(height));
            tile[ty][tx]=load(source,width,lx,ly);
            threadgroup_barrier(mem_flags::mem_threadgroup);
            for(int k=0;k<16;++k) {
                int t=origin+block+k-own+r;
                if(t>=0&&t<=2*r) sum+=(horizontal ? tile[ty][k] : tile[k][tx])*taps[p.kernelOffset+uint(t)];
            }
            threadgroup_barrier(mem_flags::mem_threadgroup);
        }
        if(id.x<width&&id.y<height) { uint i=(id.y*width+id.x)*3; result[i]=sum.x; result[i+1]=sum.y; result[i+2]=sum.z; }
        return;
    }
    if(id.x>=width||id.y>=height) return;
    int x=int(id.x),y=int(id.y);
    uint pixel=id.y*width+id.x,out=pixel*3;
    float3 a=load(source,width,x,y);
    if(stage==2) {                          // end of detail_tools: texture, clip; then monochrome
        if(flags&1) a+=(a-load(blurred,width,x,y))*p.textureAmount;
        if(flags&16) {result[out]=a.x;result[out+1]=a.y;result[out+2]=a.z;return;}  // defringe (stage 14) runs before the clip
        a=saturate(a);
        if(flags&2) a=float3(dot(a,lumaWeights));
    } else if(stage==3) {                   // clarity
        a+=(a-load(blurred,width,x,y))*p.clarityAmount;
    } else if(stage==4) {                   // sharpen
        float3 residual=a-load(blurred,width,x,y);
        if(flags&64) residual=float3(dot(residual,lumaWeights));    // detail_version 2: luminance only
        if(flags&4) residual*=saturate(fabs(residual)/p.threshold);
        if(flags&8) {
            float dx=(grayAt(source,width,height,x+1,y-1)-grayAt(source,width,height,x-1,y-1))+2*(grayAt(source,width,height,x+1,y)-grayAt(source,width,height,x-1,y))+(grayAt(source,width,height,x+1,y+1)-grayAt(source,width,height,x-1,y+1));
            float dy=(grayAt(source,width,height,x-1,y+1)-grayAt(source,width,height,x-1,y-1))+2*(grayAt(source,width,height,x,y+1)-grayAt(source,width,height,x,y-1))+(grayAt(source,width,height,x+1,y+1)-grayAt(source,width,height,x+1,y-1));
            residual*=saturate(sqrt(dx*dx+dy*dy)*p.pixelScale/p.maskDivisor);
        }
        a=a+residual*p.sharpenAmount;
    } else if(stage==6) {                   // RGB -> Lab
        a=rgbToLab(a);
    } else if(stage==7) {                   // cv2.bilateralFilter (float LUT method) on one Lab channel, then blend
        float centre=a[channel],filtered=centre;
        if(p.constantValue==0) {
            float sum=0,wsum=0;
            for(uint k=0;k<p.spaceCount;++k) {
                float4 o=space[p.spaceOffset+k];
                float value=load(source,width,reflect101(x+int(o.y),int(width)),reflect101(y+int(o.x),int(height)))[channel];
                float alpha=fabs(value-centre)*p.scaleIndex;
                int idx=clamp(int(floor(alpha)),0,4096); alpha-=float(idx);
                float w=o.z*(lut[p.lutOffset+uint(idx)]+alpha*(lut[p.lutOffset+uint(idx)+1]-lut[p.lutOffset+uint(idx)]));
                sum+=value*w; wsum+=w;
            }
            filtered=sum/wsum;
        }
        a[channel]=centre*(1-p.strength)+filtered*p.strength;
    } else if(stage==17) {                  // chroma_guided guide: (L/100, median5(a)/100, median5(b)/100)
        a=float3(a.x/100,median5(source,width,height,x,y,1)/100,median5(source,width,height,x,y,2)/100);
    } else if(stage==18) {                  // guide products I*G (source: guide)
        a=a.x*a;
    } else if(stage==19) {                  // guide products (ga*ga, ga*gb, gb*gb)
        a=float3(a.y*a.y,a.y*a.z,a.z*a.z);
    } else if(stage==20) {                  // inverse of the regularised guide covariance, row `channel` (0: i11 i12 i13, 1: i22 i23 i33)
        float3 m=a,b=load(blurred,width,x,y),c=load(extra0,width,x,y);  // means, box(I*G), box(ga*ga,ga*gb,gb*gb)
        float a11=b.x-m.x*m.x+p.scaleIndex,a12=b.y-m.x*m.y,a13=b.z-m.x*m.z;   // scaleIndex/constant: eps of L / chroma
        float a22=c.x-m.y*m.y+p.constantValue,a23=c.y-m.y*m.z,a33=c.z-m.z*m.z+p.constantValue;
        float i11=a22*a33-a23*a23,i12=a13*a23-a12*a33,i13=a12*a23-a13*a22;
        float i22=a11*a33-a13*a13,i23=a13*a12-a11*a23,i33=a11*a22-a12*a12;
        float det=a11*i11+a12*i12+a13*i13;
        a=channel==0 ? float3(i11,i12,i13)/det : float3(i22,i23,i33)/det;
    } else if(stage==21) {                  // products (p, I*p, ga*p) for chroma channel `channel` (blurred: guide)
        float3 g=load(blurred,width,x,y); float q=a[channel];
        a=float3(q,g.x*q,g.y*q);
    } else if(stage==22) {                  // product gb*p
        a=float3(load(blurred,width,x,y).z*a[channel],0,0);
    } else if(stage==23) {                  // linear coefficients A = inverse * cov(G,p) (source: guide means)
        float3 m=a,d=load(blurred,width,x,y),i1=load(extra1,width,x,y),i2=load(extra2,width,x,y);
        float c0=d.y-m.x*d.x,c1=d.z-m.y*d.x,c2=extra0[out]-m.z*d.x;
        a=float3(i1.x*c0+i1.y*c1+i1.z*c2,i1.y*c0+i2.x*c1+i2.y*c2,i1.z*c0+i2.y*c1+i2.z*c2);
    } else if(stage==24) {                  // offset B = mean(p) - A.mean(G) (source: guide means, blurred: A, extra0: means of p products)
        float3 k=load(blurred,width,x,y);
        a=float3(extra0[out]-(k.x*a.x+k.y*a.y+k.z*a.z),0,0);
    } else if(stage==25) {                  // q = mean(A).G + mean(B), blended into the channel (blurred: guide)
        float3 g=load(blurred,width,x,y),k=load(extra0,width,x,y);
        float v=a[channel],q=k.x*g.x+k.y*g.y+k.z*g.z+extra1[out];
        float blended=v+(q-v)*p.strength;
        if(channel==1) a.y=blended; else a.z=blended;
    } else if(stage==26) {                  // luma_wavelet start: P = (L, output 0, guide 0)
        a=float3(a.x,0,0);
    } else if(stage==29) {                  // luma_wavelet level energy input d*d (source: P, blurred: S)
        float d=a.x-load(blurred,width,x,y).x;
        a=float3(d*d,0,0);
    } else if(stage==30) {                  // luma_wavelet level: P' = (smooth, output + d*gain, guide)
        float3 s=load(blurred,width,x,y);
        float d=a.x-s.x,energy=load(extra0,width,x,y).x;
        float guide=channel==0 ? s.x : a.z;
        float sigma=waveletProfile(extremes,channel,guide)*1.25f;
        float gain=energy/(energy+p.scaleIndex*sigma*sigma+1e-12f);  // scaleIndex carries lambda
        a=float3(s.x,a.y+d*gain,guide);
    } else if(stage==31) {                  // luma_wavelet end: L = output + coarsest smooth (blurred: P)
        float3 w=load(blurred,width,x,y);
        a.x=w.y+w.x;
    } else if(stage==8) {                   // Lab -> RGB (clipped)
        a=labToRgb(a);
    } else if(stage==10) {                  // dehaze: per-pixel channel minimum
        a=float3(min(min(a.x,a.y),a.z));
    } else if(stage==11||stage==12) {       // cv2.erode with a square kernel: separable min, outside ignored
        float m=a.x;
        for(int o=-int(p.dehazeRadius);o<=int(p.dehazeRadius);++o) {
            int sx=stage==11 ? x+o : x,sy=stage==12 ? y+o : y;
            if(sx>=0&&sy>=0&&sx<int(width)&&sy<int(height)) m=min(m,source[(uint(sy)*width+uint(sx))*3]);
        }
        a=float3(m);
    } else if(stage==13) {                  // dehaze: a=(a-1)/clip(1-k*dark,.2,1.8)+1
        float transmission=clamp(1-p.dehazeAmount*load(blurred,width,x,y).x,.2f,1.8f);
        a=(a-1)/transmission+1;
    } else if(stage==15) {                  // develop's final clip (resident previews)
        a=saturate(a);
    } else if(stage==16) {                  // engine._add_grain: one float per pixel in `blurred`
        a+=blurred[pixel];
    } else if(stage==14) {                  // defringe, then detail_tools' clip and monochrome
        float purple=(a.x+a.z)/2-a.y;
        float lap=channelMax(source,width,height,x-1,y)+channelMax(source,width,height,x+1,y)+channelMax(source,width,height,x,y-1)+channelMax(source,width,height,x,y+1)-4*channelMax(source,width,height,x,y);
        float edge=fabs(lap)*p.edgeScale;
        float weight=(saturate(purple*6)*saturate(edge*3))*p.defringeAmount/100;
        float mean=((a.x+a.y)+a.z)/3;
        a=a*(1-weight)+mean*weight;
        a=saturate(a);
        if(flags&2) a=float3(dot(a,lumaWeights));
    } else if(stage==5) {                   // vignette
        float u=width>1 ? -1+2*float(x)/float(width-1) : -1, v=height>1 ? -1+2*float(y)/float(height-1) : -1;
        if(p.vignetteRound!=0) { u*=1+p.vignetteRound*(p.vignetteAspectX-1); v*=1+p.vignetteRound*(p.vignetteAspectY-1); }
        float d2=p.vignetteP==2 ? u*u+v*v : pow(pow(fabs(u),p.vignetteP)+pow(fabs(v),p.vignetteP),2/p.vignetteP);
        if(p.vignetteScale!=1) d2*=p.vignetteScale;
        d2=clamp(d2,0.0f,2.0f);
        if(p.vignetteExponent!=1) d2=2*pow(d2/2,p.vignetteExponent);
        float darken=d2*p.vignette;
        if(p.vignetteProtect!=0) { float l=saturate((dot(a,lumaWeights)-.5f)/.5f); darken*=1-p.vignetteProtect*l*l; }
        a*=1-darken;
    }
    result[out]=a.x; result[out+1]=a.y; result[out+2]=a.z;
}

// processing.optical_geometry: inverse radial distortion/perspective map + cv2.remap(INTER_LINEAR,
// BORDER_CONSTANT) as exact float bilinear with zero outside the frame.
static float tap(const device float* source,uint width,uint height,int x,int y,uint c) {
    return x>=0&&y>=0&&x<int(width)&&y<int(height) ? source[(uint(y)*width+uint(x))*3+c] : 0.0f;
}
static float sampleChannel(const device float* source,uint width,uint height,float mx,float my,uint c) {
    float fx0=floor(mx),fy0=floor(my);int x0=int(fx0),y0=int(fy0);
    float fx=mx-fx0,fy=my-fy0;
    return tap(source,width,height,x0,y0,c)*((1-fx)*(1-fy))+tap(source,width,height,x0+1,y0,c)*(fx*(1-fy))+tap(source,width,height,x0,y0+1,c)*((1-fx)*fy)+tap(source,width,height,x0+1,y0+1,c)*(fx*fy);
}
kernel void opticalMain(uint3 id [[thread_position_in_grid]],constant OpticalParams& p [[buffer(0)]],
                        const device float* source [[buffer(1)]],device float* result [[buffer(2)]]) {
    if(id.x>=p.width||id.y>=p.height) return;
    float u=(float(id.x)-p.halfWidth)/p.scale,v=(float(id.y)-p.halfHeight)/p.scale;
    float denominator=max(.2f,(1+p.perspectiveH*u)+p.perspectiveV*v);
    u=((u/denominator+p.shiftX)/p.zoom)/p.aspect;
    v=(v/denominator+p.shiftY)/p.zoom;
    float r2=u*u+v*v;
    float factor=((1+p.distortion1*r2)+(p.distortion2*r2)*r2)+((p.distortion3*r2)*r2)*r2;
    uint index=(id.y*p.width+id.x)*3;
    for(uint c=0;c<3;++c) {
        float mx=((u*factor)*p.channelScale[c])*p.scale+p.halfWidth;
        float my=((v*factor)*p.channelScale[c])*p.scale+p.halfHeight;
        // The CPU map is clipped by cv2.remap's fixed-point range; far outside the frame is zero either way.
        result[index+c]=fabs(mx)<1.0e9f&&fabs(my)<1.0e9f ? sampleChannel(source,p.width,p.height,mx,my,c) : 0.0f;
    }
}
)METAL";

struct Device {
    id<MTLDevice> device; id<MTLCommandQueue> queue;
    id<MTLComputePipelineState> tone, local, color, detail, optical;
    id<MTLCommandBuffer> pending;          // queued passes, not yet run
    // Read/write buffers for multi-pass stages (ping-pong image, blur scratch, blurred copy, kernels).
    size_t workCapacity=0, kernelCapacity=0, lutCapacity=0, spaceCapacity=0;
    // work[4..7] exist only while processing.chroma_guided / luma_wavelet run (guidedCapacity floats each).
    size_t guidedCapacity=0;
    id<MTLBuffer> work[8], kernels, luts, spaces, extremes, points, mask, placeholder;
    size_t pointCapacity=0, maskCapacity=0;
    // One curve buffer per use within a call (tone, colour, local edits), so none is rewritten while queued.
    id<MTLBuffer> curve[3]; size_t curveCapacity[3]={0,0,0};
    // Resident frames: stage results the preview cache keeps on the device between calls.
    static const int slotCount=32;
    id<MTLBuffer> slots[slotCount]; size_t slotCapacity[slotCount]={};
    unsigned long long memory=0;
    wchar_t name[128]={0};
};

static id<MTLBuffer> buffer(Device& g,size_t bytes) {
    id<MTLBuffer> b=[g.device newBufferWithLength:std::max<size_t>(bytes,16) options:MTLResourceStorageModeShared];
    if(!b) throw E_OUTOFMEMORY;
    return b;
}

// Run the queued passes and wait: buffer contents are then current on the CPU side and free to rewrite.
static void flush(Device& g) {
    if(!g.pending) return;
    id<MTLCommandBuffer> commands=g.pending;g.pending=nil;
    [commands commit];[commands waitUntilCompleted];
    check(commands.status==MTLCommandBufferStatusCompleted);
}

static id<MTLCommandBuffer> commands(Device& g) {
    if(!g.pending) { g.pending=[g.queue commandBuffer]; if(!g.pending) throw E_FAIL; }
    return g.pending;
}

static void upload(Device& g,id<MTLBuffer> target,const void* data,size_t bytes) {
    flush(g);
    std::memcpy(target.contents,data,bytes);
}

// One 16x16-group dispatch over width x height. buffers follow the parameter block at index 1.
static void dispatch(Device& g,id<MTLComputePipelineState> pipeline,const void* params,size_t paramBytes,
                     std::initializer_list<id<MTLBuffer>> buffers,UINT width,UINT height) {
    id<MTLComputeCommandEncoder> encoder=[commands(g) computeCommandEncoder];
    if(!encoder) throw E_FAIL;
    [encoder setComputePipelineState:pipeline];
    [encoder setBytes:params length:paramBytes atIndex:0];
    NSUInteger index=1;
    for(id<MTLBuffer> b:buffers) [encoder setBuffer:(b?b:g.placeholder) offset:0 atIndex:index++];
    [encoder dispatchThreadgroups:MTLSizeMake((width+15)/16,(height+15)/16,1) threadsPerThreadgroup:MTLSizeMake(16,16,1)];
    [encoder endEncoding];
}

static void copyFloats(Device& g,id<MTLBuffer> to,id<MTLBuffer> from,size_t floats) {
    id<MTLBlitCommandEncoder> encoder=[commands(g) blitCommandEncoder];
    if(!encoder) throw E_FAIL;
    [encoder copyFromBuffer:from sourceOffset:0 toBuffer:to destinationOffset:0 size:floats*4];
    [encoder endEncoding];
}

static id<MTLBuffer> curveBuffer(Device& g,int which,size_t points,const float* data,size_t used) {
    points=points<2?2:points;
    if(points>g.curveCapacity[which]) { g.curve[which]=buffer(g,points*8);g.curveCapacity[which]=points; }
    if(used&&data) upload(g,g.curve[which],data,used*8);
    return g.curve[which];
}

static void reserveWork(Device& g,size_t floats,size_t kernelFloats) {
    if(floats>g.workCapacity) {
        for(int i=0;i<4;++i) g.work[i]=buffer(g,floats*4);
        g.workCapacity=floats;
    }
    kernelFloats=kernelFloats<1?1:kernelFloats;
    if(kernelFloats>g.kernelCapacity) { g.kernels=buffer(g,kernelFloats*4);g.kernelCapacity=kernelFloats; }
}

static void reserveNoise(Device& g,size_t lutFloats,size_t spaceEntries) {
    if(!g.extremes) g.extremes=buffer(g,128*4);   // 0-5 extremes, 8+ luma_wavelet statistics
    lutFloats=lutFloats<1?1:lutFloats; spaceEntries=spaceEntries<1?1:spaceEntries;
    if(lutFloats>g.lutCapacity) { g.luts=buffer(g,lutFloats*4);g.lutCapacity=lutFloats; }
    if(spaceEntries>g.spaceCapacity) { g.spaces=buffer(g,spaceEntries*16);g.spaceCapacity=spaceEntries; }
}

static void reserveGuided(Device& g,size_t floats) {
    if(floats<=g.guidedCapacity) return;
    for(int i=4;i<8;++i) { g.work[i]=nil;g.work[i]=buffer(g,floats*4); }
    g.guidedCapacity=floats;
}

static void releaseGuided(Device& g,size_t floats) {
    if(floats>(1u<<23)) {                   // above ~2.8 MP: do not keep four frame-sized buffers resident
        flush(g);
        for(int i=4;i<8;++i) g.work[i]=nil;
        g.guidedCapacity=0;
    }
}

// One detail dispatch: source/extras are work indices (or -1), target receives the result (-1: none).
static void detailPassBuffer(Device& g,DetailParams params,UINT pass,int source,id<MTLBuffer> blurred,int target,
                             int extra0=-1,int extra1=-1,int extra2=-1) {
    params.stage=pass;
    auto work=[&](int i)->id<MTLBuffer>{return i>=0?g.work[i]:nil;};
    dispatch(g,g.detail,&params,sizeof(params),{g.work[source],blurred,g.kernels,g.luts,g.spaces,
             work(extra0),work(extra1),work(extra2),work(target),g.extremes},params.width,params.height);
}

static void detailPass(Device& g,DetailParams params,UINT pass,int source,int blurred,int target,
                       int extra0=-1,int extra1=-1,int extra2=-1) {
    detailPassBuffer(g,params,pass,source,blurred>=0?g.work[blurred]:nil,target,extra0,extra1,extra2);
}

static float decodeKey(UINT u) { u=(u&0x80000000u)?(u&0x7fffffffu):~u; float f; std::memcpy(&f,&u,4); return f; }

// processing.chroma_guided on the Lab frame in work[current] (see gpu_compute.cpp for the pass list).
static void chromaGuided(Device& g,const DetailParams& base,const NoiseParams& noise,size_t floats,int& current,int& other) {
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
static void lumaWavelet(Device& g,const DetailParams& base,const NoiseParams& noise,size_t floats,int& current,int& other) {
    reserveGuided(g,floats);
    std::vector<UINT> zeros(128,0);
    upload(g,g.extremes,zeros.data(),zeros.size()*4);
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

// processing.detail_tools noise reduction: RGB->Lab, chroma_guided on a/b, then luma_wavelet or
// cv2.bilateralFilter on L with OpenCV's float LUT construction, blend by strength, Lab->RGB.
static void noiseReduction(Device& g,const DetailParams& base,const NoiseParams& noise,int& current,int& other) {
    DetailParams p=base;
    detailPass(g,p,6,current,-1,other);std::swap(current,other);
    size_t floats=(size_t)base.width*base.height*3;
    if(noise.active[1]||noise.active[2]) chromaGuided(g,p,noise,floats,current,other);
    if(noise.active[0]&&noise.lumaWavelet) {
        reserveNoise(g,1,1);lumaWavelet(g,p,noise,floats,current,other);
        detailPass(g,p,8,current,-1,other);std::swap(current,other);
        releaseGuided(g,floats);
        return;
    }
    releaseGuided(g,floats);
    UINT initial[6]={0xffffffffu,0xffffffffu,0xffffffffu,0,0,0};
    reserveNoise(g,1,1);
    upload(g,g.extremes,initial,sizeof(initial));
    detailPass(g,p,9,current,-1,-1);
    flush(g);
    UINT keys[6];std::memcpy(keys,g.extremes.contents,sizeof(keys));
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
    reserveNoise(g,luts.size(),spaces.size()/4);
    if(!luts.empty()) upload(g,g.luts,luts.data(),luts.size()*4);
    if(!spaces.empty()) upload(g,g.spaces,spaces.data(),spaces.size()*4);
    for(UINT c=0;c<1;++c) {
        if(!noise.active[c]) continue;
        p.channel=c;p.lutOffset=lutOffset[c];p.spaceOffset=spaceOffset[c];p.spaceCount=spaceCount[c];
        p.scaleIndex=scale[c];p.strength=noise.strength[c];p.constant=(float)constant[c];
        detailPass(g,p,7,current,-1,other);std::swap(current,other);
    }
    detailPass(g,p,8,current,-1,other);std::swap(current,other);
}

static bool validSize(UINT width,UINT height) {
    return width&&height&&3ull*width*height<=0x3fffffffull;
}

// radii[3]/offsets[3] describe the texture, clarity and sharpen Gaussian kernels in `kernels`
// (radius 0xffffffff = step disabled).
static bool validDetail(const DetailParams* params,unsigned kernelTotal,const float* kernels,const unsigned* radii,const unsigned* offsets) {
    if(!params||!radii||!offsets||kernelTotal>1u<<20) return false;
    for(int i=0;i<3;++i) if(radii[i]!=0xffffffffu&&(!kernels||offsets[i]+2ull*radii[i]+1>kernelTotal)) return false;
    return true;
}

// Detail passes on the image in work[current]; work 2 = horizontal scratch, 3 = blurred. The kernels are
// already in g->kernels (uploadKernels, before anything is queued).
static void runDetail(Device* g,const DetailParams* params,
                      const unsigned* radii,const unsigned* offsets,int tools,int vignette,const NoiseParams* noise,
                      int& current,int& other) {
    reserveNoise(*g,1,1);                   // every detail pass binds these buffers
    auto blur=[&](int step) {
        DetailParams b=*params;b.radius=radii[step];b.kernelOffset=offsets[step];
        detailPass(*g,b,0,current,-1,2);detailPass(*g,b,1,2,-1,3);
    };
    auto combine=[&](UINT pass,int blurred) { detailPass(*g,*params,pass,current,blurred,other);std::swap(current,other); };
    if(tools&&noise&&(noise->active[0]||noise->active[1]||noise->active[2])) noiseReduction(*g,*params,*noise,current,other);
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

static void uploadKernels(Device* g,const float* kernels,unsigned kernelTotal) {
    if(kernelTotal) upload(*g,g->kernels,kernels,(size_t)kernelTotal*4);
}

static void readWork(Device* g,int index,float* output,size_t floats) {
    flush(*g);
    std::memcpy(output,g->work[index].contents,floats*4);
}

static void writeWork(Device* g,int index,const float* input,size_t floats) {
    upload(*g,g->work[index],input,floats*4);
}

static void uploadPoints(Device* g,const PointParams* points,unsigned count) {
    size_t needed=count<1?1:count;
    if(needed>g->pointCapacity) { g->points=buffer(*g,needed*sizeof(PointParams));g->pointCapacity=needed; }
    if(count) upload(*g,g->points,points,count*sizeof(PointParams));
}

static id<MTLBuffer> slotBuffer(Device* g,int slot,size_t floats) {
    if(floats>g->slotCapacity[slot]) {
        g->slots[slot]=nil;g->slotCapacity[slot]=0;
        g->slots[slot]=buffer(*g,floats*4);g->slotCapacity[slot]=floats;
    }
    return g->slots[slot];
}

static void uploadSlot(Device* g,int slot,const float* data,size_t floats) {
    upload(*g,slotBuffer(g,slot,floats),data,floats*4);
}

static void tonePass(Device* g,const ToneParams* tone,id<MTLBuffer> curve,int source,int target) {
    dispatch(*g,g->tone,tone,sizeof(ToneParams),{g->work[source],curve,g->work[target]},tone->width,tone->height);
}

static void colorPass(Device* g,const ColorParams* color,id<MTLBuffer> curve,int source,int target) {
    dispatch(*g,g->color,color,sizeof(ColorParams),{g->work[source],curve,g->points,g->work[target]},color->width,color->height);
}

static void localPass(Device* g,const LocalParams& params,id<MTLBuffer> curve,id<MTLBuffer> mask,int source,int target) {
    dispatch(*g,g->local,&params,sizeof(LocalParams),{g->work[source],curve,mask,g->work[target]},params.width,params.height);
}

// A failed call leaves nothing queued for the next one.
static int failed(Device* g,int code) { g->pending=nil; return code; }

API int grainy_gpu_abi(){return 16;}

API void* grainy_gpu_create(int warp,int* error,wchar_t* name,unsigned nameLength) {
    (void)warp;                             // Metal has no software device; tests asking for one get the hardware
    @autoreleasepool {
        auto g=new(std::nothrow) Device();
        if(!g){if(error)*error=E_OUTOFMEMORY;return nullptr;}
        try {
            g->device=MTLCreateSystemDefaultDevice();
            check(g->device!=nil);
            g->queue=[g->device newCommandQueue];
            check(g->queue!=nil);
            MTLCompileOptions* options=[MTLCompileOptions new];
            // IEEE arithmetic: no reassociation or reciprocal tricks, precise math functions. macOS 15 names
            // this mathMode = MTLMathModeSafe (0) with mathFloatingPointFunctions = ...Precise (1); they are
            // set by name so the file also builds with an older SDK, where fastMathEnabled is the switch.
            if([options respondsToSelector:NSSelectorFromString(@"setMathMode:")]) {
                [options setValue:@0 forKey:@"mathMode"];[options setValue:@1 forKey:@"mathFloatingPointFunctions"];
            } else {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
                options.fastMathEnabled=NO;
#pragma clang diagnostic pop
            }
            NSError* problem=nil;
            id<MTLLibrary> library=[g->device newLibraryWithSource:@(shaders) options:options error:&problem];
            if(!library) { NSLog(@"Grainy GPU shaders: %@",problem.localizedDescription); throw E_FAIL; }
            auto pipeline=[&](NSString* function)->id<MTLComputePipelineState> {
                id<MTLFunction> entry=[library newFunctionWithName:function];
                check(entry!=nil);
                NSError* issue=nil;
                id<MTLComputePipelineState> state=[g->device newComputePipelineStateWithFunction:entry error:&issue];
                if(!state) { NSLog(@"Grainy GPU pipeline %@: %@",function,issue.localizedDescription); throw E_FAIL; }
                check(state.maxTotalThreadsPerThreadgroup>=256);     // 16x16 groups
                return state;
            };
            g->tone=pipeline(@"toneMain");g->local=pipeline(@"localMain");g->color=pipeline(@"colorMain");
            g->detail=pipeline(@"detailMain");g->optical=pipeline(@"opticalMain");
            g->placeholder=buffer(*g,256);
            g->memory=g->device.recommendedMaxWorkingSetSize;
            NSData* wide=[g->device.name dataUsingEncoding:NSUTF32LittleEndianStringEncoding];
            size_t count=std::min<size_t>(wide.length/4,127);
            std::memcpy(g->name,wide.bytes,count*4);g->name[count]=0;
            if(name&&nameLength) { std::wcsncpy(name,g->name,nameLength-1);name[nameLength-1]=0; }
            if(error)*error=S_OK;
            return g;
        } catch(int code) {delete g;if(error)*error=code;return nullptr;}
        catch(...) {delete g;if(error)*error=E_FAIL;return nullptr;}
    }
}

API void grainy_gpu_destroy(void* handle){ @autoreleasepool { delete static_cast<Device*>(handle); } }

// Tone stage for a contiguous float32 RGB frame. input and output may be the same pointer.
API int grainy_gpu_tone(void* handle,const float* input,float* output,const ToneParams* params,const float* curvePoints) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)) return E_INVALIDARG;
    if(!toneValid(params,curvePoints)) return E_INVALIDARG;
    @autoreleasepool { try {
        size_t floats=3ull*params->width*params->height;
        reserveWork(*g,floats,0);writeWork(g,0,input,floats);
        UINT n=tonePoints(params);
        tonePass(g,params,curveBuffer(*g,0,n,curvePoints,n),0,1);
        readWork(g,1,output,floats);
        return S_OK;
    } catch(int code){return failed(g,code);} catch(...){return failed(g,E_FAIL);} }
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
    @autoreleasepool { try {
        size_t pixels=(size_t)params->width*params->height,floats=3*pixels;
        reserveWork(*g,floats,0);writeWork(g,0,region,floats);
        if(pixels>g->maskCapacity) { g->mask=buffer(*g,pixels*4);g->maskCapacity=pixels; }
        upload(*g,g->mask,mask,pixels*4);
        localPass(g,*params,curveBuffer(*g,0,curveTotal,curvePoints,curveTotal),g->mask,0,1);
        readWork(g,1,output,floats);
        return S_OK;
    } catch(int code){return failed(g,code);} catch(...){return failed(g,E_FAIL);} }
}

API int grainy_gpu_detail(void* handle,const float* input,float* output,const DetailParams* params,
                          const float* kernels,unsigned kernelTotal,const unsigned* radii,const unsigned* offsets,
                          int tools,int vignette,const NoiseParams* noise) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)||!validDetail(params,kernelTotal,kernels,radii,offsets)) return E_INVALIDARG;
    @autoreleasepool { try {
        size_t floats=3ull*params->width*params->height;
        reserveWork(*g,floats,kernelTotal);
        writeWork(g,0,input,floats);uploadKernels(g,kernels,kernelTotal);
        int current=0,other=1;
        runDetail(g,params,radii,offsets,tools,vignette,noise,current,other);
        readWork(g,current,output,floats);
        return S_OK;
    } catch(int code){return failed(g,code);} catch(...){return failed(g,E_FAIL);} }
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
    @autoreleasepool { try {
        uploadPoints(g,points,color->pointCount);
        size_t floats=3ull*tone->width*tone->height;
        reserveWork(*g,floats,kernelTotal);
        writeWork(g,0,input,floats);uploadKernels(g,kernels,kernelTotal);
        int current=0,other=1;
        UINT n=tonePoints(tone);
        id<MTLBuffer> toneCurveBuffer=curveBuffer(*g,0,n,toneCurve,n),colorCurveBuffer=curveBuffer(*g,1,colorCurveTotal,colorCurves,colorCurveTotal);
        tonePass(g,tone,toneCurveBuffer,current,other);std::swap(current,other);
        colorPass(g,color,colorCurveBuffer,current,other);std::swap(current,other);
        runDetail(g,detail,radii,offsets,1,vignette,noise,current,other);
        readWork(g,current,output,floats);
        return S_OK;
    } catch(int code){return failed(g,code);} catch(...){return failed(g,E_FAIL);} }
}

// engine.develop for a preview cache, from a kept stage result to the developed frame (see gpu_compute.cpp).
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
    UINT width=detail->width,height=detail->height;size_t pixels=(size_t)width*height,floats=3*pixels;
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
    @autoreleasepool { try {
        reserveWork(*g,floats,kernelTotal);
        // Everything the CPU provides goes up first, so the passes below queue without waiting in between.
        id<MTLBuffer> frame=slotBuffer(g,inputSlot,floats);
        if(input) writeWork(g,0,input,floats);
        id<MTLBuffer> toneCurveBuffer=nil,colorCurveBuffer=nil,layerCurveBuffer=nil;
        if(firstStage==0) { UINT n=tonePoints(tone);toneCurveBuffer=curveBuffer(*g,0,n,toneCurve,n); }
        if(firstStage<=1) { uploadPoints(g,points,color->pointCount);colorCurveBuffer=curveBuffer(*g,1,colorCurveTotal,colorCurves,colorCurveTotal); }
        if(firstStage<=2) uploadKernels(g,kernels,kernelTotal);
        if(firstStage<=2&&grainSlot>=0&&grain) uploadSlot(g,grainSlot,grain,pixels);
        if(layerCount) {
            layerCurveBuffer=curveBuffer(*g,2,layerCurveTotal<1?1:layerCurveTotal,layerCurves,layerCurveTotal);
            for(unsigned i=0;i<layerCount;++i) {
                if(masks[i]) uploadSlot(g,maskSlots[i],masks[i],pixels);
                else slotBuffer(g,maskSlots[i],pixels);
            }
        }
        if(input) copyFloats(*g,frame,g->work[0],floats);
        else copyFloats(*g,g->work[0],frame,floats);
        int current=0,other=1;
        if(firstStage==0) {
            tonePass(g,tone,toneCurveBuffer,current,other);std::swap(current,other);
            if(keep[0]>=0) copyFloats(*g,slotBuffer(g,keep[0],floats),g->work[current],floats);
        }
        if(firstStage<=1) {
            colorPass(g,color,colorCurveBuffer,current,other);std::swap(current,other);
            if(keep[1]>=0) copyFloats(*g,slotBuffer(g,keep[1],floats),g->work[current],floats);
        }
        if(firstStage<=2) {
            runDetail(g,detail,radii,offsets,1,vignette,noise,current,other);
            if(grainSlot>=0) { detailPassBuffer(*g,*detail,16,current,g->slots[grainSlot],other);std::swap(current,other); }
            if(keep[2]>=0) copyFloats(*g,slotBuffer(g,keep[2],floats),g->work[current],floats);
        }
        if(layerCount) reserveNoise(*g,1,1);
        for(unsigned i=0;i<layerCount;++i) { localPass(g,layers[i],layerCurveBuffer,g->slots[maskSlots[i]],current,other);std::swap(current,other); }
        if(clip) { reserveNoise(*g,1,1);detailPassBuffer(*g,*detail,15,current,nil,other);std::swap(current,other); }
        readWork(g,current,output,floats);
        return S_OK;
    } catch(int code){return failed(g,code);} catch(...){return failed(g,E_FAIL);} }
}

// Frees one resident slot (-1: all of them).
API int grainy_gpu_release(void* handle,int slot) {
    auto g=static_cast<Device*>(handle);
    if(!g||slot<-1||slot>=Device::slotCount) return E_INVALIDARG;
    @autoreleasepool { for(int i=0;i<Device::slotCount;++i) if(slot<0||slot==i) {g->slots[i]=nil;g->slotCapacity[i]=0;} }
    return S_OK;
}

API unsigned long long grainy_gpu_memory(void* handle) {
    auto g=static_cast<Device*>(handle);
    return g?g->memory:0;
}

API int grainy_gpu_optical(void* handle,const float* input,float* output,const OpticalParams* params) {
    auto g=static_cast<Device*>(handle);
    if(!g||!input||!output||!params||!validSize(params->width,params->height)) return E_INVALIDARG;
    @autoreleasepool { try {
        size_t floats=3ull*params->width*params->height;
        reserveWork(*g,floats,0);writeWork(g,0,input,floats);
        dispatch(*g,g->optical,params,sizeof(OpticalParams),{g->work[0],g->work[1]},params->width,params->height);
        readWork(g,1,output,floats);
        return S_OK;
    } catch(int code){return failed(g,code);} catch(...){return failed(g,E_FAIL);} }
}

// PIL's bicubic rotation needs double precision, which Metal shaders do not have: the CPU rotates.
API int grainy_gpu_rotate(void* handle,const float* input,float* output,const RotateParams* params) {
    (void)handle;(void)input;(void)output;(void)params;
    return E_NOTIMPL;
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
    @autoreleasepool { try {
        size_t floats=3ull*params->width*params->height;
        reserveWork(*g,floats,0);writeWork(g,0,input,floats);
        uploadPoints(g,points,params->pointCount);
        colorPass(g,params,curveBuffer(*g,1,curveTotal,curvePoints,curveTotal),0,1);
        readWork(g,1,output,floats);
        return S_OK;
    } catch(int code){return failed(g,code);} catch(...){return failed(g,E_FAIL);} }
}
