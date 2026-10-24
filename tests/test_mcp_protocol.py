"""mcp_protocol: JSON-RPC 2.0 / MCP for both eras, without sockets. A fake tools object with the real definitions."""
import base64
import json
import re
import time
from pathlib import Path

import pytest

from luma import mcp_protocol as protocol
from luma import mcp_tools
from luma.photo_service import FIELDS,ServiceError


class FakeTools:
    def __init__(self,result=None,fail=False):
        self.calls=[];self.result=result or {'content':[{'type':'text','text':'{}'}],'isError':False};self.fail=fail
    def definitions(self):return mcp_tools.DEFINITIONS
    def call(self,name,arguments,*,modern,deadline):
        self.calls.append((name,arguments,modern))
        if self.fail:raise RuntimeError('boom')
        return self.result


def handle(message,headers=None,flavour='http',tools=None):
    return protocol.handle(message,headers or {},flavour=flavour,tools=tools or FakeTools(),deadline=time.monotonic()+45)


def request(method,params=None,ident=1):
    message={'jsonrpc':'2.0','id':ident,'method':method}
    if params is not None:message['params']=params
    return message


@pytest.mark.parametrize('version',protocol.LEGACY)
def test_legacy_initialize_echoes_supported_version(version):
    reply=handle(request('initialize',{'protocolVersion':version,'capabilities':{},'clientInfo':{'name':'t'}}))
    assert reply.status==200
    result=reply.body['result']
    assert result['protocolVersion']==version and result['capabilities']=={'tools':{'listChanged':False}}
    assert result['serverInfo']['name']=='grainy' and result['instructions']==protocol.INSTRUCTIONS
    assert reply.body['id']==1 and 'error' not in reply.body


@pytest.mark.parametrize('version',['2099-01-01','2026-07-28',None,7])
def test_legacy_initialize_answers_latest_legacy_for_unknown_or_modern_version(version):
    reply=handle(request('initialize',{'protocolVersion':version}))
    assert reply.status==200 and reply.body['result']['protocolVersion']==protocol.LEGACY[0]


def test_notifications_and_client_responses_get_202():
    for message in ({'jsonrpc':'2.0','method':'notifications/initialized'},
                    {'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':3}},
                    {'jsonrpc':'2.0','id':5,'result':{}},{'jsonrpc':'2.0','id':5,'error':{'code':1,'message':'x'}}):
        for flavour in ('http','stdio'):
            reply=handle(message,flavour=flavour);assert reply.status==202 and reply.body is None


def test_body_without_method_result_or_error_is_32600():
    reply=handle({'jsonrpc':'2.0','id':4})
    assert reply.status==400 and reply.body['error']['code']==-32600
    assert handle({'jsonrpc':'1.0','id':4,'method':'ping'}).body['error']['code']==-32600
    assert handle({'jsonrpc':'2.0','id':4,'method':7}).status==400


def test_ping_and_empty_optional_lists():
    assert handle(request('ping')).body['result']=={}
    assert handle(request('resources/list')).body['result']=={'resources':[]}
    assert handle(request('resources/templates/list')).body['result']=={'resourceTemplates':[]}
    assert handle(request('prompts/list')).body['result']=={'prompts':[]}
    reply=handle(request('logging/setLevel',{'level':'info'}))
    assert reply.status==200 and reply.body['error']['code']==-32601


