"""Deterministic, non-generative editing primitives in floating point."""
from copy import deepcopy
import math
import numpy as np
import cv2

IDENTITY=[[0.,0.],[.25,.25],[.5,.5],[.75,.75],[1.,1.]]
EXTRA_DEFAULTS=dict(
    rgb_curves=[deepcopy(IDENTITY) for _ in range(3)],parametric=[0.,0.,0.,0.],
    mixer_mode='hsv',kelvin=6500.,wb_reference=6500.,kelvin_enabled=False,
    grading=[[0.,0.,0.] for _ in range(3)],grading_balance=0.,
    calibration=[[0.,0.] for _ in range(3)],point_colors=[],texture=0.,dehaze=0.,
    noise_luma=0.,noise_color=0.,sharpen_radius=.8,sharpen_detail=50.,sharpen_mask=0.,
    distortion=0.,distortion_k2=0.,distortion_k3=0.,lens_vignette=0.,ca_red=0.,ca_blue=0.,
    defringe=0.,perspective_v=0.,perspective_h=0.,aspect_scale=0.,shift_x=0.,shift_y=0.,
    transform_scale=100.,guides=None,lens_profile='',camera_matrix=None,
    lensfun=None,lensfun_enabled=False,lensfun_distortion=True,lensfun_tca=True,
    lensfun_vignette=True,lensfun_scale=True,upright=None,upright_crop=True,
    masks=[],retouch=[],working_space='sRGB',raw_mode='legacy',raw_kelvin=6500.,raw_tint=0.,
    raw_neutral=None,dcp_profile=None,dcp_huesat=True,dcp_look=True,dcp_tone=True,dcp_exposure=True)


def blur(a,sigma):
    return cv2.GaussianBlur(np.ascontiguousarray(a), (0,0), max(.05,float(sigma)),borderType=cv2.BORDER_REFLECT_101)


def kelvin_rgb(kelvin):
    # Planckian-locus approximation; relative to the declared reference white.
    t=float(np.clip(kelvin,2000,50000))/100
    red=255 if t<=66 else 329.698727446*(t-60)**-.1332047592
    green=99.4708025861*math.log(t)-161.1195681661 if t<=66 else 288.1221695283*(t-60)**-.0755148492
    blue=255 if t>=66 else (0 if t<=19 else 138.5177312231*math.log(t-10)-305.0447927307)
    return np.maximum(np.clip([red,green,blue],0,255)/255,.02).astype(np.float32)


def white_balance(s):
    gain=np.array([2**(s['temperature']/180+s['tint']/400),2**(-s['tint']/250),2**(-s['temperature']/180+s['tint']/400)],np.float32)
    if s.get('kelvin_enabled'):
        gain*=kelvin_rgb(s['wb_reference'])/kelvin_rgb(s['kelvin'])
        gain/=gain[1]
    return gain


def rgb_to_hsl(a):
    hsv=cv2.cvtColor(np.ascontiguousarray(np.clip(a,0,1),dtype=np.float32),cv2.COLOR_RGB2HLS)
    return hsv[...,0]/360,hsv[...,2],hsv[...,1]


def hsl_to_rgb(h,s,l):
    return cv2.cvtColor(np.stack([(h%1)*360,np.clip(l,0,1),np.clip(s,0,1)],axis=-1).astype(np.float32),cv2.COLOR_HLS2RGB)


def mix_hsl(a,groups):
    h,s,l=rgb_to_hsl(a)
    dh,ds,dl=np.zeros_like(h),np.zeros_like(h),np.zeros_like(h)
    for center,(hh,ss,ll) in zip([0,1/12,1/6,1/3,.5,2/3,.75,5/6],groups):
        # An untouched colour adds exact zeros; skipping it keeps pixels bit-identical.
        if not (hh or ss or ll):continue
        weight=np.maximum(0,1-np.abs((h-center+.5)%1-.5)/.125)**2
        if hh:dh+=weight*hh/600
        if ss:ds+=weight*ss/100
        if ll:dl+=weight*ll/200
    return hsl_to_rgb(h+dh,s*(1+ds),l+dl)


