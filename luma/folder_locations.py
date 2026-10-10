"""Read-only directory discovery and transactional catalog path repair."""
from pathlib import Path
from time import monotonic
import os
import stat
import json
from .folders import path_key, contains_folder, in_folder, below, stored_path
from .i18n import tr


def directory_identity(path):
    try:
        info=Path(path).stat()
        if not stat.S_ISDIR(info.st_mode) or not info.st_ino:return None
        # Creation time guards against reused file IDs on Windows. POSIX ctime
        # changes during a rename and must not be used as a creation timestamp.
        birth=getattr(info,'st_birthtime_ns',info.st_ctime_ns if os.name=='nt' else 0)
        return [info.st_dev,info.st_ino,birth]
    except OSError:return None


def rebase(path,source,destination):
    return str(Path(destination)/below(path,source)) if contains_folder(source,path) else str(path)


def scan_folders(paths,roots,expanded,identities,cancel):
    """Scan only catalog folders, their ancestors and expanded direct children.

    No image opens, recursive walks, junction traversal or filename-based guesses.
    Unknown old locations are deliberately left for manual reconnection.
    """
    deadline=monotonic()+3;budget=10000
    def stopped():return cancel.is_set() or monotonic()>deadline
    known={path_key(p):str(Path(p)) for p in paths+roots}
    tracked=dict(known)
    for value in list(known.values()):
        for parent in Path(value).parents:tracked.setdefault(path_key(parent),str(parent))
    observed={};missing=set();moves=[];listing={};children=set();scanned=set()
    def dirs(parent):
        nonlocal budget
        key=path_key(parent)
        if key in listing:return listing[key]
        result=[]
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if stopped() or budget<=0:break
                    budget-=1
                    # Do not traverse symlinks or junctions. Cloud directories
                    # may carry other reparse tags and remain browsable.
                    if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                        info=entry.stat(follow_symlinks=False)
                        if getattr(info,'st_reparse_tag',0) in (0xA0000003,0xA000000C):continue
                        result.append(entry.path)
        except OSError:return []
        listing[key]=result;return result
    for key,path in tracked.items():
        if stopped():break
        identity=directory_identity(path)
        if identity:observed[key]={'path':path,'id':identity}
        else:missing.add(key)
    for key in sorted(missing,key=lambda p:len(Path(p).parts)):
        if stopped() or any(contains_folder(old,key) for old,_ in moves):continue
        saved=identities.get(key)
        if not saved or key not in tracked:continue
        path=Path(tracked[key])
        if path==path.parent or directory_identity(path.parent) is None:continue
        candidates=[p for p in dirs(path.parent) if directory_identity(p)==saved['id']]
        if len(candidates)==1 and path_key(candidates[0])!=key:
            moves.append((str(path),candidates[0]))
    for key,path in known.items():
        if stopped():break
        if key not in observed or key not in expanded and key not in {path_key(p) for p in roots}:continue
        values=dirs(path);children.update(values);scanned.add(key)
    return dict(identities=observed,missing=missing,moves=moves,children=sorted(children),scanned=scanned,
                incomplete=stopped() or budget<=0)


def relink_plan(catalog,source,destination):
    source,destination=Path(source).absolute(),Path(destination).absolute()
    if source==source.parent:raise ValueError(tr('드라이브 전체를 다시 연결할 수 없습니다.'))
    if path_key(source)==path_key(destination):raise ValueError(tr('기존 위치와 다른 폴더를 선택하세요.'))
    if contains_folder(source,destination) or contains_folder(destination,source):
        raise ValueError(tr('기존 폴더의 상위·하위 폴더로 다시 연결할 수 없습니다.'))
    if not destination.is_dir():raise ValueError(tr('다시 연결할 폴더가 없습니다.'))
    rows=[dict(r) for r in catalog.db.execute('SELECT id,path,virtual_source FROM photos')]
    affected=[p for p in rows if in_folder(p['path'],source)]
    outside={path_key(p['path']) for p in rows if not in_folder(p['path'],source)}
    updates=[(str(destination/below(p['path'],source)),p['id']) for p in affected]
    if any(path_key(target) in outside for target,_ in updates):
        raise ValueError(tr('새 위치의 사진이 이미 라이브러리에 있습니다. 중복 연결은 적용하지 않았습니다.'))
    found=sum(Path(target).is_file() for target,_ in updates)
    if affected and not found:raise ValueError(tr('선택한 폴더에서 기존 사진을 찾지 못했습니다. 파일 이름과 하위 폴더 구조를 확인하세요.'))
    return dict(source=str(source),destination=str(destination),updates=updates,found=found,missing=len(updates)-found)


def apply_relink(catalog,plan):
    """Retain photo IDs and every edit; replace the entire folder prefix atomically."""
    return apply_relinks(catalog,[plan])


def apply_relinks(catalog,plans):
    """A batch of locations is one transaction, including all dependent paths."""
    roots=catalog.folder_roots()
    panel=catalog.preference('folder_panel',{})
    watches=catalog.preference('watch_folders',[])
    identities=catalog.preference('folder_identities',{})
    with catalog.db:
        for plan in plans:
            # A location found by listing the disk arrives as the disk spells it (decomposed on macOS).
            source,destination=plan['source'],stored_path(plan['destination'])
            catalog.db.executemany('UPDATE photos SET path=? WHERE id=?',[(stored_path(path),ident) for path,ident in plan['updates']])
            for old in list(roots):
                if not contains_folder(source,old):continue
                new=rebase(old,source,destination)
                catalog.db.execute('DELETE FROM folder_roots WHERE path=?',(old,))
                catalog.db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)',(new,))
                roots.remove(old);roots.append(new)
            if not any(contains_folder(root,destination) for root in roots):
                catalog.db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)',(destination,));roots.append(destination)
            if panel.get('selected'):panel['selected']=rebase(panel['selected'],source,destination)
            panel['expanded']=[rebase(p,source,destination) for p in panel.get('expanded',[])]
            watches=[rebase(p,source,destination) for p in watches]
            identities={k:v for k,v in identities.items() if not contains_folder(source,k)}
        for key,value in [('folder_panel',panel),('watch_folders',watches),('folder_identities',identities)]:
            catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
    return sum(len(plan['updates']) for plan in plans)
