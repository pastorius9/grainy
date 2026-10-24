"""Photo operations shared by the menus and the AI connection (MCP).

No widgets and no GUI objects (importing commands for EDIT_FIELDS brings in QtCore, which is harmless).
Every function that takes a `catalog` runs on the GUI thread (the catalog connection is GUI-thread only);
everything else is safe in any thread. ServiceError texts are English: they go to the AI program, never
to the Grainy window.
"""
from copy import deepcopy
from datetime import datetime,timezone
import json
import math
import re
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event,Lock

from .edit_fields import EDIT_FIELDS

FIELDS={**EDIT_FIELDS,'negative_density':('필름 농도',-3,3),'negative_range':('필름 대비',3,10),   # app.py negative sliders
        'film_strength':('필름 강도',0,100),'film_look':('필름',0,0),                                  # film_look: a stock's name
        'rotation':('회전',0,270),'flip':('좌우 반전',0,1)}       # rotation: degrees clockwise; the editor stores quarter turns
NUMERIC=[k for k in FIELDS if k not in ('monochrome','film_look','flip')]
NEGATIVE_ONLY=('negative_density','negative_range')
CURVES=('curve','rgb_curves')    # tone curve points as the editor stores them: [[x, y], ...]; rgb_curves [red, green, blue]
CURVE_POINTS=16
# apply_edits copy_from groups: Grainy's copy/sync groups (manager.EDIT_GROUPS) by an English name. HDR editing is off.
COPY_GROUPS={'working_space':'작업 색공간','light':'빛','white_balance':'화이트 밸런스','raw_profile':'RAW 프로파일',
             'color':'색상','film':'필름','color_filter':'컬러 필터','curves':'커브','detail':'디테일','effects':'효과',
             'negative':'네거티브 반전','lens':'렌즈·원근','crop':'크롭·회전','masks':'마스크','retouch':'복구'}
GEOMETRY=('crop','rotation','straighten','flip')   # what a preset leaves as it is (app.apply_preset)
MAX_TARGETS=500
MAX_ID=2**53-1
MCP_DEPTH=5                      # MCP batches that can be undone in order (owner decision)
INDEX_KEY='mcp_batches'          # catalog preference: [{id, tool, label, count, created, state}] oldest first
BATCH_PREFIX='undo:mcp:'         # must start with 'undo:' (profile_store.payload_columns scans undo:%)
LABELS={'red':'빨강','yellow':'노랑','green':'초록','blue':'파랑','purple':'보라'}   # stored Korean names
LABEL_NAMES={v:k for k,v in LABELS.items()}
FLAGS={'picked':1,'rejected':-1,'unflagged':0}
SORTS=('imported','name','capture','rating')


class ServiceError(ValueError):
    """A request the caller can correct. str(error) is English text for the AI client (never shown in the GUI)."""


class Busy(Exception):
    """Grainy did not start this request, so nothing was changed. str() is English."""


# --- validation and composition -------------------------------------------------------------------

def _number(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)


def curve_points(value,key='curve'):
    """A curve as the editor stores it, [[x, y], ...] as floats; ServiceError unless it has 2 to 16 points with
    x and y in 0..1 and x rising from 0 to 1 (the editor's end points stay at x 0 and 1)."""
    message=f'{key} must be 2 to {CURVE_POINTS} [x, y] points, x and y 0..1, x rising from 0 to 1'
    if not isinstance(value,list) or not 2<=len(value)<=CURVE_POINTS:raise ServiceError(message)
    points=[]
    for point in value:
        if not isinstance(point,list) or len(point)!=2 or not all(_number(v) and 0<=v<=1 for v in point):raise ServiceError(message)
        points.append([float(point[0]),float(point[1])])
    xs=[x for x,_ in points]
    if xs[0]!=0 or xs[-1]!=1 or any(b<=a for a,b in zip(xs,xs[1:])):raise ServiceError(message)
    return points


