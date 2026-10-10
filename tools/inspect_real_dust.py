"""Local-only unlabelled candidate review sheets; not a ground-truth benchmark."""
from pathlib import Path
import sys,json,hashlib
import numpy as np
import cv2
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from luma.engine import load_image,to_srgb
from luma.dust import detect,Options

out=ROOT/'validation/dust-0.5.28'
for index in (0,3,6):
    row=json.loads((out/'sources-local.json').read_text(encoding='utf-8'))[index]
    path=Path(row['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    rgb=to_srgb(load_image(path)[0]);gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY);h,w=gray.shape
    results={label:detect(gray,options,limit=5000) for label,options in [('ordinary',Options()),('detailed',Options(detailed=True,soft=True))]}
    (out/f'photo-{index}-baseline.json').write_text(json.dumps(dict(source_sha256=row['sha256'],results=results),indent=2),encoding='utf-8')
    im=Image.fromarray(np.uint8(np.clip(rgb,0,1)*255))
    box={0:(200,1300,1400,2000),3:(300,100,1500,800),6:(1000,100,2200,800)}[index]
    im.crop(box).save(out/f'photo-{index}-unmarked.png')
    # Mixed samples across both the ordinary and review-only ranks.
    all_spots=results['detailed']['spots'];sample=list(range(min(12,len(all_spots))))
    additional=[i for i,s in enumerate(all_spots) if s.get('review')]
    sample+=additional[:12]
    sample+=additional[len(additional)//2:len(additional)//2+12]
    sample=list(dict.fromkeys(sample))
    sheet=Image.new('RGB',(6*180,((len(sample)+5)//6)*205),'#1b1d23');draw=ImageDraw.Draw(sheet)
    for cell,i in enumerate(sample):
        s=all_spots[i];x=round(s['x']*(w-1));y=round(s['y']*(h-1));left=(cell%6)*180;top=(cell//6)*205
        patch=im.crop((x-40,y-40,x+40,y+40)).resize((160,160),Image.Resampling.NEAREST)
        sheet.paste(patch,(left+10,top+38));draw.text((left+5,top+3),f'{i}: {s["polarity"]} ({x},{y})',fill='white')
        draw.text((left+5,top+18),'review' if s.get('review') else 'ordinary',fill='orange' if s.get('review') else 'white')
    sheet.save(out/f'photo-{index}-candidates.png')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    print(index,{k:(v['total'],round(v['seconds'],2)) for k,v in results.items()},flush=True)
