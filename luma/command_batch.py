"""Frozen command targets and atomic catalog-only edits with guarded batch undo."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path

from .engine import normalized,VIDEO_EXTENSIONS
from .folders import in_folder

BATCH_KEY='undo:command_batch'
SCOPE_LABELS={'current':'현재 사진','selection':'선택한 사진','folder':'현재 폴더 전체'}


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
            raise ValueError('대상 사진 또는 보정값이 바뀌었습니다. 다시 요청해 주세요. 변경한 사진은 없습니다.')


def _preference(catalog,key,value):
    catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(key,catalog.profiles.dumps(value)))


def apply(catalog,records,values,label='Codex 일괄 명령',remember=True,*,batch_key=BATCH_KEY,after_apply=None):
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
            ident=item['id'];row=catalog.photo(ident)
            before=catalog.preference(f'undo:{ident}',{'undo':[],'redo':[]})
            after={'undo':(before.get('undo',[])+[item['settings']])[-80:],'redo':[]}
            catalog.db.execute('INSERT INTO history(photo_id,label,edits,created) VALUES(?,?,?,?)',
                (ident,label,catalog.profiles.dumps(item['settings']),now))
            extras=row['user_metadata'];extras['_last_edit_time']=now
            catalog.db.execute('UPDATE photos SET edits=?,extras=? WHERE id=?',
                (catalog.profiles.dumps(value),json.dumps(extras,ensure_ascii=False),ident))
            _preference(catalog,f'undo:{ident}',after)
            changed.append({'id':ident,'path':item['path'],'before':item['settings'],'after':value,
                            'undo_before':before,'undo_after':after})
        if remember and changed:_preference(catalog,batch_key,{'state':'applied','records':changed})
        if after_apply is not None:after_apply([item['id'] for item in changed])
    return [item['id'] for item in changed]


def restore(catalog,redo=False,*,batch_key=BATCH_KEY,after_restore=None):
    """Restore the most recent group only if no member's edits or undo changed."""
    if catalog.db.in_transaction:raise RuntimeError('다른 저장 작업이 진행 중입니다.')
    with catalog.db:
        catalog.db.execute('BEGIN IMMEDIATE')
        group=catalog.preference(batch_key,{})
        if group.get('state')!=('undone' if redo else 'applied'):
            raise ValueError('다시 실행할 일괄 작업이 없습니다.' if redo else '실행 취소할 일괄 작업이 없습니다.')
        records=group['records'];side='before' if redo else 'after';destination='after' if redo else 'before'
        verify(catalog,[{'id':r['id'],'path':r['path'],'settings':r[side]} for r in records])
        for item in records:
            if catalog.preference(f'undo:{item["id"]}',{'undo':[],'redo':[]})!=item[f'undo_{side}']:
                raise ValueError('대상 사진의 편집 이력이 바뀌었습니다. 개별 사진의 실행 취소를 사용해 주세요.')
        for item in records:
            row=catalog.photo(item['id']);extras=row['user_metadata']
            extras['_last_edit_time']=datetime.now(timezone.utc).isoformat()
            catalog.db.execute('UPDATE photos SET edits=?,extras=? WHERE id=?',
                (catalog.profiles.dumps(item[destination]),json.dumps(extras,ensure_ascii=False),item['id']))
            _preference(catalog,f'undo:{item["id"]}',item[f'undo_{destination}'])
        group['state']='applied' if redo else 'undone';_preference(catalog,batch_key,group)
        if after_restore is not None:after_restore([item['id'] for item in records])
    return [item['id'] for item in records]
