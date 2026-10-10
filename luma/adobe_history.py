"""Bring individually translated Adobe states into the existing Luma history UI."""
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import math
from .adobe_develop import convert, from_lua, VERSION

HISTORY_PREFIX = 'Lightroom 상태 · '


def adobe_time(value):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value):
        return None
    try:
        return (datetime(2001,1,1,tzinfo=timezone.utc)+timedelta(seconds=value)).isoformat()
    except (OverflowError,ValueError):
        return None


def decode_row(row, is_raw, orientation, warnings):
    try:
        result = convert(from_lua(row.get('text') or ''), is_raw=is_raw, orientation=orientation)
    except (ValueError,TypeError,UnicodeError,OverflowError):
        warnings['develop_unreadable'] += 1
        return None
    for key in result['omitted']:
        warnings['develop_field:' + key] += 1
    if not result['mapped']:
        warnings['develop_empty'] += 1
        return None
    return result


def transfer(db, image_id, photo_id, current, *, is_raw, orientation, warnings, cancel_check):
    """Rows stay streaming; only the last 80 successfully translated states
    enter Undo. A gap clears Undo continuity, but individual states remain
    available through history/snapshots and retain the original archive text.
    """
    counts = {'history':0,'snapshots':0}
    undo = deque(maxlen=80)
    for kind, target in (('Adobe_libraryImageDevelopHistoryStep','history'),
                         ('Adobe_libraryImageDevelopSnapshot','snapshots')):
        cursor = db.execute('''SELECT payload FROM lr_stage WHERE kind=? AND image=?
            ORDER BY CAST(json_extract(payload,'$.dateCreated') AS REAL),CAST(id AS INTEGER),id''', (kind,str(image_id)))
        for packed in cursor:
            cancel_check()
            row = json.loads(packed[0])
            converted = decode_row(row, is_raw, orientation, warnings)
            if converted is None:
                if target == 'history':
                    undo.clear()
                continue
            settings = converted['settings']
            name = str(row.get('name') or '이름 없는 상태')
            if converted['omitted']:
                name += ' · 일부 항목 변환'
            timestamp = adobe_time(row.get('dateCreated'))
            if timestamp is None:
                timestamp = datetime.now(timezone.utc).isoformat()
            payload = json.dumps(settings,ensure_ascii=False)
            if target == 'history':
                db.execute('INSERT INTO history(photo_id,label,edits,created) VALUES(?,?,?,?)',
                           (photo_id,HISTORY_PREFIX+name,payload,timestamp))
                if not undo or undo[-1] != settings:
                    undo.append(settings)
            else:
                db.execute('INSERT INTO snapshots(photo_id,name,edits,created) VALUES(?,?,?,?)',
                           (photo_id,'Lightroom · '+name,payload,timestamp))
            counts[target] += 1
    # Lightroom history entries describe the state after an operation. Luma's
    # undo list contains earlier states, so omit a final entry equal to current.
    if undo and undo[-1] == current:
        undo.pop()
    if undo and current is not None:
        db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',
                   (f'undo:{photo_id}',json.dumps({'undo':list(undo),'redo':[]},ensure_ascii=False)))
    return counts
