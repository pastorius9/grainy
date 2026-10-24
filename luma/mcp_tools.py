"""The AI connection's tools: definitions as data, a small JSON-Schema checker and the implementations.

Tool names, descriptions and every error text are English protocol text for the AI program; they never
go through tr(). Results are one text block of compact JSON, after the image blocks where a tool returns
images. Section 10 of the AI connection specification documents each result shape.
"""
import base64
import io
import math
import re
import threading
import time
from pathlib import Path

from . import photo_service
from .photo_service import FIELDS,MAX_ID,MAX_TARGETS,Busy,ServiceError

READ_ANNOTATIONS=dict(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
WRITE_ANNOTATIONS=dict(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False)
TARGET_TEXT="Target: one of ids, where (list_photos filters) or scope ('selection' or 'current')."
EXPORT_FORMATS=['JPEG','TIFF 16-bit','TIFF 8-bit','PNG','PNG 16-bit','AVIF','JPEG XL']
COLOR_SPACES=['sRGB','Adobe RGB','Display P3','ProPhoto RGB']
PREVIEW_BYTES=100_000
SHEET_BYTES=200_000
SHEET_CELLS=36           # contact_sheet: most cells, and the default page (a 36-frame roll in one call)
RESULT_BYTES=300_000
RENDER_MARGIN=5          # seconds left below which no more previews are rendered
UNFINISHED=("Grainy started this call but did not finish it within the time limit; it may still take effect. "
            "Check the photos (get_photo) before repeating it, or undo it with undo_last_batch.")

FILTER={
    'folder':{'type':'string','minLength':1,'maxLength':1024,'description':'Folder path as list_photos shows it.'},
    'recursive':{'type':'boolean','description':'Include subfolders (default false); needs folder.'},
    'text':{'type':'string','minLength':1,'maxLength':200,'description':'Part of the file name or a keyword.'},
    'rating_min':{'type':'integer','minimum':1,'maximum':5},
    'rating_max':{'type':'integer','minimum':0,'maximum':5,'description':'At most N stars; 0 finds unrated photos.'},
    'flag':{'type':'string','enum':['picked','rejected','unflagged']},
    'label':{'type':'string','enum':['red','yellow','green','blue','purple']},
    'collection':{'type':'integer','minimum':1,'maximum':MAX_ID,'description':'Collection id.'}}
SCOPE={'type':'string','enum':['selection','current'],
       'description':'selection: photos selected in Grainy; current: the photo open in the editor.'}
# Inside targets the filters and scope go without their descriptions (list_photos has them): tools/list
# stays under 24,000 characters.
BARE_FILTER={key:{k:v for k,v in value.items() if k!='description'} for key,value in FILTER.items()}


def TARGET(max_ids=MAX_TARGETS):
    return {
        'ids':{'type':'array','items':{'type':'integer','minimum':1,'maximum':MAX_ID},'minItems':1,'maxItems':max_ids,
               'uniqueItems':True},
        'where':{'type':'object','properties':BARE_FILTER,'additionalProperties':False,'minProperties':1},
        'scope':{'type':'string','enum':['selection','current']}}


def _range(low,high):
    return f'{low:g}..{high:g}'


def _edits_description():
    groups={}
    for key,(_,low,high) in FIELDS.items():
        if key in ('monochrome','straighten','exposure','negative_density','negative_range','film_look','rotation','flip'):continue
        groups.setdefault(_range(low,high),[]).append(key)
    ranges='; '.join(f'{", ".join(keys)} {span}' for span,keys in groups.items())
    return ("One undoable batch; entries apply in order. copy_from reads the source's current state, including earlier "
        "entries; groups limits the copy (default all but crop). preset names: list_films type presets. set rejects "
        "out-of-range values; add results clamp to: exposure -5..5 stops; "+ranges+
        "; straighten -45..45 degrees; rotation 0/90/180/270 (crop resets); negative_density -3..3, negative_range 3..10 "
        "after inversion. Set only: monochrome/flip booleans; film_look name ('' none); curve/rgb_curves [r,g,b], "
        "2-16 [x,y] points 0..1 with x rising 0 to 1. After set/add: auto_straighten uses Grainy's auto level, "
        "auto_crop detects scan borders, then crop [left,top,right,bottom] 0..1 in the displayed uncropped frame "
        "(null resets crop).")



def _tool(name,title,description,properties,required=(),read_only=False,idempotent=None):
    annotations=dict(READ_ANNOTATIONS if read_only else WRITE_ANNOTATIONS)
    if idempotent is not None:annotations['idempotentHint']=idempotent
    schema={'type':'object','properties':properties,'additionalProperties':False}
    if required:schema['required']=list(required)
    return {'name':name,'title':title,'description':description,'inputSchema':schema,'annotations':{'title':title,**annotations}}


CURVE={'type':'array','minItems':2,'maxItems':photo_service.CURVE_POINTS,
       'items':{'type':'array','minItems':2,'maxItems':2,'items':{'type':'number','minimum':0,'maximum':1}}}
EDIT_ENTRY={'type':'object','additionalProperties':False,'properties':{**TARGET(),
    'crop':{'type':['array','null'],'minItems':4,'maxItems':4,'items':{'type':'number','minimum':0,'maximum':1}},
    'auto_straighten':{'type':'boolean'},'auto_crop':{'type':'boolean'},
    'preset':{'type':'string','minLength':1,'maxLength':200},
    'copy_from':{'type':'integer','minimum':1,'maximum':MAX_ID},
    'groups':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string','enum':list(photo_service.COPY_GROUPS)}},
    'set':{'type':'object','additionalProperties':False,'minProperties':1,'properties':
           {**{k:{'type':'boolean'} if k in ('monochrome','flip') else {'type':'string'} if k=='film_look'
               else {'type':'integer','enum':[0,90,180,270]} if k=='rotation' else {'type':'number'} for k in FIELDS},
            'curve':CURVE,'rgb_curves':{'type':'array','minItems':3,'maxItems':3,'items':CURVE}}},
    'add':{'type':'object','additionalProperties':False,'minProperties':1,'properties':
           {k:{'type':'number'} for k in FIELDS if k not in ('monochrome','film_look','flip')}}}}
FILM_TYPES=('all','color','bw','slide','cine','presets')

DESTINATION={'destination':{'type':'string','enum':['default','last','path']},
    'path':{'type':'string','minLength':1,'maxLength':1024},'subfolder':{'type':'string','minLength':1,'maxLength':80}}
LABEL_FIELDS={'rating':{'type':'integer','minimum':0,'maximum':5},
    'flag':{'type':'string','enum':list(photo_service.FLAGS)},'label':{'type':'string','enum':[*photo_service.LABELS,'none']}}

DEFINITIONS=[
    _tool('list_photos','List photos',
        "List photos in the Grainy library, filtered and paged (default 50 rows). Rows: id, name, r=rating, f=flag "
        "(1 picked, -1 rejected), l=colour label, v=1 virtual copy, x=1 original missing, d=index into dirs (omitted "
        "when 0). Zero or empty fields are omitted. Give filters or scope ('selection' or 'current'), not both; no filter "
        "lists the whole library. group='folders' lists folders with photo counts instead and takes only folder, text "
        "(part of the folder path), offset and limit.",
        {**FILTER,'scope':SCOPE,'group':{'type':'string','enum':['photos','folders']},
         'sort':{'type':'string','enum':list(photo_service.SORTS)},'descending':{'type':'boolean'},
         'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':200}},read_only=True),
    _tool('get_photo','Photo details',
        "Details and edit values of up to 50 photos. Folders are in dirs with d as in list_photos; file only "
        "when it is not name. edits: non-default values (keys apply_edits accepts); other: other changed tool groups; "
        "off: disabled panels. crop: normalized displayed frame or null; straighten: manual degrees; auto_level: automatic degrees or null.",
        {'ids':{**TARGET(50)['ids']}},required=['ids'],read_only=True),
    _tool('get_preview','Photo previews',
        "Developed JPEG (current edits) of 1-4 photos, size is an upper limit on the long side (default 512); byte limits may shrink it. For many photos use contact_sheet.",
        {'ids':{**TARGET(4)['ids']},'size':{'type':'integer','minimum':128,'maximum':1568},
         'quality':{'type':'integer','minimum':40,'maximum':90}},required=['ids'],read_only=True),
    _tool('contact_sheet','Contact sheet',
        "One JPEG, up to 36 photos, labelled id/rating/P/X. "+TARGET_TEXT+
        " where/scope: offset/count page (default 36); cell upper limit 160 px. variants: one target photo, up to 12 "
        "film_look or preset alternatives, optional labels; no catalog changes. save=true also writes a new JPEG "
        "with file/look names, size upper limit 4000 px (default 2400), using export_photos destinations; returns path.",
        {**TARGET(SHEET_CELLS),'offset':{'type':'integer','minimum':0},'count':{'type':'integer','minimum':1,'maximum':SHEET_CELLS},
         'cell':{'type':'integer','minimum':96,'maximum':256},
         'variants':{'type':'array','minItems':1,'maxItems':12,'items':{'type':'object','additionalProperties':False,
             'properties':{'film_look':{'type':'string','maxLength':200},'preset':{'type':'string','minLength':1,'maxLength':200},
                           'label':{'type':'string','maxLength':80}}}},
         'save':{'type':'boolean'},'size':{'type':'integer','minimum':512,'maximum':4000},**DESTINATION}),
    _tool('list_films','Films and presets',
        "Film looks for apply_edits film_look: name, iso, type (color, bw or slide) and maker; cine=1 marks "
        "motion-picture stocks. type filters them (color, bw, slide, cine; default all); type 'presets' lists Grainy's "
        "develop presets instead (built in, and saved by the person) for apply_edits preset.",
        {'type':{'type':'string','enum':list(FILM_TYPES)}},read_only=True),
    _tool('apply_edits','Apply edits',_edits_description(),
        {'edits':{'type':'array','minItems':1,'maxItems':100,'items':EDIT_ENTRY}},required=['edits']),
    _tool('auto_tone','Auto tone',
        "Grainy's automatic tone (exposure, contrast, highlights, shadows, whites, blacks), computed separately for each "
        "photo and applied as one undoable batch. "+TARGET_TEXT+" Returns the result, or a job id for get_job if it takes "
        "longer than about 40 s.",TARGET()),
    _tool('auto_color','Auto color',
        "Grainy's automatic colour correction (channel curves) per photo, as one undoable batch. strength 0-100 "
        "(default: the strength last used in Grainy). "+TARGET_TEXT,
        {**TARGET(),'strength':{'type':'integer','minimum':0,'maximum':100}}),
    _tool('invert_negatives','Invert negatives',
        "Turn scanned film negatives into positives, as one undoable batch. roll=true measures the film base and "
        "densest part over all targeted photos together (frames of one roll scanned with the same settings; at least 2 "
        "photos); otherwise each photo is measured alone. Refine afterwards with apply_edits (negative_density, "
        "negative_range), auto_tone and auto_color. "+TARGET_TEXT,
        {**TARGET(),'roll':{'type':'boolean'}}),
    _tool('undo_last_batch','Undo last AI batch',
        "Undo the most recent AI batch in this Grainy library (made by any connected AI program), or with redo=true redo "
        "the batch undone last. Up to 5 batches can be undone in order. A batch can no longer be undone once any of its "
        "photos changed again. Includes edits and set_labels.",
        {'batch':{'type':'string','pattern':'^b[0-9a-f]{6}$'},
         'redo':{'type':'boolean'}}),
    _tool('export_photos','Export photos',
        "Export new files; never overwrite (collision numbers added). Returns files with name/path, w/h and bytes "
        "(first 100, then more_files), or a job id for get_job. dry_run=true plans names and pixel sizes without writing; "
        "bytes is null until exported. Defaults: JPEG, sRGB, full size. longest or width/height with fit inside/cover "
        "(preserves ratio, never enlarges). destination default: AI export folder (Pictures/Grainy); last: person's last "
        "export folder; path: absolute folder, created if missing. subfolder adds one level. naming: {stem}, {n:02d}, "
        "{film}, {preset}, {camera}, {date}; missing values drop one separator. JPEG XL: sRGB only. "+TARGET_TEXT,
        {**TARGET(),'format':{'type':'string','enum':EXPORT_FORMATS},'quality':{'type':'integer','minimum':40,'maximum':100},
         'longest':{'type':'integer','minimum':0,'maximum':30000,'description':'Long side px; 0 = full size.'},
         'width':{'type':'integer','minimum':1,'maximum':30000},'height':{'type':'integer','minimum':1,'maximum':30000},
         'fit':{'type':'string','enum':['inside','cover']},
         'color_space':{'type':'string','enum':COLOR_SPACES},'keep_metadata':{'type':'boolean'},
         'naming':{'type':'string','minLength':1,'maxLength':80},'dry_run':{'type':'boolean'},**DESTINATION}),
    _tool('import_folder','Import folder',
        "Import a folder's photos into the library as Grainy's own import does: files already there are skipped; "
        "originals are only read. path: an absolute folder; recursive: include subfolders (default false). Returns the result, or a job id "
        "for get_job; it gives progress and, when done, the new photo ids (list_photos with folder and recursive finds them too).",
        {'path':{'type':'string','minLength':1,'maxLength':1024},'recursive':{'type':'boolean'}},required=['path']),
    _tool('create_virtual_copies','Virtual copies',
        "Make count virtual copies of a photo (Grainy's Make Virtual Copy): new library entries of the same original, "
        "starting from its current edits, e.g. to try different looks. Returns their ids; undo_last_batch does not "
        "remove them.",
        {'id':{'type':'integer','minimum':1,'maximum':MAX_ID},'count':{'type':'integer','minimum':1,'maximum':20}},
        required=['id','count']),
    _tool('set_labels','Rating, flag, label',
        "One undoable batch of ratings (0 none), flags and/or color labels (none clears). "+TARGET_TEXT+
        " Or entries with separate targets/values, applied in order; do not mix both forms. undo_last_batch reverts it.",
        {**TARGET(),**LABEL_FIELDS,'entries':{'type':'array','minItems':1,'maxItems':100,
            'items':{'type':'object','additionalProperties':False,'properties':{**TARGET(),**LABEL_FIELDS}}}}),
    _tool('snapshot','Snapshots',
        "Named snapshots of photo edits, as in Grainy's Snapshot menu. save keeps the current edits under name; list "
        "gives each photo's snapshots, newest first; restore sets the edits back to the newest snapshot of that name as "
        "one undoable batch. Grainy itself saves a 'Before AI edits' snapshot before a photo's first AI edit. "+TARGET_TEXT,
        {**TARGET(),'action':{'type':'string','enum':['save','restore','list']},'name':{'type':'string','minLength':1,'maxLength':80}},
        required=['action']),
    _tool('open_photo','Open photo',
        "Show a photo in the Grainy window (editor or library view). Does not change the photo.",
        {'id':{'type':'integer','minimum':1,'maximum':MAX_ID},'view':{'type':'string','enum':['edit','library']}},
        required=['id'],idempotent=True),
    _tool('get_job','Job progress',
        "Progress or result of a job started by auto_tone, auto_color, invert_negatives, export_photos or import_folder. "
        "Waits up to wait seconds (default 20) for it to finish.",
        {'job':{'type':'string','pattern':'^j[0-9]+$'},'wait':{'type':'integer','minimum':0,'maximum':40}},
        required=['job'],read_only=True,idempotent=False),
]
SCHEMAS={definition['name']:definition['inputSchema'] for definition in DEFINITIONS}     # what the checker uses


