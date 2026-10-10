"""Recover pre-existing missing folders from locally stored photo evidence.

Search is limited to siblings of the highest missing directory. A folder name
is never evidence. Ambiguous, incomplete and already-cataloged matches are left
untouched. Files are only read; edits are never recalculated or replaced here.
"""
from pathlib import Path
from collections import defaultdict
from threading import Event
from time import monotonic
from datetime import datetime
import os,json,hashlib,stat
import numpy as np
from PIL import Image,ImageOps
from .folders import path_key,contains_folder,in_folder,below
from .folder_locations import directory_identity


class SearchIncomplete(Exception):pass


def missing_directory(path):
    try:return not stat.S_ISDIR(Path(path).stat().st_mode)
    except (FileNotFoundError,NotADirectoryError):return True
    except OSError:return False  # Permission and transient connection errors are not renames.


def descriptor(image):
    image=image.convert('RGB');ratio=image.width/image.height
    return np.asarray(image.resize((64,64),Image.Resampling.LANCZOS),dtype=np.float32)/255,ratio


def compare(a,b):
    import cv2
    x,ratio=a;y,other=b
    if abs(ratio/other-1)>.015:return False
    # Flat frames and repetitive/similar-looking scenes cannot identify a folder.
    if x.std()<.055 or np.abs(np.diff(x,axis=0)).mean()+np.abs(np.diff(x,axis=1)).mean()<.008:return False
    # A small low-pass removes JPEG/downsampling ringing without matching only
    # a global histogram; spatial layout and color still have to agree closely.
    error=np.abs(cv2.GaussianBlur(x,(5,5),.8)-cv2.GaussianBlur(y,(5,5),.8))
    return bool(error.mean()<.016 and np.quantile(error,.99)<.09)


def read_evidence(directory,row):
    from .preview_store import files,VERSION
    jpeg,stamp=files(directory,row['id'])
    try:
        if not jpeg.is_file() or jpeg.stat().st_size>4*1024**2:return None
        payload=jpeg.read_bytes();meta=None
        if stamp.exists():
            if stamp.stat().st_size>8192:return None
            meta=json.loads(stamp.read_text(encoding='utf-8'))
            if meta.get('version')!=VERSION or meta.get('path')!=row['path'] or meta.get('token')!=row.get('token'):
                return None
            if hashlib.sha256(payload).hexdigest()!=meta.get('sha256'):return None
        else:
            # Legacy imports stored an unedited JPEG. Avoid stale IDs after a
            # removed record was replaced with a more recently imported photo.
            if jpeg.stat().st_mtime+2<datetime.fromisoformat(row['imported']).timestamp():return None
        import io
        with Image.open(io.BytesIO(payload)) as image:
            if max(image.size)>2048:return None
            value=descriptor(image)
        return dict(value=value,rendered=meta is not None,stamp=meta)
    except (OSError,ValueError,KeyError,TypeError,Image.DecompressionBombError):return None


def candidate_descriptor(path,row,evidence):
    if evidence['rendered']:
        from .engine import load_image,develop,resize_float,output_rgb
        settings=row['settings']
        source,info=load_image(path,1800,settings['working_space'],raw_options=settings)
        original=(info.get('width',source.shape[1]),info.get('height',source.shape[0]))
        result=resize_float(develop(source,settings,output_space=None,original_size=original),320)
        result=output_rgb(result,settings['working_space'],evidence['stamp'].get('color_space','sRGB'))
        image=Image.fromarray(np.uint8(np.clip(result,0,1)*255+.5))
    else:
        from .engine import thumbnail
        image=thumbnail(path)
    return descriptor(image)