def parse_changes(set_values=None,add_values=None):
    """(changes, operations) : {key: float|bool}, {key: 'set'|'add'}.
    curve and rgb_curves (set only) are lists of points."""
    set_values=set_values or {};add_values=add_values or {}
    if not isinstance(set_values,dict) or not isinstance(add_values,dict):raise ServiceError('set and add must be objects')
    if not set_values and not add_values:raise ServiceError('give at least one value in set or add')
    changes={};operations={}
    for operation,values in (('set',set_values),('add',add_values)):
        for key,value in values.items():
            if key in CURVES:
                if operation!='set':raise ServiceError(f'{key} can only be set')
                if key=='curve':changes[key]=curve_points(value)
                else:
                    if not isinstance(value,list) or len(value)!=3:raise ServiceError('rgb_curves must be 3 curves: red, green, blue')
                    changes[key]=[curve_points(c,f'rgb_curves[{n}]') for n,c in enumerate(value)]
                operations[key]=operation;continue
            if key not in FIELDS:raise ServiceError(f'{key} is not an edit value Grainy accepts')
            if key in changes:raise ServiceError(f'{key} is given in both set and add')
            _,low,high=FIELDS[key]
            if key in ('monochrome','flip'):
                if operation!='set':raise ServiceError(f'{key} can only be set (true or false)')
                if not isinstance(value,bool):raise ServiceError(f'{key} must be true or false')
                changes[key]=value
            elif key=='rotation':
                if not _number(value) or value%90 or (operation=='set' and not 0<=value<=270) or abs(value)>270:
                    raise ServiceError('rotation must be 0, 90, 180 or 270 (add: a multiple of 90 up to 270)')
                changes[key]=float(value)
            elif key=='film_look':
                if operation!='set' or not isinstance(value,str):raise ServiceError('film_look can only be set, to a film name')
                from . import film_look
                found,close=film_look.find(value)
                if found is None:
                    raise ServiceError(f'no film named {value!r}'+(f'; close: {", ".join(close)}' if close else
                                                                   '; call list_films for the film names'))
                changes[key]=found
            else:
                if not _number(value):raise ServiceError(f'{key} must be a number')
                if operation=='set' and not low<=value<=high:raise ServiceError(f'{key} must be between {low} and {high}')
                if operation=='add' and abs(value)>high-low:raise ServiceError(f'{key} change must be between {low-high} and {high-low}')
                changes[key]=float(value)
            operations[key]=operation
    return changes,operations


def canvas(settings,info):
    """Oriented (width, height) of the uncropped photo, or None when the catalog does not know it yet."""
    w,h=info.get('width'),info.get('height')
    if not w or not h:return None
    return (h,w) if int(settings.get('rotation') or 0)%2 else (w,h)