def test_tools_list_is_deterministic_and_valid():
    first=handle(request('tools/list')).body['result']['tools'];second=handle(request('tools/list')).body['result']['tools']
    assert first==second and protocol.dumps(first)==protocol.dumps(second)
    assert [t['name'] for t in first]==['list_photos','get_photo','get_preview','contact_sheet','list_films','apply_edits',
        'auto_tone','auto_color','invert_negatives','undo_last_batch','export_photos','import_folder','create_virtual_copies',
        'set_labels','snapshot','open_photo','get_job']
    def properties(schema):
        for name,value in schema.get('properties',{}).items():
            yield name;yield from properties(value)
        if isinstance(schema.get('items'),dict):yield from properties(schema['items'])
    for tool in first:
        assert re.fullmatch('[a-z_]{1,64}',tool['name']) and tool['title'] and 'outputSchema' not in tool
        assert set(tool['annotations'])=={'title','readOnlyHint','destructiveHint','idempotentHint','openWorldHint'}
        assert tool['annotations']['openWorldHint'] is False and len(tool['description'])<=1000
        schema=tool['inputSchema'];assert schema['type']=='object' and schema['additionalProperties'] is False
        assert all(re.fullmatch('[A-Za-z0-9_.-]{1,64}',name) for name in properties(schema))
    assert len(json.dumps({'tools':first},separators=(',',':')))<=22000      # 15,460 with 12 tools; 21,312 with 17
    edits=next(t for t in first if t['name']=='apply_edits')['description']
    for key,(_,low,high) in FIELDS.items():
        assert key in edits
        if key not in ('monochrome','film_look','flip'):assert re.search(rf'{key}[^;]*{low:g}\.\.{high:g}',edits),key
    targeted=[t for t in first if 'ids' in t['inputSchema']['properties'] and 'where' in t['inputSchema']['properties']]
    assert len(targeted)==7 and all(mcp_tools.TARGET_TEXT[:-1] in t['description'] for t in targeted)
    assert "Each entry targets one of ids, where (list_photos filters) or scope ('selection' or 'current')" in edits


def test_readme_example_inverts_negatives_before_evening_out_exposure():
    """invert_negatives keeps the exposure and tone values set before it, so the example asks for them after."""
    root=Path(__file__).resolve().parents[1]
    english=next(l for l in (root/'README.md').read_text(encoding='utf-8').splitlines() if 'as a contact sheet' in l)
    korean=next(l for l in (root/'README.ko.md').read_text(encoding='utf-8').splitlines() if '밀착 이미지로 보여 주고' in l)
    assert english.index('invert the negatives')<english.index('even out the exposure')
    assert korean.index('네거티브 반전')<korean.index('노출을 고르게')


def test_tool_text_never_names_other_editors():
    """'Adobe RGB' is an export colour space, not an editor."""
    text=json.dumps(mcp_tools.DEFINITIONS)+protocol.INSTRUCTIONS+protocol.OFFLINE_TEXT+json.dumps(protocol.offline_templates())
    text=text.lower().replace('adobe rgb','')
    assert 'lightroom' not in text and 'adobe' not in text and 'photoshop' not in text


def test_unknown_tool_is_32602_missing_arguments_is_empty_and_tool_failures_are_is_error():
    reply=handle(request('tools/call',{'name':'delete_everything'}))
    assert reply.status==200 and reply.body['error']=={'code':-32602,'message':'Unknown tool: delete_everything'}
    tools=FakeTools();reply=handle(request('tools/call',{'name':'list_photos'}),tools=tools)
    assert tools.calls==[('list_photos',{},False)] and reply.body['result']['isError'] is False
    assert handle(request('tools/call',{'name':'list_photos','arguments':[1]})).body['error']['code']==-32602
    assert handle(request('tools/call',[1])).body['error']['code']==-32602
    assert handle(request('tools/call',{'name':'list_photos'}),tools=FakeTools(fail=True)).body['error']['code']==-32603
    failing=FakeTools(result={'content':[{'type':'text','text':'bad'}],'isError':True})
    assert handle(request('tools/call',{'name':'get_job','arguments':{'job':'j1'}}),tools=failing).body['result']['isError']


@pytest.mark.parametrize('arguments,message',[
    ({'edits':[{'ids':[1],'set':{'exposure':True}}]},'arguments.edits[0].set.exposure must be a number'),
    ({'edits':[{'ids':[True],'set':{'exposure':1}}]},'arguments.edits[0].ids[0] must be an integer'),
    ({'edits':[{'ids':[2**53],'set':{'exposure':1}}]},'arguments.edits[0].ids[0] must be at most 9007199254740991'),
    ({'edits':[{'ids':[1,1],'set':{'exposure':1}}]},'arguments.edits[0].ids must not repeat items'),
    ({'edits':[{'scope':'all','set':{'exposure':1}}]},'arguments.edits[0].scope must be one of selection, current'),
    ({'edits':[{'ids':[1],'set':{'path':1}}]},'arguments.edits[0].set.path is not a known argument'),
    ({'edits':[{'ids':[1],'set':{}}]},'arguments.edits[0].set needs at least 1 entries'),
    ({'edits':[]},'arguments.edits needs at least 1 items'),
    ({},'arguments.edits is required'),
    ({'edits':[{'ids':[1],'set':{'exposure':1}}],'extra':1},'arguments.extra is not a known argument')])