def _published(schema,inside_add=False):
    """The schema as tools/list shows it: without uniqueItems and minProperties, the add limits (the whole width
    of each range both ways, which the description gives) and the id maximum (2**53-1, the largest integer JSON
    readers keep exactly), so tools/list stays under 24,000 characters. The checker still applies all of them."""
    if isinstance(schema,list):return [_published(item) for item in schema]
    if not isinstance(schema,dict):return schema
    result={}
    for key,value in schema.items():
        if key in ('uniqueItems','minProperties'):continue
        if inside_add and key in ('minimum','maximum'):continue
        if key=='maximum' and value==MAX_ID:continue
        if key=='properties':result[key]={name:_published(item,inside_add=name=='add' or inside_add) for name,item in value.items()}
        else:result[key]=_published(value,inside_add)
    return result


for _definition in DEFINITIONS:_definition['inputSchema']=_published(_definition['inputSchema'])
# The curves as tools/list shows them: their point counts and the 0..1 range are in the description.
_shown=next(d for d in DEFINITIONS if d['name']=='apply_edits')['inputSchema']['properties']['edits']['items']['properties']['set']
_shown['properties']['curve']={'type':'array','items':{'type':'array','items':{'type':'number'}}}
_shown['properties']['rgb_curves']={'type':'array','minItems':3,'maxItems':3,'items':_shown['properties']['curve']}