def edited(settings,changes,operations,info,ident):
    """settings with one group of changes, as the editor's sliders would make them (pure): a change in a panel
    that is switched off switches it on, as in the editor (panel_keys.switch_on)."""
    from .engine import normalized
    from .panel_keys import switch_on,changed_keys
    if any(key in changes for key in NEGATIVE_ONLY) and not settings.get('negative'):
        raise ServiceError(f'photo {ident} is not an inverted negative; negative_density and negative_range apply only after invert_negatives')
    result=deepcopy(settings);previous_angle=float(result.get('straighten') or 0)
    if 'film_look' in changes:
        from .film_look import with_film
        result=with_film(result,changes['film_look'],explicit=set(changes))
    for key,value in changes.items():
        if key in ('monochrome','flip'):result[key]=bool(value);continue
        if key=='film_look':continue
        if key in CURVES:result[key]=deepcopy(value);continue
        if key=='rotation':
            # The editor's 90° turn (photo_actions.transform_selected): quarter turns, and the crop starts over.
            turns=int(value//90)
            quarter=turns%4 if operations.get(key)=='set' else (int(result.get('rotation') or 0)+turns)%4
            if quarter!=int(result.get('rotation') or 0):result['rotation']=quarter;result['crop']=None
            continue
        _,low,high=FIELDS[key]
        if operations.get(key)=='add':value=max(low,min(high,float(result.get(key) or 0)+value))
        result[key]=float(value)
    # Moving Highlights or Shadows takes the photo to the current process (app.py set_setting).
    if ('highlights' in changes or 'shadows' in changes) and result.get('tone_version')==2:result['tone_version']=3
    if 'straighten' in changes and not settings.get('upright'):
        size=canvas(result,info)
        if size is None:raise ServiceError(f'photo {ident}: its size is not known yet; open it once in Grainy')
        from . import straighten
        result['crop']=straighten.refit(result.get('crop'),previous_angle,float(result['straighten']),*size,None)
    return normalized(switch_on(result,changed_keys(settings,result)))


def compose(settings,groups,info,ident):
    """Each group in order, later ones refining earlier ones: a (changes, operations) pair goes through edited();
    a function (a preset or copied settings) gets the settings so far and returns new ones."""
    result=settings
    for group in groups:
        result=group(result) if callable(group) else edited(result,*group,info,ident)
    return result


def preset_settings(preset,settings,record):
    """settings after applying a preset as the editor's Presets panel does (app.apply_preset): an imported
    preset resolved for this photo, lens and RAW colour settings fitted to it, its crop and rotation kept."""
    from .adobe_preset import resolve
    from .engine import normalized
    from .optics import retarget
    from .rawcolor import retarget as retarget_raw
    try:value=resolve(deepcopy(preset),settings,record['path'])
    except (ValueError,TypeError) as error:raise ServiceError(f'the preset cannot be applied to this photo ({type(error).__name__})') from None
    try:value=retarget(value,record['info'])
    except ValueError:raise ServiceError("the preset's lens profile does not fit this photo's lens") from None
    value=retarget_raw(value,record['info'])
    for key in GEOMETRY:value[key]=deepcopy(settings[key])
    return normalized(value)


def copy_keys(groups=None):
    """The settings copy_from copies: Grainy's default copy items (everything but crop and rotation) or the
    named COPY_GROUPS. GUI-side (manager imports widgets)."""
    from .engine import defaults
    from .manager import EDIT_GROUPS
    if not groups:return set(defaults())-set(EDIT_GROUPS['크롭·회전'])-{'panels_off'}
    return {key for name in groups for key in EDIT_GROUPS[COPY_GROUPS[name]]}


def copied_settings(source,settings,info,keys):
    """settings with `keys` pasted from source, as Grainy's Paste Settings does (app.paste_edits): each pasted
    panel takes the source's on/off state, a lens profile and RAW colour settings are fitted to this photo."""
    from .engine import normalized
    from .panel_keys import carried,panels_of
    from .rawcolor import retarget as retarget_raw
    value=deepcopy(settings);value.update({key:deepcopy(source[key]) for key in keys if key in source})
    value['panels_off']=carried(settings,source,panels_of(keys))
    if 'lensfun' in keys:
        from .optics import retarget
        try:value=retarget(value,info)
        except ValueError:raise ServiceError("the source's lens profile does not fit this photo's lens; copy without the lens group") from None
    return normalized(retarget_raw(value,info))


# --- targets --------------------------------------------------------------------------------------

def where_options(where,sort=None,descending=False,allow_empty=False):
    """library_query.query options for list_photos filters."""
    where=dict(where or {})
    if 'recursive' in where and not where.get('folder'):raise ServiceError('recursive needs folder')
    if not where and not allow_empty:raise ServiceError('where needs at least one filter')
    if sort is not None and sort not in SORTS:raise ServiceError(f'sort must be one of {", ".join(SORTS)}')
    rules={}
    options=dict(search=where.get('text','') or '',folder=where.get('folder'),recursive=bool(where.get('recursive',False)),
        mode='all',rules=rules,collection=where.get('collection'),collapsed=False,stack_orders={},sort=sort,descending=bool(descending))
    flag=where.get('flag')
    if flag in ('picked','rejected'):options['mode']=flag
    elif flag=='unflagged':rules['flag']=0
    elif flag is not None:raise ServiceError('flag must be picked, rejected or unflagged')
    for key in ('rating_min','rating_max'):
        if where.get(key) is not None:rules[key]=where[key]
    if where.get('label') is not None:
        if where['label'] not in LABELS:raise ServiceError(f'label must be one of {", ".join(LABELS)}')
        rules['label']=LABELS[where['label']]
    return options


def query_ids(directory,where,*,sort=None,descending=False,cancel=None,allow_empty=False):
    """Ids matching list_photos filters, in list order. Any thread."""
    from . import library_query
    options=where_options(where,sort,descending,allow_empty)
    try:return library_query.query(Path(directory)/'catalog.sqlite',options,cancel or Event())['ids']
    except library_query.QueryCancelled:raise ServiceError('the filter took too long; narrow it') from None


def _reader(directory):
    path=(Path(directory)/'catalog.sqlite').resolve()
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
    return db


def page_rows(directory,ids):
    """(dirs, rows) for ids in order: compact list rows, zero/empty fields left out. Any thread."""
    from . import file_access
    ids=list(ids)
    if not ids:return [],[]
    with closing(_reader(directory)) as db:
        found={}
        for start in range(0,len(ids),500):
            part=ids[start:start+500]
            for r in db.execute('SELECT id,path,name,rating,flag,label,virtual_source FROM photos WHERE id IN ('+','.join('?'*len(part))+')',part):
                found[r['id']]=dict(r)
    dirs=[];positions={};rows=[]
    for ident in ids:
        r=found.get(ident)
        if r is None:continue
        folder=str(Path(r['path']).parent)
        if folder not in positions:positions[folder]=len(dirs);dirs.append(folder)
        row={'id':ident,'name':r['name']}
        if r['rating']:row['r']=r['rating']
        if r['flag']:row['f']=r['flag']
        if r['label']:row['l']=LABEL_NAMES.get(r['label'],r['label'])
        if r['virtual_source'] is not None:row['v']=1
        if positions[folder]:row['d']=positions[folder]
        if file_access.access(r['path'])==file_access.MISSING:row['x']=1
        rows.append(row)
    return dirs,rows


def folder_rows(directory,under=None,cancel=None):
    """[{'path', 'n'}] for every folder with photos (the counts of library_query), optionally only those
    inside `under`. Any thread; may refresh the derived browser index like the library list does."""
    from . import library_query
    from .folders import contains_folder
    db=library_query.connect(Path(directory)/'catalog.sqlite')
    try:
        library_query.sync_index(db,cancel or Event())
        rows=[{'path':r['folder'],'n':r['count']} for r in
              db.execute('SELECT MIN(folder) folder,COUNT(*) count FROM browser_index GROUP BY folder_key ORDER BY MIN(folder)')]
    except library_query.QueryCancelled:raise ServiceError('the folder list took too long') from None
    finally:db.close()
    return [r for r in rows if under is None or contains_folder(under,r['path'])]


def freeze(catalog,ids):
    """(records, errors): the photos as they are now; GUI thread."""
    from .engine import VIDEO_EXTENSIONS
    records=[];errors=[]
    for ident in ids:
        row=catalog.photo(ident)
        if row is None:errors.append({'id':ident,'error':'not in the catalog'});continue
        if Path(row['path']).suffix.lower() in VIDEO_EXTENSIONS:errors.append({'id':ident,'error':'video files cannot be edited'});continue
        records.append({'id':ident,'path':row['path'],'name':row['name'],'settings':row['settings'],'info':row['info']})
    return records,errors


# --- summaries ------------------------------------------------------------------------------------

def fmt(value):
    if isinstance(value,bool):return value
    number=round(float(value),2)
    return int(number) if number.is_integer() else number


GROUPS={
    'crop':('crop','upright','perspective_v','perspective_h','aspect_scale','shift_x','shift_y','transform_scale','guides'),
    'parametric_curve':('parametric',),          # curve and rgb_curves are edits (CURVES)
    'color_mix':('hsl','mixer_mode','calibration','point_colors'),
    'grading':('grading','grading_balance'),
    'raw_color':('raw_mode','raw_kelvin','raw_tint','raw_neutral','kelvin','kelvin_enabled','wb_reference'),
    'profile':('dcp_profile','dcp_huesat','dcp_look','dcp_tone','dcp_exposure','working_space'),
    'detail':('texture','dehaze','noise_luma','noise_color','sharpen_radius','sharpen_detail','sharpen_mask'),
    'lens':('distortion','distortion_k2','distortion_k3','lens_vignette','lens_profile','ca_red','ca_blue','defringe',
            'camera_matrix','lensfun','lensfun_enabled','lensfun_distortion','lensfun_tca','lensfun_vignette','lensfun_scale'),
    'negative':('negative','negative_base','negative_peak'),
    'photo_filter':('photo_filter','photo_filter_enabled','photo_filter_density','photo_filter_luminosity'),
    'film':('film_look','film_strength'),
    'tone_mapping':('tone_mapping',),
}
COUNTED=('masks','retouch')
IGNORED=('tone_version','detail_version','video_color','video_time','hdr','hdr_limit','hdr_brightness',
         'sdr_exposure','sdr_compression','upright_crop',
         'panels_off')      # listed by itself, as off
INFO=(('format','format'),('camera','camera'),('lens','lens'),('iso','iso'),('shutter','shutter'),
      ('aperture','aperture'),('focal','focal_length'),('date','date'))


def _shown(key,value):
    """One edit value as get_photo gives it: rotation in degrees, film_look as the film's name (what apply_edits takes;
    the stored id when the film is not among the shipped ones), numbers rounded."""
    if key=='rotation':return fmt(value*90)
    if key=='film_look':
        from . import film_look
        film=film_look.entry(value)
        return film['name'] if film else str(value)
    return fmt(value)


def summary(record):
    """get_photo's view of a catalog row. Only these fields: no GPS, serial numbers, owner names or maker notes."""
    from .engine import defaults
    base=defaults();settings=record['settings'];info=record.get('info') or {}
    result={'id':record['id'],'name':record['name'],'path':record['path']}
    if settings.get('crop'):result['crop']=settings['crop']
    if settings.get('upright'):result['auto_level']=fmt(settings['upright']['rotation'])
    if info.get('width') and info.get('height'):result['size']=[info['width'],info['height']]
    for name,key in INFO:
        value=info.get(key)
        if value not in (None,'',0):result[name]=fmt(value) if _number(value) else str(value)
    for name,key in (('r','rating'),('f','flag')):
        if record.get(key):result[name]=record[key]
    if record.get('label'):result['l']=LABEL_NAMES.get(record['label'],record['label'])
    if record.get('keywords'):result['keywords']=record['keywords']
    edits={k:_shown(k,settings[k]) for k in FIELDS if k in settings and settings[k]!=base[k]}
    point=lambda p:[round(float(v),4) for v in p]
    if settings.get('curve',base['curve'])!=base['curve']:edits['curve']=[point(p) for p in settings['curve']]
    if settings.get('rgb_curves',base['rgb_curves'])!=base['rgb_curves']:
        edits['rgb_curves']=[[point(p) for p in channel] for channel in settings['rgb_curves']]
    if edits:result['edits']=edits
    other=[name for name,keys in GROUPS.items() if not set(keys)<=FIELDS.keys() and any(settings.get(k)!=base.get(k) for k in keys)]
    other+=[f'{key}:{len(settings[key])}' for key in COUNTED if settings.get(key)]
    if other:result['other']=other
    if settings.get('panels_off'):result['off']=list(settings['panels_off'])
    return result


def summaries(records):
    """(dirs, get_photo entries): summary() with each folder once in dirs and d its index (left out when 0), as
    list_photos rows have it. file only when the file name is not the photo's name (a virtual copy is named
    '… · 사본' but uses its master's file); v=1 marks a virtual copy. Any thread."""
    dirs=[];positions={};entries=[]
    for record in records:
        entry=summary(record);path=Path(entry.pop('path'));folder=str(path.parent)
        if folder not in positions:positions[folder]=len(dirs);dirs.append(folder)
        compact={'id':entry.pop('id'),'name':entry.pop('name')}
        if positions[folder]:compact['d']=positions[folder]
        if path.name!=compact['name']:compact['file']=path.name
        if record.get('virtual_source') is not None:compact['v']=1
        compact.update(entry);entries.append(compact)
    return dirs,entries


# --- automatic values (worker thread) -------------------------------------------------------------

def reason(error,path):
    """Why one photo could not be read or computed, in English."""
    from . import file_access
    state=file_access.access(path)
    if state==file_access.MISSING:return 'original missing and no offline preview'
    if state==file_access.DENIED:return 'Grainy may not read the original (system permission)'
    if isinstance(error,OriginalChanged):return 'the original changed while it was read'
    return f'could not read or compute ({type(error).__name__})'


class OriginalChanged(ValueError):pass


def auto_values(records,compute,directory,*,cancel=None,progress=None,strict=True,loader=None):
    """compute(720 px source, settings) for every record, as the menus' batch auto tone does.
    Returns (values, errors); values is None when cancelled. strict=True lets the first failure through
    (the menus); strict=False puts None for that photo and adds {'id', 'error'}. loader replaces
    engine.load_image (the window passes its own reference)."""
    from .engine import load_image,resize_float
    from .preview_store import signature,load_offline
    from . import file_access
    load_image=loader or load_image
    values=[];errors=[]
    for index,record in enumerate(records):
        if cancel is not None and cancel.is_set():return None,errors
        settings=record['settings'];path=Path(record['path'])
        try:
            if file_access.present(path):
                before=signature(path)
                source,_=load_image(path,720,settings['working_space'],raw_options=settings)
                if signature(path)!=before:raise OriginalChanged('원본이 변경되었습니다.')
            else:source,_=load_offline(directory,record['id'],settings)
            values.append(compute(resize_float(source,720),settings))
        except Exception as error:
            if strict:raise
            values.append(None);errors.append({'id':record['id'],'error':reason(error,path)})
        if progress is not None:progress(index+1)
    if cancel is not None and cancel.is_set():return None,errors
    return values,errors


def auto_job(kind,records,amount=1.):
    """(records to load, compute) for an automatic edit. compute returns (settings, report) for tone, color
    and negative; for roll a sample to combine with negative.converted_together afterwards."""
    if kind=='tone':
        from .engine import auto_tone_settings
        return records,auto_tone_settings
    if kind=='color':
        from .auto_color import auto_color_settings
        return records,lambda source,settings:auto_color_settings(source,settings,amount)
    if kind not in ('negative','roll'):raise ValueError(kind)
    from . import negative
    # Loaded as negatives, so camera RAW files arrive in the camera's own colours (workflow_ui.invert_negatives).
    asked=[{**r,'settings':{**r['settings'],'negative':True}} for r in records]
    if kind=='negative':return asked,lambda source,settings:(negative.converted(source,settings),{'status':'applied'})
    return asked,negative.sample


# --- batch stack (GUI thread, on command_batch) ---------------------------------------------------

def batch_index(catalog):
    value=catalog.preference(INDEX_KEY,[])
    return [entry for entry in value if isinstance(entry,dict)] if isinstance(value,list) else []


def last_batch(catalog):
    return next((entry for entry in reversed(batch_index(catalog)) if entry.get('state')=='applied'),None)


def _write_index(catalog,entries):
    # Inside the batch transaction: catalog.save_preference would commit it.
    catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(INDEX_KEY,json.dumps(entries,ensure_ascii=False)))