def test_argument_checker_messages(arguments,message):
    with pytest.raises(ServiceError) as caught:mcp_tools.check(mcp_tools.SCHEMAS['apply_edits'],arguments)
    assert str(caught.value)==message
    with pytest.raises(ServiceError,match='arguments.job does not have the expected form'):
        mcp_tools.check(mcp_tools.SCHEMAS['get_job'],{'job':'x1'})
    with pytest.raises(ServiceError,match='arguments.where.rating_min must be an integer'):
        mcp_tools.check(mcp_tools.SCHEMAS['auto_tone'],{'where':{'rating_min':2.5}})


def test_modern_disabled_discover_is_200_32601_and_meta_is_ignored(monkeypatch):
    monkeypatch.setattr(protocol,'MODERN_ENABLED',False)
    meta={'_meta':{protocol.META_VERSION:'2026-07-28',protocol.META_CAPABILITIES:{}}}
    reply=handle(request('server/discover',meta),{'MCP-Protocol-Version':'2026-07-28','Mcp-Method':'server/discover'})
    assert reply.status==200 and reply.body['error']=={'code':-32601,'message':'Method not found'}
    reply=handle(request('tools/list',meta))
    assert reply.status==200 and 'resultType' not in reply.body['result'] and len(reply.body['result']['tools'])==len(mcp_tools.DEFINITIONS)


def modern(method,params=None,headers=None,version='2026-07-28',capabilities=True):
    meta={protocol.META_VERSION:version}
    if capabilities:meta[protocol.META_CAPABILITIES]={}
    sent={'MCP-Protocol-Version':version,'Mcp-Method':method}
    if method=='tools/call':sent['Mcp-Name']=(params or {}).get('name')
    sent.update(headers or {})
    return handle(request(method,{**(params or {}),'_meta':meta}),{k:v for k,v in sent.items() if v is not None})


@pytest.fixture
def modern_on(monkeypatch):monkeypatch.setattr(protocol,'MODERN_ENABLED',True)


def test_modern_discover_has_complete_result_meta_and_cache_hints(modern_on):
    reply=modern('server/discover')
    result=reply.body['result']
    assert reply.status==200 and result['supportedVersions']==['2026-07-28'] and result['capabilities']=={'tools':{}}
    assert result['resultType']=='complete' and result['_meta'][protocol.META_SERVER]['name']=='grainy'
    assert result['ttlMs']==600000 and result['cacheScope']=='private' and result['instructions']==protocol.INSTRUCTIONS
    listed=modern('tools/list').body['result'];assert listed['resultType']=='complete' and listed['ttlMs']==600000
    called=modern('tools/call',{'name':'list_photos','arguments':{}}).body['result']
    assert called['resultType']=='complete' and called['isError'] is False and protocol.META_SERVER in called['_meta']
    assert modern('prompts/list').body['result']['prompts']==[]


def test_modern_header_mismatch_is_32020_including_base64_names(modern_on):
    assert modern('tools/list',headers={'Mcp-Method':'tools/call'}).body['error']['code']==-32020
    assert modern('tools/list',headers={'MCP-Protocol-Version':'2025-06-18'}).body['error']['code']==-32020
    assert modern('tools/call',{'name':'list_photos'},headers={'Mcp-Name':'get_photo'}).status==400
    encoded='=?base64?'+base64.b64encode('list_photos'.encode()).decode()+'?='
    reply=modern('tools/call',{'name':'list_photos','arguments':{}},headers={'Mcp-Name':encoded})
    assert reply.status==200 and reply.body['result']['resultType']=='complete'
    wrong='=?base64?'+base64.b64encode(b'get_photo').decode()+'?='
    assert modern('tools/call',{'name':'list_photos'},headers={'Mcp-Name':wrong}).body['error']['code']==-32020


