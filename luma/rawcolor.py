"""Camera-space RAW white balance and DCP development, independent of decoding.

Existing edits retain the legacy LibRaw rendering until camera RAW mode is chosen.
Sensor buffers are kept immutable and reused across temperature/profile changes.
"""
from copy import deepcopy
import numpy as np
from . import dcp

RAW_KEYS=('raw_mode','raw_kelvin','raw_tint','raw_neutral','dcp_profile',
          'dcp_huesat','dcp_look','dcp_tone','dcp_exposure','working_space')

# Robertson isotemperature data (Wyszecki & Stiles, Color Science, table 1(3.11)).
# Values checked against Colour Science's BSD-licensed reference implementation.
LINES=np.array([
 (0,.18006,.26352,-.24341),(10,.18066,.26589,-.25479),(20,.18133,.26846,-.26876),
 (30,.18208,.27119,-.28539),(40,.18293,.27407,-.30470),(50,.18388,.27709,-.32675),
 (60,.18494,.28021,-.35156),(70,.18611,.28342,-.37915),(80,.18740,.28668,-.40955),
 (90,.18880,.28997,-.44278),(100,.19032,.29326,-.47888),(125,.19462,.30141,-.58204),
 (150,.19962,.30921,-.70471),(175,.20525,.31647,-.84901),(200,.21142,.32312,-1.0182),
 (225,.21807,.32909,-1.2168),(250,.22511,.33439,-1.4512),(275,.23247,.33904,-1.7298),
 (300,.24010,.34308,-2.0637),(325,.24792,.34655,-2.4681),(350,.25591,.34951,-2.9641),
 (375,.26400,.35200,-3.5814),(400,.27218,.35407,-4.3633),(425,.28039,.35577,-5.3762),
 (450,.28863,.35714,-6.7262),(475,.29685,.35823,-8.5955),(500,.30505,.35907,-11.324),
 (525,.31320,.35968,-15.628),(550,.32129,.36011,-23.325),
 (575,.32931,.36038,-40.770),(600,.33724,.36051,-116.45)])
DIRECTIONS=np.stack([np.ones(len(LINES)),LINES[:,3]],axis=-1)
DIRECTIONS/=np.linalg.norm(DIRECTIONS,axis=-1)[:,None]


class CameraSource(np.ndarray):
    """An ndarray of linear sensor RGB with a small, JSON-safe calibration record."""
    def __new__(cls,pixels,info):
        result=np.asarray(pixels,dtype=np.float32).view(cls)
        result.raw_info=deepcopy(info)
        return result
    def __array_finalize__(self,obj):
        self.raw_info=getattr(obj,'raw_info',None)


def enabled(settings):
    return settings.get('raw_mode','legacy')!='legacy' or bool(settings.get('dcp_profile'))


def xyz_temperature(xyz):
    xyz=np.asarray(xyz,dtype=np.float64);den=xyz[0]+15*xyz[1]+3*xyz[2]
    if not np.isfinite(den) or den<=0:raise ValueError('카메라 백색점을 계산할 수 없습니다.')
    uv=np.array([4*xyz[0]/den,6*xyz[1]/den])
    delta=uv-LINES[:,1:3]
    distance=delta[:,1]*DIRECTIONS[:,0]-delta[:,0]*DIRECTIONS[:,1]
    crossing=np.flatnonzero(distance[1:]<=0)
    i=int(crossing[0]+1) if len(crossing) else len(LINES)-1
    f=float(np.clip(distance[i-1]/max(distance[i-1]-distance[i],1e-15),0,1))
    mired=LINES[i-1,0]*(1-f)+LINES[i,0]*f
    locus=LINES[i-1,1:3]*(1-f)+LINES[i,1:3]*f
    direction=DIRECTIONS[i-1]*(1-f)+DIRECTIONS[i]*f;direction/=np.linalg.norm(direction)
    # Tint units follow 1/3000 CIE 1960 uv distance; positive correction is magenta.
    tint=-float((uv-locus)@direction)*3000
    return float(np.clip(1e6/max(mired,1e-6),1667,100000)),tint


