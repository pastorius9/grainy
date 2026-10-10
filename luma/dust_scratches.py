"""Local non-AI proposals for thin, elongated bright/dark defects.

Natural wires, twigs and lettering can satisfy the same image measurements.
Every proposal therefore requires review. The footprint follows the measured
component with a union of small discs; its enclosing oval is never repaired.
"""
import math
import cv2
import numpy as np
from .dust import checkpoint,_median,_quantiles
from .dust_shapes import MAX_PARTS,make_spot,paint


def cover(component,distance,x0,y0,*,cancel=None):
    active=np.flatnonzero(component)
    order=active[np.argsort(-distance.ravel()[active],kind='stable')]
    remaining=component.astype(bool);height,width=component.shape;parts=[]
    for index,flat in enumerate(order):
        if index%256==0:checkpoint(cancel)
        y,x=divmod(int(flat),width)
        if not remaining[y,x]:continue
        radius=float(distance[y,x])+1.25
        parts.append((float(x+x0),float(y+y0),radius,radius))
        if len(parts)>MAX_PARTS:return None
        pad=math.ceil(radius);left=max(0,x-pad);top=max(0,y-pad)
        right=min(width,x+pad+1);bottom=min(height,y+pad+1)
        yy,xx=np.ogrid[top:bottom,left:right]
        patch=remaining[top:bottom,left:right];patch[(xx-x)**2+(yy-y)**2<=radius*radius]=False
    return parts