PRESET_KEY='_preset'             # user metadata: the preset apply_edits applied last (export naming {preset})


def _set_preset(catalog,ident,name):
    """Inside the batch transaction: the photo's PRESET_KEY (None removes it); returns the value before."""
    row=catalog.db.execute('SELECT extras FROM photos WHERE id=?',(ident,)).fetchone()
    extras=json.loads(row[0] or '{}') if row else {}
    before=extras.get(PRESET_KEY)
    if name:extras[PRESET_KEY]=name
    else:extras.pop(PRESET_KEY,None)
    if before!=extras.get(PRESET_KEY):
        catalog.db.execute('UPDATE photos SET extras=? WHERE id=?',(json.dumps(extras,ensure_ascii=False),ident))
    return before


def apply_batch(catalog,records,values,*,label,tool,snapshot_name,snapshotted,switch=True,presets=None):
    """One undoable MCP batch: (batch id or None, changed ids). Each photo the AI edits first in this app
    run gets a pre-AI snapshot; snapshotted is the service's set of those photos. switch: command_batch.apply's
    (False when the values bring their own panel states). presets: {id: preset name} remembered with the
    photos the batch changes (PRESET_KEY), and set back when the batch is undone."""
    from . import command_batch
    entries=batch_index(catalog);taken={entry.get('id') for entry in entries}
    bid='b'+secrets.token_hex(3)
    while bid in taken:bid='b'+secrets.token_hex(3)
    frozen={record['id']:record for record in records}

    def after(ids):
        if not ids:return
        now=datetime.now(timezone.utc).isoformat()
        for ident in ids:
            if ident in snapshotted:continue
            payload=json.dumps(catalog.profiles.pack(deepcopy(frozen[ident]['settings']),internal=True))
            catalog.db.execute('INSERT INTO snapshots(photo_id,name,edits,created) VALUES(?,?,?,?)',(ident,snapshot_name,payload,now))
        named={str(ident):[_set_preset(catalog,ident,presets[ident]),presets[ident]] for ident in ids if (presets or {}).get(ident)}
        remember_batch(catalog,entries,{'id':bid,'tool':tool,'label':label,'count':len(ids),'created':now,'state':'applied',
                                       **({'presets':named} if named else {})})

    if catalog.db.in_transaction:raise Busy('Grainy is saving something else')
    ids=command_batch.apply(catalog,records,values,label,remember=True,batch_key=BATCH_PREFIX+bid,after_apply=after,
                            meta={'id':bid,'tool':tool},compact=True,switch=switch)
    snapshotted.update(ids)
    return (bid if ids else None),ids


