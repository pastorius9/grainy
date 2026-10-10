"""Stage-correct, non-generative colour selections in working RGB."""
import numpy as np
from copy import deepcopy


def weights(image,point,*,hsl=None):
    from .processing import rgb_to_hsl
    h,s,l=rgb_to_hsl(image) if hsl is None else hsl
    rgb=np.asarray(point.get('rgb',[.5,.5,.5]),np.float32)
    if rgb.shape!=(3,) or not np.isfinite(rgb).all():raise ValueError('선택 색상이 올바르지 않습니다.')
    rh,rs,rl=rgb_to_hsl(rgb.reshape(1,1,3))
    widths=np.asarray([point.get('range',.12),point.get('saturation_range',.6),point.get('lightness_range',.5)],np.float32)
    if not np.isfinite(widths).all() or np.any(widths<=0) or np.any(widths>[.5,1,1]):raise ValueError('선택 색상 범위가 올바르지 않습니다.')
    hue=np.ones_like(h) if rs[0,0]<.02 or widths[0]>=.5 else np.clip(1-np.abs((h-float(rh[0,0])+.5)%1-.5)/widths[0],0,1)
    saturation=np.ones_like(s) if widths[1]>=1 else np.clip(1-np.abs(s-float(rs[0,0]))/widths[1],0,1)
    lightness=np.ones_like(l) if widths[2]>=1 else np.clip(1-np.abs(l-float(rl[0,0]))/widths[2],0,1)
    return hue*saturation*lightness


def apply(image,points,on_input=None):
    from .processing import rgb_to_hsl,hsl_to_rgb
    a=image
    for index,point in enumerate(points):
        if on_input is not None:on_input(index,a)
        if point.get('version',1)>=2:
            if not any(point.get(k,0) for k in ('hue','saturation','lightness')):continue
            h,sat,l=rgb_to_hsl(a);w=weights(a,point,hsl=(h,sat,l))
        else:
            # Keep the original version's exact operations and neutral roundtrip.
            h,sat,l=rgb_to_hsl(a)
            reference=np.asarray(point.get('rgb',[.5,.5,.5]),np.float32).reshape(1,1,3)
            rh,rs,rl=rgb_to_hsl(reference)
            width=max(.005,float(point.get('range',.12)))
            w=np.clip(1-np.abs((h-float(rh[0,0])+.5)%1-.5)/width,0,1)
            w*=np.clip(1-np.abs(sat-float(rs[0,0]))/.6,0,1)
        changed=hsl_to_rgb(h+w*point.get('hue',0)/600,sat*(1+w*point.get('saturation',0)/100),l+w*point.get('lightness',0)/200)
        if point.get('version',1)>=2:
            a=np.where((w>0)[...,None],changed,a)
        else:a=changed
    if on_input is not None:on_input(len(points),a)
    return a


class _Found(Exception):
    def __init__(self,value):self.value=value


def _probe(source,settings,layer,index,callback,*,use_crop=True,original_size=None):
    from .engine import develop
    def visit(current_layer,current_index,image,mask,origin,shape):
        if layer==current_layer and index==current_index:raise _Found(callback(image,mask,origin,shape))
    try:develop(source,settings,use_crop,output_space=None,original_size=original_size,on_point=visit)
    except _Found as found:return found.value
    raise ValueError('마스크를 켜고 선택 영역 안쪽의 색을 골라 주세요.')


def sample(source,settings,point,layer=None,*,use_crop=True,original_size=None):
    position=np.asarray(point,np.float64)
    if position.shape!=(2,) or not np.isfinite(position).all() or np.any((position<0)|(position>1)):raise ValueError('사진 안쪽의 색을 골라 주세요.')
    points=settings['point_colors'] if layer is None else settings['masks'][layer].get('adjustments',{}).get('point_colors',[])
    def read(image,mask,origin,shape):
        h,w=shape;x=min(w-1,int(position[0]*w))-origin[0];y=min(h-1,int(position[1]*h))-origin[1]
        if not 0<=y<image.shape[0] or not 0<=x<image.shape[1] or mask is not None and mask[y,x]<=1e-4:
            raise ValueError('선택한 마스크의 영역 안쪽을 클릭해 주세요.')
        return np.clip(image[y,x],0,1).tolist()
    return _probe(source,settings,layer,len(points),read,use_crop=use_crop,original_size=original_size)


def preview(source,settings,target,*,use_crop=True,original_size=None):
    layer,index=target
    points=settings['point_colors'] if layer is None else settings['masks'][layer].get('adjustments',{}).get('point_colors',[])
    point=points[index]
    def read(image,mask,origin,shape):
        if point.get('version',1)>=2:weight=weights(image,point)
        else:
            from .processing import rgb_to_hsl
            h,s,l=rgb_to_hsl(image);rh,rs,rl=rgb_to_hsl(np.asarray(point['rgb'],np.float32).reshape(1,1,3))
            weight=np.clip(1-np.abs((h-float(rh[0,0])+.5)%1-.5)/max(.005,point.get('range',.12)),0,1)
            weight*=np.clip(1-np.abs(s-float(rs[0,0]))/.6,0,1)
        if mask is not None:weight=weight*mask
        result=np.zeros(shape,np.float32);x,y=origin;result[y:y+image.shape[0],x:x+image.shape[1]]=weight
        return result
    return _probe(source,settings,layer,index,read,use_crop=use_crop,original_size=original_size)


def preview_key(settings,target):
    """The selected point's output sliders cannot change its input coverage."""
    from .render_cache import settings_key
    value=deepcopy(settings);layer,index=target
    owner=value if layer is None else value['masks'][layer]['adjustments']
    points=owner['point_colors'];point=points[index]
    owner['point_colors']=points[:index]+[{k:v for k,v in point.items() if k not in ('hue','saturation','lightness')}]
    return settings_key(value)
