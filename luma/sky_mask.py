"""Select Sky: a deterministic sky selection from colour, smoothness and position (no learned model).

The sky is found on a reduced copy of the developed colour source in the displayed frame's orientation:
bright blue, white/grey or warm smooth pixels in the upper part, kept only where they connect to the top
of the frame without crossing a sharp edge (a roof line, a sea horizon), with enclosed clouds filled.
A colour guided filter (He et al.) then turns that selection into soft edges that follow the photo,
evaluated at the source's own resolution so branches and roof lines keep their detail.
"""
import numpy as np
import cv2

DETECT_SIZE=640         # long side of the detection copy; sizes below scale with it
LUMA=np.array([.2126,.7152,.0722],np.float32)
DEFAULTS=dict(sensitivity=50.,feather=30.)      # both 0..100: reach into haze and cloud, edge softness


def _box(a,r):
    return cv2.boxFilter(np.ascontiguousarray(a),-1,(2*r+1,2*r+1),borderType=cv2.BORDER_REFLECT_101)


def _ramp(x,low,high):
    t=(x-np.float32(low))*np.float32(1/(high-low))
    return np.minimum(np.maximum(t,0,out=t),1,out=t)


def view(shape,transform):
    """((x0, y0, x1, y1) source pixels of the shown frame, quarter turns that bring its top up).

    A coordinate probe through the photo's own geometry gives both, so rotation, flip, straightening and
    crop are those of the preview and export."""
    h,w=shape[:2]
    if transform is None:return (0,0,w,h),0
    scale=96/max(h,w);ph,pw=max(8,round(h*scale)),max(8,round(w*scale))
    y,x=np.mgrid[0:ph,0:pw].astype(np.float32)
    probe=np.asarray(transform(np.dstack([(x+.5)/pw,(y+.5)/ph,np.ones_like(x)])),np.float32)
    valid=probe[...,2]>.9
    if valid.sum()<4:return (0,0,w,h),0
    rows=np.nonzero(valid)[0];sx=probe[...,0][valid];sy=probe[...,1][valid]
    top=rows<=np.percentile(rows,20);bottom=rows>=np.percentile(rows,80)
    dx=(sx[top].mean()-sx[bottom].mean())*w;dy=(sy[top].mean()-sy[bottom].mean())*h
    turns=(0 if dy<=0 else 2) if abs(dy)>=abs(dx) else (1 if dx>0 else 3)
    margin=1.5/96
    x0,x1=np.clip([sx.min()-margin,sx.max()+margin],0,1);y0,y1=np.clip([sy.min()-margin,sy.max()+margin],0,1)
    box=(int(x0*w),int(y0*h),max(int(x0*w)+1,int(np.ceil(x1*w))),max(int(y0*h)+1,int(np.ceil(y1*h))))
    return box,turns


def features(rgb,sensitivity=DEFAULTS['sensitivity']):
    """Per-pixel cues of a detection copy whose first row is the top of the frame: sky likelihood 0..1,
    the blue part of it, normalised luminance, local texture and sharp edges."""
    s=float(np.clip(sensitivity,0,100))/100
    h,w=rgb.shape[:2];unit=max(h,w)/DETECT_SIZE
    # Exposure-independent: scaled to the photo's white by its brightest channel, so no hue is clipped.
    white=max(.05,float(np.percentile(rgb[::2,::2].max(-1),99.5)))
    n=np.minimum(rgb*np.float32(1/white),1);np.maximum(n,0,out=n)
    hsv=cv2.cvtColor(n,cv2.COLOR_RGB2HSV);hue,sat=hsv[...,0],hsv[...,1]
    L=n@LUMA
    sigma=max(.5,.8*unit);scale=max(unit,.25)/8
    def magnitude(a):
        gx,gy=(cv2.Sobel(a,cv2.CV_32F,dx,1-dx,ksize=3,scale=scale).reshape(h,-1) for dx in (1,0))
        return cv2.magnitude(gx,gy).reshape(a.shape)
    gradient=magnitude(cv2.GaussianBlur(L,(0,0),sigma))
    texture=_box(gradient,max(1,round(2*unit)))
    # Edges in any channel: a white wall beside a bright blue sky differs in red, not in brightness.
    colour=magnitude(cv2.GaussianBlur(n,(0,0),sigma)).max(-1)
    blue=_ramp(hue,175,195)*(1-_ramp(hue,250,275))*_ramp(sat,.04,.12)*_ramp(L,.30-.12*s,.45-.12*s)
    warm=np.maximum(1-_ramp(hue,45,65),_ramp(hue,315,335))
    pale=np.maximum((1-_ramp(sat,.08+.08*s,.20+.08*s))*_ramp(L,.55-.2*s,.75-.2*s),           # clouds, overcast, haze
                    .85*warm*(1-_ramp(sat,.55,.85))*_ramp(L,.6-.15*s,.8-.15*s))                 # sunset
    calm=1-_ramp(texture,.012+.02*s,.03+.04*s)
    y=np.linspace(0,1,h,dtype=np.float32)[:,None]
    prior=1-.55*_ramp(y,.3,1)
    edges=colour>np.float32(.035+.03*s)
    return dict(score=np.maximum(blue,pale)*calm*prior,blue=blue*calm*prior,L=L,texture=texture,edges=edges)


