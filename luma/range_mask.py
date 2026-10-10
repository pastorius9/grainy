"""Manual range selections sampled from the exact input to a local layer."""
from copy import deepcopy
import numpy as np


def color_coverage(image,samples,tolerance):
    values=np.asarray(samples,np.float32)
    if values.ndim!=2 or values.shape[1]!=3 or not 1<=len(values)<=5 or not np.isfinite(values).all():
        raise ValueError('색상 범위에는 유효한 색 1–5개가 필요합니다.')
    if not np.isfinite(tolerance) or tolerance<=0:raise ValueError('색상 범위 허용량이 올바르지 않습니다.')
    result=np.zeros(image.shape[:2],np.float32)
    for rgb in values:
        result=np.maximum(result,np.clip(1-np.linalg.norm(image-rgb,axis=-1)/max(.01,tolerance),0,1))
    return result


class _Selected(Exception):
    def __init__(self,result):self.result=result


def sample_selection(source,settings,points,layer=None,*,use_crop=True,original_size=None):
    """Return up to five representative colors and a rectangle's brightness span.

    Coordinates are normalized in the displayed, geometrically transformed image.
    A new layer samples after the last existing layer; an existing or disabled
    layer samples before its own edits. The input and settings remain untouched.
    """
    from .engine import develop,geometry
    positions=np.asarray(points,np.float64)
    if positions.ndim!=2 or positions.shape[1]!=2 or not len(positions) or not np.isfinite(positions).all() or np.any((positions<0)|(positions>1)):
        raise ValueError('사진 안쪽의 점이나 작은 사각형을 선택하세요.')
    s=deepcopy(settings);layers=s.setdefault('masks',[])
    if layer is None:layer=len(layers);layers.append(dict(components=[],adjustments={}))
    if not isinstance(layer,(int,np.integer)) or not 0<=layer<len(layers):raise ValueError('선택한 마스크를 찾을 수 없습니다.')
    def read(index,image):
        if index!=layer:return
        h,w=image.shape[:2];start,end=positions[0],positions[-1]
        x0,x1=sorted(min(w-1,int(p[0]*w)) for p in (start,end))
        y0,y1=sorted(min(h-1,int(p[1]*h)) for p in (start,end))
        # Exclude the empty border introduced by rotation, perspective or optics.
        valid=geometry(np.ones((*source.shape[:2],1),np.float32),s,use_crop)[y0:y1+1,x0:x1+1,0]>.9
        patch=image[y0:y1+1,x0:x1+1]
        if not valid.any():raise ValueError('변형 후 생긴 빈 가장자리 대신 사진 안쪽을 선택하세요.')
        luminance=patch@np.array([.2126,.7152,.0722],np.float32)
        low=float(np.clip(luminance[valid].min(),0,1));high=float(np.clip(luminance[valid].max(),0,1))
        # Bound color selection work to a 64x64 grid, including rectangle edges.
        ys=np.linspace(0,patch.shape[0]-1,min(64,patch.shape[0]),dtype=int)
        xs=np.linspace(0,patch.shape[1]-1,min(64,patch.shape[1]),dtype=int)
        candidates=patch[np.ix_(ys,xs)][valid[np.ix_(ys,xs)]]
        if not len(candidates):candidates=patch[valid][:1]
        distances=np.sum((candidates-candidates.mean(axis=0))**2,axis=1)
        chosen=[candidates[distances.argmin()]];nearest=np.sum((candidates-chosen[0])**2,axis=1)
        while len(chosen)<5 and float(nearest.max())>1e-6:
            rgb=candidates[nearest.argmax()];chosen.append(rgb)
            nearest=np.minimum(nearest,np.sum((candidates-rgb)**2,axis=1))
        raise _Selected(dict(colors=[rgb.tolist() for rgb in chosen],range=[low,high]))
    try:develop(source,s,use_crop,output_space=None,original_size=original_size,on_layer_input=read)
    except _Selected as selected:return selected.result
    raise ValueError('선택한 마스크의 입력을 읽을 수 없습니다.')
