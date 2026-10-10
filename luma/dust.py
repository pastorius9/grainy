"""Local, deterministic dust candidates and non-destructive spot repair.

Detection runs at source resolution in overlapping tiles. Candidates are small
bright/dark outliers in relatively quiet surroundings, not semantic certainty:
stars, freckles and other isolated details can be indistinguishable from dust.
"""
from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter
import math
import cv2
import numpy as np

# Scalar-q promotion differs across supported NumPy 2.x releases. Probe the
# public operation once, so the fast path preserves that runtime's old results.
_PERCENTILE_DTYPE=np.asarray(np.percentile(np.zeros(1,np.float32),50)).dtype
_PEAK_KERNEL=np.ones((3,3),np.uint8)


class Cancelled(Exception):
    pass


def checkpoint(cancel):
    if cancel is not None and cancel.is_set():raise Cancelled()


def _median(values):
    """Median of a nonempty finite float32 vector, without axis dispatch."""
    middle=values.size//2
    ordered=values.copy()
    if values.size%2:
        ordered.partition(middle);return float(ordered[middle])
    ordered.partition((middle-1,middle))
    return float((ordered[middle-1]+ordered[middle])*np.float32(.5))


@lru_cache(maxsize=512)
def _quantile_plan(size,fractions):
    positions=(size-1)*np.asarray(fractions,np.float64)
    lower=positions.astype(np.intp);upper=np.minimum(lower+1,size-1)
    weight=positions-lower
    arrays=(np.concatenate((lower,upper)),lower,upper,weight>=.5,
            (1-weight).astype(_PERCENTILE_DTYPE),weight.astype(_PERCENTILE_DTYPE))
    for array in arrays:array.setflags(write=False)
    return arrays


def _quantiles(values,*fractions):
    """NumPy's linear quantile rule for nonempty finite float32 vectors.

    Partition once for every requested endpoint. Match separate scalar
    percentile calls, including float32 weak promotion of each weight and the
    backward interpolation used for weights >= one half.
    """
    kth,lower,upper,backward,inverse,weight=_quantile_plan(values.size,fractions)
    ordered=values.copy();ordered.partition(kth)
    a,b=ordered[lower],ordered[upper];delta=b-a
    return np.where(backward,b-delta*inverse,a+delta*weight)


def _component_indices(stats,minimum,maximum,x0,y0,left,top,right,bottom):
    """Apply the same cheap bounds to all components, preserving label order."""
    boxes=stats[1:];x,y,w,h,area=boxes.T.astype(np.float64)
    small=np.minimum(w,h);large=np.maximum(w,h)
    # OpenCV components have positive extents. Keep the explicit zero guard
    # for malformed statistics without allowing a division warning.
    valid=(small>0)&(large<=maximum)&(np.sqrt(4*area/math.pi)>=minimum)
    safe=np.maximum(small,1);pixels=np.maximum(w*h,1)
    valid&=(large/safe<=3.5)&(area/pixels>=.3)
    gx=x+(w-1)/2+x0;gy=y+(h-1)/2+y0
    valid&=(gx>=left)&(gx<right)&(gy>=top)&(gy<bottom)
    return np.flatnonzero(valid)+1


@dataclass(frozen=True)
class Options:
    sensitivity: int = 50
    minimum: int = 3
    maximum: int = 40
    polarity: str = 'both'
    detailed: bool = False
    reduce_patterns: bool = False
    soft: bool = False
    suppress_grain: bool = False
    scratches: bool = False
    scratch_width: int = 12

    def validate(self):
        if not 1 <= self.sensitivity <= 100:raise ValueError('민감도는 1–100 사이여야 합니다.')
        if not 2 <= self.minimum <= self.maximum <= 160:raise ValueError('먼지 크기는 2–160 px 범위에서 최소 ≤ 최대여야 합니다.')
        if self.polarity not in ('both','dark','bright'):raise ValueError('먼지 종류를 선택하세요.')
        if not isinstance(self.detailed,bool):raise ValueError('정밀 검색 설정이 올바르지 않습니다.')
        if not isinstance(self.reduce_patterns,bool):raise ValueError('반복 무늬 검색 설정이 올바르지 않습니다.')
        if not isinstance(self.soft,bool):raise ValueError('옅은 먼지 검색 설정이 올바르지 않습니다.')
        if not isinstance(self.suppress_grain,bool):raise ValueError('입자 후보 억제 설정이 올바르지 않습니다.')
        if not isinstance(self.scratches,bool):raise ValueError('긴 흠집 검색 설정이 올바르지 않습니다.')
        if type(self.scratch_width) is not int or not 2<=self.scratch_width<=40:raise ValueError('흠집 최대 굵기는 2–40 px 사이여야 합니다.')


