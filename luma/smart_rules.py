"""Validated Lightroom search descriptors, evaluated against live Luma metadata."""
import calendar
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
from .lua_data import parse, sequence, DataError

KEY = '_adobe_smart'
VERSION = 1
LABELS = {1:'빨강',2:'노랑',3:'초록',4:'파랑',5:'보라','none':'','custom':None}
NUMBERS = {'rating':'rating','pick':'flag','isoSpeedRating':'iso'}
TEXT = {'filename':'filename','copyname':'copyname','folder':'folder','labelText':'label',
        'title':'title','caption':'caption','keywords':'keywords','camera':'camera','cameraSN':'serial',
        'lens':'lens','country':'country','state':'state','city':'city','location':'location',
        'creator':'creator','jobIdentifier':'job'}
DATE = {'captureTime':'date','touchTime':'edited'}
COMPARISONS = {'==','!=','>','<','>=','<=','in'}
TEXT_OPS = {'any','all','words','noneOf','beginsWith','endsWith','empty','notEmpty','==','!='}
DATE_OPS = {'==','!=','>','<','in','inLast','notInLast','today','yesterday','thisWeek','thisMonth','thisYear'}
FORMATS = {'DNG','RAW','JPG','TIFF','PNG','PSD','VIDEO','PSB','AVIF','JXL'}


