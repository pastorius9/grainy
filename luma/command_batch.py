"""Frozen command targets and atomic catalog-only edits with guarded batch undo."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path

from .engine import normalized,VIDEO_EXTENSIONS
from .folders import in_folder

BATCH_KEY='undo:command_batch'
SCOPE_LABELS={'current':'현재 사진','selection':'선택한 사진','folder':'현재 폴더 전체'}
UNDO_DEPTH=80          # per-photo undo entries kept, as the editor does
CHANGED='대상 사진 또는 보정값이 바뀌었습니다. 다시 요청해 주세요. 변경한 사진은 없습니다.'
HISTORY='대상 사진의 편집 이력이 바뀌었습니다. 개별 사진의 실행 취소를 사용해 주세요.'


class BatchConflict(ValueError):
    """A batch that cannot be applied or restored. kind: 'changed' (a photo's edits or path changed),
    'history' (its undo history changed) or 'none' (no such batch in the expected state)."""
    def __init__(self,message,kind):
        super().__init__(message);self.kind=kind


def capture(catalog,current_id,selected_ids,folder=None):
    current=catalog.photo(current_id) if current_id else None
    folder=folder or (str(Path(current['path']).parent) if current else None)
    folders=[row['id'] for row in catalog.db.execute('SELECT id,path FROM photos ORDER BY id')
        if folder and in_folder(row['path'],folder,False) and Path(row['path']).suffix.lower() not in VIDEO_EXTENSIONS]
    scopes={'current':[current_id] if current else [],'selection':list(dict.fromkeys(selected_ids)),'folder':folders}
    ids=set(i for values in scopes.values() for i in values)
    records={}
    for ident in sorted(ids):
        row=catalog.photo(ident)
        if row:records[ident]={'id':ident,'path':row['path'],'settings':row['settings']}
    scopes={key:[i for i in values if i in records] for key,values in scopes.items()}
    return {'photo_id':current_id,'settings':deepcopy(current['settings']) if current else {},
            'folder':folder,'scopes':scopes,'records':records}


def targets(snapshot,scope):
    if not snapshot:raise ValueError('적용할 사진을 먼저 선택해 주세요.')
    ids=snapshot.get('scopes',{}).get(scope,[])
    if not ids:raise ValueError('해당 범위에 적용할 사진이 없습니다.')
    return [snapshot['records'][ident] for ident in ids]


def describe(snapshot,scope):
    count=len(snapshot.get('scopes',{}).get(scope,[]))
    text=f'{SCOPE_LABELS[scope]} · {count:,}장'
    if scope=='folder':text+=f'\n{snapshot.get("folder") or "폴더 없음"}\n필터에 가려진 사진 포함 · 하위 폴더·영상 제외'
    return text


def verify(catalog,records):
    for item in records:
        now=catalog.photo(item['id'])
        if not now or now['path']!=item['path'] or now['settings']!=item['settings']:
            raise BatchConflict(CHANGED,'changed')


def _preference(catalog,key,value):
    catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(key,catalog.profiles.dumps(value)))


def _extras(catalog,ident,stamp):
    """The photo's user metadata with a new edit time, read without loading its settings again."""
    row=catalog.db.execute('SELECT extras FROM photos WHERE id=?',(ident,)).fetchone()
    extras=json.loads(row[0] or '{}') if row else {}
    extras['_last_edit_time']=stamp
    return json.dumps(extras,ensure_ascii=False)


# --- compact (v2) groups --------------------------------------------------------------------------
# A v1 group keeps every photo's settings before and after plus both whole undo states (up to 162
# settings objects per photo). A v2 group keeps the settings before once and rebuilds the rest; each
# photo's undo preference is compared by the SHA-256 of its stored text, so a later edit, undo or any
# other change of that photo's history refuses the batch instead of overwriting it.

def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _undo_text(catalog,ident):
    """Stored text of the photo's undo preference; a photo without one has the empty state."""
    row=catalog.db.execute('SELECT value FROM preferences WHERE key=?',(f'undo:{ident}',)).fetchone()
    return row[0] if row is not None else catalog.profiles.dumps({'undo':[],'redo':[]})


def _diff(base,entry):
    """entry as a change of base; 'order' only when entry's keys are not in base's order."""
    change={'set':{k:v for k,v in entry.items() if k not in base or base[k]!=v},'drop':[k for k in base if k not in entry]}
    if list(_undiff(base,change))!=list(entry):change['order']=list(entry)
    return change


def _undiff(base,change):
    drop=set(change.get('drop',()))
    result={**{k:v for k,v in base.items() if k not in drop},**change.get('set',{})}
    if 'order' in change:result={k:result[k] for k in change['order']}
    return result


def _state(keys,extra,undo,redo):
    """An undo preference in its original key order."""
    return {key:undo if key=='undo' else redo if key=='redo' else extra[key] for key in keys}


def _compact(item,value,before,before_text,after_text):
    """One photo of a v2 group: the full settings once, plus what rebuilds the rest."""
    old=item['settings']
    change=_diff(old,value)
    if _undiff(old,change)!=value:raise RuntimeError('compact batch record does not rebuild the new settings')
    undo=before.get('undo',[])
    head=undo[:max(0,len(undo)+1-UNDO_DEPTH)]          # entries the 80 cap drops, usually none
    return {'id':item['id'],'path':item['path'],'before':old,'after_set':change['set'],'after_drop':change['drop'],
            **({'after_order':change['order']} if 'order' in change else {}),
            'undo_head':[_diff(old,entry) for entry in head],'redo_before':before.get('redo',[]),
            'undo_extra':{k:v for k,v in before.items() if k not in ('undo','redo')},'undo_keys':list(before),
            'h_before':text_hash(before_text),'h_after':text_hash(after_text)}


def _rebuilt_after(item):
    change={'set':item['after_set'],'drop':item['after_drop']}
    if 'after_order' in item:change['order']=item['after_order']
    return _undiff(item['before'],change)


def apply(catalog,records,values,label='Codex 일괄 명령',remember=True,*,batch_key=BATCH_KEY,after_apply=None,meta=None,compact=False):
    """Write every changed photo in one transaction. remember=True keeps the batch under batch_key for
    restore(): the full (v1) group, or with compact=True a v2 group that stores about one settings object
    per photo. meta is merged into the group (restore keeps it)."""
    if len(records)!=len(values) or len({r['id'] for r in records})!=len(records):
        raise ValueError('일괄 보정 대상이 올바르지 않습니다.')
    if catalog.db.in_transaction:raise RuntimeError('다른 저장 작업이 진행 중입니다.')
    changed=[];now=datetime.now(timezone.utc).isoformat()
    with catalog.db:
        catalog.db.execute('BEGIN IMMEDIATE')
        verify(catalog,records)
        for item,value in zip(records,values):
            value=normalized(value)
            if value==item['settings']:continue
            ident=item['id']
            if compact:before_text=_undo_text(catalog,ident);before=catalog.profiles.loads(before_text)
            else:before=catalog.preference(f'undo:{ident}',{'undo':[],'redo':[]})
            after={'undo':(before.get('undo',[])+[item['settings']])[-UNDO_DEPTH:],'redo':[]}
            catalog.db.execute('INSERT INTO history(photo_id,label,edits,created) VALUES(?,?,?,?)',
                (ident,label,catalog.profiles.dumps(item['settings']),now))
            catalog.db.execute('UPDATE photos SET edits=?,extras=? WHERE id=?',
                (catalog.profiles.dumps(value),_extras(catalog,ident,now),ident))
            after_text=(compact and _after_by_text(catalog.profiles,before,before_text,item['settings'])) or catalog.profiles.dumps(after)
            catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(f'undo:{ident}',after_text))
            if compact:changed.append(_compact(item,value,before,before_text,after_text))
            else:changed.append({'id':ident,'path':item['path'],'before':item['settings'],'after':value,
                                 'undo_before':before,'undo_after':after})
        if remember and changed:
            group={**(meta or {}),'v':2,'state':'applied','records':changed} if compact else {**(meta or {}),'state':'applied','records':changed}
            _preference(catalog,batch_key,group)
        if after_apply is not None:after_apply([item['id'] for item in changed])
    return [item['id'] for item in changed]