def color_tools(a,s,on_point_input=None):
    for c,points in enumerate(s['rgb_curves']):
        p=np.asarray(points,np.float32)
        if not np.allclose(p[:,0],p[:,1]):
            a[...,c]=np.interp(a[...,c],p[:,0],p[:,1])
    if any(s['parametric']):
        lum=np.clip(a@np.array([.2126,.7152,.0722],np.float32),0,1)
        delta=np.zeros_like(lum)
        for center,value in zip([.125,.375,.625,.875],s['parametric']):
            delta+=np.maximum(0,1-np.abs(lum-center)/.375)**2*value/400
        a+=delta[...,None]
    if any(any(group) for group in s['calibration']):
        h,sat,light=rgb_to_hsl(a)
        dh=np.zeros_like(h);ds=np.zeros_like(h)
        for center,(hue,amount) in zip([0,1/3,2/3],s['calibration']):
            if not (hue or amount):continue
            w=np.maximum(0,1-np.abs((h-center+.5)%1-.5)/.25)
            if hue:dh+=w*hue/600
            if amount:ds+=w*amount/100
        a=hsl_to_rgb(h+dh,sat*(1+ds),light)
    from .point_color import apply as apply_points
    a=apply_points(a,s['point_colors'],on_point_input)
    if any(any(group) for group in s['grading']):
        l=np.clip(a@np.array([.2126,.7152,.0722],np.float32)+s['grading_balance']/300,0,1)
        weights=[(1-l)**2,4*l*(1-l),l*l]
        for w,(hue,sat,light) in zip(weights,s['grading']):
            tint=hsl_to_rgb(np.full((1,1),hue/360),np.ones((1,1)),np.full((1,1),.5))[0,0]
            a+=w[...,None]*((tint-.5)*sat/200+light/400)
    if s.get('camera_matrix') is not None:
        matrix=np.asarray(s['camera_matrix'],np.float32)
        if matrix.shape==(3,3) and np.isfinite(matrix).all():
            a=a@matrix.T
    return np.clip(a,0,1)


# sRGB (D65) <-> CIE Lab, the constants OpenCV documents for COLOR_RGB2Lab. OpenCV 5's float
# conversion is a fixed-point approximation (a/b in 1/64 steps, ~0.005 RGB roundtrip error), so the
# exact formulas are used here and in native/gpu_compute.cpp.
_LAB_THRESHOLD=np.float32(.008856)


def _rgb_to_lab(rgb):
    c=np.clip(rgb,0,1).astype(np.float32)
    c=np.where(c<=.04045,c/np.float32(12.92),((c+np.float32(.055))/np.float32(1.055))**np.float32(2.4)).astype(np.float32)
    r,g,b=c[...,0],c[...,1],c[...,2]
    x=(np.float32(.412453)*r+np.float32(.357580)*g+np.float32(.180423)*b)/np.float32(.950456)
    y=np.float32(.212671)*r+np.float32(.715160)*g+np.float32(.072169)*b
    z=(np.float32(.019334)*r+np.float32(.119193)*g+np.float32(.950227)*b)/np.float32(1.088754)
    f=lambda t:np.where(t>_LAB_THRESHOLD,np.cbrt(t),np.float32(7.787)*t+np.float32(16/116))
    fx,fy,fz=f(x),f(y),f(z)
    lightness=np.where(y>_LAB_THRESHOLD,np.float32(116)*fy-np.float32(16),np.float32(903.3)*y)
    return np.stack([lightness,np.float32(500)*(fx-fy),np.float32(200)*(fy-fz)],axis=-1).astype(np.float32)


def _lab_to_rgb(lab):
    lightness,a,b=lab[...,0],lab[...,1],lab[...,2]
    low=lightness<=np.float32(7.9996248)
    fy=np.where(low,np.float32(7.787)*(lightness/np.float32(903.3))+np.float32(16/116),(lightness+np.float32(16))/np.float32(116))
    y=np.where(low,lightness/np.float32(903.3),fy*fy*fy)
    inverse=lambda t:np.where(t<=np.float32(.20689303),(t-np.float32(16/116))/np.float32(7.787),t*t*t)
    x=inverse(fy+a/np.float32(500));z=inverse(fy-b/np.float32(200))
    # Exact inverse of the forward matrix including the white point, so neutral edits round-trip.
    rgb=np.stack([np.float32(3.07993494)*x-np.float32(1.53715152)*y-np.float32(.542783419)*z,
                  np.float32(-.921234183)*x+np.float32(1.87599)*y+np.float32(.0452441813)*z,
                  np.float32(.052889682)*x-np.float32(.204041338)*y+np.float32(1.15115166)*z],axis=-1)
    rgb=np.clip(rgb,0,1)
    return np.where(rgb<=np.float32(.0031308),rgb*np.float32(12.92),np.float32(1.055)*rgb**np.float32(1/2.4)-np.float32(.055)).astype(np.float32)


def rgb_to_lab(rgb):
    from .pixel_jobs import transform
    return transform(np.asarray(rgb,np.float32),_rgb_to_lab)


def lab_to_rgb(lab):
    from .pixel_jobs import transform
    return transform(np.asarray(lab,np.float32),_lab_to_rgb)


def _box(a,r):
    return cv2.boxFilter(np.ascontiguousarray(a),-1,(2*r+1,2*r+1),borderType=cv2.BORDER_REFLECT_101)


def chroma_guided_params(amount,pixel_scale=1.):
    """Box radius, eps of the L and chroma guide channels, and blend of chroma_guided (shared with the GPU)."""
    s=min(2.,max(0.,amount/50));eps=np.float32(.0005+.02*s)
    return max(1,int(round((2+14*s)*pixel_scale))),eps,np.float32(8)*eps,np.float32(min(1.,2*s))


