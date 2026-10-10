"""Catalog filtering, guarded filesystem operations and interoperable sidecars."""
from pathlib import Path
from contextlib import closing
from datetime import datetime
import hashlib
import json
import shutil
import sqlite3
import xml.etree.ElementTree as ET
from .engine import normalized
from .folders import below, stored_path


def matches(photo,rules,*,now=None):
    info={**json.loads(photo.get('metadata') or '{}'),**json.loads(photo.get('extras') or '{}')}
    values={**photo,**info,'extension':Path(photo['path']).suffix.lower().lstrip('.'),
            'folder':str(Path(photo['path']).parent),'date':info.get('DateTimeOriginal',info.get('date',''))}
    for key,expected in rules.items():
        if expected in ('',None,[],{}):continue
        if key=='_adobe_smart':
            from .smart_rules import evaluate
            if not evaluate(expected,photo,now=now,info=info):return False
        elif key=='any':
            if not any(matches(photo,r,now=now) for r in expected):return False
        elif key=='all':
            if not all(matches(photo,r,now=now) for r in expected):return False
        elif key=='missing':
            if (not Path(photo['path']).is_file())!=bool(expected):return False
        elif key=='virtual':
            if (photo.get('virtual_source') is not None)!=bool(expected):return False
        elif key.endswith('_min') or key.endswith('_max'):
            try:
                actual=float(values.get(key[:-4],0));limit=float(expected)
                if key.endswith('_min') and actual<limit or key.endswith('_max') and actual>limit:return False
            except (ValueError,TypeError):return False
        elif key=='date_from' or key=='date_to':
            actual=str(values['date']).replace(':','-',2)[:10]
            if not actual or key=='date_from' and actual<expected or key=='date_to' and actual>expected:return False
        elif key=='orientation':
            w,h=info.get('width',0),info.get('height',0)
            if not w or not h:return False
            if expected=='portrait' and w>=h or expected=='landscape' and w<=h or expected=='square' and w!=h:return False
        elif key in ('rating','flag','label'):
            if values.get(key)!=expected:return False
        else:
            if str(expected).casefold() not in str(values.get(key,'')).casefold():return False
    return True


def file_digest(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as file:
        while block:=file.read(1024*1024):digest.update(block)
    return digest.hexdigest()


def duplicate_groups(photos):
    sizes={};seen=set()
    for p in photos:
        path=Path(p['path'])
        if str(path).casefold() in seen or not path.is_file():continue
        seen.add(str(path).casefold());sizes.setdefault(path.stat().st_size,[]).append(p)
    groups=[]
    for candidates in sizes.values():
        if len(candidates)<2:continue
        hashes={}
        for p in candidates:hashes.setdefault(file_digest(p['path']),[]).append(p)
        groups.extend(rows for rows in hashes.values() if len(rows)>1)
    return groups


def move_files(catalog,mapping):
    """Preflight all moves; update every virtual copy sharing the physical source."""
    pairs=[];targets=set()
    for source,destination in mapping.items():
        src,dst=Path(source).resolve(),Path(destination).resolve()
        if src==dst:continue
        if not src.is_file():raise ValueError(f'원본이 없습니다: {src.name}')
        if dst.exists() or str(dst).casefold() in targets:raise ValueError(f'대상 파일이 이미 있습니다: {dst.name}')
        if not dst.parent.is_dir():raise ValueError('대상 폴더가 없습니다.')
        targets.add(str(dst).casefold());pairs.append((src,dst))
    completed=[]
    try:
        for src,dst in pairs:shutil.move(str(src),str(dst));completed.append((src,dst))
        with catalog.db:
            for src,dst in pairs:
                catalog.db.execute('UPDATE photos SET path=?,name=CASE WHEN virtual_source IS NULL THEN ? ELSE ? END WHERE path=? COLLATE NOCASE',
                    (stored_path(dst),Path(stored_path(dst)).name,Path(stored_path(dst)).name+' · 사본',str(src)))
                catalog.db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)',(stored_path(dst.parent),))
    except Exception:
        for src,dst in reversed(completed):
            if dst.exists() and not src.exists():shutil.move(str(dst),str(src))
        raise
    return len(pairs)


def relink(catalog,photo_id,new_path):
    from .engine import IMAGE_EXTENSIONS
    path=Path(new_path).resolve()
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:raise ValueError('지원하는 사진 파일을 선택해 주세요.')
    old=catalog.photo(photo_id)['path']
    catalog.db.execute('UPDATE photos SET path=? WHERE path=? COLLATE NOCASE',(stored_path(path),old));catalog.db.commit()
    catalog.register_folder(path.parent)


