"""Photo-specific dust candidates and atomic, independently undoable application."""
from copy import deepcopy
from pathlib import Path

from .command_batch import apply as apply_edits,restore as restore_edits,verify
from .engine import VIDEO_EXTENSIONS
from .folders import in_folder
from .preview_store import signature

BATCH_KEY='undo:dust_batch'


def capture_scope_ids(catalog,current_id,selected_ids,folder):
    """Capture membership without decoding every photo's full edit/profile data."""
    row=catalog.db.execute('SELECT path FROM photos WHERE id=?',(current_id,)).fetchone() if current_id else None
    folder=folder or (str(Path(row['path']).parent) if row else None)
    ids=[r['id'] for r in catalog.db.execute('SELECT id,path FROM photos ORDER BY id')
         if folder and in_folder(r['path'],folder,False) and Path(r['path']).suffix.lower() not in VIDEO_EXTENSIONS]
    return dict(folder=folder,scopes=dict(selection=list(dict.fromkeys(selected_ids)),folder=ids))


def capture_record(catalog,ident):
    row=catalog.photo(ident)
    if row is None:raise ValueError('카탈로그에서 사진을 찾을 수 없습니다.')
    if Path(row['path']).suffix.lower() in VIDEO_EXTENSIONS:
        raise ValueError('먼지 감지는 사진에서 사용할 수 있습니다.')
    return dict(id=ident,path=row['path'],settings=deepcopy(row['settings']),fingerprint=signature(row['path']))


def verify_source(catalog,record):
    verify(catalog,[record])
    if not record['fingerprint'] or signature(record['path'])!=record['fingerprint']:
        raise ValueError('원본 파일이 없거나 바뀌었습니다. 해당 사진을 다시 검출해 주세요.')


def apply_reviewed(catalog,entries,*,after_apply=None):
    """Never infer review from detection or reuse another photo's coordinates."""
    approved=[e for e in entries if e['state']=='reviewed' and e.get('operation',{}).get('spots')]
    records=[];values=[]
    for entry in approved:
        record=entry['record'];verify_source(catalog,record)
        value=deepcopy(record['settings']);value['retouch'].append(deepcopy(entry['operation']))
        records.append(record);values.append(value)
    return apply_edits(catalog,records,values,'먼지 일괄 제거',batch_key=BATCH_KEY,after_apply=after_apply)


def restore(catalog,redo=False,*,after_restore=None):
    return restore_edits(catalog,redo,batch_key=BATCH_KEY,after_restore=after_restore)