def _repeated_detail(image,x,y,w,h,*,reduce_patterns=True):
    """Reject a core reproduced by three separate neighbouring patches.

    Correlation only locates similar shapes. Check the actual core intensities
    after a robust border brightness offset: a dust mark can resemble a woven
    pattern while differing in amplitude. A nearby defect may disturb part of
    the border, so a slightly imperfect shape match can still explain a clean
    core. Only additional review candidates use this filter.
    """
    height,width=image.shape
    if min(x,y,width-x-w,height-y-h)<2:return False
    template=image[y-2:y+h+2,x-2:x+w+2]
    if float(template.std())<.004:return False
    pad=max(24,max(w,h)*4)
    left=max(0,x-pad);top=max(0,y-pad);right=min(width,x+w+pad);bottom=min(height,y+h+pad)
    correlation=cv2.matchTemplate(image[top:bottom,left:right],template,cv2.TM_CCOEFF_NORMED)
    own_x=x-2-left;own_y=y-2-top
    if not reduce_patterns:
        # Preserve the previous candidate set unless the user asks for the
        # stronger pattern filter, which can trade faint dust for fewer extras.
        peaks=np.uint8(correlation>.94)
        peaks[max(0,own_y-h*2):own_y+h*2+1,max(0,own_x-w*2):own_x+w*2+1]=0
        count,_=cv2.connectedComponents(peaks,8)
        return count-1>=3
    correlation[max(0,own_y-h*2):own_y+h*2+1,max(0,own_x-w*2):own_x+w*2+1]=-1
    peaks=(correlation>.75)&(correlation>=cv2.dilate(correlation,_PEAK_KERNEL))
    py,px=np.nonzero(peaks)
    if len(py)<3:return False
    order=np.argsort(correlation[py,px])[::-1][:24]
    border=np.ones(template.shape,bool);border[2:-2,2:-2]=False
    reference=template[2:-2,2:-2]
    tolerance=max(.006,min(.02,float(template.max()-template.min())*.12))
    matches=[]
    for index in order:
        j,i=int(py[index]),int(px[index])
        if any((i-a)**2+(j-b)**2<max(w,h)**2 for a,b in matches):continue
        patch=image[top+j:top+j+template.shape[0],left+i:left+i+template.shape[1]]
        offset=_median(template[border]-patch[border])
        error=_median(np.abs(reference-patch[2:-2,2:-2]-offset).ravel())
        if error<=tolerance:matches.append((i,j))
        if len(matches)>=3:return True
    return False