def remember_batch(catalog,entries,entry):
    """Inside the write transaction: append either kind of batch, dropping redo and the oldest excess."""
    kept=[e for e in entries if e.get('state')!='undone']
    applied=[e for e in kept if e.get('state')=='applied']
    dropped=[e for e in entries if e.get('state')=='undone']+applied[:max(0,len(applied)-(MCP_DEPTH-1))]
    drop_ids={e.get('id') for e in dropped}
    for item in dropped:catalog.db.execute('DELETE FROM preferences WHERE key=?',(BATCH_PREFIX+str(item.get('id')),))
    _write_index(catalog,[e for e in entries if e.get('id') not in drop_ids]+[entry])


def undo_batch(catalog,batch=None,redo=False):
    """Undo the most recent applied MCP batch (or redo the first undone one): (id, tool, ids)."""
    from . import command_batch
    entries=batch_index(catalog)
    if redo:candidate=next((entry for entry in entries if entry.get('state')=='undone'),None)
    else:candidate=next((entry for entry in reversed(entries) if entry.get('state')=='applied'),None)
    if candidate is None:raise ServiceError('there is no batch to redo' if redo else 'there is no batch to undo')
    cid=candidate.get('id');tool=candidate.get('tool','')
    if batch is not None and batch!=cid:raise ServiceError(f'{batch} is not the most recent batch; the most recent is {cid} ({tool})')

    def flip(ids):
        for entry in entries:
            if entry.get('id')==cid:entry['state']='applied' if redo else 'undone'
        for ident,(before,after) in (candidate.get('presets') or {}).items():
            _set_preset(catalog,int(ident),after if redo else before)
        _write_index(catalog,entries)

    if catalog.db.in_transaction:raise Busy('Grainy is saving something else')
    if tool=='set_labels':
        from .photo_labels import restore
        return cid,tool,restore(catalog,cid,redo,flip)
    try:ids=command_batch.restore(catalog,redo,batch_key=BATCH_PREFIX+str(cid),after_restore=flip,expect=cid)
    except command_batch.BatchConflict as error:
        if error.kind in ('changed','history'):
            raise ServiceError(f'{cid} can no longer be undone: some of its photos were edited or removed afterwards. '
                               'Undo those photos one by one in Grainy instead.') from None
        raise ServiceError('there is no batch to redo' if redo else 'there is no batch to undo') from None
    return cid,tool,ids