def _after_by_text(profiles,before,before_text,settings):
    """apply()'s new undo state as text, from the stored text without serialising the whole stack again;
    None unless the stored text has the form profiles.dumps writes (then the state is serialised)."""
    if list(before)!=['undo','redo']:return None
    prefix,suffix='{"undo": [','], "redo": '+profiles.dumps(before['redo'])+'}'
    if not (before_text.startswith(prefix) and before_text.endswith(suffix)):return None
    inner=before_text[len(prefix):len(before_text)-len(suffix)]
    undo=before['undo']
    for entry in undo[:max(0,len(undo)+1-UNDO_DEPTH)]:   # the oldest entries the 80 cap drops
        first=profiles.dumps(entry)
        if inner==first:inner=''
        elif inner.startswith(first+', '):inner=inner[len(first)+2:]
        else:return None
    if bool(inner)!=(len(undo)>max(0,len(undo)+1-UNDO_DEPTH)):return None
    return prefix+(inner+', ' if inner else '')+profiles.dumps(settings)+'], "redo": []}'


def _undo_by_text(profiles,item,text):
    """The undo state before the batch, rebuilt from the state the batch wrote (text) without parsing the
    photo's whole stack; None when its form is not the one apply() writes. The caller checks the hash."""
    prefix,suffix='{"undo": [','], "redo": []}'
    if not (text.startswith(prefix) and text.endswith(suffix)):return None
    inner=text[len(prefix):len(text)-len(suffix)];last=profiles.dumps(item['before'])
    if inner==last:rest=''
    elif inner.endswith(', '+last):rest=inner[:-len(last)-2]
    else:return None
    entries=[profiles.dumps(_undiff(item['before'],change)) for change in item['undo_head']]+([rest] if rest else [])
    parts=[]
    for key in item['undo_keys']:
        value=('['+', '.join(entries)+']' if key=='undo' else profiles.dumps(item['redo_before']) if key=='redo'
               else profiles.dumps(item['undo_extra'][key]))
        parts.append(json.dumps(key)+': '+value)
    return '{'+', '.join(parts)+'}'