def test_modern_unsupported_version_is_32022_with_supported_list(modern_on):
    reply=modern('tools/list',version='2099-01-01')
    assert reply.status==400 and reply.body['error']['code']==-32022
    assert reply.body['error']['data']=={'supported':list(protocol.SUPPORTED),'requested':'2099-01-01'}


def test_modern_missing_client_capabilities_is_32602(modern_on):
    reply=modern('tools/list',capabilities=False)
    assert reply.status==400 and reply.body['error']['code']==-32602


def test_modern_unknown_method_and_listen_are_404(modern_on):
    for method in ('subscriptions/listen','ping','logging/setLevel'):
        reply=modern(method);assert reply.status==404 and reply.body['error']['code']==-32601


def test_legacy_request_with_modern_version_header_gets_non_modern_400():
    reply=handle(request('tools/list'),{'mcp-protocol-version':'2026-07-28'})
    assert reply.status==400 and reply.body['error']=={'code':-32600,'message':'Unsupported MCP-Protocol-Version'}
    assert handle(request('tools/list'),{'MCP-Protocol-Version':'2025-06-18'}).status==200


@pytest.mark.parametrize('message',[[],[request('ping')],'text',7,None])
def test_arrays_and_bad_ids_are_32600(message):
    reply=handle(message)
    assert reply.status==400 and reply.body=={'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid Request'}}
    for ident in (True,1.5,None,[1],{'a':1}):
        reply=handle({'jsonrpc':'2.0','id':ident,'method':'ping'})
        assert reply.status==400 and reply.body['id'] is None and reply.body['error']['code']==-32600


def test_stdio_flavour_is_legacy_and_all_errors_are_200(monkeypatch):
    monkeypatch.setattr(protocol,'MODERN_ENABLED',True)
    for message in ([],{'jsonrpc':'2.0','id':1},{'jsonrpc':'2.0','id':True,'method':'ping'}):
        reply=handle(message,flavour='stdio');assert reply.status==200 and reply.body['error']['code']==-32600
    reply=handle(request('server/discover',{'_meta':{protocol.META_VERSION:'2026-07-28',protocol.META_CAPABILITIES:{}}}),
                 {'MCP-Protocol-Version':'2026-07-28','Mcp-Method':'server/discover'},flavour='stdio')
    assert reply.status==200 and reply.body['error']['code']==-32601
    reply=handle(request('tools/list'),{'MCP-Protocol-Version':'2099-01-01','Mcp-Method':'x'},flavour='stdio')
    assert reply.status==200 and len(reply.body['result']['tools'])==len(mcp_tools.DEFINITIONS) and 'resultType' not in reply.body['result']


@pytest.mark.parametrize('ident',[7,'a"b','id-é',0])
def test_offline_templates_become_valid_replies_for_number_and_string_ids(ident):
    templates=protocol.offline_templates()
    assert set(templates)=={'offline-initialize.json','offline-tools.json','offline-call.json'}
    for name,text in templates.items():
        assert '\n' not in text and text.isascii() and text.count('__ID__')==1
        reply=json.loads(text.replace('__ID__',json.dumps(ident)))
        assert reply['jsonrpc']=='2.0' and reply['id']==ident
    initialize=json.loads(templates['offline-initialize.json'].replace('__ID__','1'))['result']
    assert initialize['protocolVersion']==protocol.OFFLINE_VERSION and initialize['capabilities']=={'tools':{'listChanged':False}}
    assert len(json.loads(templates['offline-tools.json'].replace('__ID__','1'))['result']['tools'])==len(mcp_tools.DEFINITIONS)
    call=json.loads(templates['offline-call.json'].replace('__ID__','1'))['result']
    assert call['isError'] is True and 'Grainy did not answer' in call['content'][0]['text']


def test_bodies_are_single_line_ascii():
    tools=FakeTools(result={'content':[{'type':'text','text':'{"name":"현상.png"}\n두 줄'}],'isError':False})
    data=protocol.dumps(handle(request('tools/call',{'name':'list_photos'}),tools=tools).body)
    assert data.isascii() and b'\n' not in data and b'\\uD604' not in data and b'\\ud604' in data
    assert json.loads(data)['result']['content'][0]['text']=='{"name":"현상.png"}\n두 줄'
