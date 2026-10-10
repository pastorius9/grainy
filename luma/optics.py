"""Offline Lensfun profiles; saved calibrations remain reproducible after updates."""
from functools import lru_cache
from pathlib import Path
import hashlib
import re
import xml.etree.ElementTree as ET
import cv2
import lensfunpy as lf
import numpy as np
from .rasterio import orient_pixels

# Native maps cost 24 bytes/pixel with RGB aberration correction. Small previews
# use one map; large exports bound temporary maps independently of image size.
MAP_BUDGET = 32 * 1024**2


def _key(text):
    text=re.sub(r'(?<=\d)\.0+(?=\D|$)','',str(text).casefold())
    text=re.sub(r'\b(corporation|corp|inc|ltd|limited|co)\b','',text)
    return re.sub(r'[^a-z0-9]+','',text)


def _model_key(model,maker):
    value=_key(model);prefix=_key(maker)
    return value[len(prefix):] if prefix and value.startswith(prefix) else value


@lru_cache(maxsize=1)
def database():
    # An unrelated application's system/user database must not change saved edits.
    return lf.Database(load_common=False)


@lru_cache(maxsize=1)
def cameras():
    return tuple(sorted(database().cameras,key=lambda c:(c.maker,c.model,(getattr(c,'variant','') or ''))))


@lru_cache(maxsize=1)
def lens_records():
    records=[]
    root=Path(lf.__file__).parent/'db_files'
    for path in sorted(root.glob('*.xml')):
        tree=ET.parse(path,parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))).getroot()
        for node in tree.findall('lens'):
            xml='<lensdatabase version="1">'+ET.tostring(node,encoding='unicode')+'</lensdatabase>'
            crop=float(node.findtext('cropfactor','1'))
            records.append({'id':hashlib.sha256(xml.encode()).hexdigest()[:20],
                'maker':node.findtext('maker',''),'model':node.findtext('model',''),
                'crop':crop,'mounts':[m.text for m in node.findall('mount')],
                'xml':xml,'file':path.name})
    return tuple(sorted(records,key=lambda l:(l['maker'],l['model'],l['crop'])))


def compatible_lenses(camera):
    if camera is None:return lens_records()
    valid={(l.maker,l.model,round(l.crop_factor,3)) for l in database().find_lenses(camera)}
    return tuple(r for r in lens_records() if (r['maker'],r['model'],round(r['crop'],3)) in valid)


def match_metadata(metadata):
    maker=metadata.get('make','');model=metadata.get('camera','')
    matches=[c for c in cameras() if _key(c.maker)==_key(maker)
        and _model_key(c.model,c.maker)==_model_key(model,maker)] if maker and model else []
    camera=matches[0] if len(matches)==1 else None
    name=metadata.get('lens','');candidates=[]
    if camera and name:
        for record in compatible_lenses(camera):
            if _model_key(record['model'],record['maker'])==_model_key(name,record['maker']):candidates.append(record)
        # A lens can have separate calibrations for sensor sizes. Pick the
        # closest calibrated crop only when the actual model is unambiguous.
        if len(candidates)>1 and len({_key(r['model']) for r in candidates})==1:
            candidates.sort(key=lambda r:abs(r['crop']-camera.crop_factor))
            if abs(candidates[0]['crop']-camera.crop_factor)+.001<abs(candidates[1]['crop']-camera.crop_factor):
                candidates=candidates[:1]
    reason=('카메라를 선택해 주세요.' if camera is None else
            '사진에 렌즈 이름이 없습니다. 사용한 렌즈를 선택해 주세요.' if not name else
            '렌즈 후보를 확인해 주세요.' if len(candidates)!=1 else '')
    return camera,candidates,reason


@lru_cache(maxsize=12)
def _saved_lens(xml):
    if not isinstance(xml,str) or len(xml)>256000 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('렌즈 보정 데이터가 올바르지 않습니다.')
    root=ET.fromstring(xml)
    if root.tag!='lensdatabase' or len(root.findall('lens'))!=1:
        raise ValueError('렌즈 프로파일 하나가 필요합니다.')
    db=lf.Database(xml=xml,load_common=False,load_bundled=False)
    if len(db.lenses)!=1:raise ValueError('렌즈 보정 데이터를 읽지 못했습니다.')
    return db,db.lenses[0]


def profile_lens(record):return _saved_lens(record['xml'])[1]


