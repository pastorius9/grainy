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
import traceback
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
    'flag':{'type':'string','enum':['picked','rejected','unflagged']},
    'label':{'type':'string','enum':['red','yellow','green','blue','purple']},
    'collection':{'type':'integer','minimum':1,'maximum':MAX_ID,'description':'Collection id.'}}
SCOPE={'type':'string','enum':['selection','current'],
       'description':'selection: photos selected in Grainy; current: the photo open in the editor.'}
# Inside targets the filters and scope go without their descriptions (list_photos has them): tools/list
# stays under 16,000 characters.
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
        if key in ('monochrome','straighten','exposure','negative_density','negative_range','film_look'):continue
        groups.setdefault(_range(low,high),[]).append(key)
    ranges='; '.join(f'{", ".join(keys)} {span}' for span,keys in groups.items())
    return ("Change edit values on many photos as one undoable batch. Each entry targets one of ids, where (list_photos "
        "filters) or scope ('selection' or 'current') and gives absolute values in set and/or relative changes in add; "
        "results are clamped to the ranges. Ranges: exposure "+_range(*FIELDS['exposure'][1:])+" (stops); "+ranges+
        "; straighten "+_range(*FIELDS['straighten'][1:])+" degrees (the crop follows as in the editor); monochrome "
        "true/false (set only); negative_density "+_range(*FIELDS['negative_density'][1:])+" (brightness) and "
        "negative_range "+_range(*FIELDS['negative_range'][1:])+" (contrast), only on photos inverted with "
        "invert_negatives; film_look a film by name, e.g. 'Kodak Portra 400', 'Fujifilm Velvia 50', 'Ilford HP5 Plus' "
        "('' none; brings its grain). Entries apply in order, later ones refine earlier ones. Originals never change.")


def _tool(name,title,description,properties,required=(),read_only=False,idempotent=None):
    annotations=dict(READ_ANNOTATIONS if read_only else WRITE_ANNOTATIONS)
    if idempotent is not None:annotations['idempotentHint']=idempotent
    schema={'type':'object','properties':properties,'additionalProperties':False}
    if required:schema['required']=list(required)
    return {'name':name,'title':title,'description':description,'inputSchema':schema,'annotations':{'title':title,**annotations}}


EDIT_ENTRY={'type':'object','additionalProperties':False,'properties':{**TARGET(),
    'set':{'type':'object','additionalProperties':False,'minProperties':1,'properties':
           {k:{'type':'boolean'} if k=='monochrome' else {'type':'string'} if k=='film_look' else {'type':'number','minimum':lo,'maximum':hi}
            for k,(_,lo,hi) in FIELDS.items()}},
    'add':{'type':'object','additionalProperties':False,'minProperties':1,'properties':
           {k:{'type':'number','minimum':lo-hi,'maximum':hi-lo} for k,(_,lo,hi) in FIELDS.items() if k not in ('monochrome','film_look')}}}}

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
        "Details and current edit values of up to 50 photos. Folders are in dirs with d as in list_photos; file only "
        "when it is not name. edits lists only values that differ from Grainy's defaults (the keys apply_edits "
        "accepts); other names other changed tool groups.",
        {'ids':{**TARGET(50)['ids']}},required=['ids'],read_only=True),
    _tool('get_preview','Photo previews',
        "Developed JPEG (current edits) of 1-4 photos, long side size px (default 512). For many photos use contact_sheet.",
        {'ids':{**TARGET(4)['ids']},'size':{'type':'integer','minimum':128,'maximum':1568},
         'quality':{'type':'integer','minimum':40,'maximum':90}},required=['ids'],read_only=True),
    _tool('contact_sheet','Contact sheet',
        f"One JPEG with up to {SHEET_CELLS} small thumbnails labelled with photo id, rating (n*) and P/X for picked/rejected, "
        "so a whole roll can be seen in one call. "+TARGET_TEXT[:-1]+f"; with where or scope, offset and count (default "
        f"{SHEET_CELLS}) page through the matches. cell: thumbnail px (default 160).",
        {**TARGET(SHEET_CELLS),'offset':{'type':'integer','minimum':0},'count':{'type':'integer','minimum':1,'maximum':SHEET_CELLS},
         'cell':{'type':'integer','minimum':96,'maximum':256}},read_only=True),
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
        "photos was edited again.",
        {'batch':{'type':'string','pattern':'^b[0-9a-f]{6}$'},
         'redo':{'type':'boolean'}}),
    _tool('export_photos','Export photos',
        "Export developed copies as new files (existing files are never replaced; a number is added). format JPEG "
        "(default), TIFF 16-bit, TIFF 8-bit, PNG, PNG 16-bit, AVIF or JPEG XL (sRGB only); color_space sRGB (default), "
        "Adobe RGB, Display P3 or ProPhoto RGB. destination 'default' is the AI export folder set in Grainy "
        "(Pictures/Grainy unless changed), 'last' the folder of the person's last export from Grainy; subfolder adds one "
        "folder level. naming uses {stem} and {n}. "+TARGET_TEXT+" Returns the result, or a job id for get_job.",
        {**TARGET(),'format':{'type':'string','enum':EXPORT_FORMATS},'quality':{'type':'integer','minimum':40,'maximum':100},
         'longest':{'type':'integer','minimum':0,'maximum':30000,'description':'Long side px; 0 = full size.'},
         'color_space':{'type':'string','enum':COLOR_SPACES},'keep_metadata':{'type':'boolean'},
         'naming':{'type':'string','minLength':1,'maxLength':80},'destination':{'type':'string','enum':['default','last']},
         'subfolder':{'type':'string','minLength':1,'maxLength':80}}),
    _tool('open_photo','Open photo',
        "Show a photo in the Grainy window (editor or library view). Does not change the photo.",
        {'id':{'type':'integer','minimum':1,'maximum':MAX_ID},'view':{'type':'string','enum':['edit','library']}},
        required=['id'],idempotent=True),
    _tool('get_job','Job progress',
        "Progress or result of a job started by auto_tone, auto_color, invert_negatives or export_photos. Waits up to "
        "wait seconds (default 20) for it to finish.",
        {'job':{'type':'string','pattern':'^j[0-9]+$'},'wait':{'type':'integer','minimum':0,'maximum':40}},
        required=['job'],read_only=True,idempotent=False),
]
SCHEMAS={definition['name']:definition['inputSchema'] for definition in DEFINITIONS}     # what the checker uses