# --- export ---------------------------------------------------------------------------------------

TOKENS=('film','preset','camera','date')        # naming tokens that can be empty for a photo
NAMING=re.compile(r'(?:[^{}<>:"/\\|?*\x00-\x1f]|\{stem\}|\{n(?::0[2-6]d)?\}|\{(?:film|preset|camera|date)\})+')
DEVICES={'CON','PRN','AUX','NUL',*{f'COM{i}' for i in range(1,10)},*{f'LPT{i}' for i in range(1,10)}}
OWN_TEXT="a folder Grainy manages itself (its library, settings or program); choose another folder"
EMPTY='\x00'                                   # an empty token while a name is put together
UNSAFE=str.maketrans({**{c:'_' for c in '<>:"/\\|?*'},**{chr(i):'_' for i in range(32)}})


def check_naming(naming):
    """Only {stem}, {n}, {n:02d}..{n:06d}, {film}, {preset}, {camera} and {date} with file-name characters; nothing
    str.format could misuse."""
    message='naming may contain only {stem}, {n} or {n:02d}..{n:06d}, {film}, {preset}, {camera}, {date} and no path characters'
    if not isinstance(naming,str) or not 1<=len(naming)<=80 or naming[-1] in '. ' or not NAMING.fullmatch(naming):
        raise ServiceError(message)
    name=naming.format(stem='photo',n=1,film='Film',preset='Preset',camera='Camera',date='2026-01-01')
    if not name.strip() or Path(name).name!=name or name.split('.')[0].strip().upper() in DEVICES:raise ServiceError(message)