def candidates(gray,options,*,cancel=None,progress=None,tile_size=640):
    height,width=gray.shape;maximum=options.scratch_width
    kinds=('dark','bright') if options.polarity=='both' else (options.polarity,)
    # One byte per pixel per polarity; convolution scratch space stays tiled.
    levels=(1.,2.5)
    masks={(kind,level):np.zeros(gray.shape,np.uint8) for kind in kinds for level in levels}
    diameter=maximum*2+1
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(diameter,diameter))
    halo=maximum*2+32;tiles=math.ceil(height/tile_size)*math.ceil(width/tile_size);done=0
    floor=.004+.08*(1-options.sensitivity/100)**2;noise_factor=6-3*options.sensitivity/100
    for top in range(0,height,tile_size):
        for left in range(0,width,tile_size):
            checkpoint(cancel);y0=max(0,top-halo);x0=max(0,left-halo)
            bottom=min(height,top+tile_size);right=min(width,left+tile_size)
            part=gray[y0:min(height,bottom+halo),x0:min(width,right+halo)]
            smooth=cv2.GaussianBlur(part,(0,0),.65)
            noise=cv2.GaussianBlur(np.abs(part-smooth),(0,0),3)*1.253314
            threshold=np.maximum(floor,noise*noise_factor)
            core=(slice(top-y0,bottom-y0),slice(left-x0,right-x0))
            for kind in kinds:
                checkpoint(cancel)
                response=cv2.morphologyEx(smooth,cv2.MORPH_BLACKHAT if kind=='dark' else cv2.MORPH_TOPHAT,kernel)
                for level in levels:masks[kind,level][top:bottom,left:right]=np.uint8(response[core]>threshold[core]*level)
            done+=1
            # Reserve the last tick until contour review and footprint fitting
            # finish, so the UI never reports complete while still computing.
            if progress:progress(min(done,max(0,tiles-1)),tiles)
    spots=[];minimum_length=max(24,maximum*3)
    accepted={kind:np.zeros(gray.shape,np.uint8) for kind in kinds}
    for (kind,level),mask in masks.items():
        checkpoint(cancel)
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        for ordinal,contour in enumerate(contours):
            if ordinal%64==0:checkpoint(cancel)
            x,y,w,h=cv2.boundingRect(contour)
            if max(w,h)<minimum_length or w*h>4_000_000:continue
            perimeter=cv2.arcLength(contour,True)
            if perimeter<minimum_length*2:continue
            polygon=np.zeros((h,w),np.uint8)
            cv2.drawContours(polygon,[contour-(x,y)],-1,1,-1)
            component=mask[y:y+h,x:x+w]&polygon;area=int(component.sum())
            if area<minimum_length or perimeter*perimeter<4*math.pi*area*3:continue
            # Jagged grain around a compact dot can have a long perimeter.
            # Require an elongated distribution, a thin open curve, or a
            # genuine large interior hole (a looped hair).
            moments=cv2.moments(component,True)
            eigen=np.linalg.eigvalsh([[moments['mu20']/area,moments['mu11']/area],
                                      [moments['mu11']/area,moments['mu02']/area]])
            if eigen[1]<eigen[0]*4:
                outline_area=cv2.contourArea(contour);hull_area=cv2.contourArea(cv2.convexHull(contour))
                if outline_area>max(1,hull_area)*.25:
                    holes,hierarchy=cv2.findContours(component,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
                    largest_hole=max((cv2.contourArea(c) for c,entry in zip(holes,hierarchy[0]) if entry[3]>=0),default=0)
                    if largest_hole<outline_area*.45:continue
            distance=cv2.distanceTransform(np.pad(component,1),(cv2.DIST_L2),5)[1:-1,1:-1]
            thickness=2*float(_quantiles(distance[component>0],.85)[0])
            if thickness>maximum+1 or float(distance.max())>maximum*.65+1:continue
            if area/max(1,thickness)<minimum_length:continue
            pad=maximum*2+4;left=max(0,x-pad);top=max(0,y-pad)
            right=min(width,x+w+pad);bottom=min(height,y+h+pad)
            local=np.zeros((bottom-top,right-left),np.uint8)
            local[y-top:y-top+h,x-left:x-left+w]=component
            outer=cv2.dilate(local,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(maximum*3+1,)*2))
            inner=cv2.dilate(local,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(maximum+1,)*2))
            surrounding=gray[top:bottom,left:right][(outer>0)&(inner==0)]
            if surrounding.size<8:continue
            center=_median(gray[y:y+h,x:x+w][component>0]);background=_median(surrounding)
            contrast=background-center if kind=='dark' else center-background
            p10,p90=_quantiles(surrounding,.1,.9);spread=float(p90-p10)
            if contrast<floor or spread>contrast*2:continue
            # A top/black-hat may return only the narrow rim of a broad object.
            # Check the original intensities around that rim as well, so a
            # jagged compact mark is not misrepresented as a thin filament.
            if options.reduce_patterns:
                patch=gray[top:bottom,left:right]
                support=np.uint8(patch<background-contrast*.5 if kind=='dark' else patch>background+contrast*.5)
                _,labels=cv2.connectedComponents(support,8)
                touching=np.unique(labels[local>0]);touching=touching[touching>0]
                if touching.size:
                    support_distance=cv2.distanceTransform(np.pad(support,1),cv2.DIST_L2,5)[1:-1,1:-1]
                    values=support_distance[np.isin(labels,touching)]
                    if values.size and float(values.max())>maximum*.65+1:continue
            parts=cover(component,distance,x,y,cancel=cancel)
            if not parts:continue
            # Strong cores can separate a filament from weaker adjoining
            # texture. Preserve earlier complete footprints and add only
            # hypotheses whose centres are not already covered.
            centers=np.rint([(part[0],part[1]) for part in parts]).astype(np.intp)
            if np.mean(accepted[kind][centers[:,1],centers[:,0]]>0)>=.95:continue
            parts=[(px,py,min(rx,width/2),min(ry,height/2)) for px,py,rx,ry in parts]
            # A parent candidate can touch the image edge; its individual
            # discs remain bounded source coordinates and clip on rasterize.
            spot=make_spot(parts,gray.shape,polarity=kind,review=True,detail_kind='scratch',
                diameter=round(thickness,1),length=round(area/max(1,thickness),1),
                contrast=round(contrast,5),score=round(contrast/max(.003,spread*.5),4))
            spots.append(spot)
            paint(accepted[kind],parts)
    checkpoint(cancel)
    if progress:progress(tiles,tiles)
    return sorted(spots,key=lambda s:(-s['score'],s['y'],s['x'],s['polarity']))