def _redo_by_text(profiles,item,text):
    """The state the batch wrote, rebuilt from the state before it (text); None as above."""
    if item['undo_keys']!=['undo','redo']:return None
    prefix,suffix='{"undo": [','], "redo": '+profiles.dumps(item['redo_before'])+'}'
    if not (text.startswith(prefix) and text.endswith(suffix)):return None
    inner=text[len(prefix):len(text)-len(suffix)]
    for change in item['undo_head']:                      # the entries the 80 cap drops, from the front
        first=profiles.dumps(_undiff(item['before'],change))
        if inner==first:inner=''
        elif inner.startswith(first+', '):inner=inner[len(first)+2:]
        else:return None
    return prefix+(inner+', ' if inner else '')+profiles.dumps(item['before'])+'], "redo": []}'


def _restore_compact(catalog,records,redo):
    """Undo or redo a v2 group: [(id, settings, undo preference text)] to write once every check passed."""
    profiles=catalog.profiles
    verify(catalog,[{'id':r['id'],'path':r['path'],'settings':r['before'] if redo else _rebuilt_after(r)} for r in records])
    writes=[]
    for item in records:
        current_text=_undo_text(catalog,item['id'])
        if text_hash(current_text)!=(item['h_before'] if redo else item['h_after']):raise BatchConflict(HISTORY,'history')
        expected=item['h_after'] if redo else item['h_before']
        text=(_redo_by_text if redo else _undo_by_text)(profiles,item,current_text)
        if text is None or text_hash(text)!=expected:
            current=profiles.loads(current_text)            # an unusual stored form: rebuild from the parsed state
            if redo:state={'undo':(current.get('undo',[])+[item['before']])[-UNDO_DEPTH:],'redo':[]}
            else:
                head=[_undiff(item['before'],change) for change in item['undo_head']]
                state=_state(item['undo_keys'],item['undo_extra'],head+current.get('undo',[])[:-1],item['redo_before'])
            text=profiles.dumps(state)
        if text_hash(text)!=expected:raise BatchConflict(HISTORY,'history')     # a defensive self-check
        writes.append((item['id'],_rebuilt_after(item) if redo else item['before'],text))
    return writes


def restore(catalog,redo=False,*,batch_key=BATCH_KEY,after_restore=None,expect=None):
    """Restore the most recent group only if no member's edits or undo changed. expect: the group's id."""
    if catalog.db.in_transaction:raise RuntimeError('다른 저장 작업이 진행 중입니다.')
    with catalog.db:
        catalog.db.execute('BEGIN IMMEDIATE')
        group=catalog.preference(batch_key,{})
        if group.get('state')!=('undone' if redo else 'applied'):
            raise BatchConflict('다시 실행할 일괄 작업이 없습니다.' if redo else '실행 취소할 일괄 작업이 없습니다.','none')
        if expect is not None and group.get('id')!=expect:
            raise BatchConflict('실행 취소할 일괄 작업이 없습니다.','none')
        records=group['records']
        if group.get('v')==2:writes=_restore_compact(catalog,records,redo)
        else:
            side='before' if redo else 'after';destination='after' if redo else 'before'
            verify(catalog,[{'id':r['id'],'path':r['path'],'settings':r[side]} for r in records])
            for item in records:
                if catalog.preference(f'undo:{item["id"]}',{'undo':[],'redo':[]})!=item[f'undo_{side}']:
                    raise BatchConflict(HISTORY,'history')
            writes=[(item['id'],item[destination],catalog.profiles.dumps(item[f'undo_{destination}'])) for item in records]
        stamp=datetime.now(timezone.utc).isoformat()
        for ident,settings,state in writes:
            catalog.db.execute('UPDATE photos SET edits=?,extras=? WHERE id=?',
                (catalog.profiles.dumps(settings),_extras(catalog,ident,stamp),ident))
            catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(f'undo:{ident}',state))
        group['state']='applied' if redo else 'undone';_preference(catalog,batch_key,group)
        if after_restore is not None:after_restore([item['id'] for item in records])
    return [item['id'] for item in records]
