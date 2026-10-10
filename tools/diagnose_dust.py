"""Explain candidate rejection near known local injections, without photo edits."""
from pathlib import Path
import ast,sys,sqlite3,hashlib,json,types
from collections import Counter
import numpy as np
import cv2
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dust_quality import inject,score
from luma.engine import load_image,to_srgb


def sources(count=3):
    db=sqlite3.connect((ROOT/'data/catalog.sqlite').as_uri()+'?mode=ro',uri=True)
    paths=[Path(r[0]) for r in db.execute('SELECT DISTINCT path FROM photos ORDER BY path')];db.close()
    found=[]
    for index in np.random.default_rng(41251).permutation(len(paths)):
        path=paths[index]
        if path.suffix.lower() not in ('.jpg','.jpeg') or not path.is_file():continue
        with Image.open(path) as header:w,h=header.size
        if 3_000_000<=w*h<=13_000_000:found.append(path)
        if len(found)==count:return found
    raise ValueError('Not enough eligible local sources')


def main():
    folder=ROOT/'validation/dust-0.5.26';folder.mkdir(exist_ok=True)
    baseline=folder/'baseline_dust.py'
    if not baseline.exists():baseline.write_bytes((ROOT/'luma/dust.py').read_bytes())
    text=baseline.read_text(encoding='utf-8');lines=text.splitlines();tree=ast.parse(text)
    class Trace(ast.NodeTransformer):
        active=False
        def visit_For(self,node):
            previous=self.active
            if isinstance(node.target,ast.Name) and node.target.id=='index' and isinstance(node.iter,ast.Call) and isinstance(node.iter.func,ast.Name) and node.iter.func.id=='range':self.active=True
            node=self.generic_visit(node);self.active=previous;return node
        def visit_Continue(self,node):
            if not self.active:return node
            return [ast.copy_location(ast.Expr(ast.Call(ast.Name('_trace',ast.Load()),[ast.Call(ast.Name('locals',ast.Load()),[],[]),ast.Constant(node.lineno)],[])),node),node]
    tree=Trace().visit(tree);ast.fix_missing_locations(tree)
    module=types.ModuleType('dust_diagnostic_25');sys.modules[module.__name__]=module
    exec(compile(tree,str(baseline),'exec'),module.__dict__)
    expected=json.loads((ROOT/'validation/dust-0.5.24/final-report.json').read_text())
    report={'sources':[],'baseline_sha256':hashlib.sha256(baseline.read_bytes()).hexdigest()}
    for source_index,path in enumerate(sources()):
        if source_index!=1:continue
        digest=hashlib.sha256(path.read_bytes()).hexdigest();assert digest==expected['expanded_library'][source_index]['source_sha256']
        clean=cv2.cvtColor(to_srgb(load_image(path)[0]),cv2.COLOR_RGB2GRAY);dirty,truth=inject(clean,33150+source_index,40)
        reasons=[Counter() for _ in truth];examples=[{} for _ in truth]
        def trace(loc,line):
            if not all(k in loc for k in ('x','y','w','h','x0','y0','kind','area')):return
            gx=loc['x']+loc['x0']+(loc['w']-1)/2;gy=loc['y']+loc['y0']+(loc['h']-1)/2
            for index,t in enumerate(truth):
                if t['polarity']!=loc['kind'] or (gx-t['x'])**2+(gy-t['y'])**2>max(3,t['radius']*.7)**2:continue
                reason=lines[line-1].strip();reasons[index][reason]+=1
                if reason not in examples[index]:
                    examples[index][reason]={k:float(loc[k]) for k in ('w','h','area','diameter','cutoff','noise','contrast','spread','core_response','background_response') if k in loc and np.isscalar(loc[k])}
        module._trace=trace
        result=module.detect(dirty,module.Options(detailed=True),limit=5000);scored=score(result,truth)
        assert scored['matches']==expected['expanded_library'][source_index]['default_expanded']['matches']
        rows=[dict(truth=t,matched=scored['matches'][i],rejections=dict(reasons[i]),examples=examples[i]) for i,t in enumerate(truth)]
        report['sources'].append(dict(source_sha256=digest,shape=list(clean.shape),score=scored,diagnostics=rows))
        misses=[(i,t) for i,t in enumerate(truth) if not scored['matches'][i]]
        montage=Image.new('RGB',(4*340,((len(misses)+3)//4)*200),'#17191d');draw=ImageDraw.Draw(montage)
        for cell,(i,t) in enumerate(misses):
            x,y=t['x'],t['y'];left=(cell%4)*340;top=(cell//4)*200
            draw.text((left+5,top+3),f'{i}: {t["polarity"]}, r={t["radius"]}, soft={t["soft"]}',fill='white')
            for c,a in enumerate((clean,dirty)):
                patch=Image.fromarray(np.uint8(np.clip(a[y-32:y+32,x-32:x+32],0,1)*255)).convert('RGB').resize((164,164))
                montage.paste(patch,(left+c*170,top+25))
        montage.save(folder/'missed-pairs.png')
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
        print(json.dumps({'source':source_index+1,'matched':scored['matched'],'misses':[{k:r[k] for k in ('truth','rejections')} for r in rows if not r['matched']]},indent=2),flush=True)
    (folder/'diagnostics.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':main()
