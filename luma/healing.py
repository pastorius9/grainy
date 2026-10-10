"""Local donor selection and boundary-matched, non-generative healing.

Only new version-2 strokes use this path. Old edits keep their original pixels.
"""
import cv2
import numpy as np


def automatic_offset(image, mask):
    """Find a nearby clean translation, excluding the entire painted shape.

    Search is bounded to a 512-pixel image and a fixed set of translations.
    The known boundary, not the defect underneath the brush, ranks donors.
    """
    h,w=mask.shape;scale=min(1.,512/max(h,w))
    size=(max(1,round(w*scale)),max(1,round(h*scale)))
    a=cv2.resize(image,size,interpolation=cv2.INTER_AREA) if scale<1 else image
    # Area resize keeps very small marks in the exclusion map.
    marked=cv2.resize(np.float32(mask>0),size,interpolation=cv2.INTER_AREA)>0
    ys,xs=np.where(marked)
    if not len(xs):return None
    hh,ww=marked.shape
    radius=max(2.,float(cv2.distanceTransform(np.uint8(marked),cv2.DIST_L2,3).max()))
    margin=max(2,round(radius*.5))
    ring=(cv2.dilate(np.uint8(marked),np.ones((margin*2+1,margin*2+1),np.uint8))>0)&~marked
    ry,rx=np.where(ring)
    if not len(rx):return None
    step=max(1,len(rx)//512);rx=rx[::step];ry=ry[::step]
    baseline=a[ry,rx];best=None;best_score=float('inf')
    x0,x1=int(xs.min()),int(xs.max())+1;y0,y1=int(ys.min()),int(ys.max())+1
    footprint=marked[y0:y1,x0:x1]
    shifts=set()
    for distance in (radius*3,radius*4.5,radius*7,radius*11,max(hh,ww)*.25,max(hh,ww)*.5):
        for angle in np.linspace(0,2*np.pi,24,endpoint=False):
            shifts.add((round(distance*np.cos(angle)),round(distance*np.sin(angle))))
    # Also allow near-border strokes to find a source beyond their full width.
    for dx,dy in ((x1-x0+margin,0),(0,y1-y0+margin)):
        shifts.update(((dx,dy),(-dx,-dy)))
    for dx,dy in sorted(shifts):
        if x0+dx<0 or x1+dx>ww or y0+dy<0 or y1+dy>hh:continue
        if np.any(marked[y0+dy:y1+dy,x0+dx:x1+dx]&footprint):continue
        sx=rx+dx;sy=ry+dy
        valid=(sx>=0)&(sx<ww)&(sy>=0)&(sy<hh)
        if valid.sum()<max(4,len(rx)*.5):continue
        donor=a[sy[valid],sx[valid]];target=baseline[valid]
        # Match structure as well as average colour. A modest distance penalty
        # breaks ties on flat fields without overpowering boundary similarity.
        delta=donor-target
        score=float(np.mean(delta*delta)+.15*np.mean(np.abs(delta-delta.mean(axis=0))))
        score+=1e-5*(dx*dx+dy*dy)/max(1,ww*ww+hh*hh)
        if score<best_score:best_score=score;best=(dx*w/ww,dy*h/hh)
    return best


def blend_boundary(target, donor, weight):
    """Transfer texture, interpolating colour differences from unpainted edges."""
    binary=np.uint8(weight[...,0]>0)*255
    if not np.any(binary):return donor
    if np.all(binary):return donor
    difference=target-donor
    result=donor.copy()
    for channel in range(3):
        known=np.ascontiguousarray(difference[...,channel]);known[binary>0]=0
        result[...,channel]+=cv2.inpaint(known,binary,3,cv2.INPAINT_NS)
    return result


def inpaint(target, weight):
    binary=np.uint8(weight[...,0]>0)*255
    if np.all(binary):return target.copy()
    return np.stack([cv2.inpaint(np.ascontiguousarray(target[...,c]),binary,3,cv2.INPAINT_NS)
                     for c in range(3)],axis=-1)
