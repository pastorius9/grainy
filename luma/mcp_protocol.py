"""JSON-RPC 2.0 and the Model Context Protocol for Grainy's AI connection. No Qt, no sockets.

Two eras: the legacy one (initialize handshake, protocol versions up to 2025-11-25) that today's clients
speak, and the modern one (2026-07-28: server/discover, per-request _meta and Mcp-* headers), built and
tested but switched off by MODERN_ENABLED until it has been tried with real clients.
"""
import base64
import binascii
import json
from dataclasses import dataclass,field

from . import __version__

DEADLINE_SECONDS=45                     # every request is answered within this plus GRACE_SECONDS (mcp_service)
MODERN_ENABLED=False                    # turn on only after end-to-end runs with v2 clients
MODERN=('2026-07-28',)
LEGACY=('2025-11-25','2025-06-18','2025-03-26','2024-11-05')
SUPPORTED=MODERN+LEGACY
OFFLINE_VERSION='2025-03-26'            # the bridges' offline initialize (accepted by every TS SDK)
SERVER_INFO={'name':'grainy','title':'Grainy','version':__version__}
CACHE={'ttlMs':600000,'cacheScope':'private'}
META_VERSION='io.modelcontextprotocol/protocolVersion'
META_CAPABILITIES='io.modelcontextprotocol/clientCapabilities'
META_SERVER='io.modelcontextprotocol/serverInfo'
INSTRUCTIONS=("Grainy is a photo editor running on this computer; these tools drive its open window and catalog. "
    "Edits are non-destructive: original files are never written, and every editing call is one batch that "
    "undo_last_batch reverts, including set_labels (imports, saved files and virtual copies are outside undo). Work in few calls: target many photos at once (ids, a where filter, or scope "
    "'selection'/'current'), give different values per photo in one apply_edits call, and prefer relative add values "
    "(no need to read values first). Look at many photos with contact_sheet before using get_preview. Calls that "
    "take long return a job id; poll it with get_job. If a call says Grainy is busy, nothing was changed: wait briefly "
    "and retry or ask the person. After updating Grainy, start a new AI session: an existing session keeps its old tool list.")
# What the bridges answer while Grainy cannot be reached, in English and Korean: the person may read it as it is.
OFFLINE_TEXT=("Grainy did not answer: it is not running on this computer, its AI connection is off, or it is still "
    "starting or busy. Ask the person to start Grainy or, if it is already running, to open Settings > AI Connection "
    "(MCP)… in Grainy, tick 'Turn on the AI connection' and look at the status shown there. Then try again; this AI "
    "program does not need to be restarted. If you had asked for a change, check the photos before repeating it.\n"
    "Grainy가 응답하지 않았습니다. 이 컴퓨터에서 Grainy가 실행 중이 아니거나, AI 연결이 꺼져 있거나, 아직 시작 중이거나 바쁩니다. "
    "Grainy를 실행하세요. 이미 실행 중이면 Grainy의 설정 › AI 연결 (MCP)…에서 'AI 연결 켜기'를 켜고 그 창의 상태를 확인하세요. "
    "그런 다음 다시 시도하면 되며, AI 프로그램은 다시 시작하지 않아도 됩니다.")
OFFLINE_NOTE=(" When this session started Grainy did not answer (it was not running, or its AI connection was off); the "
    "tools work as soon as it runs with the connection on, without restarting this program.")
# The bridges' own answer when even the offline files are missing (escaped to ASCII inside the scripts).
OFFLINE_ERROR=("Grainy is not running, or its AI connection is off. Start Grainy, or turn its AI connection on "
    "(Grainy: Settings > AI Connection (MCP)…). / Grainy가 실행 중이 아니거나 AI 연결이 꺼져 있습니다. Grainy를 실행하거나 "
    "Grainy의 설정 › AI 연결 (MCP)…에서 AI 연결을 켜세요.")


@dataclass
class Reply:
    status:int
    body:dict|None=None
    headers:dict=field(default_factory=dict)


def dumps(obj):
    """Compact ASCII JSON: Korean arrives as \\uXXXX and there is never a raw newline."""
    return json.dumps(obj,separators=(',',':'),ensure_ascii=True).encode()


def error(ident,code,message,data=None):
    body={'code':code,'message':message}
    if data is not None:body['data']=data
    return {'jsonrpc':'2.0','id':ident,'error':body}


def result(ident,value):
    return {'jsonrpc':'2.0','id':ident,'result':value}


def initialize_result(version,offline=False):
    return {'protocolVersion':version if version in LEGACY else LEGACY[0],
            'capabilities':{'tools':{'listChanged':False}},
            'serverInfo':SERVER_INFO,'instructions':INSTRUCTIONS+(OFFLINE_NOTE if offline else '')}


def valid_id(value):
    return isinstance(value,str) or (isinstance(value,int) and not isinstance(value,bool))


def header_value(value):
    """A header as sent; values of the form =?base64?...?= are decoded first."""
    if isinstance(value,str) and value.startswith('=?base64?') and value.endswith('?='):
        try:return base64.b64decode(value[9:-2],validate=True).decode('utf-8')
        except (binascii.Error,ValueError):return None
    return value


