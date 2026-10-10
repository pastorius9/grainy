"""Portable Adobe preset sources with partial, target-aware application."""
import base64
import hashlib
from pathlib import Path
from . import adobe_develop as develop
from .engine import RAW_EXTENSIONS,normalized
from .lua_data import DataError

KEY = '_adobe_preset'
LIMIT = 16*1024*1024
GEOMETRY = ('crop','rotation','straighten','flip')


def values(record):
    if record.get('version') != develop.VERSION or record.get('format') not in ('xmp','lrtemplate'):
        raise DataError('지원하지 않는 Adobe 프리셋 기록입니다.')
    encoded=record.get('data')
    if not isinstance(encoded,str) or len(encoded)>(LIMIT+2)//3*4:
        raise DataError('Adobe 프리셋의 원문 크기가 올바르지 않습니다.')
    try:data=base64.b64decode(encoded,validate=True)
    except ValueError as error:raise DataError('Adobe 프리셋 원문을 읽지 못했습니다.') from error
    if len(data)>LIMIT or hashlib.sha256(data).hexdigest()!=record.get('sha256'):
        raise DataError('Adobe 프리셋 원문의 체크섬이 일치하지 않습니다.')
    return develop.from_xmp_bytes(data) if record['format']=='xmp' else develop.from_lua(data.decode('utf-8-sig'))


def translate(record,base=None,is_raw=True):
    original=normalized(base or {})
    result=develop.convert(values(record),base=original,is_raw=is_raw)
    # A reusable preset must not replace another photograph's composition.
    for key in GEOMETRY:result['settings'][key]=original[key]
    for key in list(result['mapped']):
        if key=='HasCrop' or key.startswith('Crop'):
            result['mapped'].remove(key)
            result['omitted'][key]='사진별 크롭은 프리셋에 적용하지 않습니다.'
    return result


def read(path):
    path=Path(path)
    with path.open('rb') as file:data=file.read(LIMIT+1)
    if len(data)>LIMIT:raise DataError('Adobe 프리셋 파일이 지원 크기를 넘었습니다.')
    record={'version':develop.VERSION,'format':path.suffix.lower().lstrip('.'),
            'data':base64.b64encode(data).decode('ascii'),'sha256':hashlib.sha256(data).hexdigest()}
    result=translate(record)
    if not result['mapped']:raise DataError('변환 가능한 프리셋 보정이 없습니다.\n'+develop.summary(result))
    note='\n\n파일에 지정된 항목만 적용합니다. RAW 색온도는 RAW 사진에만 적용하며 사진별 크롭은 유지합니다.'
    record['report']=develop.summary(result)+note
    record['preview']=develop.summary(result,limit=5)+note
    return {**result['settings'],KEY:record}


def resolve(preset,base,path):
    if KEY not in preset:return normalized(preset)
    return translate(preset[KEY],base=base,is_raw=Path(path).suffix.lower() in RAW_EXTENSIONS)['settings']