def make_profile(record,crop,focal,aperture=0,distance=1000,orientation=1,camera=''):
    values=np.asarray([crop,focal,aperture,distance],float)
    if not np.isfinite(values).all() or not (.1<=crop<=20 and .1<=focal<=5000 and 0<=aperture<=128 and .01<=distance<=100000):
        raise ValueError('센서 크기·초점 거리·조리개·촬영 거리를 확인해 주세요.')
    lens=profile_lens(record)
    if lens.min_focal and not lens.min_focal-.01<=focal<=lens.max_focal+.01:
        raise ValueError('초점 거리가 선택한 렌즈 범위를 벗어났습니다.')
    if orientation not in range(1,9):raise ValueError('사진 방향 정보가 올바르지 않습니다.')
    caps=[]
    if lens.interpolate_distortion(focal) is not None:caps.append('distortion')
    if lens.interpolate_tca(focal) is not None:caps.append('tca')
    if aperture>0 and lens.interpolate_vignetting(focal,aperture,distance) is not None:caps.append('vignette')
    if not caps:raise ValueError('이 촬영 조건에서 사용할 보정 데이터가 없습니다.')
    return {'xml':record['xml'],'name':record['model'],'maker':record['maker'],'camera':camera,
        'crop':float(crop),'focal':float(focal),'aperture':float(aperture),'distance':float(distance),
        'orientation':int(orientation),'capabilities':caps,'database_file':record.get('file',''),
        'source':'Lensfun','license':'CC BY-SA 3.0','library_version':list(lf.lensfun_version)}


def automatic_profile(metadata):
    camera,matches,reason=match_metadata(metadata)
    if reason:return None,reason
    focal=metadata.get('focal_length')
    if not focal:return None,'초점 거리 정보가 없습니다. 촬영 조건을 입력해 주세요.'
    try:
        result=make_profile(matches[0],camera.crop_factor,float(focal),float(metadata.get('aperture') or 0),
            float(metadata.get('subject_distance') or 1000),int(metadata.get('source_orientation') or 1),camera.model)
    except (ValueError,TypeError) as error:return None,str(error)
    return result,''


def retarget(settings,metadata):
    """Copy a calibration, but use the receiving photo's capture conditions."""
    from copy import deepcopy
    result=deepcopy(settings);data=result.get('lensfun')
    if not data:return result
    try:
        camera,_,_=match_metadata(metadata)
        name=metadata.get('lens')
        if name and _model_key(name,data['maker'])!=_model_key(data['name'],data['maker']):
            replacement,reason=automatic_profile(metadata)
            if replacement is None:raise ValueError('대상 렌즈가 달라 프로파일을 적용하지 않았습니다. '+reason)
            result['lensfun']=replacement;return result
        crop=data['crop']
        if camera:
            if not any(_key(r['model'])==_key(data['name']) for r in compatible_lenses(camera)):
                raise ValueError('대상 카메라와 렌즈의 호환 정보를 확인해 주세요.')
            crop=camera.crop_factor
        elif metadata.get('camera') and data.get('camera') and _key(metadata['camera'])!=_key(data['camera']):
            raise ValueError('대상 카메라의 센서 크기를 확인해 주세요.')
        record={'xml':data['xml'],'model':data['name'],'maker':data['maker'],'file':data.get('database_file','')}
        # No EXIF (e.g. film scans): the explicitly copied manual conditions
        # remain usable. Explain their provenance in the profile label.
        focal=metadata.get('focal_length') or data['focal']
        aperture=metadata.get('aperture') or (0 if metadata.get('camera') else data['aperture'])
        profile=make_profile(record,crop,float(focal),float(aperture),
            float(metadata.get('subject_distance') or 1000),int(metadata.get('source_orientation') or 1),
            camera.model if camera else metadata.get('camera') or data.get('camera',''))
        if not metadata.get('focal_length'):profile['note']='초점 거리 정보가 없어 복사한 촬영 조건을 사용합니다.'
        result['lensfun']=profile
    except (ValueError,TypeError,KeyError) as error:
        result['lensfun_enabled']=False;data['note']=str(error)
    return result


