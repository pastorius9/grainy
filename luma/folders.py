"""Catalog-only folder hierarchy; never scans, moves, or modifies source files."""
from __future__ import annotations
import os
import unicodedata
from pathlib import Path
from .user_paths import MAC


def path_key(path):
    key=os.path.normcase(os.path.normpath(str(path)))
    # macOS volumes ignore case and Unicode composition by default, as the catalog's NOCASE paths do.
    return unicodedata.normalize('NFC',key).lower() if MAC else key


def stored_path(path):
    """The text a catalog keeps for a file. macOS lists names decomposed (Finder-made Korean folders),
    which typed searches and path comparisons would miss; the composed form opens the same file."""
    return unicodedata.normalize('NFC',str(path)) if MAC else str(path)


def in_folder(photo_path, folder, recursive=True):
    parent=Path(photo_path).parent
    folder=Path(folder)
    if not recursive:
        return path_key(parent)==path_key(folder)
    try:
        return os.path.commonpath([path_key(parent),path_key(folder)])==path_key(folder)
    except ValueError:  # Different Windows drives.
        return False


def contains_folder(parent,child):
    return in_folder(Path(child)/'__folder_probe__',parent,True)


def below(path,folder):
    """path relative to a folder it is in, compared the way the catalog compares paths.
    Path.relative_to follows the system's rule for names instead: exact on macOS, where a saved
    folder key or a re-typed location differs from the stored path in case or composition."""
    if not contains_folder(folder,Path(path)):raise ValueError(f'{path} is not in {folder}')
    return Path(*Path(path).parts[len(Path(folder).parts):])


def folder_nodes(photos,registered_roots):
    """Return compact trees grouped by drive, with direct and recursive counts.

    Registered import roots are kept; unknown historical paths become roots.
    Nested roots are shown once under the outermost registered ancestor.
    """
    roots={path_key(p):str(Path(p)) for p in registered_roots}
    parents={path_key(Path(p['path']).parent):str(Path(p['path']).parent) for p in photos}
    for key,path in parents.items():
        if not any(contains_folder(root,path) for root in roots.values()):
            roots[key]=path
    compact=[]
    for path in sorted(roots.values(),key=lambda p:(len(Path(p).parts),path_key(p))):
        if not any(contains_folder(root,path) for root in compact):
            compact.append(path)
    nodes={}
    def add(path,parent=None,drive=False):
        key=path_key(path)
        if key not in nodes:
            nodes[key]={'key':key,'path':str(path),'parent':parent,'drive':drive,
                        'name':str(path).rstrip('\\/') if drive else Path(path).name,
                        'direct':0,'total':0}
        return key
    for root in compact:
        root=Path(root)
        drive_key=add(root.anchor,drive=True)
        root_key=add(root,drive_key) if path_key(root)!=drive_key else drive_key
        members=[p for p in list(parents.values())+list(roots.values()) if contains_folder(root,p)]
        for path in members:
            relative=below(path,root)
            current,parent=root,root_key
            for part in relative.parts:
                current=current/part
                parent=add(current,parent)
    for photo in photos:
        key=path_key(Path(photo['path']).parent)
        count=photo.get('count',1)
        nodes[key]['direct']+=count
        while key is not None:
            nodes[key]['total']+=count
            key=nodes[key]['parent']
    return sorted(nodes.values(),key=lambda node:(len(Path(node['path']).parts),node['key']))
