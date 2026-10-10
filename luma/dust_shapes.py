"""A reviewed filament is a bounded union of small source-coordinate ellipses.

Keeping the narrow footprint, rather than its enclosing oval, avoids removing
the clean interior of a curved hair. Scale changes thickness around each part.
"""
import math
import numpy as np

MAX_PARTS=2048


def ellipses(spot,shape):
    from .dust import spot_bounds
    parent=spot_bounds(spot,shape)
    if 'parts' not in spot:return [parent]
    parts=spot['parts']
    if not isinstance(parts,list) or not 1<=len(parts)<=MAX_PARTS:raise ValueError('흠집의 제거 범위가 올바르지 않습니다.')
    result=[]
    for part in parts:
        if not isinstance(part,dict) or set(part)!={'x','y','rx','ry'}:raise ValueError('흠집의 좌표가 올바르지 않습니다.')
        result.append(spot_bounds({**part,'scale':spot.get('scale',100)},shape))
    return result


def bounds(parts,shape):
    h,w=shape[:2]
    return (max(0,math.floor(min(x-rx for x,y,rx,ry in parts))-1),
        max(0,math.floor(min(y-ry for x,y,rx,ry in parts))-1),
        min(w,math.ceil(max(x+rx for x,y,rx,ry in parts))+2),
        min(h,math.ceil(max(y+ry for x,y,rx,ry in parts))+2))


def paint(mask,parts,x0=0,y0=0):
    """Rasterize only each ellipse's small rectangle, with the legacy rule."""
    h,w=mask.shape
    for x,y,rx,ry in parts:
        left=max(x0,math.floor(x-rx));top=max(y0,math.floor(y-ry))
        right=min(x0+w,math.ceil(x+rx)+1);bottom=min(y0+h,math.ceil(y+ry)+1)
        if left>=right or top>=bottom:continue
        yy,xx=np.ogrid[top:bottom,left:right]
        patch=mask[top-y0:bottom-y0,left-x0:right-x0]
        patch[((xx-x)/rx)**2+((yy-y)/ry)**2<=1]=255


def make_spot(parts,shape,**metadata):
    """Parts use source pixel coordinates; store only normalized geometry."""
    h,w=shape[:2]
    if not 1<=len(parts)<=MAX_PARTS:raise ValueError('흠집이 너무 깁니다. 나누어 표시하거나 붓 크기를 늘려 주세요.')
    left,top,right,bottom=bounds(parts,shape)
    cx=(left+right-1)/2;cy=(top+bottom-1)/2
    result=dict(x=cx/max(1,w-1),y=cy/max(1,h-1),rx=min(.5,max(.75,(right-left)/2)/w),
        ry=min(.5,max(.75,(bottom-top)/2)/h),
        parts=[dict(x=x/max(1,w-1),y=y/max(1,h-1),rx=rx/w,ry=ry/h) for x,y,rx,ry in parts],**metadata)
    ellipses(result,shape)
    return result


def manual(points,shape,diameter):
    h,w=shape[:2];radius=min(float(diameter)/2,min(h,w)/2)
    if not .5<=radius<=80:raise ValueError('흠집 붓 크기가 올바르지 않습니다.')
    points=np.asarray(points,np.float64)
    if points.ndim!=2 or points.shape[1]!=2 or not 1<=len(points)<=8192 or not np.isfinite(points).all() or np.any((points<0)|(points>1)):
        raise ValueError('표시한 흠집 경로가 올바르지 않습니다.')
    pixels=points*[max(1,w-1),max(1,h-1)];centers=[pixels[0]]
    for a,b in zip(pixels[:-1],pixels[1:]):
        count=max(1,math.ceil(float(np.linalg.norm(b-a))/max(.5,radius*.75)))
        if len(centers)+count>MAX_PARTS:raise ValueError('흠집이 너무 깁니다. 나누어 표시하거나 붓 크기를 늘려 주세요.')
        centers.extend(a+(b-a)*(i/count) for i in range(1,count+1))
    return make_spot([(float(x),float(y),radius,radius) for x,y in centers],shape,
        manual=True,polarity='manual',diameter=radius*2,score=0,detail_kind='scratch')