# --- argument checker -----------------------------------------------------------------------------

def _is(kind,value):
    if isinstance(kind,list):return any(_is(k,value) for k in kind)
    if kind=='null':return value is None
    if kind=='integer':return isinstance(value,int) and not isinstance(value,bool)
    if kind=='number':return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
    if kind=='string':return isinstance(value,str)
    if kind=='boolean':return isinstance(value,bool)
    if kind=='object':return isinstance(value,dict)
    if kind=='array':return isinstance(value,list)
    return False


def _show(value):
    return f'{value:g}' if isinstance(value,float) else str(value)


def check(schema,value,path='arguments'):
    """Validate value against the JSON-Schema subset these tools use; ServiceError names the place."""
    kind=schema.get('type')
    if kind is not None and not _is(kind,value):
        kind=' or '.join(kind) if isinstance(kind,list) else kind
        article='an' if kind[0] in 'aeiou' else 'a'
        raise ServiceError(f'{path} must be {article} {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ServiceError(f'{path} must be one of '+', '.join(str(v) for v in schema['enum']))
    if 'minimum' in schema and value<schema['minimum']:raise ServiceError(f'{path} must be at least {_show(schema["minimum"])}')
    if 'maximum' in schema and value>schema['maximum']:raise ServiceError(f'{path} must be at most {_show(schema["maximum"])}')
    if isinstance(value,str):
        if len(value)<schema.get('minLength',0):raise ServiceError(f'{path} must have at least {schema["minLength"]} characters')
        if 'maxLength' in schema and len(value)>schema['maxLength']:
            raise ServiceError(f'{path} must have at most {schema["maxLength"]} characters')
        if 'pattern' in schema and not re.search(schema['pattern'],value):raise ServiceError(f'{path} does not have the expected form')
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0):raise ServiceError(f'{path} needs at least {schema["minItems"]} items')
        if 'maxItems' in schema and len(value)>schema['maxItems']:raise ServiceError(f'{path} takes at most {schema["maxItems"]} items')
        if schema.get('uniqueItems'):
            seen=[]
            for item in value:
                key=(type(item).__name__,repr(item))
                if key in seen:raise ServiceError(f'{path} must not repeat items')
                seen.append(key)
        if 'items' in schema:
            for index,item in enumerate(value):check(schema['items'],item,f'{path}[{index}]')
    if isinstance(value,dict):
        properties=schema.get('properties',{})
        for key in schema.get('required',()):
            if key not in value:raise ServiceError(f'{path}.{key} is required')
        if schema.get('additionalProperties') is False:
            for key in value:
                if key not in properties:raise ServiceError(f'{path}.{key} is not a known argument')
        if len(value)<schema.get('minProperties',0):raise ServiceError(f'{path} needs at least {schema["minProperties"]} entries')
        for key,item in value.items():
            if key in properties:check(properties[key],item,f'{path}.{key}')


# --- results --------------------------------------------------------------------------------------

def text_block(value):
    """Compact JSON text; the HTTP body escapes non-ASCII again (mcp_protocol.dumps)."""
    import json
    return {'type':'text','text':json.dumps(value,separators=(',',':'),ensure_ascii=False)}


def image_block(data):
    return {'type':'image','data':base64.b64encode(data).decode('ascii'),'mimeType':'image/jpeg'}


def failure(text):
    return {'content':[{'type':'text','text':text}],'isError':True}


class JobFailure(ServiceError):
    """A failed/cancelled job still carries its partial outputs across the MCP boundary."""
    def __init__(self,job):
        super().__init__(f'job {job.id} {job.state}: {job.error or "no reason given"}')
        self.job=job


def _last_line(error):
    return photo_service.english_error(error)


