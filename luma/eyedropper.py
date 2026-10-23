"""White balance eyedropper over an area: which pixels of a dragged rectangle are averaged."""
import numpy as np

CLIPPED=.98       # a pixel with a channel this close to full has lost its colour (as in workflow_data.automatic_white_balance)


def mean_colour(pixels,floor=.005):
    """The average of (n,3) or (h,w,3) pixels without the clipped ones (a channel at full) and the black ones (a channel
    at or under `floor`), or None when none is left."""
    flat=np.asarray(pixels,np.float32).reshape(-1,3)
    keep=np.isfinite(flat).all(axis=1)&(flat.max(axis=1)<CLIPPED)&(flat.min(axis=1)>floor)
    return flat[keep].mean(axis=0,dtype=np.float64) if keep.any() else None


def area_block(frame,area):
    """The pixels of a (h,w,3) frame inside a rectangle given as fractions of it (x0,y0,x1,y1): at least one pixel."""
    h,w=frame.shape[:2];x0,y0,x1,y1=np.clip(area,0,1)
    left,top=min(w-1,int(x0*w)),min(h-1,int(y0*h))
    return frame[top:max(top+1,min(h,int(np.ceil(y1*h)))),left:max(left+1,min(w,int(np.ceil(x1*w))))]
