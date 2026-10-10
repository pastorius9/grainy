"""Deterministic line/vanishing-point alignment; no learned image model."""
import cv2
import numpy as np


class AlignmentError(ValueError):pass


def _points(matrix,points):
    p=np.concatenate([np.asarray(points,float),np.ones((*np.shape(points)[:-1],1))],axis=-1)@matrix.T
    if np.any(np.abs(p[...,2])<1e-8):raise AlignmentError('원근 보정 범위가 너무 큽니다. 가이드를 사용해 주세요.')
    return p[...,:2]/p[...,2,None]


def _weighted_median(values,weights):
    order=np.argsort(values);values=values[order];weights=weights[order]
    return float(values[np.searchsorted(np.cumsum(weights),weights.sum()/2)])


def _roll(lines,lengths):
    delta=lines[:,1]-lines[:,0]
    angle=np.rad2deg(np.arctan2(delta[:,1],delta[:,0]))
    residual=(angle+45)%90-45
    keep=np.abs(residual)<=25
    if keep.sum()<3:raise AlignmentError('수평·수직 기준선이 부족합니다. 네 모서리 가이드를 사용해 주세요.')
    values=residual[keep];weights=lengths[keep]
    bins=np.arange(-25.25,25.75,.5)
    hist,edges=np.histogram(values,bins,weights=weights)
    # A small window avoids splitting the same direction across adjacent bins.
    peak=int(np.argmax(np.convolve(hist,np.ones(5),mode='same')))
    center=(edges[peak]+edges[peak+1])/2
    inliers=np.abs(values-center)<2
    if inliers.sum()<3 or weights[inliers].sum()<weights.sum()*.3:
        raise AlignmentError('일관된 수평선을 찾지 못했습니다. 가이드를 사용해 주세요.')
    angle=_weighted_median(values[inliers],weights[inliers])
    return angle,float(weights[inliers].sum()/weights.sum())


def _horizon_roll(lines,lengths,width,height):
    """Conservative fallback for one broad, straight horizontal boundary."""
    delta=lines[:,1]-lines[:,0]
    angle=(np.rad2deg(np.arctan2(delta[:,1],delta[:,0]))+90)%180-90
    middle=lines.mean(axis=1)
    keep=(np.abs(delta[:,0])>=width*.45)&(np.abs(angle)<=25)&(middle[:,1]>height*.15)&(middle[:,1]<height*.85)
    if not keep.any():raise AlignmentError('뚜렷한 수평 기준선을 찾지 못했습니다. 아래 수평 슬라이더로 조절해 주세요.')
    values=angle[keep];weights=lengths[keep]
    result=_weighted_median(values,weights)
    # Two long conflicting boundaries do not identify which one is the horizon.
    if np.any(np.abs(values-result)>1.5):
        raise AlignmentError('긴 기준선들의 방향이 서로 다릅니다. 아래 수평 슬라이더로 조절해 주세요.')
    return result,float(weights.sum()/lengths.sum())