def detect(gray, options=Options(), *, cancel=None, progress=None, tile_size=640, limit=500):
    """Return normalized source-coordinate ellipses, ranked by contrast/noise.

    gray is encoded luminance in [0, 1]. No resampling is done; pixel sizes in
    Options always refer to this source. Halo ownership avoids tile duplicates.
    """
    options.validate();checkpoint(cancel)
    gray=np.asarray(gray,dtype=np.float32)
    if gray.ndim!=2 or min(gray.shape)<1 or not np.isfinite(gray).all():raise ValueError('유효한 사진 밝기 배열이 필요합니다.')
    if tile_size<32 or limit<1:raise ValueError('Invalid detector bounds')
    height,width=gray.shape;started=perf_counter();maximum=options.maximum
    halo=maximum*2+12
    sizes=sorted(set([maximum+3+(maximum%2)]+[size for size in (9,17) if options.detailed and options.minimum+3<=size<maximum+3]),reverse=True)
    kernels=[cv2.getStructuringElement(cv2.MORPH_RECT,(size,size)) for size in sizes]
    threshold=.003+(.095*(1-options.sensitivity/100)**2)
    noise_factor=7-4*options.sensitivity/100
    kinds=('dark','bright') if options.polarity=='both' else (options.polarity,)
    spots=[];total=0;completed=0;accepted={}
    tiles=math.ceil(height/tile_size)*math.ceil(width/tile_size)
    soft=options.detailed and options.soft
    stages=1+int(soft)+int(options.scratches)
    for top in range(0,height,tile_size):
        for left in range(0,width,tile_size):
            checkpoint(cancel)
            y0=max(0,top-halo);x0=max(0,left-halo)
            part=gray[y0:min(height,top+tile_size+halo),x0:min(width,left+tile_size+halo)]
            smooth=cv2.GaussianBlur(part,(0,0),.65)
            # A robust local noise floor keeps isolated film grain from turning
            # into hundreds of candidates. The ring test below rejects edges.
            noise=float(np.median(np.abs(part-smooth)))/.67449
            cutoff=max(threshold,noise*noise_factor)
            for kind in kinds:
                checkpoint(cancel)
                operation=cv2.MORPH_BLACKHAT if kind=='dark' else cv2.MORPH_TOPHAT
                for kernel in kernels:
                    response=cv2.morphologyEx(smooth,operation,kernel)
                    # Stronger seed levels separate a dust core from adjoining
                    # low-contrast texture. Keep the first (largest) accepted region.
                    for level in (cutoff,cutoff*2.5,cutoff*6):
                        count,labels,stats,_=cv2.connectedComponentsWithStats(np.uint8(response>level),8)
                        eligible=_component_indices(stats,options.minimum,maximum,x0,y0,left,top,min(width,left+tile_size),min(height,top+tile_size))
                        checkpoint(cancel)
                        for ordinal,index in enumerate(eligible):
                            if ordinal%128==0:checkpoint(cancel)
                            index=int(index)
                            x,y,w,h,area=map(int,stats[index]);diameter=math.sqrt(4*area/math.pi)
                            cx=x+(w-1)/2;cy=y+(h-1)/2;gx=cx+x0;gy=cy+y0
                            cell=(int(gx//maximum),int(gy//maximum),kind)
                            neighbors=[spot for dy in (-1,0,1) for dx in (-1,0,1) for spot in accepted.get((cell[0]+dx,cell[1]+dy,kind),[])]
                            if any(((gx-ax)/arx)**2+((gy-ay)/ary)**2<=1 for ax,ay,arx,ary in neighbors):continue
                            # Fit the entire component, including its diagonal pixels,
                            # rather than removing a circle that clips irregular dust.
                            local=labels[y:y+h,x:x+w]==index
                            yy,xx=np.nonzero(local);rx=max(1.,(w-1)/2);ry=max(1.,(h-1)/2)
                            fit=max(1.,float(np.sqrt(((xx+x-cx)/rx)**2+((yy+y-cy)/ry)**2).max()))
                            rx=rx*fit+1.5;ry=ry*fit+1.5
                            pad=math.ceil(max(rx,ry)*2.4)+2
                            if min(gx,gy,width-1-gx,height-1-gy)<pad:continue
                            iy,ix=round(cy),round(cx)
                            ring=part[iy-pad:iy+pad+1,ix-pad:ix+pad+1]
                            if ring.shape!=(pad*2+1,pad*2+1):continue
                            offsets=np.arange(-pad,pad+1,dtype=np.float64);dy,dx=offsets[:,None],offsets[None,:]
                            distance=((dx+ix-cx)/rx)**2+((dy+iy-cy)/ry)**2
                            surrounding=ring[(distance>=1.5**2)&(distance<=2.3**2)]
                            if surrounding.size<8:continue
                            center=_median(smooth[y:y+h,x:x+w][local])
                            background=_median(surrounding)
                            contrast=background-center if kind=='dark' else center-background
                            p10,p90=_quantiles(surrounding,.1,.9);spread=float(p90-p10)
                            quiet=contrast>=cutoff and spread<=contrast*(.25+options.sensitivity*.006)
                            review=not quiet or kernel.shape[0]!=sizes[0]
                            if review and not options.detailed:continue
                            if review:
                                # A periodic texture creates many similar local
                                # extrema. A new candidate must stand above its
                                # own neighbourhood's morphological response.
                                local_response=response[iy-pad:iy+pad+1,ix-pad:ix+pad+1]
                                response_ring=local_response[(distance>=1.3**2)&(distance<=2.3**2)]
                                background_response=float(_quantiles(response_ring,.6)[0])
                                core_response=_median(response[y:y+h,x:x+w][local])
                                if core_response<max(cutoff,background_response*2.5):continue
                            if not quiet:
                                # A round, isolated local residual may be dust
                                # adjoining an edge. Require a quiet contiguous
                                # arc, and keep it separate for human review.
                                if not options.detailed or kernel.shape[0]>17 or area/(w*h)<.55 or max(w,h)/min(w,h)>1.8:continue
                                coords=np.stack((xx,yy),axis=0).astype(np.float64)
                                eigen=np.linalg.eigvalsh(np.cov(coords))
                                if eigen[0]<eigen[1]*.45:continue
                                angle=((np.arctan2(dy+iy-cy,dx+ix-cx)+math.pi)*4/math.pi).astype(int)%8
                                arc=[];samples=[]
                                for sector in range(8):
                                    values=ring[(distance>=1.1**2)&(distance<=1.7**2)&(angle==sector)]
                                    if values.size<3:arc.append(False);samples.append(values);continue
                                    difference=_median(values)-center
                                    if kind=='bright':difference=-difference
                                    p15,p85=_quantiles(values,.15,.85);deviation=float(p85-p15)
                                    arc.append(difference>max(cutoff*1.6,.045) and deviation<difference*.65);samples.append(values)
                                starts=[i for i in range(8) if all(arc[(i+j)%8] for j in range(3))]
                                if not starts:continue
                                arc_contrasts=[]
                                for start in starts:
                                    background=_median(np.concatenate([samples[(start+j)%8] for j in range(3)]))
                                    arc_contrasts.append(background-center if kind=='dark' else center-background)
                                contrast=max(arc_contrasts)
                                spread=max(spread,contrast)

                            # Defer patch matching until the cheaper size,
                            # compactness and quiet-arc tests have passed.
                            if review and _repeated_detail(smooth,x,y,w,h,reduce_patterns=options.reduce_patterns):continue

                            score=core_response/max(.003,noise,background_response) if review else contrast/max(.003,noise+spread*.5)
                            if not review:accepted.setdefault(cell,[]).append((gx,gy,rx,ry))
                            spots.append(dict(x=gx/max(1,width-1),y=gy/max(1,height-1),
                                rx=rx/width,ry=ry/height,polarity=kind,review=review,score=round(score,4),
                                diameter=round(diameter,1),contrast=round(contrast,5)))
                            total+=1
                    if not options.detailed and len(spots)>limit*2:spots=sorted(spots,key=lambda s:(s.get('review',False),-s['score'],s['y'],s['x']))[:limit]
            completed+=1
            if progress is not None:progress(completed,tiles*stages)
    checkpoint(cancel)
    ordered=sorted(spots,key=lambda s:(s.get('review',False),-s['score'],s['y'],s['x']))
    if options.detailed:
        # Stable spatial suppression gives existing ordinary candidates priority.
        # The grid keeps a dense scan from turning into a quadratic comparison.
        kept=[];bins={}
        for spot in ordered:
            checkpoint(cancel)
            x=spot['x']*width;y=spot['y']*height;cell=(int(x//maximum),int(y//maximum))
            neighbors=[s for dy in (-1,0,1) for dx in (-1,0,1) for s in bins.get((cell[0]+dx,cell[1]+dy),[])]
            if any(s['polarity']==spot['polarity'] and ((spot['x']-s['x'])/s['rx'])**2+((spot['y']-s['y'])/s['ry'])**2<=1 for s in neighbors):continue
            kept.append(spot);bins.setdefault(cell,[]).append(spot)
        if soft:
            from .dust_soft import candidates,merge
            extra=candidates(gray,options,cancel=cancel,tile_size=tile_size,
                progress=(lambda done,_:progress(tiles+done,tiles*stages)) if progress is not None else None)
            kept=merge(kept,extra,width,height,maximum,cancel=cancel)
        total=len(kept);spots=kept[:limit]
    else:spots=ordered[:limit]
    if options.scratches:
        from .dust_scratches import candidates
        extra=candidates(gray,options,cancel=cancel,tile_size=tile_size,
            progress=(lambda done,_:progress(tiles*(1+int(soft))+done,tiles*stages)) if progress is not None else None)
        total+=len(extra)
        # The user explicitly enabled filament search. Reserve display space
        # for it when the shared candidate limit is reached.
        spots=spots[:max(0,limit-len(extra))]+extra[:limit]
    return dict(spots=spots,total=total,truncated=total>limit,width=width,height=height,
        seconds=perf_counter()-started)


def spot_bounds(spot, shape):
    h,w=shape[:2]
    values=np.asarray([spot[k] for k in ('x','y','rx','ry')],np.float64)
    if not np.isfinite(values).all() or np.any(values[:2]<0) or np.any(values[:2]>1) or np.any(values[2:]<=0) or np.any(values[2:]>.5):
        raise ValueError('먼지 위치 또는 크기가 올바르지 않습니다.')
    scale=float(spot.get('scale',100))/100
    if not np.isfinite(scale) or not .25<=scale<=2.5:raise ValueError('먼지 제거 범위가 올바르지 않습니다.')
    x,y=values[:2]*[max(1,w-1),max(1,h-1)];rx,ry=np.maximum(.75,values[2:]*[w,h]*scale)
    return x,y,rx,ry


def visualize(gray,strength=50):
    """Inspect both dark and light local deviations without altering edits."""
    if not np.isfinite(strength) or not 1<=strength<=100:raise ValueError('강조 강도는 1–100 사이여야 합니다.')
    residual=cv2.absdiff(gray,cv2.GaussianBlur(gray,(0,0),1.5))
    return np.clip((residual-.003)*(4+strength*.6),0,1).astype(np.float32)


def repair(image, spots, *, cancel=None, copy=True, method='smooth'):
    """Inpaint only the union of chosen ellipses, preserving every other pixel.

    Intersecting regions are repaired together. Work is local to each connected
    group; hundreds of tiny spots never require hundreds of full-frame masks.
    """
    if method not in ('smooth','texture'):raise ValueError('먼지 복구 방식이 올바르지 않습니다.')
    checkpoint(cancel)
    original=image.copy() if method=='texture' and not copy else image
    result=image.copy() if copy else image
    if not spots:return result
    h,w=result.shape[:2]
    regions=[]
    from .dust_shapes import ellipses as spot_ellipses,bounds,paint
    for spot in spots:
        checkpoint(cancel);parts=spot_ellipses(spot,result.shape)
        regions.append([*bounds(parts,result.shape),parts])
    # Bounding-box union is conservative: a nearby candidate cannot pollute
    # another candidate's fill. The masks themselves remain exact ellipses.
    groups=[]
    for region in regions:
        checkpoint(cancel);index=0
        while index<len(groups):
            other=groups[index]
            if region[0]<=other[2] and other[0]<=region[2] and region[1]<=other[3] and other[1]<=region[3]:
                region=[min(region[0],other[0]),min(region[1],other[1]),max(region[2],other[2]),max(region[3],other[3]),region[4]+other[4]]
                groups.pop(index);index=0
            else:index+=1
        groups.append(region)
    padded=[]
    for left,top,right,bottom,ellipses in groups:
        pad=max(8,math.ceil(max(max(rx,ry) for _,_,rx,ry in ellipses)*1.5))
        padded.append((max(0,left-pad),max(0,top-pad),min(w,right+pad),min(h,bottom+pad)))

    def fill(index):
        checkpoint(cancel)
        x0,y0,x1,y1=padded[index];ellipses=groups[index][4]
        patch=result[y0:y1,x0:x1];mask=np.zeros(patch.shape[:2],np.uint8)
        paint(mask,ellipses,x0,y0)
        if not np.any(mask) or np.all(mask):return
        for channel in range(3):
            fixed=cv2.inpaint(np.ascontiguousarray(patch[...,channel]),mask,3,cv2.INPAINT_NS)
            np.copyto(patch[...,channel],np.maximum(fixed,0),where=mask>0)
    # A group reads its padded patch and writes only inside its own box. Groups run in waves on the pixel
    # workers: one waits for every earlier group whose box its patch reads or whose patch its box overlaps,
    # so each sees exactly the pixels of the sequential order.
    from .pixel_jobs import run_all
    boxes=np.array([g[:4] for g in groups],np.int64).reshape(-1,4)   # inclusive/exclusive as used by paint
    reads=np.array(padded,np.int64).reshape(-1,4)
    wave=np.zeros(len(groups),np.int64)
    for j in range(len(groups)):
        if not j:continue
        b=boxes[:j];r=reads[:j]
        # Overlap tests on [left,right] x [top,bottom] with a one-pixel margin (conservative).
        reads_earlier=(reads[j,0]<=b[:,2]+1)&(b[:,0]<=reads[j,2]+1)&(reads[j,1]<=b[:,3]+1)&(b[:,1]<=reads[j,3]+1)
        read_by_earlier=(r[:,0]<=boxes[j,2]+1)&(boxes[j,0]<=r[:,2]+1)&(r[:,1]<=boxes[j,3]+1)&(boxes[j,1]<=r[:,3]+1)
        depends=reads_earlier|read_by_earlier
        if depends.any():wave[j]=wave[:j][depends].max()+1
    for level in range(int(wave.max())+1 if len(wave) else 0):
        checkpoint(cancel)
        run_all(lambda index=int(index):fill(index) for index in np.flatnonzero(wave==level))
    if method=='texture':
        from .dust_texture import restore
        selected=np.zeros((h,w),np.uint8)
        for region in regions:paint(selected,region[4])
        restore(original,result,selected,cancel=cancel)
    checkpoint(cancel)
    return result
