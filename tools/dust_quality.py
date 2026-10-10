"""Compare dust detectors on fixed, independently injected reference scenes.

The development set includes the four building spots and generated textures.
Use --fresh for separate seeds after fixing the algorithm for a validation run.
Clean synthetic scenes are known dust-free. Real-scene unmatched candidates
are unlabelled (not a measured false-positive count).
"""
from pathlib import Path
import sys,json,hashlib,importlib.util,time
from dataclasses import asdict
import numpy as np
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module


def backgrounds():
    h,w=520,760;y,x=np.mgrid[:h,:w].astype(np.float32)
    gray=np.full((h,w),.52,np.float32)
    gradient=.28+.3*x/w+.16*y/h
    rng=np.random.default_rng(20260923)
    grain=gradient+rng.normal(0,.018,(h,w)).astype(np.float32)
    edge=gradient+.22*(x>300+y*.22)
    cv2.line(edge,(90,70),(660,430),.16,4)
    texture=gradient+.06*np.sin(x*.48)*np.sin(y*.31)+rng.normal(0,.006,(h,w)).astype(np.float32)
    geometry=gradient.copy()
    for start in range(70,730,80):cv2.line(geometry,(start,50),(start-40,470),.14,3)
    for start in range(70,500,70):cv2.line(geometry,(50,start),(700,start),.84,3)
    return dict(flat=gray,gradient=gradient,grain=grain,edges=edge,texture=texture,lines=geometry)


def inject(clean,seed,count=24):
    rng=np.random.default_rng(seed);h,w=clean.shape;dirty=clean.copy();truth=[]
    attempts=0
    while len(truth)<count and attempts<count*100:
        attempts+=1;x=int(rng.integers(50,w-50));y=int(rng.integers(50,h-50))
        if any((x-t['x'])**2+(y-t['y'])**2<45**2 for t in truth):continue
        index=len(truth);radius=int(rng.integers(3,10));kind='dark' if index%2==0 else 'bright'
        soft=index%3==0;pad=radius*3 if soft else radius+2
        yy,xx=np.mgrid[-pad:pad+1,-pad:pad+1]
        alpha=np.exp(-(xx*xx+yy*yy)/(2*(radius*.55)**2)) if soft else ((xx/radius)**2+(yy/max(2,radius*.7))**2<=1).astype(float)
        amount=(.14 if soft else .25)*(-1 if kind=='dark' else 1)
        patch=dirty[y-pad:y+pad+1,x-pad:x+pad+1]
        patch[:]=np.clip(patch+alpha*amount,0,1)
        truth.append(dict(x=x,y=y,radius=radius,polarity=kind,soft=soft))
    assert len(truth)==count
    return dirty,truth


def score(result,truth):
    spots=result['spots'];h,w=result['height'],result['width'];used=set();matches=[]
    for t in truth:
        possible=[(np.hypot(s['x']*(w-1)-t['x'],s['y']*(h-1)-t['y']),i) for i,s in enumerate(spots)
            if i not in used and s['polarity']==t['polarity']]
        distance,index=min(possible,default=(float('inf'),-1))
        found=bool(distance<=max(3.,t['radius']*.7))
        if found:used.add(index)
        matches.append(found)
    return dict(injected=len(truth),matched=sum(matches),missed=sum(not x for x in matches),
        extra=len(spots)-len(used),total=result['total'],seconds=result['seconds'],matches=matches)


def main():
    from luma import dust
    folder=ROOT/'validation/dust-0.5.23';folder.mkdir(exist_ok=True)
    baseline=load_module(folder/'baseline_dust.py','dust_baseline_22')
    fresh='--fresh' in sys.argv;scenes=backgrounds();results={};unchanged={}
    for index,(name,clean) in enumerate(scenes.items()):
        dirty,truth=inject(clean,(18100 if fresh else 8100)+index)
        row={};candidates={}
        for label,module,options in [('before',baseline,baseline.Options()),('fast_after',dust,dust.Options()),('detailed_after',dust,dust.Options(detailed=True))]:
            result=module.detect(dirty,options);row[label]=score(result,truth)
            candidates[label]=[{k:v for k,v in s.items() if k!='review'} for s in result['spots']]
            row[label]['ordinary_matched']=score({**result,'spots':[s for s in result['spots'] if not s.get('review')]},truth)['matched']
            cleaned=module.detect(clean,options);row[label]['clean_candidates']=cleaned['total']
            row[label]['clean_ordinary_candidates']=sum(not s.get('review') for s in cleaned['spots'])
        unchanged[name]=candidates['before']==candidates['fast_after']
        assert unchanged[name],name
        results[name]=row
    from luma.engine import load_image,to_srgb
    image=to_srgb(load_image(ROOT/'validation/building.jpg')[0]);clean=cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
    dirty,truth=inject(clean,191502 if fresh else 91502)
    results['building']={label:score(module.detect(dirty,options),truth) for label,module,options in [('before',baseline,baseline.Options()),('fast_after',dust,dust.Options()),('detailed_after',dust,dust.Options(detailed=True))]}
    report=dict(settings=dict(fast=asdict(dust.Options()),detailed=asdict(dust.Options(detailed=True))),scenes=results,
        fast_candidate_values_unchanged=unchanged,validation_seeds='fresh' if fresh else 'development',
        baseline_sha256=hashlib.sha256((folder/'baseline_dust.py').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256((ROOT/'luma/dust.py').read_bytes()).hexdigest(),
        interpretation='Synthetic clean_candidates are false detections. Building extras are unlabelled. Injections do not establish real dust accuracy.')
    (folder/('quality-fresh.json' if fresh else 'quality.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({name:{label:{k:v for k,v in row.items() if k!='matches'} for label,row in rows.items()} for name,rows in results.items()},indent=2))


if __name__=='__main__':main()
