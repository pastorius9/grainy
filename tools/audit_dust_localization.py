"""Complement the unchanged centre-match score with actual mask coverage.

Never relabel these injected scenes as a real-dust ground truth dataset.
"""
from pathlib import Path
import sys,json,hashlib,math
import numpy as np
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dust_quality import load_module,inject,score
from diagnose_dust import sources
from luma.engine import load_image,to_srgb


def footprint(spot,truth,width,height):
    x=spot['x']*max(1,width-1);y=spot['y']*max(1,height-1)
    rx=max(.75,spot['rx']*width);ry=max(.75,spot['ry']*height)
    tx,ty,r=truth['x'],truth['y'],truth['radius']
    a=r*.55*math.sqrt(-2*math.log(.2)) if truth['soft'] else r
    b=a if truth['soft'] else max(2,r*.7)
    left=max(0,math.floor(min(x-rx,tx-a)));right=min(width,math.ceil(max(x+rx,tx+a))+1)
    top=max(0,math.floor(min(y-ry,ty-b)));bottom=min(height,math.ceil(max(y+ry,ty+b))+1)
    yy,xx=np.mgrid[top:bottom,left:right]
    expected=((xx-tx)/a)**2+((yy-ty)/b)**2<=1
    actual=((xx-x)/rx)**2+((yy-y)/ry)**2<=1
    intersection=int(np.count_nonzero(actual&expected));truth_area=int(np.count_nonzero(expected));actual_area=int(np.count_nonzero(actual))
    return dict(coverage=intersection/max(1,truth_area),iou=intersection/max(1,truth_area+actual_area-intersection),
                area_ratio=actual_area/max(1,truth_area),center_distance=math.hypot(x-tx,y-ty))


def evaluate(result,truth,clean,dirty):
    h,w=clean.shape;center_score=score(result,truth);rows=[]
    for index,t in enumerate(truth):
        candidates=[]
        for j,s in enumerate(result['spots']):
            if s['polarity']!=t['polarity']:continue
            if abs(s['x']*(w-1)-t['x'])>s['rx']*w+t['radius'] or abs(s['y']*(h-1)-t['y'])>s['ry']*h+t['radius']:continue
            overlap=footprint(s,t,w,h);candidates.append(dict(candidate=j,**overlap))
        best=max(candidates,key=lambda r:r['iou'],default=dict(candidate=None,coverage=0.,iou=0.,area_ratio=0.,center_distance=None))
        x,y,r=t['x'],t['y'],t['radius'];difference=dirty[y-r*3:y+r*3+1,x-r*3:x+r*3+1]-clean[y-r*3:y+r*3+1,x-r*3:x+r*3+1]
        rows.append(dict(truth=t,center_matched=center_score['matches'][index],peak_injected_change=float(np.max(np.abs(difference))),
                         best=best,adequate_footprint=best['coverage']>=.8 and best['iou']>=.2))
    return dict(center_matched=center_score['matched'],injected=len(truth),adequate_footprint=sum(r['adequate_footprint'] for r in rows),
                peak_change_below_003=sum(r['peak_injected_change']<.03 for r in rows),
                center_miss_but_adequate=sum(not r['center_matched'] and r['adequate_footprint'] for r in rows),rows=rows)


def main():
    folder=ROOT/'validation/dust-0.5.26';baseline=load_module(folder/'baseline_dust.py','audit_baseline')
    prototype=load_module(folder/'dog_v2.py','audit_prototype')
    report=dict(criteria='Original centre score unchanged. Complement: at least 80% of injected core covered and intersection-over-union at least 0.2. Gaussian core alpha>=0.2.',sources=[])
    # An exactly coincident hard ellipse must have identical raster masks.
    test=dict(x=50,y=50,radius=5,soft=False)
    identical=footprint(dict(x=50/99,y=50/99,rx=.05,ry=.035),test,100,100)
    assert identical['iou']==identical['coverage']==identical['area_ratio']==1
    for index,path in enumerate(sources()):
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        clean=cv2.cvtColor(to_srgb(load_image(path)[0]),cv2.COLOR_RGB2GRAY);dirty,truth=inject(clean,33150+index,40)
        row=dict(source_sha256=digest,results={})
        for label,module in [('before',baseline),('prototype',prototype)]:
            result=module.detect(dirty,module.Options(detailed=True),limit=5000)
            row['results'][label]=evaluate(result,truth,clean,dirty)
            print(index+1,label,{k:v for k,v in row['results'][label].items() if k!='rows'},flush=True)
        report['sources'].append(row)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
        (folder/'localization-audit.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    report.update(status='passed',original_sources_preserved=True)
    (folder/'localization-audit.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':main()
