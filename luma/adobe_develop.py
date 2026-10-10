"""Explicit, reported translation of Adobe edit parameters to Luma controls.

This translates intent/values, not Adobe's proprietary rendering algorithm.
Unknown fields are returned to the caller; no unsupported edit is silently
claimed as applied. Import callers keep the original text alongside the result.
"""
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from .engine import normalized
from .lua_data import parse, sequence, DataError

VERSION = 1
CRS = 'http://ns.adobe.com/camera-raw-settings/1.0/'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
SCALARS = {
    'Exposure2012':('exposure', -5, 5), 'Contrast2012':('contrast', -100, 100),
    'Highlights2012':('highlights', -100, 100), 'Shadows2012':('shadows', -100, 100),
    'Whites2012':('whites', -100, 100), 'Blacks2012':('blacks', -100, 100),
    'Clarity2012':('clarity', -100, 100), 'Texture':('texture', -100, 100),
    'Dehaze':('dehaze', -100, 100), 'Saturation':('saturation', -100, 100),
    'Vibrance':('vibrance', -100, 100), 'LuminanceSmoothing':('noise_luma', 0, 100),
    'ColorNoiseReduction':('noise_color', 0, 100), 'Sharpness':('sharpen', 0, 150),
    'SharpenRadius':('sharpen_radius', .2, 5), 'SharpenDetail':('sharpen_detail', 0, 100),
    'SharpenEdgeMasking':('sharpen_mask', 0, 100), 'GrainAmount':('grain', 0, 100),
    'IncrementalTemperature':('temperature', -100, 100), 'IncrementalTint':('tint', -100, 100),
    'GrainSize':('grain_size', 0, 100), 'GrainFrequency':('grain_roughness', 0, 100),
    'PostCropVignetteMidpoint':('vignette_midpoint', 0, 100), 'PostCropVignetteRoundness':('vignette_roundness', -100, 100),
    'PostCropVignetteFeather':('vignette_feather', 0, 100), 'PostCropVignetteHighlightContrast':('vignette_highlights', 0, 100),
}
INFORMATION = {'Version', 'ProcessVersion', 'HasSettings', 'RawFileName', 'UUID',
               'SupportsAmount', 'SupportsColor', 'SupportsMonochrome', 'SupportsHighDynamicRange',
               'SupportsNormalDynamicRange', 'SupportsSceneReferred', 'SupportsOutputReferred',
               'PresetType', 'Cluster', 'Name', 'Group', 'Description', 'SortName'}
COLORS = ('Red', 'Orange', 'Yellow', 'Green', 'Aqua', 'Blue', 'Purple', 'Magenta')


def number(value, low, high):
    if isinstance(value, bool):
        raise ValueError('숫자 대신 논리값이 저장되어 있습니다.')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('숫자 형식이 아닙니다.') from error
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f'지원 범위 {low}–{high} 밖의 값입니다.')
    return result