def chroma_guided(lab,amount,pixel_scale=1.):
    """Colour noise reduction, in place: a colour-guided filter (He et al.) of a* and b* whose guide is
    L* plus a 5x5 median of a* and b*.

    High-ISO colour noise forms blotches several pixels wide; averaging chroma over a wide window
    that follows the guide's edges removes them without bleeding colour across object edges. The
    median chroma in the guide keeps edges between colours of similar brightness (a red strap on dark
    cloth, a white eye on a red face), which an L*-only guide smeared by several pixels; its larger eps
    keeps the remaining chroma noise from steering the filter.
    Measured on ISO 3200/6400/32000 CC0 raws and a synthetic noisy photo with a clean reference; see
    CHANGELOG 0.5.59. native/gpu_compute.cpp (chromaGuided) matches it.
    """
    r,eps,chroma_eps,blend=chroma_guided_params(amount,pixel_scale)
    hundred=np.float32(100)
    guide=np.dstack([lab[...,0]/hundred]+[cv2.medianBlur(np.ascontiguousarray(lab[...,c]),5)/hundred for c in (1,2)])
    i,ga,gb=guide[...,0],guide[...,1],guide[...,2]
    means=_box(guide,r);m0,m1,m2=means[...,0],means[...,1],means[...,2]
    products=_box(guide*i[...,None],r);squares=_box(np.dstack([ga*ga,ga*gb,gb*gb]),r)
    a11=products[...,0]-m0*m0+eps;a12=products[...,1]-m0*m1;a13=products[...,2]-m0*m2
    a22=squares[...,0]-m1*m1+chroma_eps;a23=squares[...,1]-m1*m2;a33=squares[...,2]-m2*m2+chroma_eps
    i11=a22*a33-a23*a23;i12=a13*a23-a12*a33;i13=a12*a23-a13*a22
    i22=a11*a33-a13*a13;i23=a13*a12-a11*a23;i33=a11*a22-a12*a12
    det=a11*i11+a12*i12+a13*i13
    i11/=det;i12/=det;i13/=det;i22/=det;i23/=det;i33/=det
    for c in (1,2):
        p=lab[...,c].copy()
        d=_box(np.dstack([p,i*p,ga*p]),r);e=_box(gb*p,r)
        c0=d[...,1]-m0*d[...,0];c1=d[...,2]-m1*d[...,0];c2=e-m2*d[...,0]
        k=np.dstack([i11*c0+i12*c1+i13*c2,i12*c0+i22*c1+i23*c2,i13*c0+i23*c1+i33*c2])
        b=d[...,0]-(k[...,0]*m0+k[...,1]*m1+k[...,2]*m2)
        k=_box(k,r);b=_box(b,r)
        q=k[...,0]*i+k[...,1]*ga+k[...,2]*gb+b
        lab[...,c]=p+(q-p)*blend
    return lab


WAVELET_LEVELS=5
WAVELET_ESTIMATED=3     # finest levels whose noise is measured; coarser ones halve per level
WAVELET_BINS=8          # brightness bins of the noise profile (L* 0-100)
_B3=np.array([1,4,6,4,1],np.float32)/16


def wavelet_kernel(level):
    """B3-spline taps of an a trous level (holes between taps), shared with the GPU."""
    k=np.zeros(4*2**level+1,np.float32);k[::2**level]=_B3
    return k


def _atrous_smooth(x,level):
    k=wavelet_kernel(level)
    return cv2.sepFilter2D(x,-1,k,k,borderType=cv2.BORDER_REFLECT_101)