class Tools:
    """The tool implementations for an McpService (or a test stub with the same attributes).

    Each call runs on a copy bound to the GuiCall of the server the request arrived on (gui). A request left over
    from a server that was stopped finds that GuiCall closed, so it never writes through a server started later."""
    def __init__(self,service,gui=None):
        self.service=service;self.bound=gui

    def definitions(self):return DEFINITIONS

    def call(self,name,arguments,*,modern=False,deadline,gui=None):
        from .mcp_service import Unfinished
        try:
            check(SCHEMAS[name],arguments)
            content=getattr(type(self)(self.service,gui),'tool_'+name)(arguments,deadline)
            return {'content':content,'isError':False}
        except JobFailure as error:
            return {'content':[{'type':'text','text':str(error)},text_block(error.job.view())],'isError':True}
        except ServiceError as error:return failure(str(error))
        except Busy as error:return failure(f'Grainy is busy: {error}. Nothing was changed; try again in a moment.')
        except Unfinished:return failure(UNFINISHED)
        except Exception as error:
            last=_last_line(error)
            self.service.note(name,f'failed: {last}')
            return failure(f'Grainy could not finish this call: {last}')

    # helpers
    @property
    def directory(self):return self.service.directory

    def gui(self,fn,deadline):
        """fn on the GUI thread through this request's own server: Busy once that server was stopped."""
        if self.bound is None:raise Busy('the AI connection is restarting')
        return self.bound.call(fn,deadline)

    def alive(self):
        w=self.service.window()
        if w is None or getattr(w,'closing',True):raise Busy('Grainy is closing or reopening its window')
        return w

    def left(self,deadline):return deadline-time.monotonic()

    def background(self,fn,deadline):
        from .mcp_io import run
        return run(self.service,fn,deadline)

    def destination(self,arguments,deadline,*,create=True):
        """Preferences on the GUI; every directory resolve/check/mkdir on a bounded worker."""
        from .quick_export import previous
        from .mcp_service import ready
        from .mcp_io import check_deadline
        destination=arguments.get('destination','path' if 'path' in arguments else 'default')
        if (destination=='path')!=('path' in arguments):
            raise ServiceError("destination 'path' needs path, and path needs destination 'path'")
        def saved():
            w=ready(self.service.window())
            return previous(w.catalog) if destination=='last' else self.service.preferences.values.get('mcp_export_folder')
        value=self.gui(saved,deadline)
        own=self.service.own_folders()
        def resolve():
            from .mcp_service import pictures_folder
            if destination=='path':root=photo_service.named_folder(arguments['path'],own)
            elif destination=='last':
                if not value:raise ServiceError("the person has not exported from Grainy yet; use destination 'default'")
                root=Path(value['folder'])
                if not root.is_dir():
                    raise ServiceError(f"the folder of the person's last export ({root}) is not available now, for "
                                       "example on a drive that is not connected; use destination 'default'")
            else:root=Path(value) if isinstance(value,str) and value and Path(value).is_dir() else pictures_folder()
            folder=photo_service.export_folder(root,arguments.get('subfolder'),own,create=False)
            check_deadline(deadline)
            if create:
                try:folder.mkdir(parents=True,exist_ok=True)
                except OSError as error:raise ServiceError('the export folder cannot be created: '+photo_service.english_error(error)) from None
            return folder
        return self.background(resolve,deadline)


    def query(self,where,deadline,**options):
        """where -> ids on this thread, cancelled 5 s before the deadline."""
        cancel=threading.Event()
        timer=threading.Timer(max(0.,self.left(deadline)-RENDER_MARGIN),cancel.set);timer.daemon=True;timer.start()
        try:return photo_service.query_ids(self.directory,where,cancel=cancel,**options)
        finally:timer.cancel()

    def one_target(self,arguments):
        given=[key for key in ('ids','where','scope') if key in arguments]
        if len(given)!=1:raise ServiceError('give exactly one of ids, where or scope')
        return given[0]

    @staticmethod
    def scope_ids(w,scope):
        """GUI thread."""
        if scope=='current':
            if w.current_id is None:raise ServiceError('no photo is open in Grainy')
            return [w.current_id]
        if w.library_loading:raise Busy('the photo list is still loading')
        return list(dict.fromkeys(w.selected_ids()))

    def pre_ids(self,arguments,deadline):
        """ids or a where filter, resolved on this thread; None for a scope (resolved in the GUI call)."""
        kind=self.one_target(arguments)
        if kind=='ids':return list(arguments['ids'])
        if kind=='where':return self.query(arguments['where'],deadline)
        return None

    @staticmethod
    def limit(ids):
        if len(ids)>MAX_TARGETS:
            raise ServiceError(f'{len(ids)} photos match; at most {MAX_TARGETS} per call. Narrow the filter or pass ids.')
        return ids

    @staticmethod
    def usable(records,errors):
        if not records:
            shown='; '.join(f"{e['id']}: {e['error']}" for e in errors[:5]) or 'no photos matched'
            raise ServiceError(f'none of the photos can be used: {shown}')

    def read_commit(self,ids):
        """GUI call of the read tools: save the open photo's pending change when it is targeted and safe."""
        from .mcp_service import ready,safe_commit
        w=ready(self.service.window())
        if w.current_id is not None and w.current_id in ids and not safe_commit(w):
            return 'the open photo has unsaved changes; values shown are the last saved ones'
        return None

    def reader_rows(self,ids):
        from .catalog import Catalog
        with Catalog.open_reader(self.directory) as reader:return [reader.photo(i) for i in ids]

    # read tools
    def tool_list_photos(self,arguments,deadline):
        filters={key:arguments[key] for key in FILTER if key in arguments}
        scope=arguments.get('scope')
        if scope and filters:raise ServiceError('give either filters or scope, not both')
        offset=arguments.get('offset',0);limit=arguments.get('limit',50)
        if arguments.get('group')=='folders':
            if scope:raise ServiceError("group 'folders' takes no scope")
            other=[key for key in arguments if key not in ('group','folder','text','offset','limit')]
            if other:raise ServiceError(f"group 'folders' takes only folder, text, offset and limit, not {', '.join(other)}")
            self.alive()
            cancel=threading.Event()
            timer=threading.Timer(max(0.,self.left(deadline)-RENDER_MARGIN),cancel.set);timer.daemon=True;timer.start()
            try:rows=photo_service.folder_rows(self.directory,filters.get('folder'),cancel)
            finally:timer.cancel()
            if 'text' in filters:            # part of the folder path, either slash, any case
                part=filters['text'].casefold().replace('\\','/')
                rows=[row for row in rows if part in row['path'].casefold().replace('\\','/')]
            out={'total':len(rows),'folders':rows[offset:offset+limit]}
            if offset+limit<len(rows):out['next']=offset+limit
            return [text_block(out)]
        if scope:
            from .mcp_service import ready
            ids=self.gui(lambda:self.scope_ids(ready(self.service.window()),scope),deadline)
        else:
            self.alive()
            ids=self.query(filters,deadline,sort=arguments.get('sort'),descending=arguments.get('descending',False),allow_empty=True)
        dirs,rows=photo_service.page_rows(self.directory,ids[offset:offset+limit])
        out={'total':len(ids),'offset':offset}
        if offset+limit<len(ids):out['next']=offset+limit
        out.update(dirs=dirs,photos=rows)
        return [text_block(out)]

    def tool_get_photo(self,arguments,deadline):
        ids=list(arguments['ids'])
        note=self.gui(lambda:self.read_commit(ids),deadline)
        rows=self.reader_rows(ids)
        dirs,photos=photo_service.summaries([row for row in rows if row is not None])
        out={'dirs':dirs,'photos':photos}
        missing=[ident for ident,row in zip(ids,rows) if row is None]
        if missing:out['missing']=missing
        if note:out['note']=note
        return [text_block(out)]

    def render_slot(self,deadline):
        if not self.service.render_lock.acquire(timeout=max(0.,min(5.,self.left(deadline)))):
            raise Busy('another preview is being made')

    def tool_get_preview(self,arguments,deadline):
        from . import photo_images
        from PIL import Image
        from .engine import VIDEO_EXTENSIONS
        ids=list(arguments['ids']);size=arguments.get('size',512);quality=arguments.get('quality',70)
        note=self.gui(lambda:self.read_commit(ids),deadline)
        rows=self.reader_rows(ids)
        images=[];previews=[];errors=[];total=0
        self.render_slot(deadline)
        try:
            for ident,row in zip(ids,rows):
                if row is None:errors.append({'id':ident,'error':'not in the catalog'});continue
                if Path(row['path']).suffix.lower() in VIDEO_EXTENSIONS:errors.append({'id':ident,'error':'video files are not supported'});continue
                if self.left(deadline)<RENDER_MARGIN:errors.append({'id':ident,'error':'not rendered in time; ask for fewer photos'});continue
                try:pixels=self.background(lambda row=row:photo_images.render_preview(row,size,self.directory),deadline)
                except ServiceError as error:
                    errors.append({'id':ident,'error':str(error)});continue
                except Exception as error:
                    errors.append({'id':ident,'error':photo_service.reason(error,row['path'])});continue
                data=photo_images.jpeg(pixels,quality,PREVIEW_BYTES)
                if total+len(data)>RESULT_BYTES:
                    errors.append({'id':ident,'error':'left out to keep the reply small; ask for fewer photos or a smaller size'});continue
                total+=len(data);images.append(image_block(data))
                with Image.open(io.BytesIO(data)) as decoded:previews.append({'id':ident,'w':decoded.width,'h':decoded.height})
        finally:self.service.render_lock.release()
        out={'previews':previews}
        if errors:out['errors']=errors
        if note:out['note']=note
        return images+[text_block(out)]

    def tool_contact_sheet(self,arguments,deadline):
        from . import photo_images,film_look
        from .engine import VIDEO_EXTENSIONS
        from .mcp_io import check_deadline
        kind=self.one_target(arguments);cell=arguments.get('cell',160);saving=arguments.get('save',False)
        if not saving and any(k in arguments for k in (*DESTINATION,'size')):
            raise ServiceError('destination and size need save=true')
        if kind=='ids':ids=list(arguments['ids']);page=ids;start=0
        else:
            if kind=='where':ids=self.query(arguments['where'],deadline)
            else:
                from .mcp_service import ready
                ids=self.gui(lambda:self.scope_ids(ready(self.service.window()),arguments['scope']),deadline)
            start=arguments.get('offset',0);page=ids[start:start+arguments.get('count',SHEET_CELLS)]
        if not page:raise ServiceError('no photos to show' if not ids else 'offset is past the last photo')
        variants=arguments.get('variants')
        if variants and (len(ids)!=1 or len(page)!=1):raise ServiceError('variants needs exactly one photo')
        alternatives=[]
        for variant in variants or []:
            if ('film_look' in variant)==('preset' in variant):raise ServiceError('each variant needs film_look or preset, not both')
            if 'film_look' in variant:
                changes=photo_service.parse_changes({'film_look':variant['film_look']})
                alternatives.append({'changes':changes,'label':variant.get('label',variant['film_look'] or 'No film'),
                                     'look':variant['film_look'] or 'No film'})
            else:
                def preset():return self.find_preset(self.alive(),variant['preset'])
                name,base=self.gui(preset,deadline)
                alternatives.append({'preset':base,'label':variant.get('label',name),'look':name})
        note=self.gui(lambda:self.read_commit(page),deadline)
        folder=self.destination(arguments,deadline,create=False) if saving else None
        self.render_slot(deadline)
        try:
            def render():
                records={row['id']:row for row in self.reader_rows(page) if row is not None}
                count=len(alternatives) or len(page);columns=min(photo_images.MAX_COLUMNS,count)
                side=max(columns,math.ceil(count/columns));size=arguments.get('size',2400)
                saved_cell=(size-2*(side-1))//side
                requested=2*max(cell,saved_cell) if saving else 2*cell
                cells=[];full=[];not_ready=[];errors=[]
                items=[(page[0],v) for v in alternatives] if alternatives else [(i,None) for i in page]
                for ident,variant in items:
                    check_deadline(deadline)
                    record=records.get(ident);pixels=None;look=''
                    if record is not None and Path(record['path']).suffix.lower() not in VIDEO_EXTENSIONS:
                        if variant:
                            settings=(photo_service.preset_settings(variant['preset'],record['settings'],record) if 'preset' in variant else
                                      photo_service.edited(record['settings'],*variant['changes'],record['info'],ident))
                            record={**record,'settings':settings};look=variant['look']
                        else:
                            film=film_look.entry(record['settings'].get('film_look'))
                            look=film['name'] if film else str(record.get('user_metadata',{}).get(photo_service.PRESET_KEY) or '')
                        try:
                            if not variant and not saving:pixels=photo_images.cached_thumbnail(self.directory,ident)
                            if pixels is None and self.left(deadline)>=RENDER_MARGIN:
                                pixels=photo_images.render_preview(record,requested,self.directory)
                        except Exception as error:errors.append({'id':ident,'error':photo_service.reason(error,record['path'])})
                    if pixels is None:not_ready.append(ident)
                    mark=photo_images.mark(ident,record.get('rating',0) if record else 0,record.get('flag',0) if record else 0)
                    label=variant['label'] if variant else mark
                    cells.append((ident,pixels,label))
                    full.append((ident,pixels,mark+'\n'+(record['name'] if record else str(ident))+'\n'+
                                 ((label+' · '+look) if variant and label!=look else look)))
                check_deadline(deadline)
                data=photo_images.jpeg(photo_images.contact_sheet(cells,cell),70,SHEET_BYTES)
                saved=photo_images.contact_sheet(full,saved_cell,captions=True) if saving else None
                encoded=photo_images.jpeg(saved,90) if saved is not None else None
                out={'ids':page,'columns':columns,'total':len(ids)}
                if alternatives:out['variants']=[v['label'] for v in alternatives]
                if kind!='ids' and start+len(page)<len(ids):out['next']=start+len(page)
                missing=[i for i in page if i not in records]
                if missing:out['missing']=missing
                if not_ready:out['not_ready']=list(dict.fromkeys(i for i in not_ready if i in records))
                if errors:out['errors']=errors
                if note:out['note']=note
                return data,out,encoded,saved.size if saved else None
            data,out,encoded,dimensions=self.background(render,deadline)
        finally:self.service.render_lock.release()
        if saving:
            def save():
                # Recheck resolved destination immediately before creating; exclusively claim the new file.
                target=photo_service.export_folder(folder,None,self.service.own_folders(),create=False)
                check_deadline(deadline);target.mkdir(parents=True,exist_ok=True)
                index=1
                while True:
                    check_deadline(deadline)
                    path=target/photo_service.safe_filename('contact-sheet','.jpg',index)
                    try:
                        with path.open('xb') as handle:handle.write(encoded)
                        return {'path':str(path),'w':dimensions[0],'h':dimensions[1],'bytes':len(encoded)}
                    except FileExistsError:index+=1
            out['saved']=self.background(save,deadline)
        return [image_block(data),text_block(out)]

    # write tools
    def tool_apply_edits(self,arguments,deadline):
        from .mcp_service import ready,safe_commit
        from .engine import VIDEO_EXTENSIONS as VIDEO
        from . import photo_edits
        entries=[]
        for index,entry in enumerate(arguments['edits']):
            where=f'arguments.edits[{index}]'
            if not any(key in entry for key in ('preset','copy_from','set','add','crop','auto_straighten','auto_crop')):
                raise ServiceError(f'{where} needs preset, copy_from, set or add, crop or an automatic option')
            if 'preset' in entry and 'copy_from' in entry:raise ServiceError(f'{where}: give preset or copy_from, not both')
            if 'groups' in entry and 'copy_from' not in entry:raise ServiceError(f'{where}: groups needs copy_from')
            changes=photo_service.parse_changes(entry.get('set'),entry.get('add')) if 'set' in entry or 'add' in entry else None
            ids=self.pre_ids(entry,deadline)
            if ids is not None:self.limit(ids)
            parsed={**entry,'changes':changes,'ids':ids,'scope':entry.get('scope'),'preset':entry.get('preset'),
                    'copy_from':entry.get('copy_from')}
            if 'crop' in entry:parsed['crop']=photo_edits.crop_value(entry['crop'])
            entries.append(parsed)

        def prepare():
            w=ready(self.service.window())
            for entry in entries:
                if entry['ids'] is None:entry['ids']=self.scope_ids(w,entry['scope'])
            targets=self.limit(list(dict.fromkeys(i for entry in entries for i in entry['ids'])))
            source_ids={e['copy_from'] for e in entries if e['copy_from'] is not None}
            affected=set(targets)|source_ids
            ready(w,write=True,ids=affected,selection=any(e['scope']=='selection' for e in entries))
            if w.current_id in affected:
                if not safe_commit(w):raise Busy('the open photo cannot be saved yet')
                w.save_keywords()
            sources={}
            for ident in source_ids:
                row=w.catalog.photo(ident)
                if row is None:raise ServiceError(f'copy_from photo {ident} is not in the catalog')
                if Path(row['path']).suffix.lower() in VIDEO:raise ServiceError(f'copy_from photo {ident} is a video')
                sources[ident]=row
            for entry in entries:
                if entry['preset'] is not None:entry['preset_name'],entry['base']=self.find_preset(w,entry['preset'])
                if entry['copy_from'] is not None:entry['keys']=photo_service.copy_keys(entry.get('groups'))
            records,errors=photo_service.freeze(w.catalog,targets)
            self.usable(records,errors)
            return records,errors,sources
        records,errors,sources=self.gui(prepare,deadline)
        used,values,failures,presets=self.background(
            lambda:photo_edits.compose(entries,records,sources,self.directory,deadline),deadline)
        def finish():
            from . import command_batch
            w=ready(self.service.window(),write=True,ids=set(sources)|{r['id'] for r in records})
            if w.current_id in set(sources)|{r['id'] for r in records}:
                if not safe_commit(w):raise Busy('the open photo cannot be saved yet')
            try:command_batch.verify(w.catalog,list({r['id']:r for r in [*records,*sources.values()]}.values()))
            except command_batch.BatchConflict:
                raise ServiceError('a source or target changed during this call; nothing was applied; try again') from None
            return self.write_batch(w,used,values,errors+failures,label='AI 보정',tool='apply_edits',presets=presets)
        return [text_block(self.gui(finish,deadline))]

    def write_batch(self,w,used,values,errors,*,label,tool,presets=None):
        """GUI thread: one undoable batch of settings that bring their own panel states (photo_service.edited
        switched on what set/add changed); the reply apply_edits and snapshot restore give."""
        from .mcp_service import capped
        self.usable(used,errors)
        bid,changed=photo_service.apply_batch(w.catalog,used,values,label=label,tool=tool,snapshot_name=tr_snapshot(),
                                              snapshotted=self.service.snapshotted,switch=False,presets=presets)
        if changed:w.refresh_command_edits(changed,label)
        return {'batch':bid,'changed':len(changed),'unchanged':len(used)-len(changed),**capped(errors)}

    @staticmethod
    def find_preset(w,name):
        """(name, settings) of one of Grainy's presets (built in or saved), by name in any case."""
        presets=w.all_presets
        found=name if name in presets else next((key for key in presets if key.casefold()==name.strip().casefold()),None)
        if found is None:
            names=list(presets)
            raise ServiceError(f'no preset named {name!r}; presets: '+', '.join(names[:40])+(f' and {len(names)-40} more' if len(names)>40 else ''))
        return found,presets[found]

    def tool_undo_last_batch(self,arguments,deadline):
        from .mcp_service import ready
        redo=arguments.get('redo',False)

        def work():
            w=ready(self.service.window())
            ready(w,write=True,ids=[w.current_id] if w.current_id is not None else [])
            if w.current_id is not None:w.commit();w.save_keywords()
            bid,tool,ids=photo_service.undo_batch(w.catalog,arguments.get('batch'),redo)
            if ids:
                if tool=='set_labels':
                    w.refresh_lists()
                    if w.current_id in ids:w.load_controls()
                else:w.refresh_command_edits(ids,'AI 다시 실행' if redo else 'AI 실행 취소')
            return {'redone' if redo else 'undone':bid,'tool':tool,'photos':len(ids)}

        return [text_block(self.gui(work,deadline))]

    def job_reply(self,job,deadline):
        from . import mcp_service
        job.finished.wait(max(0.,min(mcp_service.WAIT_SECONDS,self.left(deadline)-2)))
        if job.state=='done':return [text_block(job.result or {})]
        if job.state in ('failed','cancelled'):raise JobFailure(job)
        view={'job':job.id,'state':job.state,'done':job.done,'total':job.total}
        if job.phase:view['phase']=job.phase
        return [text_block(view)]

    def start_auto(self,arguments,deadline,kind,label,tool,amount=None):
        from .mcp_service import ready
        ids=self.pre_ids(arguments,deadline)
        if ids is not None:self.limit(ids)
        scope=arguments.get('scope')
        service=self.service

        def work():
            w=ready(service.window())
            targets=self.limit(ids if ids is not None else self.scope_ids(w,scope))
            ready(w,write=True,ids=targets,selection=scope=='selection')
            if kind=='roll' and len(targets)<2:raise ServiceError('roll=true needs at least 2 photos of the same roll')
            if w.current_id is not None and w.current_id in targets:w.commit();w.save_keywords()
            records,errors=photo_service.freeze(w.catalog,targets)
            self.usable(records,errors)
            if kind=='roll' and len(records)<2:raise ServiceError('roll=true needs at least 2 photos that can be used')
            value=amount
            if kind=='color' and value is None:
                saved=w.catalog.preference('auto_color_amount',100)
                value=float(saved)/100 if isinstance(saved,(int,float)) and not isinstance(saved,bool) else 1.
            return service.start_auto(w,kind,records,label=label,tool=tool,amount=1. if value is None else value,extra_errors=errors)

        return self.job_reply(self.gui(work,deadline),deadline)

    def tool_auto_tone(self,arguments,deadline):
        return self.start_auto(arguments,deadline,'tone','AI 자동톤','auto_tone')

    def tool_auto_color(self,arguments,deadline):
        strength=arguments.get('strength')
        return self.start_auto(arguments,deadline,'color','AI 자동 색','auto_color',None if strength is None else strength/100)

    def tool_invert_negatives(self,arguments,deadline):
        if arguments.get('roll',False):
            return self.start_auto(arguments,deadline,'roll','AI 네거티브 반전 (한 롤)','invert_negatives')
        return self.start_auto(arguments,deadline,'negative','AI 네거티브 반전','invert_negatives')

    def tool_export_photos(self,arguments,deadline):
        from .mcp_service import ready
        naming=arguments.get('naming','{stem}_grainy');photo_service.check_naming(naming)
        boxed='width' in arguments or 'height' in arguments
        if boxed and arguments.get('longest'):raise ServiceError('give longest, or width and/or height, not both')
        if 'fit' in arguments and not boxed:raise ServiceError('fit needs width and/or height')
        size=(arguments.get('width',0),arguments.get('height',0),arguments.get('fit','inside')) if boxed else arguments.get('longest',0)
        options=dict(format=arguments.get('format','JPEG'),quality=arguments.get('quality',90),longest=size,
                     color_space=arguments.get('color_space','sRGB'),keep_metadata=arguments.get('keep_metadata',False),naming=naming)
        if options['format']=='JPEG XL' and options['color_space']!='sRGB':raise ServiceError('JPEG XL is exported in sRGB only')
        ids=self.pre_ids(arguments,deadline)
        if ids is not None:self.limit(ids)
        scope=arguments.get('scope');service=self.service
        def prepare():
            w=ready(service.window())
            targets=self.limit(ids if ids is not None else self.scope_ids(w,scope))
            ready(w,export=True,ids=targets,selection=scope=='selection')
            note=None if arguments.get('dry_run',False) else self.read_commit(targets)
            records=[w.catalog.photo(i) for i in targets]
            if arguments.get('dry_run',False):
                from copy import deepcopy
                records=[{**r,'settings':deepcopy(w.settings)} if r is not None and r['id']==w.current_id else r for r in records]
            valid,errors=photo_service.freeze(w.catalog,targets);allowed={r['id'] for r in valid}
            self.usable(valid,errors)
            return [r for r in records if r is not None and r['id'] in allowed],errors,note
        records,errors,note=self.gui(prepare,deadline)
        folder=self.destination(arguments,deadline,create=not arguments.get('dry_run',False))
        if arguments.get('dry_run',False):
            def plan():
                outputs=[];failed=list(errors)
                for record,path in photo_service.export_plans(records,folder,options['format'],naming):
                    try:size=photo_service.planned_size(record,size_option)
                    except Exception as error:
                        failed.append({'id':record['id'],'error':photo_service.reason(error,record['path'])});continue
                    outputs.append({'photo_id':record['id'],'path':str(path),'size':size,'bytes':None})
                from .mcp_service import capped
                return {'dry_run':True,'planned':len(outputs),'folder':str(folder),**photo_service.output_files(outputs),
                        **capped(failed),**({'note':note} if note else {})}
            size_option=size
            return [text_block(self.background(plan,deadline))]
        def start():
            w=ready(service.window(),export=True)
            return service.start_export(w,[r['id'] for r in records],folder,options,extra_errors=errors)
        return self.job_reply(self.gui(start,deadline),deadline)

    def tool_import_folder(self,arguments,deadline):
        from .mcp_service import ready
        def resolve():
            folder=photo_service.named_folder(arguments['path'],self.service.own_folders())
            if not folder.is_dir():raise ServiceError(f'{folder} is not a folder on this computer (or not available now)')
            return folder
        folder=self.background(resolve,deadline)
        def work():
            w=ready(self.service.window(),write=True)
            return self.service.start_import(w,folder,arguments.get('recursive',False))
        return self.job_reply(self.gui(work,deadline),deadline)

    def tool_create_virtual_copies(self,arguments,deadline):
        from .mcp_service import ready
        from .engine import VIDEO_EXTENSIONS
        ident=arguments['id'];count=arguments['count']

        def work():
            w=ready(self.service.window())
            ready(w,write=True,ids=[ident])
            photo=w.catalog.photo(ident)
            if photo is None:raise ServiceError(f'photo {ident} is not in the catalog')
            if Path(photo['path']).suffix.lower() in VIDEO_EXTENSIONS:raise ServiceError(f'photo {ident} is a video')
            if w.current_id==ident:w.commit();w.save_keywords()      # the copies start from what the editor shows
            copies=[w.catalog.virtual_copy(ident) for _ in range(count)]
            w.refresh_lists()
            return {'copies':copies}

        return [text_block(self.gui(work,deadline))]

    def tool_set_labels(self,arguments,deadline):
        from .mcp_service import ready,capped
        from . import photo_labels
        if 'entries' in arguments and len(arguments)!=1:raise ServiceError('give entries or top-level targets and values, not both')
        entries=[]
        for entry in arguments.get('entries',[arguments]):
            fields={}
            if 'rating' in entry:fields['rating']=entry['rating']
            if 'flag' in entry:fields['flag']=photo_service.FLAGS[entry['flag']]
            if 'label' in entry:fields['label']=photo_service.LABELS.get(entry['label'],'')
            if not fields:raise ServiceError('give rating, flag and/or label')
            ids=self.pre_ids(entry,deadline)
            if ids is not None:self.limit(ids)
            entries.append({'ids':ids,'scope':entry.get('scope'),'fields':fields})
        def work():
            w=ready(self.service.window())
            for entry in entries:
                if entry['ids'] is None:entry['ids']=self.scope_ids(w,entry['scope'])
            targets=self.limit(list(dict.fromkeys(i for entry in entries for i in entry['ids'])))
            ready(w,write=True,ids=targets,selection=any(e['scope']=='selection' for e in entries))
            if w.current_id in targets:w.commit();w.save_keywords()
            bid,changed,unchanged,errors=photo_labels.apply(w.catalog,entries)
            if changed:
                w.refresh_lists()
                if w.current_id in changed:w.load_controls()
            return {'batch':bid,'changed':len(changed),'unchanged':unchanged,**capped(errors)}
        return [text_block(self.gui(work,deadline))]

    def tool_snapshot(self,arguments,deadline):
        from .mcp_service import ready
        action=arguments['action'];name=arguments.get('name')
        if action!='list' and name is None:raise ServiceError(f'{action} needs name')
        if name is not None and not name.strip():raise ServiceError('name must not be blank')
        ids=self.pre_ids(arguments,deadline)
        if ids is not None:self.limit(ids)
        scope=arguments.get('scope')
        if action=='list':
            if ids is None:
                ids=self.gui(lambda:self.scope_ids(ready(self.service.window()),scope),deadline)
            return [text_block(self.snapshot_list(ids,name))]

        def work():
            w=ready(self.service.window())
            targets=self.limit(ids if ids is not None else self.scope_ids(w,scope))
            ready(w,write=True,ids=targets,selection=scope=='selection')
            if w.current_id is not None and w.current_id in targets:w.commit();w.save_keywords()
            records,errors=photo_service.freeze(w.catalog,targets)
            if action=='save':
                self.usable(records,errors)
                for record in records:w.catalog.snapshot(record['id'],name.strip())
                from .mcp_service import capped
                return {'saved':len(records),**capped(errors)}
            used=[];values=[]
            for record in records:
                stored=self.find_snapshot(w.catalog,record['id'],name)
                if stored is None:errors.append({'id':record['id'],'error':f'no snapshot named {name!r}'});continue
                used.append(record);values.append(stored)
            return self.write_batch(w,used,values,errors,label='AI 스냅샷 복원',tool='snapshot')

        return [text_block(self.gui(work,deadline))]

    @staticmethod
    def find_snapshot(catalog,ident,name):
        """The settings of the photo's newest snapshot called name (exactly, or else in any case), or None."""
        from .engine import normalized
        rows=catalog.db.execute('SELECT name,edits FROM snapshots WHERE photo_id=? ORDER BY id DESC',(ident,)).fetchall()
        row=next((r for r in rows if r['name']==name),None) or next((r for r in rows if (r['name'] or '').casefold()==name.strip().casefold()),None)
        return None if row is None else normalized(catalog.profiles.loads(row['edits']))

    def snapshot_list(self,ids,name=None):
        """{photos: [{id, snapshots: [{name, created}]}]}, newest first, at most 20 a photo; read in this thread."""
        from datetime import datetime
        from .catalog import Catalog
        found={}
        with Catalog.open_reader(self.directory) as reader:
            for start in range(0,len(ids),500):
                part=ids[start:start+500]
                for row in reader.db.execute('SELECT photo_id,name,created FROM snapshots WHERE photo_id IN ('+','.join('?'*len(part))+
                                             ') ORDER BY id DESC',part):
                    if name is not None and (row['name'] or '').casefold()!=name.strip().casefold():continue
                    try:created=datetime.fromisoformat(row['created']).astimezone().strftime('%Y-%m-%d %H:%M')
                    except (TypeError,ValueError):created=''
                    found.setdefault(row['photo_id'],[]).append({'name':row['name'],'created':created})
            present={row[0] for start in range(0,len(ids),500)
                     for row in reader.db.execute('SELECT id FROM photos WHERE id IN ('+','.join('?'*len(ids[start:start+500]))+')',
                                                  ids[start:start+500])}
        out={'photos':[{'id':ident,'snapshots':found.get(ident,[])[:20]} for ident in ids if ident in present]}
        missing=[ident for ident in ids if ident not in present]
        if missing:out['missing']=missing
        return out

    def tool_list_films(self,arguments,deadline):
        kind=arguments.get('type','all')
        if kind=='presets':
            from .mcp_service import ready
            def names():
                w=ready(self.service.window())
                saved=set(w.catalog.presets())
                return [n for n in w.all_presets if n not in saved],[n for n in w.all_presets if n in saved]
            built_in,saved=self.gui(names,deadline)
            return [text_block({'built_in':built_in,'saved':saved})]
        from . import film_look
        types={'colour-negative':'color','bw-negative':'bw','colour-slide':'slide'}
        films=[]
        for entry in film_look.catalog():
            film_type=types.get(entry.get('kind'),entry.get('kind'));cine=entry.get('group')=='cine'
            if kind not in ('all',film_type) and not (kind=='cine' and cine):continue
            films.append({'name':entry['name'],'iso':entry.get('iso'),'type':film_type,'maker':entry.get('maker'),**({'cine':1} if cine else {})})
        return [text_block({'films':films})]

    def tool_open_photo(self,arguments,deadline):
        from .mcp_service import ready
        from .engine import VIDEO_EXTENSIONS
        ident=arguments['id'];view=arguments.get('view','edit')

        def work():
            w=ready(self.service.window())
            ready(w,write=True,ids=[w.current_id] if w.current_id is not None else [])
            photo=w.catalog.photo(ident)
            if photo is None:raise ServiceError(f'photo {ident} is not in the catalog')
            if Path(photo['path']).suffix.lower() in VIDEO_EXTENSIONS:raise ServiceError(f'photo {ident} is a video; Grainy cannot open it')
            w.activate(ident);w.set_mode(0 if view=='edit' else 1)
            out={'opened':ident}
            if ident in w.photo_model.positions:
                w.photo_selection.blockSignals(True)
                w.active_list().restore_selection([ident],ident)
                w.photo_selection.blockSignals(False)
            else:out['note']='not in the current list view (filters or another folder are shown)'
            return out

        return [text_block(self.gui(work,deadline))]

    def tool_get_job(self,arguments,deadline):
        job=self.service.jobs.get(arguments['job'])
        if job is None:raise ServiceError('unknown job (jobs are kept for this Grainy session only)')
        job.finished.wait(max(0.,min(arguments.get('wait',20),self.left(deadline)-2)))
        return [text_block(job.view())]


def tr_snapshot():
    """The pre-AI snapshot's name in the interface language."""
    from .i18n import tr
    return tr('AI 작업 전 · {0}',time.strftime('%Y-%m-%d %H:%M'))