def temperature_xyz(kelvin,tint=0.):
    if not np.isfinite([kelvin,tint]).all() or not 2000<=kelvin<=50000 or not -150<=tint<=150:
        raise ValueError('RAW 색온도는 2,000–50,000K, 색조는 -150–150 범위입니다.')
    mired=1e6/kelvin;i=int(np.clip(np.searchsorted(LINES[:,0],mired),1,len(LINES)-1))
    f=(mired-LINES[i-1,0])/(LINES[i,0]-LINES[i-1,0])
    uv=LINES[i-1,1:3]*(1-f)+LINES[i,1:3]*f
    direction=DIRECTIONS[i-1]*(1-f)+DIRECTIONS[i]*f;direction/=np.linalg.norm(direction)
    u,v=uv-direction*tint/3000
    # Extreme UI tint can leave the physical chromaticity triangle. Keep a
    # positive white instead of producing negative sensor multipliers.
    return np.maximum(np.array([1.5*u/v,1.,(4-u-10*v)/(2*v)]),1e-6)


def default_profile(info):
    if info.get('base_profile'):
        p=deepcopy(info['base_profile'])
        for key in ('cm1','cm2','cm3','fm1','fm2','fm3','xy1','xy2','xy3','curve'):
            if p.get(key) is not None:p[key]=np.asarray(p[key],dtype=float)
        return p
    cm=np.asarray(info['xyz_to_camera'],dtype=np.float64)
    if cm.shape!=(3,3) or not np.isfinite(cm).all() or np.linalg.cond(cm)>1e6:
        raise ValueError('이 카메라의 RAW 색상 행렬을 해석할 수 없습니다.')
    return dict(cm1=cm,cm2=None,fm1=None,fm2=None,t1=6504.,t2=None,signature='',
                hs1=None,hs2=None,look=None,curve=np.array([[0.,0.],[1.,1.]]),exposure=0.)


def neutral_white(profile,neutral,info):
    neutral=np.asarray(neutral,dtype=np.float64)
    if neutral.shape!=(3,) or not np.isfinite(neutral).all() or np.any(neutral<=0):
        raise ValueError('RAW 화이트밸런스 중립점이 유효하지 않습니다.')
    if profile.get('cm3') is not None or profile.get('custom_illuminants'):
        from .illuminants import xyz_to_xy,xy_to_xyz
        last=np.array([.3457,.3585])
        for step in range(30):
            white=xy_to_xyz(last);w=dcp.weight(profile,6504.,white)
            cm=individual_matrix(profile,info,w)@dcp.interpolate(profile,'cm',w)
            next_xy=xyz_to_xy(np.linalg.solve(cm,neutral))
            if np.sum(np.abs(next_xy-last))<1e-7:break
            if step==29:next_xy=(next_xy+last)*.5
            last=next_xy
        xyz=xy_to_xyz(next_xy)
        temperature,tint=xyz_temperature(xyz)
        return xyz,temperature,tint
    temperature=6504.
    for _ in range(30):
        w=dcp.weight(profile,temperature)
        cm=individual_matrix(profile,info,w)@dcp.interpolate(profile,'cm',w)
        xyz=np.linalg.solve(cm,neutral)
        new,tint=xyz_temperature(xyz)
        if abs(new-temperature)<.01:break
        temperature=(temperature+new)/2
    return xyz,new,tint


def individual_matrix(profile,info,w):
    cc=np.eye(3)
    if info.get('calibration_signature','')==profile.get('signature','') and info.get('calibration'):
        cc=dcp.calibration_matrix(info['calibration'],w)
    return np.diag(info.get('analog',[1.,1.,1.]))@cc


def resolve_white(profile,info,settings,with_white=False):
    mode=settings.get('raw_mode','as_shot')
    if mode=='custom':
        temperature=float(settings['raw_kelvin']);tint=float(settings['raw_tint'])
        xyz=temperature_xyz(temperature,tint);w=dcp.weight(profile,temperature,xyz)
        neutral=individual_matrix(profile,info,w)@dcp.interpolate(profile,'cm',w)@xyz
        neutral=np.maximum(neutral,max(float(np.max(neutral)),1.)*1e-4)
    else:
        if mode=='sample':neutral=settings.get('raw_neutral')
        else:neutral=info['daylight_neutral'] if mode=='daylight' else info['neutral']
        xyz,temperature,tint=neutral_white(profile,neutral,info)
        neutral=np.asarray(neutral,dtype=np.float64)
    if np.any(neutral<=0) or not np.isfinite(neutral).all():raise ValueError('선택한 RAW 백색점이 카메라 범위를 벗어났습니다.')
    values=(neutral/neutral[1],temperature,tint)
    return (*values,xyz) if with_white else values