def horizon(L,candidates):
    """Row offsets (per column) of a straight, level edge that darkens downwards with sky-like pixels on
    both sides, such as a sea horizon; None when there is none. The sky never continues below it."""
    h,w=L.shape;unit=max(h,w)/DETECT_SIZE;k=max(2,round(3*unit))
    smooth=cv2.GaussianBlur(L,(0,0),max(.5,unit))
    drop=np.zeros_like(L);drop[k:h-k]=smooth[:h-2*k]-smooth[2*k:]
    edge=((drop>.06)&(cv2.dilate(candidates,np.ones((2*k+1,1),np.uint8))>0)).astype(np.uint8)*255
    lines=cv2.HoughLinesP(edge,1,np.pi/720,max(20,round(.12*w)),minLineLength=.25*w,maxLineGap=max(2,round(.01*w)))
    if lines is None:return None
    best=None
    for x0,y0,x1,y1 in np.asarray(lines).reshape(-1,4):
        if x1==x0 or abs(y1-y0)>.1*abs(x1-x0):continue
        xs=np.arange(min(x0,x1),max(x0,x1)+1);ys=np.round(y0+(xs-x0)*(y1-y0)/(x1-x0)).astype(int)
        above=np.clip(ys-2*k,0,h-1);below=np.clip(ys+2*k,0,h-1)
        # Sky above all along, and below it enough sky-like water or haze for the sky to leak into.
        if (candidates[above,xs]>0).mean()<.85 or (candidates[below,xs]>0).mean()<.3:continue
        # Water and land below a horizon are darker than the sky above it; a cloud base is not.
        sky=smooth[:max(1,int(min(y0,y1)))][candidates[:max(1,int(min(y0,y1)))]>0]
        if not len(sky) or smooth[below,xs].mean()>np.median(sky)-.06:continue
        if best is None or min(y0,y1)<best[1]:best=(x0,y0,x1,y1)
    if best is None:return None
    x0,y0,x1,y1=best
    return y0+(np.arange(w)-x0)*(y1-y0)/(x1-x0)


