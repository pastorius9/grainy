"""Persistent photo workflows independent of widgets and account services."""
from copy import deepcopy
from datetime import datetime,timezone
from fractions import Fraction
import hashlib
import json
import math
import numpy as np


def automatic_white_balance(source,settings):
    from .engine import geometry,resize_float
    from .rawcolor import develop_camera
    a=geometry(develop_camera(resize_float(source,640),settings),settings)
    rgb=np.maximum(a.reshape(-1,3),0)
    peak=rgb.max(axis=1);floor=rgb.min(axis=1)
    valid=np.isfinite(rgb).all(axis=1)&(floor>.005)&(peak<.98)
    if valid.sum()<16:raise ValueError('화이트 밸런스를 계산할 유효한 색상 영역이 부족합니다.')
    pixels=rgb[valid]
    # Trim extreme luminance and prefer low-chroma surfaces. This is an
    # explicitly statistical white balance; it cannot infer scene intent.
    luminance=pixels@np.array([.2126,.7152,.0722])
    lo,hi=np.percentile(luminance,[10,90]);pixels=pixels[(luminance>=lo)&(luminance<=hi)]
    chroma=(pixels.max(axis=1)-pixels.min(axis=1))/np.maximum(pixels.max(axis=1),1e-6)
    neutral=pixels[chroma<=np.percentile(chroma,40)]
    average=np.maximum(np.mean(neutral,axis=0),1e-6)
    gains=np.exp(np.mean(np.log(average)))/average
    r,b=np.log2(gains[[0,2]]/gains[1])
    values=deepcopy(settings)
    values.update(kelvin_enabled=False,temperature=float(np.clip(90*(r-b),-100,100)),
                  tint=float(np.clip((r+b)/.013,-100,100)))
    return values


def capture_exposure(info):
    def number(key):
        value=info.get(key)
        try:result=float(Fraction(str(value).strip()))
        except (ValueError,TypeError,ZeroDivisionError):raise ValueError('촬영 노출을 맞추려면 셔터·조리개·ISO 정보가 필요합니다.') from None
        if not math.isfinite(result) or result<=0:raise ValueError('촬영 노출 정보가 올바르지 않습니다.')
        return result
    return math.log2(number('shutter')*number('iso')/number('aperture')**2)


def match_exposure(reference,target):
    total=capture_exposure(reference['info'])+reference['settings']['exposure']
    result=deepcopy(target['settings'])
    result['exposure']=float(np.clip(total-capture_exposure(target['info']),-5,5))
    return result


def stack_members(catalog,photo_id):
    photo=catalog.photo(photo_id)
    if photo is None or photo['stack_id'] is None:return []
    return [r[0] for r in catalog.db.execute('SELECT id FROM photos WHERE stack_id=? ORDER BY id',(photo['stack_id'],))]


def stack_order(catalog,photo_id,position=None):
    members=stack_members(catalog,photo_id)
    if not members:return []
    key=str(catalog.photo(photo_id)['stack_id']);orders=catalog.preference('stack_orders',{})
    saved=[i for i in orders.get(key,[]) if i in members]
    ordered=saved+[i for i in members if i not in saved]
    if position is not None:
        ordered.remove(photo_id);ordered.insert(max(0,min(int(position),len(ordered))),photo_id)
        orders[key]=ordered;catalog.save_preference('stack_orders',orders)
    return ordered


def arrange_stacks(rows,orders,collapsed):
    groups={}
    for row in rows:
        if row['stack_id'] is not None:groups.setdefault(row['stack_id'],[]).append(row['id'])
    emitted=set();result=[]
    for row in rows:
        key=row['stack_id']
        if key is None:result.append(row['id']);continue
        saved=orders.get(str(key),[])
        if not collapsed and not saved:result.append(row['id']);continue
        if key in emitted:continue
        emitted.add(key);members=groups[key];available=set(members)
        ordered=[i for i in saved if i in available];seen=set(ordered)
        ordered.extend(i for i in members if i not in seen)
        result.extend(ordered[:1] if collapsed else ordered)
    return result