def develop_camera(source,settings):
    if not isinstance(source,CameraSource):return np.asarray(source)
    info=source.raw_info;record=settings.get('dcp_profile')
    profile=dcp.compiled(record) if record else default_profile(info)
    if record and not dcp.camera_matches(profile,info):raise ValueError('DCP 카메라 모델이 현재 RAW와 다릅니다.')
    neutral,temperature,tint,white=resolve_white(profile,info,settings,with_white=True)
    matrix=dcp.camera_to_xyz(profile,neutral,temperature,info.get('calibration'),info.get('analog'),info.get('calibration_signature',''),white)
    transform=(dcp.XYZ_PROPHOTO@matrix).T.astype(np.float32)
    a=np.asarray(source)@transform
    if record:
        hs=dcp.interpolate(profile,'hs',dcp.weight(profile,temperature,white))
        # Bound table scratch buffers for full-resolution images.
        result=np.empty_like(a)
        for y in range(0,len(a),128):
            part=a[y:y+128]
            if hs is not None and settings.get('dcp_huesat',True):part=dcp.table_map(part,hs,profile['hs_encoding'])
            if settings.get('dcp_exposure',True):part=part*2**profile['exposure']
            if profile['look'] is not None and settings.get('dcp_look',True):part=dcp.table_map(part,profile['look'],profile['look_encoding'])
            if settings.get('dcp_tone',True):part=dcp.tone_map(part,profile['curve'])
            result[y:y+128]=part
        a=result
    if settings.get('working_space','sRGB')!='ProPhoto':
        from .colorio import convert,profile as icc
        a=convert(a,icc('Linear ProPhoto'),icc('Linear sRGB'))
    return np.asarray(a,dtype=np.float32)


def retarget(settings,metadata):
    result=deepcopy(settings)
    from .engine import RAW_EXTENSIONS
    is_raw='.'+str(metadata.get('format','')).lower() in RAW_EXTENSIONS
    if not is_raw:
        result.update(raw_mode='legacy',raw_neutral=None,dcp_profile=None)
    elif result.get('dcp_profile') and not dcp.camera_matches(dcp.compiled(result['dcp_profile']),metadata):
        result['dcp_profile']=None
    # A sampled sensor neutral belongs to a camera, unlike a Kelvin selection.
    if result.get('raw_mode')=='sample':
        result['raw_mode']='custom';result['raw_neutral']=None
    return result


def read_dng_calibration(path,info):
    """Read only color tags; pixel decoding remains LibRaw's responsibility."""
    import tifffile
    from pathlib import Path
    if Path(path).suffix.lower()!='.dng':return
    with tifffile.TiffFile(path) as tf:
        pages=[tf.pages[0]]
        if tf.pages[0].pages:pages+=list(tf.pages[0].pages)
        tags={}
        endian=tf.byteorder
        wanted={50708,50721,50722,50723,50724,50727,50728,50778,50779,50931,50932,50964,50965,
                52529,52530,52531,52532,52533,52534,52535}
        for page in pages:
            for tag in page.tags.values():
                if tag.code not in wanted:continue
                limit=8022 if tag.code in (52533,52534,52535) else 1024
                if tag.count>limit:raise ValueError('DNG 색상 태그 크기가 잘못되었습니다.')
                value=tag.value
                if tag.dtype in (5,10):
                    a=np.asarray(value,dtype=float).reshape(-1,2)
                    if np.any(a[:,1]==0):raise ValueError('DNG 색상 태그 분모가 0입니다.')
                    value=a[:,0]/a[:,1]
                tags[tag.code]=value
    def mat(tag):
        if tag not in tags:return None
        a=np.asarray(tags[tag],float)
        if a.size!=9 or not np.isfinite(a).all() or np.linalg.cond(a.reshape(3,3))>1e6:
            raise ValueError('DNG 카메라 색상 행렬이 잘못되었습니다.')
        return a.reshape(3,3).tolist()
    cm1=mat(50721)
    if cm1 is not None:
        p={k:v.tolist() if isinstance(v,np.ndarray) else v for k,v in dcp.parse_tags(tags,endian).items()}
        info['base_profile']=p;info['xyz_to_camera']=cm1
    info['unique_camera']=str(tags.get(50708,info.get('camera','')))
    for key,tag in [('neutral',50728),('analog',50727)]:
        if tag in tags:
            a=np.asarray(tags[tag],float)
            if a.size!=3 or not np.isfinite(a).all() or np.any(a<=0):raise ValueError('DNG 백색점 또는 아날로그 배율이 잘못되었습니다.')
            info[key]=a.tolist()
    calibration=[mat(tag) for tag in (50723,50724,52530)]
    if any(v is not None for v in calibration):
        info['calibration']=[v if v is not None else np.eye(3).tolist() for v in calibration]
        info['calibration_signature']=str(tags.get(50931,''))