def boolean(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.casefold() in ('true', 'false'):
        return value.casefold() == 'true'
    raise ValueError('논리값 형식이 아닙니다.')


def curve(value):
    if isinstance(value, dict):
        value = sequence(value)
    if not isinstance(value, list):
        raise ValueError('커브 점 배열이 아닙니다.')
    if value and isinstance(value[0], str):
        points = [v.split(',') for v in value]
    elif value and isinstance(value[0], (list, tuple, dict)):
        points = [sequence(v) if isinstance(v, dict) else v for v in value]
    else:
        if len(value) % 2:
            raise ValueError('커브 좌표가 짝을 이루지 않습니다.')
        points = [value[i:i+2] for i in range(0, len(value), 2)]
    if not 2 <= len(points) <= 256 or any(len(p) != 2 for p in points):
        raise ValueError('커브 점의 수가 올바르지 않습니다.')
    result = [[number(x, 0, 255)/255, number(y, 0, 255)/255] for x, y in points]
    if any(a[0] >= b[0] for a, b in zip(result, result[1:])):
        raise ValueError('커브 입력 좌표가 오름차순이 아닙니다.')
    return result


def from_lua(text):
    values = parse(text)
    # Lightroom .lrtemplate and saved catalog develop tables are both data.
    if isinstance(values.get('value'), dict) and isinstance(values['value'].get('settings'), dict):
        return values['value']['settings']
    if isinstance(values.get('settings'), dict):
        return values['settings']
    return values


def from_xmp(path):
    with Path(path).open('rb') as file:
        data = file.read(16*1024*1024+1)
    return from_xmp_bytes(data)


def xmp_root(data):
    if len(data) > 16*1024*1024:
        raise DataError('XMP 파일이 지원 크기를 넘었습니다.')
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    text = data.decode(encoding)
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b', text, re.I):
        raise DataError('외부 정의가 포함된 XMP는 읽지 않습니다.')
    try:return ET.fromstring(text)
    except ET.ParseError as error:raise DataError('XMP 문서 구문을 읽지 못했습니다.') from error


def from_xmp_bytes(data):
    root = xmp_root(data)
    values = {}
    def put(key, value):
        if key in values and values[key] != value:
            raise DataError('서로 다른 값이 중복된 XMP 항목입니다: ' + key)
        values[key] = value
    for desc in root.iter('{' + RDF + '}Description'):
        for key, value in desc.attrib.items():
            if key.startswith('{' + CRS + '}'):
                put(key.split('}', 1)[1], value)
        for child in desc:
            if not child.tag.startswith('{' + CRS + '}'):
                continue
            key = child.tag.split('}', 1)[1]
            seq = child.find('{' + RDF + '}Seq')
            if seq is not None:
                put(key, [li.text or '' for li in seq.findall('{' + RDF + '}li')])
            elif len(child) or child.attrib:
                # Retain evidence of an unsupported complex field, not a
                # misleading empty string which would appear to be inactive.
                put(key, {'xml': ET.tostring(child, encoding='unicode')})
            else:
                put(key, child.text or '')
    if not values:
        raise DataError('Adobe 보정 항목이 없는 XMP입니다.')
    return values


def convert(values, *, base=None, is_raw=False, orientation=None):
    if not isinstance(values, dict) or any(not isinstance(k, str) for k in values):
        raise DataError('이름이 있는 Adobe 보정 표가 아닙니다.')
    settings = normalized(base or {})
    mapped, omitted, used = [], {}, set()
    def apply(key, action):
        if key not in values:
            return
        used.add(key)
        try:
            action(values[key])
        except (ValueError, TypeError, KeyError, IndexError) as error:
            omitted[key] = str(error)
        else:
            mapped.append(key)
    if 'AlreadyApplied' in values:
        used.add('AlreadyApplied')
        if boolean(values['AlreadyApplied']):
            return {'settings': settings, 'mapped': [], 'omitted': {'AlreadyApplied':'픽셀에 이미 반영된 보정을 중복 적용하지 않습니다.'}}
    for key, (target, low, high) in SCALARS.items():
        apply(key, lambda value, k=target, lo=low, hi=high: settings.__setitem__(k, number(value, lo, hi)))
    def vignette(value):
        # Lightroom darkens with negative amounts; Grainy's post-crop vignette only darkens (0-100).
        amount = number(value, -100, 100)
        if amount > 0:raise ValueError('밝게 하는 비네팅은 지원하지 않습니다.')
        settings['vignette'] = -amount
    apply('PostCropVignetteAmount', vignette)
    for old, new, target in (('Exposure','Exposure2012','exposure'), ('Contrast','Contrast2012','contrast'), ('Clarity','Clarity2012','clarity')):
        if new not in values:
            low, high = (-4,4) if old == 'Exposure' else (-100,100)
            apply(old, lambda value, k=target, lo=low, hi=high: settings.__setitem__(k, number(value,lo,hi)))
        elif old in values:
            used.add(old)
    apply('ConvertToGrayscale', lambda value: settings.__setitem__('monochrome', boolean(value)))
    for prefix, channel in (('HueAdjustment',0), ('SaturationAdjustment',1), ('LuminanceAdjustment',2)):
        for index, color in enumerate(COLORS):
            def hsl(value, i=index, c=channel):
                settings['hsl'][i][c] = number(value,-100,100)
                settings['mixer_mode'] = 'hsl'
            apply(prefix+color, hsl)
    for index, color in enumerate(('Red','Green','Blue')):
        for channel, suffix in enumerate(('Hue','Saturation')):
            apply(color+suffix, lambda value, i=index, c=channel: settings['calibration'][i].__setitem__(c,number(value,-100,100)))
    for suffix, target in (('',None), ('Red',0), ('Green',1), ('Blue',2)):
        key = 'ToneCurvePV2012'+suffix if 'ToneCurvePV2012'+suffix in values else 'ToneCurve'+suffix
        apply(key, lambda value, t=target: settings.__setitem__('curve',curve(value)) if t is None else settings['rgb_curves'].__setitem__(t,curve(value)))
        # A display name is not a curve definition; only consume it when
        # coordinates were actually supplied and validated.
        if key in mapped:
            used.update(('ToneCurveName','ToneCurveName2012'))
    splits = (('ParametricShadowSplit',25), ('ParametricMidtoneSplit',50), ('ParametricHighlightSplit',75))
    custom_splits = any(k in values and str(values[k]) not in (str(v),str(float(v))) for k,v in splits)
    if not custom_splits:
        used.update(k for k,_ in splits)
        for i,key in enumerate(('ParametricShadows','ParametricDarks','ParametricLights','ParametricHighlights')):
            apply(key,lambda value,i=i: settings['parametric'].__setitem__(i,number(value,-100,100)))
    for index, tone in ((0,'Shadow'), (2,'Highlight')):
        for component, suffix, limit in ((0,'Hue',360),(1,'Saturation',100)):
            apply('SplitToning'+tone+suffix, lambda value,i=index,c=component,hi=limit: settings['grading'][i].__setitem__(c,number(value,0,hi)))
    for index,tone in enumerate(('Shadow','Midtone','Highlight')):
        for component,suffix,low,high in ((0,'Hue',0,360),(1,'Sat',0,100),(2,'Lum',-100,100)):
            apply('ColorGrade'+tone+suffix,lambda value,i=index,c=component,lo=low,hi=high: settings['grading'][i].__setitem__(c,number(value,lo,hi)))
    apply('SplitToningBalance',lambda value: settings.__setitem__('grading_balance',number(value,-100,100)))
    apply('ColorGradeBalance',lambda value: settings.__setitem__('grading_balance',number(value,-100,100)))
    # Absolute Kelvin is only meaningful before RAW demosaiced RGB is balanced.
    wb = str(values.get('WhiteBalance','')).replace(' ','').casefold()
    if is_raw and wb == 'asshot':
        settings.update(raw_mode='as_shot',raw_neutral=None,temperature=0.,tint=0.,kelvin_enabled=False)
        mapped.extend(k for k in ('Temperature','Tint','WhiteBalance') if k in values)
        used.update(('Temperature','Tint','WhiteBalance'))
    elif is_raw and 'Temperature' in values:
        try:
            temperature = number(values['Temperature'],2000,50000)
            tint = number(values.get('Tint',0),-150,150)
        except ValueError as error:
            omitted['WhiteBalance'] = str(error)
        else:
            settings.update(raw_mode='custom',raw_kelvin=temperature,raw_tint=tint,raw_neutral=None,
                            temperature=0.,tint=0.,kelvin_enabled=False)
            mapped.extend(k for k in ('Temperature','Tint','WhiteBalance') if k in values)
        used.update(('Temperature','Tint','WhiteBalance'))
    # Non-rotated normalized crop is supported. Rotation/orientation-dependent
    # Adobe coordinates need a separately verified transform, not guesswork.
    if 'HasCrop' in values:
        used.add('HasCrop')
        try:
            has_crop = boolean(values['HasCrop'])
            if not has_crop:
                settings['crop'] = None
                mapped.append('HasCrop')
                used.update(('CropLeft','CropRight','CropTop','CropBottom','CropAngle','CropConstrainToWarp'))
            elif orientation not in (None,'AB',1,'1') or number(values.get('CropAngle',0),-180,180) != 0:
                omitted['HasCrop'] = '회전된 Adobe 크롭 좌표는 변환하지 않습니다.'
            else:
                keys = ('CropLeft','CropTop','CropRight','CropBottom')
                rect = [number(values[k],0,1) for k in keys]
                if rect[0] >= rect[2] or rect[1] >= rect[3]:
                    raise ValueError('크롭 영역의 순서가 잘못되었습니다.')
                settings['crop'] = rect
                mapped.extend(('HasCrop',*keys));used.update((*keys,'CropAngle'))
        except (ValueError,KeyError) as error:
            omitted['HasCrop'] = str(error)
    for key in values.keys()-used-INFORMATION:
        omitted[key] = '현재 변환하지 않는 Adobe 보정 항목입니다.'
    return {'settings': settings, 'mapped': sorted(set(mapped)), 'omitted': dict(sorted(omitted.items()))}


def summary(result,limit=None):
    text = f'변환한 보정 항목: {len(result["mapped"])}개\n'
    text += 'Luma의 현상 방식으로 적용하므로 Lightroom과 색·명암·효과 강도가 달라질 수 있습니다.'
    if result['omitted']:
        items=list(result['omitted'].items())
        shown=items if limit is None else items[:limit]
        text += f'\n\n변환하지 않은 항목 {len(items)}개:\n' + '\n'.join(f'• {key}: {reason}' for key,reason in shown)
        if len(shown)<len(items):text+=f'\n외 {len(items)-len(shown)}개 · 자세한 내용에서 전체 항목을 확인하세요.'
    return text