def _noise_profile(detail,guide):
    """Noise scale of one wavelet level per brightness bin: mean |d| clipped at 2.5x its global mean
    (so edges barely count), for bins of the guide's L*. Sparse bins use the overall value."""
    a=np.abs(detail);clipped=np.minimum(a,np.float32(2.5*float(a.mean(dtype=np.float64))))
    index=np.clip((guide*np.float32(WAVELET_BINS/100)).astype(np.int32),0,WAVELET_BINS-1).ravel()
    sums=np.bincount(index,clipped.ravel().astype(np.float64),WAVELET_BINS);counts=np.bincount(index,minlength=WAVELET_BINS)
    overall=float(clipped.mean(dtype=np.float64))
    return np.where(counts>max(256,index.size//2000),sums/np.maximum(counts,1),overall).astype(np.float32)


def wavelet_lambda(amount,pixel_scale=1.):
    """Shrinkage strength. Reduced previews already average noise away, and the noise measured on them
    includes texture; lambda*pixel_scale kept their effect closest to the downscaled full render on
    real raws (1/4-size ISO 3200 preview: difference 0.017 -> 0.007)."""
    return np.float32((amount/100*3)**2*pixel_scale)


def wavelet_profiles(L):
    """The measured noise profiles of luma_wavelet's estimated levels for the frame L."""
    x=np.ascontiguousarray(L,np.float32);guide=None;profiles=[]
    for level in range(WAVELET_ESTIMATED):
        smooth=_atrous_smooth(x,level)
        if guide is None:guide=smooth
        profiles.append(_noise_profile(x-smooth,guide));x=smooth
    return np.stack(profiles)


def luma_wavelet(L,amount,pixel_scale=1.,profiles=None):
    """Luminance noise reduction (detail_version 3): an a trous B3-spline wavelet whose levels are
    scaled by E/(E+lambda*sigma^2), E the local (5x5) energy of the level and sigma its noise measured on
    this photo per brightness. Noise is lowered evenly like fine grain instead of leaving the smeared
    blotches of a bilateral filter; texture with more energy than the noise is kept. On ISO
    3200/6400/32000 CC0 raws the blotchy (1.5-6 px) noise fell by a third to a half; see CHANGELOG
    0.5.61. native/gpu_compute.cpp (lumaWavelet) matches it."""
    lam=wavelet_lambda(amount,pixel_scale);x=np.ascontiguousarray(L,np.float32);out=np.zeros_like(x);guide=None;profile=None
    centres=(np.arange(WAVELET_BINS,dtype=np.float32)+.5)*np.float32(100/WAVELET_BINS)
    for level in range(WAVELET_LEVELS):
        smooth=_atrous_smooth(x,level);d=x-smooth
        if guide is None:guide=smooth
        if level>=WAVELET_ESTIMATED:profile=profile*np.float32(.5)
        else:profile=profiles[level] if profiles is not None else _noise_profile(d,guide)
        sigma=np.interp(guide,centres,profile).astype(np.float32)*np.float32(1.25)
        energy=cv2.boxFilter(d*d,-1,(5,5),borderType=cv2.BORDER_REFLECT_101)
        out+=d*(energy/(energy+lam*sigma*sigma+np.float32(1e-12)));x=smooth
    return out+x


def luma_noise_reduction(L,amount,pixel_scale=1.):
    """Edge-preserving bilateral filtering of L*, blended by the slider strength (detail_version 1-2)."""
    strength=amount/100;diameter=2*round(3*pixel_scale)+1
    filtered=cv2.bilateralFilter(np.ascontiguousarray(L),diameter,2+strength*16,(1+strength*3)*pixel_scale)
    return L*(1-strength)+filtered*strength


def detail_tools(a,s,pixel_scale=1.):
    # Radius is measured in original pixels, including reduced/offline previews.
    diameter=2*round(3*pixel_scale)+1
    if diameter>1 and (s['noise_luma'] or s['noise_color']):
        lab=rgb_to_lab(a)
        # Colour first: its guide is the original luminance.
        if s['noise_color']:
            chroma_guided(lab,s['noise_color'],pixel_scale)
        if s['noise_luma']:
            if s.get('detail_version',1)>=3:lab[...,0]=luma_wavelet(lab[...,0],s['noise_luma'],pixel_scale,s.get('wavelet_profiles'))
            else:lab[...,0]=luma_noise_reduction(lab[...,0],s['noise_luma'],pixel_scale)
        a=lab_to_rgb(lab)
    if s['dehaze']:
        strength=s['dehaze']/100
        dark=cv2.erode(a.min(axis=-1),np.ones((diameter,diameter),np.uint8))
        transmission=np.clip(1-strength*.8*dark,.2,1.8)
        a=(a-1)/transmission[...,None]+1
    if s['texture']:
        a+=(a-blur(a,1.3*pixel_scale))*s['texture']/100
    if s['defringe']:
        maximum=a.max(axis=-1);minimum=a.min(axis=-1)
        purple=(a[...,0]+a[...,2])/2-a[...,1]
        edge=np.abs(cv2.Laplacian(maximum,cv2.CV_32F))*pixel_scale**2
        weight=np.clip(purple*6,0,1)*np.clip(edge*3,0,1)*s['defringe']/100
        a=a*(1-weight[...,None])+a.mean(axis=-1)[...,None]*weight[...,None]
    return np.clip(a,0,1).astype(np.float32)


def sharpen(a,amount,radius,detail,masking,pixel_scale=1.,luminance=False):
    """Unsharp mask. luminance=True (detail_version 2) adds the same luminance detail to all three
    channels, so leftover colour noise is not amplified."""
    weights=np.array([.2126,.7152,.0722],np.float32)
    residual=a-blur(a,radius*pixel_scale)
    if luminance:residual=(residual@weights)[...,None]
    if detail<100:
        threshold=(1-detail/100)*.012
        residual*=np.clip(np.abs(residual)/max(threshold,1e-6),0,1)
    if masking:
        gray=a@weights
        edge=np.hypot(cv2.Sobel(gray,cv2.CV_32F,1,0),cv2.Sobel(gray,cv2.CV_32F,0,1))*pixel_scale
        residual*=np.clip(edge/(.002+masking/200),0,1)[...,None]
    return a+residual*amount/65


def optical_geometry(a,s):
    """Inverse radial distortion / perspective map, before crop and rotation."""
    if s.get('lensfun_enabled'):
        from .optics import lens_geometry
        a=lens_geometry(a,s)
    keys=('distortion','distortion_k2','distortion_k3','perspective_h','perspective_v','aspect_scale','shift_x','shift_y','ca_red','ca_blue')
    if not any(s.get(k,0) for k in keys) and s.get('transform_scale',100)==100 and not s.get('guides'):
        return a
    h,w=a.shape[:2]
    if s.get('guides'):
        corners=np.asarray(s['guides'],np.float32)*[w-1,h-1]
        if corners.shape==(4,2):
            matrix=cv2.getPerspectiveTransform(corners.astype(np.float32),np.array([[0,0],[w-1,0],[w-1,h-1],[0,h-1]],np.float32))
            a=cv2.warpPerspective(a,matrix,(w,h),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
            if a.ndim==2:a=a[...,None]
    if a.ndim==3 and a.shape[-1] in (1,3):
        from . import native_gpu
        warped=native_gpu.optical_any(a,s)
        if warped is not None:return warped
    # Row/column vectors broadcast to the same per-pixel float32 operations as a full grid.
    x=np.arange(w,dtype=np.float32)[None,:];y=np.arange(h,dtype=np.float32)[:,None]
    scale=max(w,h)/2
    u=(x-(w-1)/2)/scale;v=(y-(h-1)/2)/scale
    denominator=np.maximum(.2,1+s.get('perspective_h',0)/160*u+s.get('perspective_v',0)/160*v)
    zoom=max(.2,s.get('transform_scale',100)/100)
    u=(u/denominator+s.get('shift_x',0)/100)/zoom/(2**(s.get('aspect_scale',0)/100))
    v=(v/denominator+s.get('shift_y',0)/100)/zoom
    r2=u*u+v*v
    factor=1+s.get('distortion',0)/100*r2+s.get('distortion_k2',0)/100*r2*r2+s.get('distortion_k3',0)/100*r2*r2*r2
    offsets=[(s.get('ca_red',0) if c==0 else s.get('ca_blue',0) if c==2 else 0)/10000 if a.shape[-1]==3 else 0 for c in range(a.shape[-1])]
    maps={}
    def remap(c,ca):
        if ca not in maps:
            maps[ca]=((u*factor*(1+ca)*scale+(w-1)/2).astype(np.float32),(v*factor*(1+ca)*scale+(h-1)/2).astype(np.float32))
        mx,my=maps[ca]
        return cv2.remap(a if c is None else a[...,c],mx,my,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
    if len(set(offsets))==1 and a.shape[-1] in (1,3,4):
        # Without lateral CA every channel shares one map; OpenCV samples each channel identically.
        result=remap(None,offsets[0])
        return result[...,None] if result.ndim==2 else result
    return np.stack([remap(c,ca) for c,ca in enumerate(offsets)],axis=-1)


def brush_mask(shape,points,radius,feather=70,with_bounds=False):
    """with_bounds=True also returns (y0,y1,x0,x1) containing every nonzero pixel, or None."""
    h,w=shape[:2];mask=np.zeros((h,w),np.float32)
    radius=max(1,float(radius)*min(h,w))
    samples=[];bounds=None
    for i,p in enumerate(points):
        p=np.array(p,np.float32)*[w-1,h-1]
        if i:
            count=max(1,int(np.linalg.norm(p-previous)/max(1,radius*.25)))
            samples.extend(np.linspace(previous,p,count+1))
        else:samples.append(p)
        previous=p
    for px,py in samples:
        x0,x1=max(0,int(px-radius-1)),min(w,int(px+radius+2))
        y0,y1=max(0,int(py-radius-1)),min(h,int(py+radius+2))
        if x1<=x0 or y1<=y0:continue
        y,x=np.mgrid[y0:y1,x0:x1]
        d=np.hypot(x-px,y-py)/radius
        weight=np.clip((1-d)/max(.01,float(feather)/100),0,1)
        mask[y0:y1,x0:x1]=np.maximum(mask[y0:y1,x0:x1],weight)
        bounds=(y0,y1,x0,x1) if bounds is None else (min(bounds[0],y0),max(bounds[1],y1),min(bounds[2],x0),max(bounds[3],x1))
    return (mask,bounds) if with_bounds else mask


def component_mask(shape,component):
    h,w=shape[:2];kind=component.get('type','brush')
    p=component.get('points',[[.5,.5]])
    if kind=='brush':return brush_mask(shape,p,component.get('radius',.04),component.get('feather',70))
    # Row/column vectors broadcast to the same per-pixel operations as a full coordinate grid.
    x=np.arange(w,dtype=np.float32)[None,:];y=np.arange(h,dtype=np.float32)[:,None];x/=max(1,w-1);y/=max(1,h-1)
    start=np.asarray(p[0]);end=np.asarray(p[-1])
    if kind=='linear':
        delta=end-start
        return np.clip(((x-start[0])*delta[0]+(y-start[1])*delta[1])/max(1e-6,float(delta@delta)),0,1)
    if kind=='radial':
        center=(start+end)/2;radius=np.maximum(np.abs(end-start)/2,.002)
        d=np.hypot((x-center[0])/radius[0],(y-center[1])/radius[1])
        return np.clip((1-d)/max(.01,component.get('feather',70)/100),0,1)
    return np.ones((h,w),np.float32)


def _layer_component(image,source_shape,component,transform,source_image):
    kind=component.get('type')
    if kind=='luma':
        l=image@np.array([.2126,.7152,.0722],np.float32)
        low,high=component.get('range',[.25,.75]);soft=max(.001,component.get('softness',.1))
        return np.clip((l-low+soft)/soft,0,1)*np.clip((high+soft-l)/soft,0,1)
    if kind=='color':
        if 'samples' in component:
            from .range_mask import color_coverage
            return color_coverage(image,component['samples'],component.get('tolerance',.3))
        reference=np.asarray(component.get('rgb',[.5,.5,.5]),np.float32)
        return np.clip(1-np.linalg.norm(image-reference,axis=-1)/max(.01,component.get('tolerance',.3)),0,1)
    source_mask=component_mask(source_shape,component)
    if kind=='brush' and component.get('auto_mask'):
        source_mask=limit_brush_color(source_image if source_image is not None else image,component,source_mask)
    # float32 like the image: OpenCV's float64 remap uses coarser fixed-point weights (up to 2.4e-4 off).
    return transform(np.asarray(source_mask,np.float32)[...,None])[...,0]


# Range components read the developed image; every other kind depends only on geometry and the source.
IMAGE_COMPONENTS=('luma','color')


def image_dependent(layer):
    return any(c.get('enabled',True) and c.get('type') in IMAGE_COMPONENTS for c in layer.get('components',[]))


def resident_layers(frame,source_shape,layers,transform,cache,geometry_key,source_image,version,with_disabled=False):
    """apply_local's shape masks for the GPU preview chain, or None when a layer needs the CPU path.

    Returns ([(mask, device key, edit)] for enabled layers, [(index, mask)] for on_mask). Masks come from the
    same cache entries as apply_local, so either path reuses the other's masks.
    """
    from .render_cache import settings_key
    from . import native_gpu
    edits=[];masks=[]
    for index,layer in enumerate(layers):
        enabled=layer.get('enabled',True)
        if not enabled and not with_disabled:continue
        if image_dependent(layer):return None
        edit=layer.get('adjustments',{})
        if enabled and native_gpu.local_params(edit) is None:return None
        layer_key=settings_key({k:v for k,v in layer.items() if k not in ('adjustments','name','enabled')})
        key=(('geometry',geometry_key),layer_key)
        mask=cache.evaluate(f'mask:{index}',key,lambda:layer_mask(frame,source_shape,layer,transform,source_image,
            cache=cache,layer_index=index,geometry_key=geometry_key))
        masks.append((index,mask))
        if enabled:edits.append((mask,(version,key),edit))
    return edits,masks


def layer_mask(image,source_shape,layer,transform,source_image=None,*,cache=None,cache_key=None,layer_index=0,geometry_key=None):
    """geometry_key, when given, caches shape components independently of tone/colour/detail edits."""
    from .render_cache import settings_key
    result=np.zeros(image.shape[:2],np.float32)
    for index,component in enumerate(layer.get('components',[])):
        if not component.get('enabled',True):continue
        kind=component.get('type')
        calculate=lambda:_layer_component(image,source_shape,component,transform,source_image)
        if cache:
            base=cache_key if geometry_key is None or kind in IMAGE_COMPONENTS else ('geometry',geometry_key)
            component_key=(base,settings_key({k:v for k,v in component.items() if k not in ('flow','density','operation','invert','enabled')}))
            mask=cache.evaluate(f'component:{layer_index}:{index}',component_key,calculate)
        else:mask=calculate()
        if component.get('invert'):mask=1-mask
        operation=component.get('operation','add')
        if kind=='brush' and component.get('paint_version')==2:
            # Each completed stroke deposits flow once, independently of pointer
            # event frequency. Repeated strokes build toward the density ceiling.
            flow=float(np.clip(component.get('flow',100)/100,0,1))
            density=float(np.clip(component.get('density',100)/100,0,1))
            coverage=mask*flow
            if operation=='subtract':result*=1-coverage*density
            elif operation=='intersect':result*=coverage*density
            else:result+=np.maximum(density-result,0)*coverage
        elif index==0:result=mask.copy()
        elif operation=='subtract':result*=1-mask
        elif operation=='intersect':result*=mask
        else:result=np.maximum(result,mask)
    if layer.get('invert'):result=1-result
    return np.clip(result*layer.get('opacity',1),0,1)


def limit_brush_color(image,component,mask):
    """Gate in source coordinates before geometry and global/local adjustments.

    Source sampling keeps a cropped-out seed available and prevents feedback
    from the developing stroke's own changes in brightness or hue.
    """
    if image.shape[:2]!=mask.shape:raise ValueError('Brush colour source dimensions do not match')
    seed=component.get('seed',component.get('points',[[.5,.5]])[0])
    h,w=image.shape[:2];x=int(np.clip(round(seed[0]*(w-1)),0,w-1));y=int(np.clip(round(seed[1]*(h-1)),0,h-1))
    radius=max(1,round(min(h,w)*.0025))
    reference=image[max(0,y-radius):min(h,y+radius+1),max(0,x-radius):min(w,x+radius+1)].mean(axis=(0,1))
    rows=np.flatnonzero((mask>0).any(axis=1));cols=np.flatnonzero((mask>0).any(axis=0))
    if not len(rows):return mask
    y0,y1=rows[0],rows[-1]+1;x0,x1=cols[0],cols[-1]+1
    difference=np.linalg.norm(image[y0:y1,x0:x1]-reference,axis=-1)
    tolerance=max(.001,float(component.get('auto_tolerance',.18)))
    result=mask.copy();result[y0:y1,x0:x1]*=np.clip(1-difference/tolerance,0,1)
    return result


def apply_local(image,source_shape,layers,transform,*,cache=None,cache_key=None,on_mask=None,source_image=None,pixel_scale=1.,on_point=None,on_layer_input=None,geometry_key=None,detail_version=1):
    """detail_version is the photo's: local sharpening and luminance noise reduction use the same methods."""
    from .render_cache import settings_key
    if cache:cache_key=(cache_key,pixel_scale)
    for index,layer in enumerate(layers):
        if on_layer_input is not None:on_layer_input(index,image)
        if not layer.get('enabled',True) and on_mask is None:continue
        if cache:
            # mask_key chains the image state for the local edit caches below; a shape-only
            # mask is itself cached by geometry so global slider changes do not rebuild it.
            layer_key=settings_key({k:v for k,v in layer.items() if k not in ('adjustments','name','enabled')})
            mask_key=(cache_key,layer_key)
            shape_only=geometry_key is not None and not image_dependent(layer)
            mask=cache.evaluate(f'mask:{index}',(('geometry',geometry_key),layer_key) if shape_only else mask_key,
                lambda:layer_mask(image,source_shape,layer,transform,source_image,
                cache=cache,cache_key=cache_key,layer_index=index,geometry_key=geometry_key))
        else:
            mask=layer_mask(image,source_shape,layer,transform,source_image)
        if on_mask is not None:on_mask(index,mask)
        if not layer.get('enabled',True):continue
        edit=layer.get('adjustments',{})
        if detail_version>1 and (edit.get('sharpen') or edit.get('noise_luma')):edit={**edit,'detail_version':detail_version}
        # Changing a preceding layer must invalidate image-dependent range masks.
        if cache:
            cache_key=(mask_key,settings_key(edit))
            image=cache.evaluate(f'local:{index}',cache_key,
                lambda:_local_region(image,mask,edit,cache,mask_key,index,pixel_scale))
        else:image=_local_region(image,mask,edit,index=index,pixel_scale=pixel_scale,on_point=on_point)
    return image


def _local_region(image,mask,edit,cache=None,mask_key=None,index=0,pixel_scale=1.,on_point=None):
    """Work only where the mask contributes, including the clarity filter halo."""
    from .engine import to_linear,to_srgb
    occupied=mask>0
    rows=np.flatnonzero(occupied.any(axis=1));cols=np.flatnonzero(occupied.any(axis=0))
    if not len(rows):return image.copy()
    detail=any(edit.get(k) for k in ('texture','dehaze','noise_luma','noise_color','sharpen'))
    pad=max(4,math.ceil(32*pixel_scale)) if detail else math.ceil(10*pixel_scale) if edit.get('clarity') else 0
    if edit.get('noise_color'):
        # chroma_guided reaches two box radii plus the median's 2 pixels.
        pad=max(pad,2*chroma_guided_params(edit['noise_color'],pixel_scale)[0]+3)
    wavelet=bool(edit.get('noise_luma')) and edit.get('detail_version',1)>=3
    if wavelet:pad=max(pad,72)     # luma_wavelet: five levels reach 62 pixels, the energy box and guide 2 more
    y0,y1=max(0,rows[0]-pad),min(mask.shape[0],rows[-1]+pad+1)
    x0,x1=max(0,cols[0]-pad),min(mask.shape[1],cols[-1]+pad+1)
    region=image[y0:y1,x0:x1]
    if on_point is None:
        from . import native_gpu
        blended=native_gpu.local(region,mask[y0:y1,x0:x1],edit)
        if blended is not None:
            result=image.copy()
            result[y0:y1,x0:x1]=blended
            return result
    linear_key=(mask_key,int(y0),int(y1),int(x0),int(x1))
    linear=cache.evaluate(f'linear:{index}',linear_key,lambda:to_linear(region)) if cache else to_linear(region)
    a=to_srgb(linear*(2**edit.get('exposure',0)))
    hdr=bool(edit.get('hdr'))
    a=(a-.5)*(1+edit.get('contrast',0)/125)+.5
    a+=np.array([edit.get('temperature',0),-edit.get('tint',0)*.5,-edit.get('temperature',0)],np.float32)/400
    gray=a@np.array([.2126,.7152,.0722],np.float32)
    a=gray[...,None]+(a-gray[...,None])*(1+edit.get('saturation',0)/100)
    a+=edit.get('shadows',0)/300*(1-np.clip(gray,0,1))[...,None]**2
    a+=edit.get('highlights',0)/300*np.clip(gray,0,1)[...,None]**2
    if edit.get('whites') or edit.get('blacks'):
        black=edit.get('blacks',0)/200
        white=1+edit.get('whites',0)/200
        a=a*(white-black)+black
    # Local color tools operate on bounded chroma, while highlight intensity
    # survives their SDR curves and color conversions.
    hdr_gain=np.maximum(1.,a.max(axis=-1,keepdims=True)) if hdr else 1.
    if hdr:a=a/hdr_gain
    for channel,points in enumerate(edit.get('local_curves',[])):
        curve=np.asarray(points,np.float32)
        if np.array_equal(curve[:,0],curve[:,1]):continue
        if channel==0:a=np.interp(a,curve[:,0],curve[:,1]).astype(np.float32)
        else:a[...,channel-1]=np.interp(a[...,channel-1],curve[:,0],curve[:,1])
    if edit.get('hue'):
        h,s,l=rgb_to_hsl(a);a=hsl_to_rgb(h+edit['hue']/360,s,l)
    if any(any(group) for group in edit.get('hsl',[])):a=mix_hsl(a,edit['hsl'])
    if edit.get('point_colors') or on_point is not None:
        from .point_color import apply as apply_points
        callback=(lambda i,value:on_point(index,i,value,mask[y0:y1,x0:x1],(x0,y0),mask.shape)) if on_point is not None else None
        a=apply_points(a,edit.get('point_colors',[]),callback)
    if hdr:a=a*hdr_gain
    if edit.get('clarity'):a+=(a-blur(a,2*pixel_scale))*edit['clarity']/100
    if detail:
        options={**EXTRA_DEFAULTS,**edit}
        if wavelet:
            # Noise is measured on the whole frame, so the result does not depend on the mask's extent.
            measure=lambda:wavelet_profiles(rgb_to_lab(np.clip(image,0,1))[...,0])
            options['wavelet_profiles']=cache.evaluate(f'noise:{index}',mask_key,measure) if cache else measure()
        if hdr:
            from .hdr import color_operation
            a=color_operation(a,lambda bounded:detail_tools(bounded,options,pixel_scale))
        else:a=detail_tools(a,options,pixel_scale)
        if edit.get('sharpen'):a=sharpen(a,edit['sharpen'],.8,50,0,pixel_scale,luminance=edit.get('detail_version',1)>=2)
    weight=mask[y0:y1,x0:x1,None]
    result=image.copy()
    result[y0:y1,x0:x1]=region*(1-weight)+(np.maximum(a,0) if hdr else np.clip(a,0,1))*weight
    return result


def apply_retouch(source,operations):
    if not operations:return source
    a=source.copy();h,w=a.shape[:2]
    for op in operations:
        if not op.get('enabled',True):continue
        if op.get('type')=='dust':
            from .dust import repair
            repair(a,op.get('spots',[]),copy=False,method=op.get('repair_method','smooth'))
            continue
        points=op.get('points',[[.5,.5]])
        mask,bounds=brush_mask(a.shape,points,op.get('radius',.015),op.get('feather',60),with_bounds=True)
        if bounds is None:continue
        # Nonzero pixels lie inside the painted windows; searching those avoids a full-frame scan.
        ys,xs=np.where(mask[bounds[0]:bounds[1],bounds[2]:bounds[3]]>0)
        if not len(xs):continue
        ys=ys+bounds[0];xs=xs+bounds[2]
        pad=max(5,int(op.get('radius',.015)*min(h,w)*2))
        x0,x1=max(0,xs.min()-pad),min(w,xs.max()+pad+1)
        y0,y1=max(0,ys.min()-pad),min(h,ys.max()+pad+1)
        part=a[y0:y1,x0:x1];weight=mask[y0:y1,x0:x1][...,None]
        kind=op.get('type','heal')
        if kind=='red_eye':
            fixed=part.copy()
            red=part[...,0]>np.maximum(part[...,1],part[...,2])*1.4
            fixed[...,0]=np.where(red,(part[...,1]+part[...,2])/2,part[...,0])
        elif kind=='inpaint':
            binary=np.uint8(weight[...,0]>.05)*255
            fixed=np.stack([cv2.inpaint(part[...,c].copy(),binary,3,cv2.INPAINT_NS) for c in range(3)],axis=-1)
        elif kind=='heal' and op.get('version',1)>=2:
            from .healing import automatic_offset,blend_boundary,inpaint
            donor=op.get('source')
            offset=automatic_offset(a,mask) if donor is None else (np.asarray(donor)-np.asarray(points[0]))*[w,h]
            if offset is None:
                fixed=inpaint(part,weight)
            else:
                yy,xx=np.mgrid[y0:y1,x0:x1].astype(np.float32)
                sampled=cv2.remap(a,xx+float(offset[0]),yy+float(offset[1]),cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101)
                fixed=blend_boundary(part,sampled,weight)
        else:
            donor=np.asarray(op.get('source',points[0]))-np.asarray(points[0])
            yy,xx=np.mgrid[y0:y1,x0:x1].astype(np.float32)
            fixed=cv2.remap(a,xx+float(donor[0])*w,yy+float(donor[1])*h,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101)
            if kind=='heal':fixed+=blur(part,max(1,pad/3))-blur(fixed,max(1,pad/3))
        a[y0:y1,x0:x1]=part*(1-weight)+fixed*weight
    np.maximum(a,0,out=a)   # a is this function's copy
    return a if a.dtype==np.float32 else a.astype(np.float32)
