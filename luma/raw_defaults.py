"""Explicit RAW import rules, pinned profiles and stop-based ISO interpolation."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import unicodedata
from .engine import defaults,normalized,RAW_EXTENSIONS

PREFERENCE='raw_defaults_v1'
GROUPS={
    'RAW 프로파일':('dcp_profile','dcp_huesat','dcp_look','dcp_tone','dcp_exposure','working_space'),
    '화이트 밸런스':('raw_mode','raw_kelvin','raw_tint','raw_neutral','temperature','tint','kelvin','wb_reference','kelvin_enabled'),
    '빛':('exposure','contrast','highlights','shadows','whites','blacks'),
    '색상·커브':('saturation','vibrance','monochrome','mixer_mode','hsl','grading','grading_balance','calibration',
               'curve','rgb_curves','parametric','camera_matrix'),
    '디테일':('clarity','texture','dehaze','sharpen','sharpen_radius','sharpen_detail','sharpen_mask','noise_luma','noise_color'),
    '효과':('grain','vignette'),
}
ISO_KEYS=('noise_luma','noise_color','sharpen','sharpen_radius','sharpen_detail','sharpen_mask','clarity','texture','dehaze','blacks')
# Built-in development for newly imported RAW files (before any user rule), chosen on CC0 samples
# (Sony A7 III, Canon R6, Nikon Z6, Fujifilm X-T3). A user 'all RAW' rule replaces any of these values.
BUILTIN_RAW=dict(exposure=1.5,blacks=-30.,sharpen=60.,sharpen_radius=1.,sharpen_detail=25.,noise_color=25.)
BUILTIN_LINEAR_RAW={**BUILTIN_RAW,'exposure':.75}   # demosaiced (LinearRaw) DNG such as Apple ProRAW
# Built-in noise reduction by ISO, interpolated in stops, chosen on six CC0 raws: Panasonic G9 ISO 3200,
# Panasonic S5 6400, Sony A7C 12800, Nikon D5600 12800, Sony FX3 20000, Canon EOS R50 32000. Colour noise
# reduction is stronger above ISO 6400, where 25 left visible colour noise (D5600, FX3, R50). Sharpening
# stays at 60 whatever the ISO.
BUILTIN_ISO_LUMA_NR=((400,0.),(3200,12.),(6400,25.),(32000,30.))
BUILTIN_ISO_COLOR_NR=((6400,25.),(32000,50.))
BUILTIN_ISO_SHARPEN=((6400,60.),(32000,60.))


def _by_iso(table,iso):
    """Stop-interpolated value of (iso, value) points; the end values outside them."""
    if iso<=table[0][0]:return float(table[0][1])
    for (lo,a),(hi,b) in zip(table,table[1:]):
        if iso<=hi:return float(round(a+(b-a)*math.log2(iso/lo)/math.log2(hi/lo)))
    return float(table[-1][1])


def builtin_luma_nr(iso):
    """Built-in luminance noise reduction for a RAW shot at `iso` (None: unknown ISO, no reduction)."""
    return 0. if iso is None else _by_iso(BUILTIN_ISO_LUMA_NR,iso)


def builtin_iso_detail(iso):
    """Built-in luminance NR, colour NR and sharpening for a RAW shot at `iso` (unknown: the base values)."""
    if iso is None:return dict(noise_luma=0.,noise_color=BUILTIN_RAW['noise_color'],sharpen=BUILTIN_RAW['sharpen'])
    return dict(noise_luma=_by_iso(BUILTIN_ISO_LUMA_NR,iso),noise_color=_by_iso(BUILTIN_ISO_COLOR_NR,iso),
                sharpen=_by_iso(BUILTIN_ISO_SHARPEN,iso))


def linear_raw(path):
    """True for DNG files whose raw data is already demosaiced (PhotometricInterpretation LinearRaw)."""
    if Path(path).suffix.lower()!='.dng':return False
    try:
        import tifffile
        with tifffile.TiffFile(path) as tif:
            pages=[p for page in tif.pages for p in [page,*(page.pages or [])]]
            kinds={int(p.photometric) for p in pages}
    except Exception:return False
    return 34892 in kinds and 32803 not in kinds


def key(value):return ''.join(c for c in unicodedata.normalize('NFKC',str(value)).casefold() if c.isalnum())


def camera_key(info):
    make=key(info.get('make',''))
    for suffix in ('corporation','company','incorporated','inc','co','ltd'):
        if make.endswith(suffix):make=make[:-len(suffix)]
    model=key(info.get('camera',''))
    if make and model.startswith(make):model=model[len(make):]
    return make+':'+model if model else ''


def serial(info):return str(info.get('serial','')).strip().strip('\0')


def is_raw(info,path=''):
    return (Path(path).suffix.lower() if path else '.'+str(info.get('format','')).lower()) in RAW_EXTENSIONS


def iso_value(info):
    try:value=float(info.get('iso',0))
    except (ValueError,TypeError):return None
    return value if math.isfinite(value) and 0<value<=1e8 else None


def iso_anchors(photos):
    if len(photos)<2:raise ValueError('서로 다른 ISO의 RAW 사진을 두 장 이상 선택하세요.')
    cameras={camera_key(p['info']) for p in photos}
    if len(cameras)!=1 or not next(iter(cameras)):raise ValueError('같은 카메라 모델의 사진을 선택하세요.')
    anchors={}
    for p in photos:
        iso=iso_value(p['info'])
        if not is_raw(p['info'],p['path']) or iso is None:raise ValueError('ISO를 읽을 수 있는 RAW 사진만 선택하세요.')
        values={k:float(p['settings'][k]) for k in ISO_KEYS}
        if not all(math.isfinite(v) for v in values.values()):raise ValueError('ISO 보정값이 유효하지 않습니다.')
        if iso in anchors and anchors[iso]!=values:raise ValueError('같은 ISO에 서로 다른 보정값이 있습니다. 기준 사진을 한 장씩 선택하세요.')
        anchors[iso]=values
    if len(anchors)<2:raise ValueError('서로 다른 ISO가 두 개 이상 필요합니다.')
    return [{'iso':i,'values':v} for i,v in sorted(anchors.items())]


def make_rule(info,settings,scope='model',groups=None,anchors=(),name=''):
    if scope not in ('master','model','serial'):raise ValueError('기본값 적용 대상이 잘못되었습니다.')
    if scope!='master' and not camera_key(info):raise ValueError('카메라 모델을 읽을 수 없습니다.')
    if scope=='serial' and not serial(info):raise ValueError('이 RAW에서 바디 일련번호를 읽을 수 없습니다.')
    groups=list(GROUPS) if groups is None else list(groups)
    if not groups or any(g not in GROUPS for g in groups):raise ValueError('저장할 보정 항목을 선택하세요.')
    s=normalized(settings);values={k:deepcopy(s[k]) for g in groups for k in GROUPS[g]}
    if values.get('raw_mode')=='sample':values.update(raw_mode='custom',raw_neutral=None)
    if values.get('dcp_profile'):
        from .dcp import compiled,camera_matches
        p=compiled(values['dcp_profile'])
        if scope!='master' and not camera_matches(p,info):raise ValueError('현재 카메라와 다른 DCP입니다.')
        # A profile operates on sensor data even if WB was not selected to copy.
        if values.get('raw_mode','legacy')=='legacy':values['raw_mode']='as_shot'
    target=[scope,camera_key(info) if scope!='master' else '',serial(info) if scope=='serial' else '']
    ident=hashlib.sha256(json.dumps(target).encode()).hexdigest()[:24]
    rule=dict(id=ident,scope=scope,camera=target[1],serial=target[2],enabled=True,
              name=name or ('모든 RAW' if scope=='master' else str(info.get('camera',''))+(' · '+target[2] if target[2] else '')),
              values=values,groups=groups,iso=deepcopy(list(anchors)))
    json.dumps(rule,allow_nan=False)
    return rule


def resolve(info,rules,path='',base=None):
    """Import settings for a RAW file; base=None (a new import) also applies the built-in RAW development."""
    s=normalized(base or {});notes=[]
    if not is_raw(info,path):return s,notes
    if base is None:
        s.update(BUILTIN_LINEAR_RAW if info.get('linear_raw') else BUILTIN_RAW);s['tone_version']=3
        s.update(builtin_iso_detail(iso_value(info)))
        notes.append('RAW 기본 현상')
    matches=[r for r in rules if r.get('enabled',True) and (r['scope']=='master' or
        r.get('camera')==camera_key(info) and (r['scope']=='model' or r.get('serial')==serial(info) and bool(serial(info))))]
    if not matches:return s,notes
    rule=max(matches,key=lambda r:{'master':0,'model':1,'serial':2}[r['scope']])
    s.update(deepcopy(rule['values']));notes.append('RAW 기본값: '+rule['name'])
    if s.get('dcp_profile'):
        from .dcp import compiled,camera_matches
        if not camera_matches(compiled(s['dcp_profile']),info):
            s['dcp_profile']=None;notes.append('다른 카메라용 DCP는 제외했습니다.')
    points=rule.get('iso',[]);iso=iso_value(info)
    if points and iso is not None:
        points=sorted(points,key=lambda p:p['iso']);lo=points[0];hi=points[-1]
        for point in points:
            if point['iso']<=iso:lo=point
            if point['iso']>=iso:hi=point;break
        if iso<=points[0]['iso']:lo=hi=points[0]
        if iso>=points[-1]['iso']:lo=hi=points[-1]
        f=0 if hi['iso']==lo['iso'] else math.log2(iso/lo['iso'])/math.log2(hi['iso']/lo['iso'])
        for k in ISO_KEYS:s[k]=lo['values'][k]*(1-f)+hi['values'][k]*f
        notes.append(f'ISO {iso:g} 디테일·검정 보정')
    elif points:notes.append('ISO가 없어 저장된 기본 보정값을 사용했습니다.')
    return normalized(s),notes


def import_preview(path,rules,size=240):
    from .engine import read_metadata,load_image,develop
    from PIL import Image
    import numpy as np
    info={**read_metadata(path),'format':Path(path).suffix[1:].upper(),'linear_raw':linear_raw(path)}
    s,notes=resolve(info,rules,path)
    source,info=load_image(path,size,s['working_space'],raw_options=s)
    if notes:info['raw_default_applied']=notes
    info['preview_color_space']='ProPhoto RGB' if s['working_space']=='ProPhoto' else 'sRGB'
    return Image.fromarray(np.uint8(np.clip(develop(source,s,output_space=info['preview_color_space'],
        original_size=(info['width'],info['height'])),0,1)*255)),info,s