def _published(schema,inside_add=False):
    """The schema as tools/list shows it: without uniqueItems and minProperties, the add limits (the whole width
    of each range both ways, which the description gives) and the id maximum (2**53-1, the largest integer JSON
    readers keep exactly), so tools/list stays under 16,000 characters. The checker still applies all of them."""
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


# --- argument checker -----------------------------------------------------------------------------

def _is(kind,value):
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


def _last_line(error):
    text=''.join(traceback.format_exception_only(type(error),error)).strip().splitlines()
    return text[-1][:300] if text else type(error).__name__


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
                try:pixels=photo_images.render_preview(row,size,self.directory)
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
        from . import photo_images
        from .engine import VIDEO_EXTENSIONS
        kind=self.one_target(arguments);cell=arguments.get('cell',160)
        if kind=='ids':ids=list(arguments['ids']);page=ids;start=0
        else:
            if kind=='where':ids=self.query(arguments['where'],deadline)
            else:
                from .mcp_service import ready
                ids=self.gui(lambda:self.scope_ids(ready(self.service.window()),arguments['scope']),deadline)
            start=arguments.get('offset',0);page=ids[start:start+arguments.get('count',SHEET_CELLS)]
        if not page:raise ServiceError('no photos to show' if not ids else 'offset is past the last photo')
        _,rows=photo_service.page_rows(self.directory,page)
        found={row['id']:row for row in rows}
        pixels={ident:photo_images.cached_thumbnail(self.directory,ident) for ident in page if ident in found}
        wanted=[ident for ident in page if ident in found and pixels.get(ident) is None]
        if wanted:
            records={row['id']:row for row in self.reader_rows(wanted) if row is not None}
            self.render_slot(deadline)
            try:
                for ident in wanted:
                    record=records.get(ident)
                    if record is None or Path(record['path']).suffix.lower() in VIDEO_EXTENSIONS:continue
                    if self.left(deadline)<RENDER_MARGIN:break
                    try:pixels[ident]=photo_images.render_preview(record,2*cell,self.directory)
                    except Exception:pixels[ident]=None
            finally:self.service.render_lock.release()
        cells=[];not_ready=[]
        for ident in page:
            row=found.get(ident,{})
            image=pixels.get(ident)
            if image is None:not_ready.append(ident)
            cells.append((ident,image,photo_images.mark(ident,row.get('r',0),row.get('f',0))))
        sheet=photo_images.contact_sheet(cells,cell)
        data=photo_images.jpeg(sheet,70,SHEET_BYTES)
        columns=min(photo_images.MAX_COLUMNS,len(cells))
        out={'ids':page,'columns':columns,'total':len(ids)}
        if kind!='ids' and start+len(page)<len(ids):out['next']=start+len(page)
        missing=[ident for ident in page if ident not in found]
        if missing:out['missing']=missing
        if not_ready:out['not_ready']=[ident for ident in not_ready if ident in found]
        return [image_block(data),text_block(out)]

    # write tools
    def tool_apply_edits(self,arguments,deadline):
        from .mcp_service import ready,capped
        entries=[]
        for index,entry in enumerate(arguments['edits']):
            try:changes=photo_service.parse_changes(entry.get('set'),entry.get('add'))
            except ServiceError as error:raise ServiceError(f'arguments.edits[{index}]: {error}') from None
            kind=[key for key in ('ids','where','scope') if key in entry]
            if len(kind)!=1:raise ServiceError(f'arguments.edits[{index}] needs exactly one of ids, where or scope')
            ids=list(entry['ids']) if kind[0]=='ids' else self.query(entry['where'],deadline) if kind[0]=='where' else None
            if ids is not None:self.limit(ids)
            entries.append((changes,ids,entry.get('scope')))
        service=self.service

        def work():
            w=ready(service.window())
            resolved=[(changes,ids if ids is not None else self.scope_ids(w,scope)) for changes,ids,scope in entries]
            targets=self.limit(list(dict.fromkeys(i for _,ids in resolved for i in ids)))
            ready(w,write=True,ids=targets,selection=any(scope=='selection' for _,_,scope in entries))
            if w.current_id is not None and w.current_id in targets:w.commit();w.save_keywords()
            records,errors=photo_service.freeze(w.catalog,targets)
            members=[set(ids) for _,ids in resolved]
            used=[];values=[]
            for record in records:
                groups=[changes for (changes,_),ids in zip(resolved,members) if record['id'] in ids]
                try:values.append(photo_service.compose(record['settings'],groups,record['info'],record['id']))
                except ServiceError as error:errors.append({'id':record['id'],'error':str(error)});continue
                used.append(record)
            self.usable(used,errors)
            snapshot=tr_snapshot()
            bid,changed=photo_service.apply_batch(w.catalog,used,values,label='AI 보정',tool='apply_edits',
                                                  snapshot_name=snapshot,snapshotted=service.snapshotted)
            if changed:w.refresh_command_edits(changed,'AI 보정')
            return {'batch':bid,'changed':len(changed),'unchanged':len(used)-len(changed),**capped(errors)}

        return [text_block(self.gui(work,deadline))]

    def tool_undo_last_batch(self,arguments,deadline):
        from .mcp_service import ready
        redo=arguments.get('redo',False)

        def work():
            w=ready(self.service.window())
            ready(w,write=True,ids=[w.current_id] if w.current_id is not None else [])
            if w.current_id is not None:w.commit();w.save_keywords()
            bid,tool,ids=photo_service.undo_batch(w.catalog,arguments.get('batch'),redo)
            if ids:w.refresh_command_edits(ids,'AI 다시 실행' if redo else 'AI 실행 취소')
            return {'redone' if redo else 'undone':bid,'tool':tool,'photos':len(ids)}

        return [text_block(self.gui(work,deadline))]

    def job_reply(self,job,deadline):
        from . import mcp_service
        job.finished.wait(max(0.,min(mcp_service.WAIT_SECONDS,self.left(deadline)-2)))
        if job.state=='done':return [text_block(job.result or {})]
        if job.state in ('failed','cancelled'):raise ServiceError(f'job {job.id} {job.state}: {job.error or "no reason given"}')
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
        options=dict(format=arguments.get('format','JPEG'),quality=arguments.get('quality',90),longest=arguments.get('longest',0),
                     color_space=arguments.get('color_space','sRGB'),keep_metadata=arguments.get('keep_metadata',False),naming=naming)
        if options['format']=='JPEG XL' and options['color_space']!='sRGB':raise ServiceError('JPEG XL is exported in sRGB only')
        ids=self.pre_ids(arguments,deadline)
        if ids is not None:self.limit(ids)
        scope=arguments.get('scope');service=self.service

        def work():
            from .quick_export import previous
            w=ready(service.window())
            targets=self.limit(ids if ids is not None else self.scope_ids(w,scope))
            ready(w,export=True,ids=targets,selection=scope=='selection')
            if arguments.get('destination','default')=='last':
                last=previous(w.catalog)
                if not last:raise ServiceError("the person has not exported from Grainy yet; use destination 'default'")
                if not Path(last['folder']).is_dir():
                    raise ServiceError(f"the folder of the person's last export ({last['folder']}) is not available now, for "
                                       "example on a drive that is not connected; use destination 'default'")
                root=Path(last['folder'])
            else:root=service.export_root()
            folder=photo_service.export_folder(root,arguments.get('subfolder'))
            records,errors=photo_service.freeze(w.catalog,targets)
            self.usable(records,errors)
            return service.start_export(w,[r['id'] for r in records],folder,options,extra_errors=errors)

        return self.job_reply(self.gui(work,deadline),deadline)

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
