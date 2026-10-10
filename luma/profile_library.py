"""Local DCP discovery with a disposable, incremental index; never copy profiles."""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import os
import sqlite3
from . import dcp
from .user_paths import MAC,local_data

INDEX_VERSION=3  # Recheck cached rejections for triple/custom illuminants.


def profile_roots(directory,extra=()):
    roots=[Path(directory)/'camera-profiles']
    for variable,relative in [('PROGRAMDATA','Adobe/CameraRaw/CameraProfiles'),
            ('APPDATA','Adobe/CameraRaw/CameraProfiles'),('LOCALAPPDATA','Luma/CameraProfiles')]:
        if os.environ.get(variable):roots.append(Path(os.environ[variable])/relative)
    if MAC:
        # Adobe's own macOS locations (all users, then this user), then Grainy's.
        support=Path('Library')/'Application Support'/'Adobe'/'CameraRaw'/'CameraProfiles'
        roots.extend([Path('/')/support,Path.home()/support,local_data('Luma')/'CameraProfiles'])
    roots.extend(Path(p) for p in extra)
    return list(dict.fromkeys(str(p.resolve()) for p in roots))


def scan(roots,cache,cancel=None,limit=20000):
    """Parse changed profiles on a worker. Cache only names/digests/errors, not bytes.

    A scan cancelled or capped never removes index entries; returned rows always
    represent files seen during this scan, including unreadable/unsupported ones.
    """
    cache=Path(cache);cache.parent.mkdir(parents=True,exist_ok=True)
    rows=[];errors=[];seen=set();parsed=0;cancelled=False;truncated=False
    with closing(sqlite3.connect(cache,timeout=10)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS profiles(path TEXT PRIMARY KEY,stamp TEXT,summary TEXT)')
        for root in roots:
            if cancel is not None and cancel.is_set():cancelled=True;break
            if not Path(root).is_dir():continue
            for folder,dirs,files in os.walk(root,followlinks=False,onerror=lambda e:errors.append(str(e))):
                dirs[:]=[n for n in dirs if not Path(folder,n).is_symlink() and not Path(folder,n).is_junction()]
                if cancel is not None and cancel.is_set():cancelled=True;break
                for name in sorted(files):
                    if not name.lower().endswith('.dcp'):continue
                    path=Path(folder,name).resolve();key=os.path.normcase(str(path))
                    if key in seen:continue
                    if len(seen)>=limit:truncated=True;break
                    seen.add(key)
                    if cancel is not None and cancel.is_set():cancelled=True;break
                    summary={'path':str(path),'name':path.stem,'camera':'','copyright':'','sha256':'','error':''}
                    try:
                        stat=path.stat();stamp=f'{INDEX_VERSION}:{stat.st_mtime_ns}:{stat.st_size}'
                        old=db.execute('SELECT summary FROM profiles WHERE path=? AND stamp=?',(key,stamp)).fetchone()
                        if old:
                            try:summary=json.loads(old[0])
                            except (ValueError,TypeError):old=None
                        if not old:
                            parsed+=1
                            try:
                                if stat.st_size>dcp.MAX_PROFILE_BYTES:raise ValueError('DCP가 4MB를 초과합니다.')
                                data=path.read_bytes()
                                summary['sha256']=hashlib.sha256(data).hexdigest()
                                # Extract identity even when a newer transform is unsupported.
                                tags=dcp.read_tags(data,wanted={50936,50708,50942})
                                summary.update(name=str(tags.get(50936,path.stem)),camera=str(tags.get(50708,'')),
                                               copyright=str(tags.get(50942,'')))
                                dcp.parse(data)
                            except (ValueError,TypeError,OSError,OverflowError) as error:summary['error']=str(error)
                            db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?)',(key,stamp,json.dumps(summary)))
                    except OSError as error:summary['error']=str(error)
                    rows.append(summary)
                if cancelled or truncated:break
            if cancelled or truncated:break
        if not cancelled and not truncated:
            stale=[r[0] for r in db.execute('SELECT path FROM profiles') if r[0] not in seen]
            db.executemany('DELETE FROM profiles WHERE path=?',[(p,) for p in stale])
    # Identical profiles in overlapping vendor/user folders appear just once.
    unique={}
    for row in rows:
        key=row['sha256'] or row['path']
        if key in unique:unique[key]['paths'].append(row['path'])
        else:unique[key]={**row,'paths':[row['path']]}
    return dict(profiles=sorted(unique.values(),key=lambda r:(r['camera'].casefold(),r['name'].casefold(),r['path'])),
                files=len(seen),parsed=parsed,errors=errors,cancelled=cancelled,truncated=truncated)


def compatible(rows,metadata,search='',favorites=None,only_favorites=False):
    words=search.casefold().split();favorites=set(favorites or ())
    return [r for r in rows if dcp.camera_matches(r,metadata)
            and (not only_favorites or r['sha256'] in favorites)
            and all(w in (r['name']+' '+r['camera']).casefold() for w in words)]


def load_selected(row,metadata):
    """Revalidate at application time; a changed/deleted file must be rescanned."""
    error=None
    for path in row.get('paths',[row['path']]):
        try:
            record=dcp.load_profile(path)
            if record['sha256']!=row['sha256']:raise ValueError('프로파일이 변경되었습니다. 목록을 다시 검색하세요.')
            if not dcp.camera_matches(record,metadata):raise ValueError('현재 카메라와 다른 프로파일입니다.')
            return record
        except (ValueError,OSError) as e:error=e
    raise ValueError('프로파일을 읽을 수 없습니다. '+str(error or '목록을 다시 검색하세요.'))
