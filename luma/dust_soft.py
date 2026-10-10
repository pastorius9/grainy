"""Optional multi-scale soft-spot candidates, always requiring explicit review."""
import math
import cv2
import numpy as np

from .dust import checkpoint,_quantiles,_repeated_detail


def candidates(gray,options,*,cancel=None,progress=None,tile_size=640):
    height,width=gray.shape;spots=[]
    floor=.002+.03*(1-options.sensitivity/100)**2
    # A deliberately optional higher noise threshold trades recall for a
    # shorter review list. Keep the original search exactly when disabled.
    factor=(8-4*options.sensitivity/100) if options.suppress_grain else (5.5-3*options.sensitivity/100)
    # The smallest scale avoids promoting individual film-grain peaks.
    scales=[s for s in (1.4,2.2,3.5,5.5,8.5,13.5,21.,34.) if options.minimum<=4*s<=options.maximum]
    # Float Gaussian kernels extend about four sigma. Include both convolutions
    # in the local response/noise estimate and the optional template search.
    kernels={s:cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*max(1,round(s))+1,)*2) for s in scales}
    geometry={}
    for sigma in scales:
        r=2.2*sigma+1;pad=math.ceil(r*2.3)
        offsets=np.arange(-pad,pad+1);distance=offsets[:,None]**2+offsets[None,:]**2
        geometry[sigma]=(r,pad,(distance>(r*1.3)**2)&(distance<(r*2.2)**2))
    completed=0;total=math.ceil(height/tile_size)*math.ceil(width/tile_size)
    for top in range(0,height,tile_size):
        for left in range(0,width,tile_size):
            checkpoint(cancel)
            for sigma in scales:
                checkpoint(cancel)
                # Small scales need only their own convolution/review halo.
                # Do not filter the largest scale's overlap on every pass.
                halo=math.ceil((28 if options.reduce_patterns else 24)*sigma)+24
                y0=max(0,top-halo);x0=max(0,left-halo)
                part=gray[y0:min(height,top+tile_size+halo),x0:min(width,left+tile_size+halo)]
                smooth=cv2.GaussianBlur(part,(0,0),sigma)
                difference=smooth-cv2.GaussianBlur(part,(0,0),sigma*1.6)
                local_noise=cv2.GaussianBlur(np.abs(difference),(0,0),sigma*4)*1.253314
                kernel=kernels[sigma];r,pad,ring_mask=geometry[sigma]
                for kind in (('dark','bright') if options.polarity=='both' else (options.polarity,)):
                    response=difference if kind=='bright' else -difference
                    threshold=np.maximum(floor,local_noise*factor)
                    peaks=(response>threshold)&(response>=cv2.dilate(response,kernel))
                    ys,xs=np.nonzero(peaks)
                    for index,(y,x) in enumerate(zip(ys,xs)):
                        if index%128==0:checkpoint(cancel)
                        gx=int(x)+x0;gy=int(y)+y0
                        if not left<=gx<min(width,left+tile_size) or not top<=gy<min(height,top+tile_size):continue
                        if min(gx,gy,width-1-gx,height-1-gy)<pad or min(x,y,part.shape[1]-1-x,part.shape[0]-1-y)<pad:continue
                        step=max(1,round(sigma*.5));peak=float(response[y,x])
                        xx=float(response[y,x+step]+response[y,x-step]-2*peak)
                        yy=float(response[y+step,x]+response[y-step,x]-2*peak)
                        xy=float(response[y+step,x+step]-response[y-step,x+step]-response[y+step,x-step]+response[y-step,x-step])/4
                        eigen=np.linalg.eigvalsh([[xx,xy],[xy,yy]])
                        if eigen[1]>=0 or eigen[1]/eigen[0]<.35:continue
                        ring=np.abs(difference[y-pad:y+pad+1,x-pad:x+pad+1])[ring_mask]
                        if peak<max(floor,float(_quantiles(ring,.8)[0])*2.5):continue
                        if options.reduce_patterns:
                            size=math.ceil(r)
                            if _repeated_detail(smooth,int(x)-size,int(y)-size,size*2+1,size*2+1,reduce_patterns=True):continue
                        spots.append(dict(x=gx/max(1,width-1),y=gy/max(1,height-1),rx=r/width,ry=r/height,
                            polarity=kind,review=True,detail_kind='soft',diameter=round(4*sigma,1),contrast=round(peak,5),
                            score=round(peak/max(floor,float(local_noise[y,x])),4)))
            completed+=1
            if progress is not None:progress(completed,total)
    checkpoint(cancel)
    return spots


def merge(existing,additional,width,height,maximum,*,cancel=None):
    """Keep every prior candidate; suppress only redundant new hypotheses."""
    kept=list(existing);bins={}
    def cell(spot):return int(spot['x']*(width-1)//maximum),int(spot['y']*(height-1)//maximum)
    for spot in existing:bins.setdefault(cell(spot),[]).append(spot)
    for spot in sorted(additional,key=lambda s:(-s['score'],s['y'],s['x'],s['polarity'])):
        checkpoint(cancel);x=spot['x']*(width-1);y=spot['y']*(height-1);cx,cy=cell(spot)
        neighbors=[s for dy in (-1,0,1) for dx in (-1,0,1) for s in bins.get((cx+dx,cy+dy),[])]
        if any(s['polarity']==spot['polarity'] and (x-s['x']*(width-1))**2+(y-s['y']*(height-1))**2<=max(3,min(s['rx'],spot['rx'])*width*.5)**2 for s in neighbors):continue
        kept.append(spot);bins.setdefault((cx,cy),[]).append(spot)
    return kept