def _safe(value,limit=60):
    """A metadata value as part of a file name: path and control characters become '_', no dot or space at the end."""
    return str(value).translate(UNSAFE).strip()[:limit].rstrip('. ')


def name_values(record):
    """{film, preset, camera, date} of a catalog record for export naming; '' where the photo has none."""
    from . import film_look
    settings=record.get('settings') or {};info=record.get('info') or {};extras=record.get('user_metadata') or {}
    film=film_look.entry(settings.get('film_look'))
    stamp=str(extras.get('DateTimeOriginal') or info.get('date') or '')
    date=re.match(r'(\d{4})[:\-](\d{2})[:\-](\d{2})',stamp)
    return {'film':_safe(film['name']) if film else '','preset':_safe(extras.get(PRESET_KEY) or ''),
            'camera':_safe(info.get('camera') or ''),'date':'-'.join(date.groups()) if date else ''}


def file_stem(naming,stem,n,values):
    """naming filled in: an empty token is dropped together with one separator (space, '_', '-' or '.') before it,
    or after it when it starts the name; a name left empty is the source's stem."""
    name=naming.format(stem=stem,n=n,**{key:values.get(key) or EMPTY for key in TOKENS})
    if EMPTY not in name:return name
    while EMPTY in name:
        name=re.sub('^'+EMPTY+r'[ _.\-]?','',name) if name.startswith(EMPTY) else re.sub(r'[ _.\-]?'+EMPTY,'',name,count=1)
    return name.strip().rstrip('. ') or stem


def export_folder(root,subfolder,own=(),*,create=True):
    """root, or one folder level below it; created when missing. Never inside one of the `own` folders
    (named_folder)."""
    from .folders import contains_folder
    root=Path(root)
    if subfolder is not None:
        bad=(not isinstance(subfolder,str) or not 1<=len(subfolder)<=80 or subfolder in ('.','..')
             or any(c in subfolder for c in '<>:"/\\|?*') or any(ord(c)<32 for c in subfolder)
             or subfolder[-1] in '. ' or subfolder.split('.')[0].strip().upper() in DEVICES)
        if bad:raise ServiceError('subfolder must be one folder name without path characters')
        target=(root/subfolder).resolve()
        if target.parent!=root.resolve():raise ServiceError('subfolder must be one folder name without path characters')
    else:target=root.resolve()
    if any(contains_folder(Path(folder).resolve(),target) for folder in own):
        raise ServiceError('the export folder is inside '+OWN_TEXT)
    if create:
        try:target.mkdir(parents=True,exist_ok=True)
        except OSError as error:raise ServiceError('the export folder cannot be created: '+english_error(error)) from None
    return target


def named_folder(text,own,what='path'):
    """A folder the AI program named: absolute, resolved, and outside Grainy's own folders (`own`: its library,
    settings and program folders), whose files Grainy manages itself. It need not exist."""
    from .folders import contains_folder
    if not isinstance(text,str) or not text.strip() or '\x00' in text:raise ServiceError(f'{what} must be a folder path')
    path=Path(text.strip())
    if not path.is_absolute():raise ServiceError(f'{what} must be an absolute folder path (with its drive or starting at /)')
    try:path=path.resolve()
    except (OSError,RuntimeError):raise ServiceError(f'{what} cannot be used') from None
    if any(contains_folder(Path(folder).resolve(),path) for folder in own):
        raise ServiceError(f'{what} is inside '+OWN_TEXT)
    return path


def image_files(folder,recursive,cancel,limit,*,own=()):
    """The image files Grainy imports in folder (and its subfolders), sorted; None when cancelled, or
    {'error'} past `limit` files. Any thread."""
    from .engine import IMAGE_EXTENSIONS
    found=[]
    for path in (Path(folder).rglob('*') if recursive else Path(folder).iterdir()):
        if cancel.is_set():return None
        if path.suffix.lower() in IMAGE_EXTENSIONS and path.is_file():
            # Validate every resolved file, including file links and directory junctions.
            resolved=named_folder(str(path),own,what='import file')
            found.append(str(resolved))
            if len(found)>limit:
                return {'error':f'more than {limit} image files; import fewer at a time (subfolders, or recursive false)'}
    return sorted(found)


def english_error(error):
    """Stable English at the protocol boundary; never forward localized traceback/OS text."""
    if isinstance(error,PermissionError):return 'permission denied'
    if isinstance(error,FileNotFoundError):return 'file or folder not found'
    if isinstance(error,FileExistsError):return 'output already exists; try again for a new name'
    if isinstance(error,OSError):return 'file system operation failed'
    return 'operation failed ('+(type(error).__name__ if isinstance(error,BaseException) else 'internal error')+')'