def _modifier(settings,shape,color=False):
    data=settings.get('lensfun')
    if not data or not settings.get('lensfun_enabled'):return None
    values=np.asarray([data['crop'],data['focal'],data.get('aperture',0),data.get('distance',1000)],float)
    if not np.isfinite(values).all() or not (.1<=values[0]<=20 and .1<=values[1]<=5000 and 0<=values[2]<=128 and .01<=values[3]<=100000):
        raise ValueError('렌즈 프로파일의 촬영 조건이 올바르지 않습니다.')
    _,lens=_saved_lens(data['xml'])
    flags=0
    if color:
        if settings.get('lensfun_vignette',True) and data.get('aperture',0)>0:flags=lf.ModifyFlags.VIGNETTING
    else:
        if settings.get('lensfun_distortion',True):flags|=lf.ModifyFlags.DISTORTION
        if settings.get('lensfun_tca',True):flags|=lf.ModifyFlags.TCA
        if flags and settings.get('lensfun_scale',True):flags|=lf.ModifyFlags.SCALE
    if not flags:return None
    h,w=shape[:2];modifier=lf.Modifier(lens,data['crop'],w,h)
    modifier.initialize(data['focal'],data.get('aperture',0),data.get('distance',1000),
        scale=0 if settings.get('lensfun_scale',True) else 1,pixel_format=np.float32,flags=int(flags))
    return modifier


def _sensor_view(pixels,data):
    orientation=int(data.get('orientation',1));inverse={6:8,8:6}.get(orientation,orientation)
    if orientation not in range(1,9):raise ValueError('사진 방향 정보가 올바르지 않습니다.')
    return orient_pixels(pixels,inverse),orientation


def lens_shading(pixels,settings):
    if not settings.get('lensfun_enabled') or not settings.get('lensfun') or not settings.get('lensfun_vignette',True):return pixels
    sensor,orientation=_sensor_view(pixels,settings['lensfun'])
    modifier=_modifier(settings,sensor.shape,color=True)
    if modifier is None:return pixels
    result=np.array(sensor,dtype=np.float32,order='C',copy=True)
    if not modifier.apply_color_modification(result):return pixels
    return np.ascontiguousarray(orient_pixels(result,orientation))


def lens_geometry(pixels,settings):
    if not settings.get('lensfun_enabled') or not settings.get('lensfun'):return pixels
    sensor,orientation=_sensor_view(pixels,settings['lensfun'])
    sensor=np.ascontiguousarray(sensor);h,w,channels=sensor.shape
    modifier=_modifier(settings,sensor.shape)
    if modifier is None:return pixels
    chromatic=channels==3 and settings.get('lensfun_tca',True)
    output=np.empty_like(sensor)
    map_bytes=h*w*(24 if chromatic else 8)
    if map_bytes<=MAP_BUDGET:
        coords=modifier.apply_subpixel_geometry_distortion() if chromatic else modifier.apply_geometry_distortion()
        if coords is None:return pixels
        if chromatic:
            for c in range(channels):
                output[...,c]=cv2.remap(np.ascontiguousarray(sensor[...,c]),coords[:,:,c,0],coords[:,:,c,1],cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
        else:
            output=cv2.remap(sensor,coords,None,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
            if output.ndim==2:output=output[...,None]
        return np.ascontiguousarray(orient_pixels(output,orientation))
    # Bounded coordinate buffers, including for full-resolution exports. Work
    # one contiguous plane at a time rather than copying a strided full image
    # inside every remap call. Masks/coordinate maps use geometry only.
    if chromatic:
        # One coordinate block serves all three planes (it holds every channel's coordinates).
        planes=[np.ascontiguousarray(sensor[...,c]) for c in range(channels)]
        for y in range(0,h,128):
            count=min(128,h-y)
            coords=modifier.apply_subpixel_geometry_distortion(yu=y,width=w,height=count)
            if coords is None:return pixels
            for c,plane in enumerate(planes):
                output[y:y+count,:,c]=cv2.remap(plane,coords[:,:,c,0],coords[:,:,c,1],cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
    else:
        for y in range(0,h,128):
            count=min(128,h-y);coords=modifier.apply_geometry_distortion(yu=y,width=w,height=count)
            if coords is None:return pixels
            strip=cv2.remap(sensor,coords,None,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
            output[y:y+count]=strip[...,None] if strip.ndim==2 else strip
    return np.ascontiguousarray(orient_pixels(output,orientation))


def description(data):
    if not data:return '자동 렌즈 프로파일 없음'
    names={'distortion':'왜곡','tca':'색수차','vignette':'주변광량'}
    available=' · '.join(names[k] for k in data.get('capabilities',[]) if k in names)
    missing='\n조리개 정보 없음: 주변광량 보정 제외' if not data.get('aperture') else ''
    note='\n'+data['note'] if data.get('note') else ''
    return f'{data["name"]}\n{data["focal"]:g}mm · f/{data["aperture"]:g} · 센서 배율 {data["crop"]:.3g}\n{available}{missing}{note}'
