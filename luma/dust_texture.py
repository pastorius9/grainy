"""Deterministic local texture transfer for explicitly selected dust masks.

Transport RGB residuals together from an unmasked nearby patch. The smooth
repair supplies large-scale tone; no random grain or external model is used.
The original and smooth sources stay immutable until every tile is prepared.
"""
import cv2
import numpy as np

BLOCK=8
CONTEXT=4
HALO=6
SEARCH=48


def restore(original,smooth,mask,*,cancel=None,stats=None):
    from .dust import checkpoint
    checkpoint(cancel)
    h,w=mask.shape
    active=np.maximum.reduceat(np.maximum.reduceat(mask,np.arange(0,h,BLOCK),axis=0),np.arange(0,w,BLOCK),axis=1)
    rows,columns=np.nonzero(active)
    # Blocks only read original/smooth and queue their writes, so they are prepared on the pixel
    # workers in contiguous chunks; the writes are applied afterwards in the original block order.
    from .pixel_jobs import run_all,WORKERS
    blocks=list(zip(rows,columns));size=max(1,-(-len(blocks)//(WORKERS*4)))
    chunks=[blocks[i:i+size] for i in range(0,len(blocks),size)]
    results=[None]*len(chunks)
    def prepare(index):
        results[index]=_prepare(original,smooth,mask,chunks[index],cancel)
    run_all(lambda index=index:prepare(index) for index in range(len(chunks)))
    pending=[item for result in results for item in result[0]]
    transferred=sum(result[1] for result in results);fallback=sum(result[2] for result in results)
    checkpoint(cancel)
    for y,x,selected,values in pending:
        smooth[y:y+selected.shape[0],x:x+selected.shape[1]][selected]=values
    if stats is not None:stats.update(tiles=len(rows),transferred_pixels=transferred,fallback_pixels=fallback)
    return smooth


def _prepare(original,smooth,mask,blocks,cancel):
    from .dust import checkpoint
    h,w=mask.shape;pending=[];transferred=0;fallback=0
    for row,column in blocks:
        checkpoint(cancel)
        y=int(row)*BLOCK;x=int(column)*BLOCK;bottom=min(h,y+BLOCK);right=min(w,x+BLOCK)
        top=max(0,y-CONTEXT);left=max(0,x-CONTEXT)
        end_y=min(h,bottom+CONTEXT);end_x=min(w,right+CONTEXT)
        th=end_y-top;tw=end_x-left
        sy=max(HALO,top-SEARCH);sx=max(HALO,left-SEARCH)
        ey=min(h-HALO,end_y+SEARCH);ex=min(w-HALO,end_x+SEARCH)
        selected=mask[y:bottom,x:right]>0
        if ey-sy<th or ex-sx<tw:
            fallback+=int(selected.sum());continue
        source=original[sy-HALO:ey+HALO,sx-HALO:ex+HALO]
        source_low=cv2.GaussianBlur(source,(HALO*2+1,HALO*2+1),1.5)[HALO:-HALO,HALO:-HALO]
        local=smooth[max(0,top-HALO):min(h,end_y+HALO),max(0,left-HALO):min(w,end_x+HALO)]
        low=cv2.GaussianBlur(local,(HALO*2+1,HALO*2+1),1.5)
        ty=top-max(0,top-HALO);tx=left-max(0,left-HALO)
        target_low=low[ty:ty+th,tx:tx+tw]
        count=th*tw
        score=cv2.matchTemplate(source_low,target_low,cv2.TM_SQDIFF)/(count*3)
        means=cv2.boxFilter(source_low,-1,(tw,th),anchor=(0,0),normalize=True)[:score.shape[0],:score.shape[1]]
        offset=means-target_low.mean(axis=(0,1))
        # Mostly compare shape while retaining a small penalty for tone shifts.
        score-=np.mean(offset*offset,axis=-1)*.9
        yy,xx=np.ogrid[:score.shape[0],:score.shape[1]]
        score+=np.float32(1e-6)*((xx+sx-left)**2+(yy+sy-top)**2)/(SEARCH*SEARCH)
        # Reject ALL selected marks, including nearby non-intersecting groups,
        # and the Gaussian support around the entire source template.
        occupied=(mask[sy-HALO:ey+HALO,sx-HALO:ex+HALO]>0).astype(np.uint8)
        integral=cv2.integral(occupied,sdepth=cv2.CV_32S)
        kh=th+HALO*2;kw=tw+HALO*2
        used=integral[kh:,kw:]-integral[:-kh,kw:]-integral[kh:,:-kw]+integral[:-kh,:-kw]
        score[used>0]=np.inf
        position=int(np.argmin(score));dy,dx=divmod(position,score.shape[1])
        if not np.isfinite(score[dy,dx]):
            fallback+=int(selected.sum());continue
        by=y-top;bx=x-left;bh=bottom-y;bw=right-x
        donor=original[sy+dy+by:sy+dy+by+bh,sx+dx+bx:sx+dx+bx+bw]
        donor_low=source_low[dy+by:dy+by+bh,dx+bx:dx+bx+bw]
        value=np.maximum(target_low[by:by+bh,bx:bx+bw]+donor-donor_low,0)
        pending.append((y,x,selected,value[selected].copy()))
        transferred+=int(selected.sum())
    return pending,transferred,fallback