def _plumb_rolls(lines,lengths,width,height):
    """Tilts that upright edges (poles, walls, door frames) support, best first: dicts with angle,
    share (of upright line length that agrees), count, heights (agreeing length in frame heights) and
    centred (the agreeing edges are not all near one side of the frame).

    Looking up or down makes uprights lean away from each other across the frame, so with edges on
    both sides their angle is fitted as a line over the horizontal position and read at the centre.
    Edges near the sides count less: there they also lean with the lens and with a slight pitch.
    Slanted horizontal edges, which perspective tilts by any amount, take no part."""
    delta=lines[:,1]-lines[:,0]
    direction=np.rad2deg(np.arctan2(delta[:,1],delta[:,0]))
    upright=np.abs((direction+90)%180-90)>=65
    if upright.sum()<2:return []
    residual=((direction+45)%90-45)[upright];lengths=lengths[upright]
    x=(lines[upright].mean(axis=1)[:,0]-(width-1)/2)/(width/2)
    weights=lengths*(1-.5*np.minimum(1,np.abs(x)))
    candidates=[(float(r),0.) for r in residual]
    for i in range(len(x)):
        for j in range(i+1,len(x)):
            if x[i]*x[j]>=0 or abs(x[i]-x[j])<.3:continue       # a slope needs an edge on each side
            slope=(residual[i]-residual[j])/(x[i]-x[j])
            if abs(slope)<=12:candidates.append((float(residual[i]-slope*x[i]),float(slope)))
    found=[]
    for level,slope in candidates:
        if abs(level)>20:continue
        inliers=np.abs(residual-(level+slope*x))<1.5
        if inliers.sum()<2:continue
        left,right=weights[inliers&(x<0)].sum(),weights[inliers&(x>=0)].sum()
        if slope and min(left,right)<.2*(left+right):continue
        reach=float(np.ptp(x[inliers]))/2
        found.append((float(weights[inliers].sum())*(.5+reach),level,slope,inliers))
    results=[]
    for score,level,slope,inliers in sorted(found,key=lambda item:-item[0]):
        if slope:
            # Weighted least squares on the agreeing segments.
            design=np.stack([np.ones(inliers.sum()),x[inliers]],axis=1)*np.sqrt(weights[inliers])[:,None]
            angle=float(np.linalg.lstsq(design,residual[inliers]*np.sqrt(weights[inliers]),rcond=None)[0][0])
        else:angle=_weighted_median(residual[inliers],weights[inliers])
        if any(abs(angle-r['angle'])<1 for r in results):continue
        results.append(dict(angle=angle,score=score,share=float(lengths[inliers].sum()/lengths.sum()),count=int(inliers.sum()),
            heights=float(lengths[inliers].sum()/height),centred=bool(np.abs(x[inliers]).min()<.5 or np.ptp(np.sign(x[inliers]))>0),
            both=bool(np.ptp(np.sign(x[inliers]))>0)))
        if len(results)==5:break
    return results


def _wide_roll(lines,lengths,width,height):
    """Tilt from level edges that agree across most of the frame's width (a broken shoreline, field
    edges, a far roofline): (angle, share of level line length), or None. One surface full of parallel
    lines (siding, steps, a table edge seen at an angle) does not reach far enough to count."""
    delta=lines[:,1]-lines[:,0]
    angle=(np.rad2deg(np.arctan2(delta[:,1],delta[:,0]))+90)%180-90
    level=np.abs(angle)<=25
    if level.sum()<3:return None
    values=angle[level];weights=lengths[level];points=lines[level]
    hist,edges=np.histogram(values,np.arange(-25.25,25.75,.5),weights=weights)
    smooth=np.convolve(hist,np.ones(5),mode='same')
    best=None
    for peak in np.argsort(smooth)[::-1][:12]:
        if smooth[peak]<=0:break
        center=(edges[peak]+edges[peak+1])/2
        inliers=np.abs(values-center)<1.5
        if inliers.sum()<3:continue
        x=np.sort(points[inliers][...,0],axis=1)
        covered=np.zeros(64,bool)                 # share of the width under the agreeing segments
        for low,high in x:covered[int(low/width*64):int(np.ceil(high/width*64))]=True
        reach=float(covered.mean());rows=float(np.ptp(points[inliers][...,1].mean(axis=1)))/height
        if reach<.6 or rows<.08:continue          # most of the width, and more than one edge
        score=float(weights[inliers].sum())*reach
        if best is None or score>best[0]:best=(score,inliers)
    if best is None:return None
    return _weighted_median(values[best[1]],weights[best[1]]),float(weights[best[1]].sum()/weights.sum())