def finite(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise DataError('숫자 검색 조건이 올바르지 않습니다.')
    return value


def translate(text):
    node = parse(text,max_bytes=1024*1024,max_items=10000,max_depth=32)
    if isinstance(node.get('value'),dict):
        node = node['value']
    return {KEY:validate(node)}


def validate(node, depth=0):
    if not isinstance(node,dict) or depth>24:
        raise DataError('검색 조건의 중첩이 지원 범위를 넘었습니다.')
    if 'criteria' not in node:
        mode = node.get('combine','intersect')
        if mode not in ('union','intersect','exclude') or any(type(k) is not int and k!='combine' for k in node):
            raise DataError('지원하지 않는 검색 조합입니다.')
        children = sequence({k:v for k,v in node.items() if type(k) is int})
        if not children or len(children)>1000:
            raise DataError('검색 조건이 비어 있거나 너무 많습니다.')
        return {'combine':mode,'children':[validate(c,depth+1) for c in children]}
    if set(node)-{'criteria','operation','value','value2','value_units'}:
        raise DataError('추가 검색 옵션은 아직 변환하지 않습니다.')
    field, op, value = node.get('criteria'),node.get('operation'),node.get('value')
    if not isinstance(field,str) or not isinstance(op,str):
        raise DataError('검색 항목과 비교 방식은 문자열이어야 합니다.')
    result = {'criteria':field,'operation':op}
    if field in NUMBERS:
        if op not in COMPARISONS:
            raise DataError('지원하지 않는 숫자 비교입니다.')
        result['value'] = finite(value)
        if op=='in':
            result['value2'] = finite(node.get('value2'))
            if result['value']>result['value2']:
                raise DataError('검색 범위의 시작이 끝보다 큽니다.')
    elif field in TEXT:
        if op not in TEXT_OPS or op not in ('empty','notEmpty') and not isinstance(value,str):
            raise DataError('지원하지 않는 문자 비교입니다.')
        if op not in ('empty','notEmpty'):
            result['value'] = value
    elif field in DATE:
        if op not in DATE_OPS:
            raise DataError('지원하지 않는 날짜 비교입니다.')
        if op in ('inLast','notInLast'):
            n = finite(value)
            if n<0 or n>10000 or n!=int(n) or node.get('value_units') not in ('hours','days','weeks','months','years'):
                raise DataError('지원하지 않는 상대 날짜 범위입니다.')
            result.update(value=int(n),value_units=node['value_units'])
        elif op not in ('today','yesterday','thisWeek','thisMonth','thisYear'):
            result['value'] = date.fromisoformat(value).isoformat()
            if op=='in':
                result['value2'] = date.fromisoformat(node.get('value2')).isoformat()
                if result['value']>result['value2']:
                    raise DataError('날짜 범위의 시작이 끝보다 큽니다.')
    elif field=='labelColor':
        if op not in ('==','!=') or type(value) not in (int,str) or value not in LABELS:
            raise DataError('지원하지 않는 색 라벨 조건입니다.')
        result['value'] = value
    elif field=='fileFormat':
        if op not in ('==','!=') or not isinstance(value,str) or value not in FORMATS:
            raise DataError('지원하지 않는 파일 형식 조건입니다.')
        result['value'] = value
    elif field=='hasGPSData':
        if op not in ('isTrue','isFalse'):
            raise DataError('지원하지 않는 GPS 검색 조건입니다.')
    else:
        raise DataError('현재 변환하지 않는 검색 항목: '+str(field))
    return result


def _compare(actual, op, value, value2=None):
    if actual is None:
        return False
    if op=='==':return actual==value
    if op=='!=':return actual!=value
    if op=='>':return actual>value
    if op=='<':return actual<value
    if op=='>=':return actual>=value
    if op=='<=':return actual<=value
    if op=='in':return value<=actual<=value2
    return False


def _date(value):
    if not value:return None
    try:
        text = str(value)
        if re.match(r'^\d{4}:\d\d:',text):text=text.replace(':','-',2)
        parsed = datetime.fromisoformat(text)
        # Capture timestamps may lack a zone; dates remain local camera dates.
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _window(node, now):
    op = node['operation'];today=now.date()
    if op=='today':return today,today
    if op=='yesterday':return today-timedelta(days=1),today-timedelta(days=1)
    if op=='thisWeek':return today-timedelta(days=today.weekday()),today
    if op=='thisMonth':return today.replace(day=1),today
    if op=='thisYear':return today.replace(month=1,day=1),today
    n,unit=node['value'],node['value_units']
    if unit=='hours':return now-timedelta(hours=n),now
    if unit in ('days','weeks'):return today-timedelta(days=n*(7 if unit=='weeks' else 1)),today
    months=n*(12 if unit=='years' else 1)
    absolute=today.year*12+today.month-1-months
    year,month=divmod(absolute,12);month+=1
    if year<1:return date.min,today
    return date(year,month,min(today.day,calendar.monthrange(year,month)[1])),today


def evaluate(node, photo, *, now=None, info=None):
    now = now or datetime.now()
    if info is None:
        info = {**json.loads(photo.get('metadata') or '{}'),**json.loads(photo.get('extras') or '{}')}
    path = Path(photo['path'])
    values = {**info,**photo,'filename':path.name,'folder':str(path.parent),
              'date':info.get('DateTimeOriginal',info.get('date')),'edited':info.get('_last_edit_time'),
              'copyname':info.get('_copy_name','')}
    def test(rule):
        if 'combine' in rule:
            parts=(test(c) for c in rule['children'])
            if rule['combine']=='union':return any(parts)
            if rule['combine']=='exclude':return not any(parts)
            return all(parts)
        field,op=rule['criteria'],rule['operation'];expected=rule.get('value')
        if field in NUMBERS:
            try:actual=float(values.get(NUMBERS[field]))
            except (TypeError,ValueError):return False
            return math.isfinite(actual) and _compare(actual,op,expected,rule.get('value2'))
        if field in TEXT:
            actual=str(values.get(TEXT[field]) or '').casefold()
            if op=='empty':return not actual.strip()
            if op=='notEmpty':return bool(actual.strip())
            value=expected.casefold()
            if field=='labelText':value={'red':'빨강','yellow':'노랑','green':'초록','blue':'파랑','purple':'보라'}.get(value,value)
            if op in ('==','!='):return _compare(actual,op,value)
            if op=='beginsWith':return actual.startswith(value)
            if op=='endsWith':return actual.endswith(value)
            words=re.findall(r'[^\s,]+',value)
            if not words:return False
            if op=='words':return all(re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',actual) for word in words)
            hits=[word in actual for word in words]
            if op=='all':return all(hits)
            if op=='noneOf':return not any(hits)
            return any(hits)
        if field in DATE:
            actual=_date(values.get(DATE[field]))
            if actual is None:return False
            if op in ('==','!=','>','<','in'):
                return _compare(actual.date().isoformat(),op,expected,rule.get('value2'))
            start,end=_window(rule,now)
            found=start<=(actual if isinstance(start,datetime) else actual.date())<=end
            return not found if op=='notInLast' else found
        if field=='labelColor':
            label=values.get('label') or ''
            found=label not in set(LABELS.values()) if expected=='custom' else label==LABELS[expected]
            return found if op=='==' else not found
        if field=='fileFormat':
            from .engine import RAW_EXTENSIONS,VIDEO_EXTENSIONS
            ext=path.suffix.lower()
            actual='DNG' if ext=='.dng' else 'RAW' if ext in RAW_EXTENSIONS else 'VIDEO' if ext in VIDEO_EXTENSIONS else {'jpg':'JPG','jpeg':'JPG','tif':'TIFF','tiff':'TIFF'}.get(ext[1:],ext[1:].upper())
            return _compare(actual,op,expected)
        if field=='hasGPSData':
            try:found=-90<=float(info['latitude'])<=90 and -180<=float(info['longitude'])<=180
            except (KeyError,TypeError,ValueError):found=False
            return found if op=='isTrue' else not found
        return False
    return bool(test(node))


def sql_condition(node):
    """Native SQL for common star/flag/color rules; all-or-nothing fallback.
    Never weaken a compound rule by omitting an unsupported child.
    """
    if 'combine' in node:
        parts=[sql_condition(c) for c in node['children']]
        if any(p is None for p in parts):return None
        operator=' AND ' if node['combine']=='intersect' else ' OR '
        expression='('+operator.join(p[0] for p in parts)+')'
        if node['combine']=='exclude':expression='NOT '+expression
        return expression,[v for p in parts for v in p[1]]
    field,op,value=node['criteria'],node['operation'],node.get('value')
    if field in ('rating','pick'):
        col='p.rating' if field=='rating' else 'p.flag'
        if op=='in':return f'{col} BETWEEN ? AND ?',[value,node['value2']]
        if op in COMPARISONS:return f'{col} {op} ?',[value]
    if field=='labelColor' and op in ('==','!='):
        if value=='custom':return 'p.label '+('NOT IN' if op=='==' else 'IN')+' (?,?,?,?,?,?)',['','빨강','노랑','초록','파랑','보라']
        return 'p.label '+op+' ?',[LABELS[value]]
    return None


def time_dependent(rules):
    if not isinstance(rules,dict):return False
    if KEY in rules:return time_dependent(rules[KEY])
    if rules.get('criteria') in DATE and rules.get('operation') in DATE_OPS-{'==','!=','>','<','in'}:
        return True
    return any(time_dependent(child) for key in ('children','any','all') for child in rules.get(key,[]))


def describe(node, indent=0):
    if 'combine' in node:
        title={'union':'아래 조건 중 하나','intersect':'아래 조건 모두','exclude':'아래 조건 모두 제외'}[node['combine']]
        return ' '*indent+title+'\n'+'\n'.join(describe(c,indent+2) for c in node['children'])
    fields={'rating':'별점','pick':'선택 표시','labelColor':'색 라벨','keywords':'키워드','captureTime':'촬영일','touchTime':'보정일','fileFormat':'파일 형식'}
    return ' '*indent+f'{fields.get(node["criteria"],node["criteria"])} · {node["operation"]} · {node.get("value","")} {node.get("value_units","")}'.rstrip()
