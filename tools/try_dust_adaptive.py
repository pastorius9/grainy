"""Development comparison. Frozen source and result evidence per prototype."""
from pathlib import Path
import sys,json,hashlib
import numpy as np
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dust_quality import load_module,inject,score,backgrounds
from diagnose_dust import sources
from luma.engine import load_image,to_srgb


def main():
    name=sys.argv[1] if len(sys.argv)>1 else 'adaptive_v1'
    folder=ROOT/'validation/dust-0.5.26';baseline=load_module(folder/'baseline_dust.py','dust_old')
    candidate=load_module(folder/f'{name}.py','dust_new');modules=[('before',baseline),('after',candidate)]
    report=dict(source_sha256=hashlib.sha256((folder/f'{name}.py').read_bytes()).hexdigest(),synthetic={},library=[])
    def save():(folder/f'{name}.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    for index,(label,clean) in enumerate(backgrounds().items()):
        dirty,truth=inject(clean,38100+index);row={}
        for key,module in modules:
            result=module.detect(dirty,module.Options(detailed=True),limit=5000)
            row[key]=score(result,truth);row[key]['clean_candidates']=module.detect(clean,module.Options(detailed=True),limit=5000)['total']
        report['synthetic'][label]=row;save()
        print(label,[(k,v['matched'],v['extra'],v['clean_candidates']) for k,v in row.items()],flush=True)
    expected=json.loads((ROOT/'validation/dust-0.5.24/final-report.json').read_text())
    for index,path in enumerate(sources()):
        digest=hashlib.sha256(path.read_bytes()).hexdigest();assert digest==expected['expanded_library'][index]['source_sha256']
        clean=cv2.cvtColor(to_srgb(load_image(path)[0]),cv2.COLOR_RGB2GRAY);dirty,truth=inject(clean,33150+index,40)
        row=dict(source_sha256=digest,results={})
        for key,module in modules:
            result=module.detect(dirty,module.Options(detailed=True),limit=5000);row['results'][key]=score(result,truth)
            print('Photo',index+1,key,row['results'][key]['matched'],result['total'],round(result['seconds'],2),flush=True)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
        report['library'].append(row);save()
    report['status']='complete';save()


if __name__=='__main__':main()