def recover_folders(directory,cancel=None,*,sources=None,timeout=120):
    from .catalog import Catalog
    from .engine import normalized
    cancel=cancel or Event();deadline=monotonic()+timeout
    def check():
        if cancel.is_set() or monotonic()>deadline:raise SearchIncomplete()
    with Catalog.open_reader(directory) as catalog:
        rows=[dict(r) for r in catalog.db.execute('''SELECT p.id,p.path,p.metadata,p.edits,p.imported,
            p.virtual_source,s.token FROM photos p LEFT JOIN preview_state s ON s.photo_id=p.id''')]
        for row in rows:row['settings']=normalized(catalog.profiles.loads(row['edits']))
    groups=defaultdict(list);parent_state={};evidence_cache={};stat_cache={};list_cache={};parent_guards={}
    def missing(path):
        key=path_key(path)
        if key not in parent_state:parent_state[key]=missing_directory(path)
        return parent_state[key]
    for row in rows:
        parent=Path(row['path']).parent
        if not missing(parent):continue
        while parent!=parent.parent and missing(parent.parent):parent=parent.parent
        if parent==parent.parent:continue
        if sources and not any(contains_folder(p,parent) or contains_folder(parent,p) for p in sources):continue
        groups[str(parent)].append(row)
    def siblings(parent):
        key=path_key(parent)
        if key in list_cache:return list_cache[key]
        result=[]
        try:
            before=Path(parent).stat().st_mtime_ns
            with os.scandir(parent) as entries:
                for index,entry in enumerate(entries):
                    check()
                    if index>=10000:raise SearchIncomplete()
                    if not entry.is_dir(follow_symlinks=False) or entry.is_symlink():continue
                    if getattr(entry.stat(follow_symlinks=False),'st_reparse_tag',0) in (0xA0000003,0xA000000C):continue
                    result.append(Path(entry.path))
            if Path(parent).stat().st_mtime_ns!=before:raise SearchIncomplete()
        except OSError:raise SearchIncomplete()
        parent_guards[key]=before;list_cache[key]=result;return result
    def file_stat(path):
        check();key=path_key(path)
        if key not in stat_cache:
            try:
                info=path.stat()
                stat_cache[key]=dict(size=info.st_size,mtime_ns=info.st_mtime_ns) if stat.S_ISREG(info.st_mode) else None
            except (FileNotFoundError,NotADirectoryError):stat_cache[key]=None
            except OSError:raise SearchIncomplete()
        return stat_cache[key]
    plans=[];unresolved=[];incomplete=False
    try:
        for source,members in groups.items():
            check();source=Path(source)
            # Virtual copies provide no additional independent evidence.
            unique={}
            for row in sorted(members,key=lambda p:(p['virtual_source'] is not None,p['id'])):
                unique.setdefault(path_key(row['path']),row)
            physical=list(unique.values())
            anchors=[]
            for row in physical:
                info=json.loads(row['metadata'] or '{}');fingerprint=info.get('file_fingerprint')
                if not fingerprint:
                    try:
                        stamp=json.loads((Path(directory)/'thumbnails'/f"{row['id']}.json").read_text(encoding='utf-8'))
                        if stamp.get('path')==row['path'] and stamp.get('token')==row.get('token'):fingerprint=stamp.get('source')
                    except (OSError,ValueError):pass
                row['fingerprint']=fingerprint
                if fingerprint:anchors.append(row)
            # Check discriminating recorded files first; names order candidates
            # only for responsiveness, never as an acceptance criterion.
            ordered=anchors+[p for p in physical if p not in anchors]
            options=sorted(siblings(source.parent),key=lambda p: (p.name.casefold().endswith(source.name.casefold()),p.name),reverse=True)
            matches=[]
            for destination in options:
                check()
                if path_key(destination)==path_key(source):continue
                stats=[];exact=set();valid=True
                for row in ordered:
                    target=destination/below(row['path'],source);info=file_stat(target)
                    if info is None:valid=False;break
                    fingerprint=row['fingerprint']
                    if fingerprint and any(info[k]!=fingerprint.get(k) for k in ('size','mtime_ns')):valid=False;break
                    if fingerprint:exact.add((info['size'],info['mtime_ns']))
                    stats.append((str(target),info))
                if not valid:continue
                proof='file_stats';visual=[]
                # Require three independent exact file signatures, with every
                # other known signature matching and every catalog file present.
                # Older catalogs need three distinct matching saved previews.
                if len(exact)<3:
                    proof='saved_previews';unmatched=0
                    for row in ordered:
                        check()
                        if row['id'] not in evidence_cache:evidence_cache[row['id']]=read_evidence(directory,row)
                        evidence=evidence_cache[row['id']]
                        if evidence is None:continue
                        if not compare(evidence['value'],evidence['value']):continue
                        if any(np.abs(evidence['value'][0]-v['value'][0]).mean()<.035 for v in visual):continue
                        target=destination/below(row['path'],source)
                        try:current=candidate_descriptor(target,row,evidence)
                        except (OSError,ValueError,RuntimeError):valid=False;break
                        if not compare(evidence['value'],current):
                            # A saved preview may predate an edit/decoder change.
                            # It supplies no positive evidence; require three
                            # other distinct matches and stop after two misses.
                            unmatched+=1
                            if unmatched>2:valid=False;break
                            continue
                        visual.append(dict(id=row['id'],value=evidence['value']))
                        if len(visual)>=3:break
                    if not valid or len(visual)<3:continue
                updates=[(str(destination/below(p['path'],source)),p['id']) for p in members]
                outside={path_key(p['path']) for p in rows if not in_folder(p['path'],source)}
                collision=any(path_key(target) in outside for target,_ in updates)
                matches.append(dict(source=str(source),destination=str(destination),updates=updates,found=len(updates),missing=0,
                    physical=len(physical),proof=proof,anchors=[v['id'] for v in visual],collision=collision,
                    expected_paths={str(p['id']):p['path'] for p in members},guards=stats,
                    identity=directory_identity(destination),parent_mtime_ns=parent_guards[path_key(source.parent)]))
            if len(matches)==1 and not matches[0]['collision']:plans.append(matches[0])
            else:unresolved.append(dict(source=str(source),reason='ambiguous' if len(matches)>1 else 'already_cataloged' if matches else 'insufficient_evidence'))
    except SearchIncomplete:incomplete=True
    # One destination must not silently collapse two old source folders.
    destinations=defaultdict(list)
    for plan in plans:destinations[path_key(plan['destination'])].append(plan)
    for group in destinations.values():
        if len(group)>1:
            for plan in group:
                plans.remove(plan);unresolved.append(dict(source=plan['source'],reason='ambiguous'))
    return dict(plans=[] if cancel.is_set() else plans,unresolved=unresolved,total=len(groups),incomplete=incomplete,cancelled=cancel.is_set())


def validate_recovery(catalog,plans):
    """Reject stale worker results before any catalog mutation."""
    rows=[dict(r) for r in catalog.db.execute('SELECT id,path FROM photos')]
    for plan in plans:
        if not missing_directory(plan['source']) or directory_identity(plan['destination'])!=plan['identity']:return False
        try:
            if Path(plan['destination']).parent.stat().st_mtime_ns!=plan['parent_mtime_ns']:return False
        except OSError:return False
        current={str(p['id']):p['path'] for p in rows if in_folder(p['path'],plan['source'])}
        if current!=plan['expected_paths']:return False
        outside={path_key(p['path']) for p in rows if not in_folder(p['path'],plan['source'])}
        if any(path_key(path) in outside for path,_ in plan['updates']):return False
        for path,before in plan['guards']:
            try:
                after=Path(path).stat()
                if after.st_size!=before['size'] or after.st_mtime_ns!=before['mtime_ns']:return False
            except OSError:return False
    return True