def _level(lines,lengths,width,height):
    """(angle, confidence, algorithm) for the Level mode, or AlignmentError.

    Two kinds of evidence are trusted: upright edges, and a horizon (one long boundary, or level edges
    that agree across the frame). When many uprights and a horizon agree the photo is turned by up to
    15 degrees. Either one alone only supports a small turn: uprights lean with the viewpoint (and
    rocks or branches pass for uprights), and a long level boundary is as often a table, a shelf or
    decking seen at an angle as a horizon. Anything less leaves the photo alone: a wrong turn is worse
    than none."""
    plumbs=_plumb_rolls(lines,lengths,width,height)
    try:horizon=_horizon_roll(lines,lengths,width,height)
    except AlignmentError:horizon=None
    wide=_wide_roll(lines,lengths,width,height)
    levels=[(level,name) for level,name in ((horizon,'lines-plumb-horizon-v3'),(wide,'lines-plumb-wide-v3')) if level is not None]
    best=plumbs[0] if plumbs else None
    strong=best is not None and best['count']>=4 and best['heights']>=.5 and best['share']>=.5 and best['centred']
    if strong:
        # Many uprights: they decide. A level edge that disagrees is a surface seen at an angle.
        for level,name in levels:
            if abs(best['angle']-level[0])<=1.5 and abs(level[0])<=15:
                return (best['angle']+level[0])/2,max(level[1],best['share']),name
        if abs(best['angle'])<=2:return best['angle'],best['share'],'lines-plumb-v3'
    else:
        # Few uprights: one of their directions has to agree with the horizon.
        for level,name in levels:
            for plumb in plumbs:
                if abs(plumb['angle']-level[0])<=1.5 and plumb['score']>=.5*best['score'] and abs(level[0])<=6:
                    return (plumb['angle']+level[0])/2,max(level[1],plumb['share']),name
        contradicted=horizon is not None and best is not None and best['count']>=3 and abs(best['angle']-horizon[0])>3
        # A long boundary by itself: a small turn, or a scene that is nothing but that boundary (sea and sky).
        if horizon is not None and not contradicted and (abs(horizon[0])<=4 or horizon[1]>=.7):
            return horizon[0],horizon[1],'lines-horizon-v2'
    raise AlignmentError('믿을 만한 수평·수직 기준선을 찾지 못했습니다. 아래 수평 슬라이더로 조절해 주세요.')


def _vanishing(lines,lengths,vertical):
    if len(lines)<3:raise AlignmentError('원근을 계산할 기준선이 부족합니다.')
    delta=lines[:,1]-lines[:,0]
    p=np.concatenate([lines,np.ones((len(lines),2,1))],axis=2)
    equations=np.cross(p[:,0],p[:,1]);equations/=np.linalg.norm(equations[:,:2],axis=1)[:,None]
    mid=lines.mean(axis=1)
    if np.ptp(mid[:,0 if vertical else 1])<.25:
        raise AlignmentError('화면의 여러 위치에서 기준선이 필요합니다.')
    weights=lengths/lengths.max()
    pairs=np.array([(i,j) for i in range(len(lines)) for j in range(i+1,len(lines))])
    if len(pairs)>500:pairs=pairs[np.random.default_rng(42).choice(len(pairs),500,replace=False)]
    best=None;score=-1
    for i,j in pairs:
        point=np.cross(equations[i],equations[j]);norm=np.linalg.norm(point)
        if norm<1e-9:continue
        point/=norm
        direction=point[:2]-mid*point[2]
        den=np.linalg.norm(direction,axis=1)
        error=np.abs(equations@point)/np.maximum(den,1e-9)
        keep=error<np.sin(np.deg2rad(1.3))
        value=weights[keep].sum()
        if value>score:score=value;best=keep
    if best is None or best.sum()<3 or weights[best].sum()<weights.sum()*.4:
        raise AlignmentError('같은 방향으로 모이는 직선을 찾지 못했습니다.')
    if np.ptp(mid[best,0 if vertical else 1])<.25:
        raise AlignmentError('기준선이 한쪽에 몰려 있어 안정적으로 보정할 수 없습니다.')
    _,_,vh=np.linalg.svd(equations[best]*np.sqrt(weights[best,None]))
    point=vh[-1]
    direction=point[:2]-mid[best]*point[2]
    error=np.abs(equations[best]@point)/np.maximum(np.linalg.norm(direction,axis=1),1e-9)
    if np.median(error)>np.sin(np.deg2rad(1.5)):raise AlignmentError('원근 추정의 오차가 큽니다.')
    return point,int(best.sum()),float(weights[best].sum()/weights.sum())