def quick_collection(catalog):
    ident=catalog.preference('quick_collection')
    if not any(c['id']==ident and c['rules'] is None for c in catalog.collections()):
        from .i18n import tr
        ident=catalog.add_collection(tr('빠른 컬렉션'));catalog.save_preference('quick_collection',ident)
    return ident


def toggle_quick(catalog,ids):
    ident=quick_collection(catalog);members=catalog.collection_ids(ident)
    remove=bool(ids) and all(i in members for i in ids)
    if remove:
        with catalog.db:catalog.db.executemany('DELETE FROM collection_members WHERE collection_id=? AND photo_id=?',[(ident,i) for i in ids])
    else:catalog.collection_add(ident,ids)
    return ident,not remove


METADATA_KEYS=('title','caption','creator','copyright','location','city','country','latitude','longitude','DateTimeOriginal','keywords')


def metadata_presets(catalog):return catalog.preference('metadata_presets',{})


def save_metadata_preset(catalog,name,values):
    name=name.strip()
    if not name:raise ValueError('프리셋 이름을 입력하세요.')
    unknown=set(values)-set(METADATA_KEYS)
    if unknown:raise ValueError('지원하지 않는 메타데이터 항목입니다.')
    values=deepcopy(values)
    for key,maximum in [('latitude',90),('longitude',180)]:
        if values.get(key) not in ('',None):
            if not math.isfinite(float(values[key])) or abs(float(values[key]))>maximum:raise ValueError('GPS 좌표 범위를 확인하세요.')
    if values.get('DateTimeOriginal'):datetime.strptime(values['DateTimeOriginal'],'%Y:%m:%d %H:%M:%S')
    presets=metadata_presets(catalog);presets[name]=values;catalog.save_preference('metadata_presets',presets)


def apply_metadata_preset(catalog,ids,values):
    if set(values)-set(METADATA_KEYS):raise ValueError('지원하지 않는 메타데이터 항목입니다.')
    with catalog.db:
        for ident in ids:
            row=catalog.photo(ident)
            if row is None:raise ValueError('사진 목록이 바뀌었습니다.')
            extras=row['user_metadata'];extras.update({k:v for k,v in values.items() if k!='keywords'})
            catalog.db.execute('UPDATE photos SET extras=?,keywords=? WHERE id=?',
                (json.dumps(extras,ensure_ascii=False),values.get('keywords',row['keywords']),ident))


def add_keyword(catalog,ids,keyword):
    keyword=keyword.strip()
    if not keyword or ',' in keyword:raise ValueError('키워드 하나를 입력하세요. 계층은 |로 구분합니다.')
    with catalog.db:
        for ident in ids:
            row=catalog.photo(ident)
            words=[w.strip() for w in row['keywords'].split(',') if w.strip()]
            if keyword.casefold() not in {w.casefold() for w in words}:words.append(keyword)
            catalog.db.execute('UPDATE photos SET keywords=? WHERE id=?',(', '.join(words),ident))


def edit_signature(settings):
    from .engine import normalized
    return hashlib.sha256(json.dumps(normalized(settings),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def record_exports(catalog,outputs):
    history=catalog.preference('export_history',[])
    stamp=datetime.now(timezone.utc).isoformat()
    history.extend({**item,'created':stamp} for item in outputs)
    catalog.save_preference('export_history',history[-2000:])


def export_history(catalog,ids):
    selected=set(ids)
    result=[]
    for item in reversed(catalog.preference('export_history',[])):
        if item.get('photo_id') not in selected:continue
        photo=catalog.photo(item['photo_id'])
        if photo:result.append({**item,'edited_since':item.get('edit_signature')!=edit_signature(photo['settings'])})
    return result