def detect(rgb,sensitivity=DEFAULTS['sensitivity']):
    """Binary sky of a detection copy (first row = top of the frame); empty when no sky reaches the top.

    Which regions are sky is decided at the default sensitivity; the slider then lets that sky reach
    further into haze and cloud, or keeps its clearest part, but never makes another region sky."""
    h,w=rgb.shape[:2];f=features(rgb,DEFAULTS['sensitivity'])
    candidates=((f['score']>.5)&~f['edges']).astype(np.uint8)
    line=horizon(f['L'],candidates)
    below=None if line is None else np.arange(h)[:,None]>=line[None,:]-1
    if below is not None:candidates[below]=0
    count,labels,stats,centres=cv2.connectedComponentsWithStats(candidates,connectivity=4)
    if count<2:return np.zeros((h,w),bool)
    flat=labels.ravel();area=stats[:,cv2.CC_STAT_AREA].astype(np.float64)
    blue=np.bincount(flat,(f['blue']>.5).ravel(),count)/np.maximum(area,1)
    bright=np.bincount(flat,f['L'].ravel(),count)/np.maximum(area,1)
    band=max(2,round(.03*h));inband=labels[:band]
    width=np.bincount(np.unique(inband*w+np.arange(w))//w,minlength=count)       # top-band columns per component
    seeded=width>0;seeded[0]=False
    high=stats[:,cv2.CC_STAT_TOP]<=.15*h
    accept=(blue>=.5)&((seeded&(area>=.002*h*w))|(high&(area>=.01*h*w)))
    # A pale region that runs most of the way down is a wall, a floor or a light leak rather than sky.
    pale=seeded&~accept&((width>=.2*w)|(area>=.02*h*w))&(bright>=.55)&(stats[:,cv2.CC_STAT_TOP]+stats[:,cv2.CC_STAT_HEIGHT]<=.75*h)
    for label in np.nonzero(pale)[0]:
        # Walls between windows are pale and smooth too; a pale sky has few enclosed gaps.
        x,y,bw,bh=stats[label,:4];part=(labels[y:y+bh,x:x+bw]==label).astype(np.uint8)
        outside=np.pad(1-part,1,constant_values=1);cv2.floodFill(outside,None,(0,0),2)
        accept[label]=(outside[1:-1,1:-1]==1).sum()<=.12*area[label]
    accept[0]=False
    if not accept.any():return np.zeros((h,w),bool)
    # Sky seen through foliage: blue, smooth pieces above the sky's lower reach in a similar brightness.
    reach=np.percentile(np.nonzero(accept[labels])[0],90)
    loose=~accept&(blue>=.6)&(centres[:,1]<=reach)&(area>=.0002*h*w)
    loose&=np.abs(bright-np.average(bright[accept],weights=area[accept]))<.15;loose[0]=False
    sky=(accept|loose)[labels]
    if float(sensitivity)!=DEFAULTS['sensitivity']:
        f=features(rgb,sensitivity);reach=((f['score']>.5)&~f['edges']).astype(np.uint8)
        if below is not None:reach[below]=0
        line=horizon(f['L'],reach)          # water that only now looks like sky
        if line is not None:reach[np.arange(h)[:,None]>=line[None,:]-1]=0
        count,labels=cv2.connectedComponents(reach,connectivity=4)
        keep=np.zeros(count,bool);keep[labels[sky&(reach>0)]]=True;keep[0]=False
        sky=keep[labels]
    # Enclosed clouds, birds and wires: holes that reach no side or bottom edge.
    count,labels,stats,_=cv2.connectedComponentsWithStats((~sky).astype(np.uint8),connectivity=4)
    edge=np.zeros(count,bool);edge[np.concatenate([labels[:,0],labels[:,-1],labels[-1]])]=True
    minor=stats[:,cv2.CC_STAT_AREA]<.25*sky.sum()
    calm=np.bincount(labels.ravel(),f['texture'].ravel(),count)/np.maximum(stats[:,cv2.CC_STAT_AREA],1)<.03
    tiny=stats[:,cv2.CC_STAT_AREA]<.002*h*w
    fill=~edge&minor&(calm|tiny);fill[0]=False
    return sky|fill[labels]


def _guided_coefficients(guide,p,r,eps):
    """Box-averaged (a_r, a_g, a_b, b) of a colour guided filter of p, so q = a.I + b at any resolution."""
    m=_box(guide,r);mp=_box(p,r)
    cov=_box(guide*p[...,None],r)-m*mp[...,None]
    i=guide
    rr=_box(i[...,0]*i[...,0],r)-m[...,0]*m[...,0]+eps;rg=_box(i[...,0]*i[...,1],r)-m[...,0]*m[...,1]
    rb=_box(i[...,0]*i[...,2],r)-m[...,0]*m[...,2];gg=_box(i[...,1]*i[...,1],r)-m[...,1]*m[...,1]+eps
    gb=_box(i[...,1]*i[...,2],r)-m[...,1]*m[...,2];bb=_box(i[...,2]*i[...,2],r)-m[...,2]*m[...,2]+eps
    i11=gg*bb-gb*gb;i12=rb*gb-rg*bb;i13=rg*gb-rb*gg;i22=rr*bb-rb*rb;i23=rb*rg-rr*gb;i33=rr*gg-rg*rg
    det=rr*i11+rg*i12+rb*i13
    c0,c1,c2=cov[...,0],cov[...,1],cov[...,2]
    a=np.dstack([i11*c0+i12*c1+i13*c2,i12*c0+i22*c1+i23*c2,i13*c0+i23*c1+i33*c2])/det[...,None]
    b=mp-(a*m).sum(-1)
    return np.dstack([_box(a,r),_box(b,r)]).astype(np.float32)


def _apply(coefficients,guide):
    """q = a.I + b with the coefficients bilinearly upsampled to the guide's size, in row tiles."""
    from .pixel_jobs import rows
    h,w=guide.shape[:2];ch,cw=coefficients.shape[:2]
    u=((np.arange(w,dtype=np.float32)+.5)*np.float32(cw/w)-.5).astype(np.float32)
    def part(first,stop):
        v=((np.arange(first,stop,dtype=np.float32)+.5)*np.float32(ch/h)-.5).astype(np.float32)
        mx=np.ascontiguousarray(np.broadcast_to(u[None,:],(stop-first,w)));my=np.ascontiguousarray(np.broadcast_to(v[:,None],(stop-first,w)))
        c=cv2.remap(coefficients,mx,my,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
        q=c[...,0]*guide[first:stop,:,0]+c[...,1]*guide[first:stop,:,1]+c[...,2]*guide[first:stop,:,2]+c[...,3]
        return np.minimum(np.maximum(q,0,out=q),c[...,4],out=q)[...,None]
    return rows(h,w,part,channels=1)[...,0]


def detection_copy(source,transform=None):
    """(the shown frame's region of the source, its reduced copy turned top-up, (box, turns))."""
    (x0,y0,x1,y1),turns=view(source.shape,transform)
    region=np.asarray(source[y0:y1,x0:x1,:3],np.float32)
    rh,rw=region.shape[:2];factor=min(1.,DETECT_SIZE/max(rh,rw))
    small=cv2.resize(region,(max(1,round(rw*factor)),max(1,round(rh*factor))),interpolation=cv2.INTER_AREA) if factor<1 else region.copy()
    return region,np.ascontiguousarray(np.rot90(small,turns)),((x0,y0,x1,y1),turns)


def sky_mask(source,component,transform=None):
    """Coverage 0..1 in source pixels for a 'sky' component. source is the developed colour source."""
    h,w=source.shape[:2];result=np.zeros((h,w),np.float32)
    region,small,((x0,y0,x1,y1),turns)=detection_copy(source,transform)
    if min(small.shape[:2])<16:return result
    sky=detect(small,component.get('sensitivity',DEFAULTS['sensitivity']))
    if not sky.any():return result
    # The filter's coefficients are smooth over its window, so half the detection size carries them.
    feather=float(np.clip(component.get('feather',DEFAULTS['feather']),0,100))/100
    sh,sw=small.shape[:2];size=(max(1,sw//2),max(1,sh//2))
    guide=cv2.resize(small,size,interpolation=cv2.INTER_AREA);p=cv2.resize(sky.astype(np.float32),size,interpolation=cv2.INTER_AREA)
    unit=max(size)/(DETECT_SIZE/2)
    r=max(1,round((1+5*feather)*unit));eps=np.float32(1e-4+3e-3*feather)
    # The linear model learns "brighter is sky" from dark branches and would carry it onto a white wall
    # nearby; the result stays within a soft margin of the detected sky.
    grow=1+r//2;reach=_box(cv2.dilate(p,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*grow+1,2*grow+1))),max(1,r//2))
    coefficients=np.dstack([_guided_coefficients(guide,p,r,eps),np.minimum(reach,1)])
    coefficients=np.ascontiguousarray(np.rot90(coefficients,-turns))
    result[y0:y1,x0:x1]=_apply(coefficients,region)
    return result