def reduce_false_color(rgb):
    """3x3 median of R-G and B-G after demosaicing (the idea of LibRaw's median_filter_passes=1).

    Removes coloured speckles along fine detail without touching luminance detail. On CC0
    raw.pixls.us samples (Sony, Nikon, Canon Bayer; Fujifilm X-Trans) false colour dropped while
    sharpness was unchanged; see CHANGELOG 0.5.58. Row tiles with a one-row halo on
    the pixel workers give the same values as one full-frame pass (~0.14 s for 24 MP).
    """
    import cv2
    from .pixel_jobs import rows
    rgb=np.ascontiguousarray(rgb,dtype=np.float32);h,w=rgb.shape[:2]
    if rgb.ndim!=3 or rgb.shape[2]!=3 or h<3 or w<3:return rgb
    def tile(first,stop):
        a=max(0,first-1);b=min(h,stop+1);part=rgb[a:b];green=part[...,1];out=part.copy()
        for c in (0,2):out[...,c]=cv2.medianBlur(np.ascontiguousarray(part[...,c]-green),3)+green
        return out[first-a:first-a+(stop-first)]
    return rows(h,w,tile)


def decode(raw,metadata,max_size,path=None):
    import rawpy
    if raw.num_colors!=3 or raw.color_desc[:3]!=b'RGB':
        raise ValueError('카메라 RAW 보정은 3채널 RGB 센서에서 지원됩니다. 기존 현상을 사용하세요.')
    cm=raw.rgb_xyz_matrix[:3].astype(float)
    gains=np.asarray(raw.camera_whitebalance[:3],float)
    daylight=np.asarray(raw.daylight_whitebalance[:3],float)
    if np.any(gains<=0):gains=daylight.copy()
    if np.any(gains<=0) or np.any(daylight<=0):raise ValueError('카메라 화이트밸런스 정보가 없습니다.')
    info={k:metadata[k] for k in ('make','camera','format') if k in metadata}
    info.update(xyz_to_camera=cm.tolist(),neutral=(gains[1]/gains).tolist(),
                daylight_neutral=(daylight[1]/daylight).tolist())
    if path:read_dng_calibration(path,info)
    default_profile(info)
    # LibRaw handles linearization, black/white levels, demosaic, sensor orientation.
    # Unity multipliers avoid baking WB/clipping into the reusable camera buffer.
    half=max_size is not None and max(raw.sizes.width,raw.sizes.height)>max_size*2
    pixels=raw.postprocess(use_camera_wb=False,use_auto_wb=False,user_wb=[1.,1.,1.,1.],
        no_auto_bright=True,output_bps=16,gamma=(1,1),output_color=rawpy.ColorSpace.raw,
        adjust_maximum_thr=0,half_size=half)
    _,temperature,tint=neutral_white(default_profile(info),info['neutral'],info)
    metadata.update(raw_native=True,raw_info=info,raw_as_shot_kelvin=round(temperature),raw_as_shot_tint=round(tint,2))
    from .rasterio import _normalise
    pixels=_normalise(pixels,16)   # pixels.astype(np.float32)/65535 on the row workers
    if not half:pixels=reduce_false_color(pixels)   # half-size previews are not demosaiced
    return CameraSource(pixels,info)