def handle(message,headers,*,flavour,tools,deadline):
    """One JSON-RPC message (already parsed) to a Reply. flavour: 'http' (/mcp) or 'stdio' (/mcp/stdio, the
    bridges: always the legacy era, every JSON-RPC error with HTTP status 200). tools provides definitions()
    and call(name, arguments, *, modern, deadline)."""
    stdio=flavour=='stdio'
    lower={str(k).lower():str(v) for k,v in (headers.items() if headers is not None else ())}

    def fail(status,ident,code,text,data=None):
        return Reply(200 if stdio else status,error(ident,code,text,data))

    if not isinstance(message,dict):return fail(400,None,-32600,'Invalid Request')     # arrays (batches) included
    ident=message.get('id') if valid_id(message.get('id')) else None
    if message.get('jsonrpc')!='2.0':return fail(400,ident,-32600,'Invalid Request')
    if 'method' not in message:
        if 'result' in message or 'error' in message:return Reply(202)            # a client's response
        return fail(400,ident,-32600,'Invalid Request')
    method=message['method']
    if not isinstance(method,str):return fail(400,ident,-32600,'Invalid Request')
    if 'id' not in message:return Reply(202)                                       # notifications are accepted and ignored
    if not valid_id(message['id']):return fail(400,None,-32600,'Invalid Request')
    params=message.get('params')
    if params is None:params={}
    if not isinstance(params,dict):return fail(200,ident,-32602,'Invalid params')
    if method=='initialize':return Reply(200,result(ident,initialize_result(params.get('protocolVersion'))))
    if stdio:
        if method=='server/discover':return Reply(200,error(ident,-32601,'Method not found'))
        return _legacy(method,ident,params,lower,tools,deadline,check=False)
    if not MODERN_ENABLED:
        # The legacy signal that makes a v2 client fall back to initialize; before any version header check.
        if method=='server/discover':return Reply(200,error(ident,-32601,'Method not found'))
        return _legacy(method,ident,params,lower,tools,deadline,check=True)
    meta=params.get('_meta')
    if isinstance(meta,dict) and META_VERSION in meta:return _modern(method,ident,params,meta,lower,tools,deadline)
    return _legacy(method,ident,params,lower,tools,deadline,check=True)


def _call(ident,params,tools,deadline,modern):
    name=params.get('name')
    known={definition['name'] for definition in tools.definitions()}
    if not isinstance(name,str) or name not in known:return None,error(ident,-32602,f'Unknown tool: {name}')
    arguments=params.get('arguments')
    if arguments is None:arguments={}
    if not isinstance(arguments,dict):return None,error(ident,-32602,'Invalid params: arguments must be an object')
    try:return tools.call(name,arguments,modern=modern,deadline=deadline),None
    except Exception:return None,error(ident,-32603,'Internal error')


def _legacy(method,ident,params,headers,tools,deadline,*,check):
    if check:
        version=headers.get('mcp-protocol-version')
        if version is not None and version not in LEGACY:
            return Reply(400,error(ident,-32600,'Unsupported MCP-Protocol-Version'))
    fixed={'ping':{},'resources/list':{'resources':[]},'resources/templates/list':{'resourceTemplates':[]},
           'prompts/list':{'prompts':[]}}
    if method in fixed:return Reply(200,result(ident,fixed[method]))
    if method=='tools/list':return Reply(200,result(ident,{'tools':tools.definitions()}))
    if method=='tools/call':
        value,failure=_call(ident,params,tools,deadline,False)
        return Reply(200,failure if failure is not None else result(ident,value))
    return Reply(200,error(ident,-32601,'Method not found'))


def _modern(method,ident,params,meta,headers,tools,deadline):
    version=meta.get(META_VERSION)
    if header_value(headers.get('mcp-protocol-version'))!=version:return Reply(400,error(ident,-32020,'HeaderMismatch'))
    if version not in MODERN:
        return Reply(400,error(ident,-32022,'Unsupported protocol version',{'supported':list(SUPPORTED),'requested':version}))
    if not isinstance(meta.get(META_CAPABILITIES),dict):return Reply(400,error(ident,-32602,'Invalid params: clientCapabilities'))
    if header_value(headers.get('mcp-method'))!=method:return Reply(400,error(ident,-32020,'HeaderMismatch'))
    if method=='tools/call' and header_value(headers.get('mcp-name'))!=params.get('name'):
        return Reply(400,error(ident,-32020,'HeaderMismatch'))

    def complete(value):
        return Reply(200,result(ident,{**value,'resultType':'complete','_meta':{META_SERVER:SERVER_INFO}}))

    if method=='server/discover':
        return complete({'supportedVersions':list(MODERN),'capabilities':{'tools':{}},'instructions':INSTRUCTIONS,**CACHE})
    lists={'tools/list':lambda:{'tools':tools.definitions(),**CACHE},'resources/list':lambda:{'resources':[],**CACHE},
           'resources/templates/list':lambda:{'resourceTemplates':[],**CACHE},'prompts/list':lambda:{'prompts':[],**CACHE}}
    if method in lists:return complete(lists[method]())
    if method=='tools/call':
        value,failure=_call(ident,params,tools,deadline,True)
        return Reply(200,failure) if failure is not None else complete(value)
    # subscriptions/listen is not offered: it would never carry a notification (listChanged is false).
    return Reply(404,error(ident,-32601,'Method not found'))


def offline_templates():
    """The bridges' answers while Grainy is not reachable, one line each, with __ID__ for the request id."""
    from .mcp_tools import DEFINITIONS

    def line(value):
        return dumps(result('__ID__',value)).decode('ascii').replace('"__ID__"','__ID__')

    return {'offline-initialize.json':line(initialize_result(OFFLINE_VERSION,offline=True)),
            'offline-tools.json':line({'tools':DEFINITIONS}),
            'offline-call.json':line({'content':[{'type':'text','text':OFFLINE_TEXT}],'isError':True})}