def safe_filename(stem,suffix,index=1):
    """A portable component, with space for its suffix AND collision number (UTF-8/UTF-16)."""
    import unicodedata
    stem=unicodedata.normalize('NFC',str(stem)).translate(UNSAFE).strip().rstrip('. ') or 'photo'
    # Windows also treats superscript digits as DOS device numbers.
    device=stem.split('.')[0].strip().upper().translate(str.maketrans('¹²³','123'))
    if device in DEVICES:stem='_'+stem
    tail=('' if index==1 else f'_{index}')+suffix
    while len((stem+tail).encode('utf-8'))>255 or len((stem+tail).encode('utf-16-le'))//2>255:stem=stem[:-1]
    return (stem.rstrip('. ') or 'photo')+tail


def export_plans(records,folder,format='JPEG',naming='{stem}_grainy'):
    """Reserve names in memory only. Real writers still use exclusive creation to close races."""
    import unicodedata
    from .quick_export import SUFFIXES
    key=lambda name:unicodedata.normalize('NFC',name).casefold()
    folder=Path(folder)
    reserved={key(p.name) for p in folder.iterdir()} if folder.is_dir() else set()
    plans=[]
    for n,record in enumerate(records,1):
        suffix=Path(record['path']).suffix if format=='Original' else SUFFIXES[format]
        stem=file_stem(naming,Path(record['path']).stem,n,name_values(record))
        index=1
        while True:
            name=safe_filename(stem,suffix,index);sidecar=Path(name).with_suffix('.xmp').name
            if key(name) not in reserved and (format!='Original' or key(sidecar) not in reserved):break
            index+=1
        reserved.add(key(name))
        if format=='Original':reserved.add(key(sidecar))
        plans.append((record,folder/name))
    return plans


def output_files(outputs,limit=100):
    """Compact file results; stat is done by the worker, never by the GUI completion callback."""
    files=[{'id':o['photo_id'],'name':Path(o['path']).name,'path':o['path'],
            'w':o['size'][0],'h':o['size'][1],'bytes':o.get('bytes')} for o in outputs[:limit]]
    return {'files':files,**({'more_files':len(outputs)-limit} if len(outputs)>limit else {})}


def planned_size(record,longest=0):
    """Dimensions after the same fixed canvas, orientation, crop rounding and resize as engine.export_image."""
    from .engine import load_image,export_longest
    settings=record['settings'];info=record.get('info') or {}
    w,h=info.get('width'),info.get('height')
    if not w or not h:
        _,info=load_image(record['path'],32,settings['working_space'],raw_options=settings)
        w,h=info['width'],info['height']
    if int(settings.get('rotation') or 0)%2:w,h=h,w
    crop=settings.get('crop')
    if crop:
        l,t=min(w-1,max(0,round(crop[0]*w))),min(h-1,max(0,round(crop[1]*h)))
        r,b=max(l+1,min(w,round(crop[2]*w))),max(t+1,min(h,round(crop[3]*h)))
        w,h=r-l,b-t
    side=export_longest((h,w,3),longest) if longest else max(w,h)
    factor=min(1,side/max(w,h))
    return max(1,round(w*factor)),max(1,round(h*factor))


def export_records(records,folder,format='JPEG',quality=95,longest=0,color_space='sRGB',keep_metadata=False,
                   naming='{stem}_grainy',grayscale=True,dither=True,*,cancel=None,progress=None,exporter=None,english=False):
    """Export new files, with dimensions/bytes. English failures for MCP; localized UI errors elsewhere."""
    from concurrent.futures import ThreadPoolExecutor
    from .engine import export_image
    from .quick_export import export_concurrency
    from .workflow_data import edit_signature
    export_image=exporter or export_image
    plans=export_plans(records,folder,format,naming)
    lock=Lock();state={'done':0,'skipped':0}
    def one(plan):
        record,destination=plan
        try:
            if cancel is not None and cancel.is_set():
                with lock:state['skipped']+=1
                return None,None
            if format=='Original':
                from .quick_export import copy_original
                size=copy_original(record,destination)
            else:
                size=export_image(record['path'],destination,record['settings'],format,quality,longest or None,
                    color_space,keep_metadata,record['user_metadata'],record['keywords'],grayscale=grayscale,dither=dither)
            try:byte_size=destination.stat().st_size
            except OSError:byte_size=None
            return {'path':str(destination),'size':size,'bytes':byte_size,'photo_id':record['id'],
                    'edit_signature':edit_signature(record['settings'])},None
        except Exception as error:
            return None,record['name']+': '+('could not export: '+english_error(error) if english else str(error))
        finally:
            if progress is not None:
                with lock:
                    state['done']+=1;progress(state['done'])
    with ThreadPoolExecutor(export_concurrency([record for record,_ in plans]),thread_name_prefix='GrainyExport') as pool:
        results=list(pool.map(one,plans))
    return {'outputs':[output for output,_ in results if output],
            'failures':[failure for _,failure in results if failure],'skipped':state['skipped']}