def _crop_scale(matrix,width,height):
    corners=np.array([[0,0],[width-1,0],[width-1,height-1],[0,height-1]],float)
    center=np.array([(width-1)/2,(height-1)/2])
    inverse=np.linalg.inv(matrix)
    def fits(zoom):
        points=_points(inverse,(corners-center)/zoom+center)
        return bool(np.all(points>=-.001) and np.all(points<=[width-1+.001,height-1+.001]))
    if fits(1):return 1.
    if not fits(2.5):raise AlignmentError('보정 후 잘리는 영역이 너무 큽니다. 수동 원근이나 가이드를 사용해 주세요.')
    lo,hi=1.,2.5
    for _ in range(30):
        mid=(lo+hi)/2
        if fits(mid):hi=mid
        else:lo=mid
    return float(hi*1.001)


def estimate(image,mode='auto'):
    if mode not in ('level','vertical','full','auto'):raise ValueError('알 수 없는 자동 정렬 모드입니다.')
    h,w=image.shape[:2]
    if min(h,w)<40:raise AlignmentError('사진이 너무 작아 자동 정렬을 계산할 수 없습니다.')
    # Display-encoded luminance exposes structural edges in shadow regions.
    from .engine import to_srgb
    rgb=np.clip(to_srgb(image),0,1)
    gray=np.uint8(np.clip(rgb@np.array([.2126,.7152,.0722],np.float32),0,1)*255+.5)
    found=cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(gray)[0]
    if found is None:raise AlignmentError('사진에서 직선 기준을 찾지 못했습니다.')
    lines=found.reshape(-1,2,2).astype(float)
    lengths=np.linalg.norm(lines[:,1]-lines[:,0],axis=1)
    keep=lengths>=max(20,min(h,w)*.06)
    lines=lines[keep];lengths=lengths[keep]
    if len(lines)>160:
        order=np.argsort(lengths)[-160:];lines=lines[order];lengths=lengths[order]
    algorithm='lines-v1'
    if mode=='level':angle,confidence,algorithm=_level(lines,lengths,w,h)
    else:
        try:angle,confidence=_roll(lines,lengths)
        except AlignmentError:
            if mode!='auto':raise
            angle,confidence=_horizon_roll(lines,lengths,w,h);algorithm='lines-horizon-v2'
    center=((w-1)/2,(h-1)/2)
    rotation=np.eye(3);rotation[:2]=cv2.getRotationMatrix2D(center,angle,1)
    scale=max(w,h)/2
    normal=np.array([[1/scale,0,-center[0]/scale],[0,1/scale,-center[1]/scale],[0,0,1]],float)
    leveled=_points(normal@rotation,lines)
    delta=leveled[:,1]-leveled[:,0]
    vertical=np.abs(delta[:,1])>np.abs(delta[:,0])*1.5
    horizontal=np.abs(delta[:,0])>np.abs(delta[:,1])*1.5
    perspective=np.eye(3);support=0;chosen=mode;vertical_fallback=None
    if mode!='level':
        try:
            vp_v,count_v,conf_v=_vanishing(leveled[vertical],lengths[vertical],True)
            if abs(vp_v[1])<.2:raise AlignmentError('수직 소실점을 안정적으로 계산할 수 없습니다.')
            perspective[2,1]=-vp_v[2]/vp_v[1]
            perspective[0,1]=-vp_v[0]/vp_v[1]
            support=count_v;confidence=min(confidence,conf_v);chosen='vertical'
            vertical_fallback=(perspective.copy(),support,confidence)
            if mode in ('full','auto'):
                try:
                    vp_h,count_h,conf_h=_vanishing(leveled[horizontal],lengths[horizontal],False)
                    horizon=np.cross(vp_h,vp_v)
                    if abs(horizon[2])<1e-5:raise AlignmentError('소실선이 사진에 너무 가깝습니다.')
                    projective=np.eye(3);projective[2]=horizon/horizon[2]
                    u=(projective@vp_h)[:2];v=(projective@vp_v)[:2]
                    u/=np.linalg.norm(u);v/=np.linalg.norm(v)
                    if u[0]<0:u=-u
                    if v[1]<0:v=-v
                    axes=np.stack([u,v],axis=1)
                    if abs(np.linalg.det(axes))<.3:raise AlignmentError('두 기준 방향을 분리하기 어렵습니다.')
                    affine=np.eye(3);affine[:2,:2]=np.linalg.inv(axes)
                    perspective=affine@projective
                    support+=count_h;confidence=min(confidence,conf_h);chosen='full'
                except AlignmentError:
                    if mode=='full':raise
        except AlignmentError:
            if mode!='auto':raise
            perspective=np.eye(3);chosen='level'
    matrix=np.linalg.inv(normal)@perspective@normal@rotation
    if mode=='auto' and chosen=='full':
        # Balanced automatic mode preserves composition. The explicit Full
        # mode remains available for deliberate facade rectification.
        try:excessive=_crop_scale(matrix,w,h)>1.4
        except AlignmentError:excessive=True
        if excessive:
            perspective,support,confidence=vertical_fallback;chosen='vertical'
            matrix=np.linalg.inv(normal)@perspective@normal@rotation
    if mode=='auto' and chosen=='vertical':
        try:excessive=_crop_scale(matrix,w,h)>1.4
        except AlignmentError:excessive=True
        if excessive:matrix=rotation;chosen='level';support=0
    corners=np.array([[0,0,1],[w-1,0,1],[w-1,h-1,1],[0,h-1,1]],float)
    denominators=(corners@matrix.T)[:,2]
    if np.any(denominators<.3) or np.any(denominators>3):
        raise AlignmentError('원근 보정이 지나치게 큽니다. 수동 가이드를 사용해 주세요.')
    zoom=_crop_scale(matrix,w,h)
    n=np.diag([1/(w-1),1/(h-1),1])
    return {'matrix':(n@matrix@np.linalg.inv(n)).tolist(),'crop_scale':zoom,'mode':chosen,
        'requested_mode':mode,'rotation':float(angle),'lines':len(lines),'inliers':support,
        'confidence':float(confidence),'algorithm':algorithm}


def apply(image,settings):
    data=settings.get('upright')
    if not data:return image
    matrix=np.asarray(data['matrix'],float)
    if matrix.shape!=(3,3) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix))<1e-8:
        raise ValueError('자동 원근 보정 행렬이 올바르지 않습니다.')
    zoom=float(data.get('crop_scale',1)) if settings.get('upright_crop',True) else 1.
    if not np.isfinite(zoom) or not 1<=zoom<=2.6:raise ValueError('자동 원근 확대 비율이 올바르지 않습니다.')
    h,w=image.shape[:2];n=np.diag([max(1,w-1),max(1,h-1),1])
    matrix=n@matrix@np.linalg.inv(n)
    center=np.array([(w-1)/2,(h-1)/2]);crop=np.array([[zoom,0,center[0]*(1-zoom)],[0,zoom,center[1]*(1-zoom)],[0,0,1]])
    result=cv2.warpPerspective(image,crop@matrix,(w,h),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
    return result[...,None] if result.ndim==2 else result
