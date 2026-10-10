"""Versioned, atomic thumbnails. A catalog token prevents stale worker commits."""
from contextlib import closing
from pathlib import Path
from copy import deepcopy
import hashlib
import io
import json
import os
import sqlite3
import time
from uuid import uuid4
import numpy as np
from PIL import Image

VERSION=4


def connect(directory):
    db=sqlite3.connect(Path(directory)/'catalog.sqlite',timeout=10)
    db.row_factory=sqlite3.Row;return db


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS preview_state(photo_id INTEGER PRIMARY KEY,token TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS preview_added AFTER INSERT ON photos BEGIN
            INSERT OR REPLACE INTO preview_state VALUES(NEW.id,lower(hex(randomblob(16)))); END;
        CREATE TRIGGER IF NOT EXISTS preview_changed AFTER UPDATE OF edits,path ON photos
            WHEN OLD.edits != NEW.edits OR OLD.path != NEW.path BEGIN
            INSERT OR REPLACE INTO preview_state VALUES(NEW.id,lower(hex(randomblob(16)))); END;
        CREATE TRIGGER IF NOT EXISTS preview_removed AFTER DELETE ON photos BEGIN
            DELETE FROM preview_state WHERE photo_id=OLD.id; END;
        INSERT OR IGNORE INTO preview_state SELECT id,lower(hex(randomblob(16))) FROM photos;
    ''')


def signature(path):
    path=Path(path)
    try:
        stat=path.stat()
        return {'path':str(path.resolve()),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns} if path.is_file() else None
    except OSError:return None


def snapshot(directory,ident,full=False):
    columns='p.path,s.token'+(',p.edits,p.metadata' if full else '')
    with closing(connect(directory)) as db:
        db.execute('BEGIN')
        row=db.execute(f'SELECT {columns} FROM photos p JOIN preview_state s ON s.photo_id=p.id WHERE p.id=?',(ident,)).fetchone()
        if row is not None and full:
            from .profile_store import ProfileCodec
            settings=ProfileCodec(db).loads(row['edits'])
    if row is None:return None
    result=dict(row);result['id']=ident
    result['source']=signature(row['path'])
    result['offline']=signature(Path(directory)/'previews'/f'{ident}.npz') if result['source'] is None else None
    if full:
        from .engine import normalized
        result['settings']=normalized(settings);result['info']=json.loads(row['metadata'])
    return result


def files(directory,ident):
    base=Path(directory)/'thumbnails';return base/f'{ident}.jpg',base/f'{ident}.json'


def inspect(directory,ident):
    current=snapshot(directory,ident)
    if current is None:return dict(image=None,rebuild=False,status='카탈로그에서 제거됨',snapshot=None)
    jpeg,stamp=files(directory,ident);payload=None;image=None;valid=False;color_space='sRGB'
    try:
        if jpeg.stat().st_size>4*1024**2:raise ValueError('thumbnail too large')
        payload=jpeg.read_bytes()
        with Image.open(io.BytesIO(payload)) as im:
            if max(im.size)>2048:raise ValueError('thumbnail dimensions')
            image=im.convert('RGB');image.thumbnail((320,240))
        if stamp.stat().st_size>8192:raise ValueError('thumbnail stamp too large')
        meta=json.loads(stamp.read_text(encoding='utf-8'))
        if not isinstance(meta,dict):raise ValueError('thumbnail stamp')
        color_space=meta.get('color_space','sRGB')
        if color_space not in ('sRGB','ProPhoto RGB'):color_space='sRGB';raise ValueError('thumbnail color space')
        valid=(meta['version']==VERSION and meta['token']==current['token'] and meta['path']==current['path']
               and meta['sha256']==hashlib.sha256(payload).hexdigest())
        if current['source'] is not None:valid=valid and meta.get('source')==current['source']
        elif current['offline'] is not None and meta.get('source') is None:valid=valid and meta.get('offline')==current['offline']
        # A current cached render remains useful when the original goes offline.
    except (OSError,ValueError,KeyError,TypeError,Image.DecompressionBombError):valid=False
    missing=current['source'] is None
    rebuild=not valid and (not missing or current['offline'] is not None)
    status=('오프라인' if missing else '') if valid else ('갱신 중' if rebuild else '원본 없음 · 이전 미리보기' if image else '원본 없음')
    return dict(image=image,rebuild=rebuild,status=status,snapshot=current,color_space=color_space)


def load_offline(directory,ident,settings):
    from .rawcolor import CameraSource,enabled
    from .colorio import profile,convert
    path=Path(directory)/'previews'/f'{ident}.npz'
    fingerprint=signature(path)
    with np.load(path,allow_pickle=False) as data:
        source=data['pixels'].astype(np.float32);info=json.loads(str(data['info']))
        if not enabled(settings) and 'legacy_pixels' in data:
            source=data['legacy_pixels'].astype(np.float32);info['raw_native']=False
    if info.get('raw_native'):
        source=CameraSource(source,info['raw_info']);info['working_space']=settings['working_space']
    elif enabled(settings):raise ValueError('이 오프라인 미리보기에는 센서 정보가 없습니다. 원본을 연결해 RAW 미리보기를 다시 만드세요.')
    elif info.get('working_space','sRGB')!=settings['working_space']:
        source=convert(source,profile('Linear ProPhoto' if info.get('working_space')=='ProPhoto' else 'Linear sRGB'),
            profile('Linear ProPhoto' if settings['working_space']=='ProPhoto' else 'Linear sRGB'))
        info['working_space']=settings['working_space']
    info['offline_preview']=True
    info['offline_fingerprint']=fingerprint
    return source,info


def publish(directory,state,image,cancel=None,color_space='sRGB'):
    """Encode off-thread; serialize final validity check and atomic replacement."""
    if cancel is not None and cancel.is_set():return False
    if not isinstance(image,Image.Image):image=Image.fromarray(np.uint8(np.clip(image,0,1)*255+.5))
    image=image.convert('RGB');image.thumbnail((320,320))
    from .colorio import profile
    if color_space not in ('sRGB','ProPhoto RGB'):raise ValueError('지원하지 않는 썸네일 색공간입니다.')
    stream=io.BytesIO();image.save(stream,format='JPEG',quality=88,icc_profile=profile(color_space));payload=stream.getvalue()
    stamp={k:deepcopy(state.get(k)) for k in ('token','path','source','offline')}
    stamp.update(version=VERSION,color_space=color_space,sha256=hashlib.sha256(payload).hexdigest())
    jpeg,meta=files(directory,state['id']);jpeg.parent.mkdir(exist_ok=True,parents=True)
    temp=jpeg.with_name(jpeg.name+'.'+uuid4().hex+'.tmp');temp_meta=meta.with_name(meta.name+'.'+uuid4().hex+'.tmp')
    try:
        temp.write_bytes(payload);temp_meta.write_text(json.dumps(stamp),encoding='utf-8')
        with closing(connect(directory)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT p.path,s.token FROM photos p JOIN preview_state s ON p.id=s.photo_id WHERE p.id=?',(state['id'],)).fetchone()
                if row is None or row['token']!=state['token'] or row['path']!=state['path']:return False
                if signature(state['path'])!=state['source']:return False
                if state['source'] is None and signature(Path(directory)/'previews'/f"{state['id']}.npz")!=state.get('offline'):return False
                if cancel is not None and cancel.is_set():return False
                replace_file(temp,jpeg);replace_file(temp_meta,meta)
                return True
            finally:db.rollback()
    finally:
        temp.unlink(missing_ok=True);temp_meta.unlink(missing_ok=True)


def replace_file(source,target):
    # Windows readers can briefly hold a cache file without delete sharing.
    for attempt in range(20):
        try:os.replace(source,target);return
        except PermissionError:
            if attempt==19:raise
            time.sleep(.005)


def rebuild(directory,ident,cancel=None,force=False,cached=None):
    from .engine import load_image,develop,resize_float,output_rgb
    if cancel is not None and cancel.is_set():return '중단'
    result=inspect(directory,ident)
    if not force and not result['rebuild']:return result['status'] or '최신'
    state=snapshot(directory,ident,full=True)
    if state is None:return '제거됨'
    from .rawcolor import CameraSource,enabled
    reusable=(cached is not None and cached[1]['source']==state['source'] and cached[1]['offline']==state['offline']
        and cached[1]['working_space']==state['settings']['working_space']
        and isinstance(cached[0],CameraSource)==enabled(state['settings']))
    if reusable:source=cached[0];info=state['info']
    elif state['source'] is not None:source,info=load_image(state['path'],1800,state['settings']['working_space'],raw_options=state['settings'])
    elif state['offline'] is not None:source,info=load_offline(directory,ident,state['settings'])
    else:return '원본 없음'
    if cancel is not None and cancel.is_set():return '중단'
    original_size=(info.get('width',source.shape[1]),info.get('height',source.shape[0]))
    if reusable:original_size=cached[1].get('original_size',original_size)
    image=resize_float(develop(source,state['settings'],output_space=None,original_size=original_size),320)
    space='ProPhoto RGB' if state['settings']['working_space']=='ProPhoto' else 'sRGB'
    image=output_rgb(image,state['settings']['working_space'],space)
    return '갱신 완료' if publish(directory,state,image,cancel,color_space=space) else '변경됨 · 다시 확인'


def store_render(directory,ident,settings,image,source_signature,offline_signature=None,color_space='sRGB'):
    state=snapshot(directory,ident,full=True)
    if state is None or state['settings']!=settings:return False
    if state['source']!=source_signature or state['offline']!=offline_signature:return False
    return publish(directory,state,image,color_space=color_space)
