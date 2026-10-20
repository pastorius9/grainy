"""Developed previews and contact sheets for the AI connection (MCP). No Qt; any thread.

Never uses the window's render caches (live_cache, quality_cache): they are not thread-safe.
"""
import io
import math
import numpy as np
from PIL import Image,ImageDraw,ImageFont

BACKGROUND=(32,32,32)          # #202020
LABEL_HEIGHT=18
MAX_COLUMNS=6


def render_preview(record,longest,directory):
    """HxWx3 uint8 sRGB of the photo with its current edits, long side <= longest (reference_view.render_photo)."""
    from .engine import load_image,develop,output_rgb,resize_float
    from . import file_access,preview_store
    s=record['settings']
    if file_access.present(record['path']):
        source,info=load_image(record['path'],min(4096,max(1024,2*int(longest))),s['working_space'],raw_options=s)
    else:
        source,info=preview_store.load_offline(directory,record['id'],s)
    saved=record.get('info') or {}
    width=info.get('width') or saved.get('width') or source.shape[1]
    height=info.get('height') or saved.get('height') or source.shape[0]
    working=develop(source,s,output_space=None,original_size=(width,height))
    rgb=output_rgb(working,s['working_space'])
    if max(rgb.shape[:2])>longest:rgb=resize_float(np.ascontiguousarray(rgb,dtype=np.float32),int(longest))
    return np.uint8(np.clip(rgb,0,1)*255+.5)


def cached_thumbnail(directory,ident):
    """The library's own thumbnail as uint8 sRGB when it shows the current edits, else None."""
    from . import preview_store
    state=preview_store.inspect(directory,ident)
    image=state.get('image')
    if image is None or state.get('rebuild') or state.get('status') not in ('','오프라인','접근 권한 필요'):return None
    pixels=np.asarray(image.convert('RGB'),dtype=np.uint8)
    if state.get('color_space')=='ProPhoto RGB':
        from .colorio import convert,profile
        converted=convert(np.ascontiguousarray(pixels,dtype=np.float32)/255,profile('ProPhoto RGB'),profile('sRGB'))
        pixels=np.uint8(np.clip(converted,0,1)*255+.5)
    return pixels


def jpeg(array,quality=70,max_bytes=None):
    """JPEG bytes without an ICC profile (the pixels are plain sRGB), smaller until under max_bytes:
    quality down by 10 to 40, then the size by 0.85 each time."""
    image=array if isinstance(array,Image.Image) else Image.fromarray(np.asarray(array,dtype=np.uint8))
    image=image.convert('RGB');quality=int(quality)
    while True:
        stream=io.BytesIO();image.save(stream,format='JPEG',quality=quality,optimize=True);data=stream.getvalue()
        if max_bytes is None or len(data)<=max_bytes or max(image.size)<=16:return data
        if quality>40:quality=max(40,quality-10)
        else:image=image.resize((max(1,round(image.width*.85)),max(1,round(image.height*.85))),Image.Resampling.LANCZOS)


def _font():
    try:return ImageFont.load_default(size=13)
    except TypeError:return ImageFont.load_default()      # Pillow without FreeType


def mark(ident,rating=0,flag=0):
    """The ASCII label of a contact-sheet cell, e.g. '#123  3*  P'."""
    text=f'#{ident}'
    if rating:text+=f'  {int(rating)}*'
    if flag==1:text+='  P'
    elif flag==-1:text+='  X'
    return text


def contact_sheet(cells,cell=160):
    """One image of cells [(id, uint8 array or None, label text)], at most 6 columns; each cell is cell x cell
    with the picture letterboxed on #202020 above an 18 px label strip. None is drawn as 'not ready'."""
    count=max(1,len(cells));columns=min(MAX_COLUMNS,count);rows=math.ceil(count/columns)
    gap=2;sheet=Image.new('RGB',(columns*cell+(columns-1)*gap,rows*cell+(rows-1)*gap),(12,12,12))
    draw=ImageDraw.Draw(sheet);font=_font();area=(cell,cell-LABEL_HEIGHT)
    for index,(ident,pixels,label) in enumerate(cells):
        x=(index%columns)*(cell+gap);y=(index//columns)*(cell+gap)
        draw.rectangle([x,y,x+cell-1,y+cell-1],fill=BACKGROUND)
        if pixels is None:
            draw.rectangle([x+4,y+4,x+cell-5,y+area[1]-4],fill=(70,70,70))
            draw.text((x+cell//2,y+area[1]//2),'not ready',fill=(210,210,210),font=font,anchor='mm')
        else:
            picture=Image.fromarray(np.asarray(pixels,dtype=np.uint8)).convert('RGB')
            picture.thumbnail(area,Image.Resampling.LANCZOS)
            sheet.paste(picture,(x+(area[0]-picture.width)//2,y+(area[1]-picture.height)//2))
        draw.text((x+4,y+area[1]+LABEL_HEIGHT//2),str(label),fill=(235,235,235),font=font,anchor='lm')
    return sheet