def move_folder(catalog,source,destination):
    src,dst=Path(source).resolve(),Path(destination).resolve()
    if not src.is_dir() or src==src.parent:raise ValueError('사진 폴더를 선택해 주세요.')
    if dst.exists() or dst==src or src in dst.parents:raise ValueError('폴더 안쪽이나 기존 폴더로 이동할 수 없습니다.')
    photos=[];roots=[]
    for p in catalog.photos():
        try:photos.append((stored_path(dst/below(Path(p['path']).resolve(),src)),p['id']))
        except ValueError:pass
    for root in catalog.folder_roots():
        try:roots.append((root,stored_path(dst/below(Path(root).resolve(),src))))
        except ValueError:pass
    shutil.move(str(src),str(dst))
    try:
        with catalog.db:
            catalog.db.executemany('UPDATE photos SET path=? WHERE id=?',photos)
            for old,new in roots:
                catalog.db.execute('DELETE FROM folder_roots WHERE path=?',(old,))
                catalog.db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)',(new,))
    except Exception:
        shutil.move(str(dst),str(src));raise


def restore_backup(catalog,path):
    source=Path(path).resolve()
    if source==catalog.directory.joinpath('catalog.sqlite').resolve():raise ValueError('현재 카탈로그와 같은 파일입니다.')
    with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('백업 파일이 손상되었습니다.')
        if not {'photos','history','preferences'}<={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            raise ValueError('Luma 카탈로그 백업이 아닙니다.')
        from .profile_store import verify_references
        verify_references(db)
        catalog.backup(catalog.directory/'backups'/f'before-restore-{datetime.now():%Y%m%d-%H%M%S}.sqlite')
        db.backup(catalog.db)
    catalog._migrate_photos()
    catalog._migrate_browser()
    from .preview_store import migrate
    migrate(catalog.db)
    catalog._migrate_profiles()
    from .dust_store import migrate as migrate_dust
    migrate_dust(catalog.db)


NS={'x':'adobe:ns:meta/','rdf':'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'xmp':'http://ns.adobe.com/xap/1.0/','dc':'http://purl.org/dc/elements/1.1/',
    'lr':'http://ns.adobe.com/lightroom/1.0/','luma':'https://luma.local/ns/1.0/'}
for prefix,uri in NS.items():ET.register_namespace(prefix,uri)


def write_sidecar(photo,path):
    """Standard rating/keywords plus explicit Luma settings, never fake Adobe edits."""
    path=Path(path)
    if path.exists():
        root=ET.parse(path).getroot()
    else:root=ET.Element(f'{{{NS["x"]}}}xmpmeta')
    rdf=root.find(f'{{{NS["rdf"]}}}RDF')
    if rdf is None:rdf=ET.SubElement(root,f'{{{NS["rdf"]}}}RDF')
    desc=ET.SubElement(rdf,f'{{{NS["rdf"]}}}Description')
    # Remove only previous Luma descriptions to preserve third-party metadata.
    for old in list(rdf):
        if old is not desc and f'{{{NS["luma"]}}}Settings' in old.attrib:rdf.remove(old)
    desc.set(f'{{{NS["xmp"]}}}Rating',str(photo['rating']))
    desc.set(f'{{{NS["xmp"]}}}Label',photo.get('label',''))
    desc.set(f'{{{NS["luma"]}}}Settings',json.dumps(photo['settings'],ensure_ascii=False))
    desc.set(f'{{{NS["luma"]}}}Metadata',json.dumps(photo.get('user_metadata',{}),ensure_ascii=False))
    keywords=[k.strip() for k in photo['keywords'].split(',') if k.strip()]
    for namespace,name in [('dc','subject'),('lr','hierarchicalSubject')]:
        bag=ET.SubElement(ET.SubElement(desc,f'{{{NS[namespace]}}}{name}'),f'{{{NS["rdf"]}}}Bag')
        for k in keywords:ET.SubElement(bag,f'{{{NS["rdf"]}}}li').text=k
    temporary=path.with_name(path.name+'.luma-temp')
    ET.ElementTree(root).write(temporary,encoding='utf-8',xml_declaration=True)
    temporary.replace(path)


def read_sidecar(path):
    from .adobe_develop import xmp_root
    with Path(path).open('rb') as file:root=xmp_root(file.read(16*1024*1024+1))
    result={}
    for desc in root.iter(f'{{{NS["rdf"]}}}Description'):
        rating=desc.get(f'{{{NS["xmp"]}}}Rating')
        if rating is not None:result['rating']=max(0,min(5,int(rating)))
        label=desc.get(f'{{{NS["xmp"]}}}Label')
        if label is not None:result['label']=label
        edits=desc.get(f'{{{NS["luma"]}}}Settings')
        if edits is not None:result['settings']=normalized(json.loads(edits))
        meta=desc.get(f'{{{NS["luma"]}}}Metadata')
        if meta is not None:result['user_metadata']=json.loads(meta)
    keywords=root.findall('.//lr:hierarchicalSubject/rdf:Bag/rdf:li',NS) or root.findall('.//dc:subject/rdf:Bag/rdf:li',NS)
    if keywords:result['keywords']=', '.join(dict.fromkeys(x.text for x in keywords if x.text))
    return result


def relink_folder(catalog,source,destination):
    from .folder_locations import relink_plan,apply_relink
    return apply_relink(catalog,relink_plan(catalog,source,destination))
