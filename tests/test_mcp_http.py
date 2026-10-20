"""mcp_http: the AI connection's HTTP endpoint on real loopback sockets with a stub host."""
import http.client
import json
import socket
import struct
import sys
import threading
import time

import pytest

from luma import mcp_http,mcp_tools

TOKEN='0123456789abcdef'*4


class StubTools:
    def __init__(self):self.calls=[];self.guis=[];self.gate=None;self.entered=threading.Event()
    def definitions(self):return mcp_tools.DEFINITIONS
    def call(self,name,arguments,*,modern,deadline,gui=None):
        self.calls.append((name,arguments));self.guis.append(gui)
        self.entered.set()
        if self.gate is not None:assert self.gate.wait(10)
        return {'content':[{'type':'text','text':json.dumps(arguments,ensure_ascii=False)}],'isError':False}


class Host:
    def __init__(self):
        self.token=TOKEN;self.calls=threading.BoundedSemaphore(4);self.notes=[];self.tools=StubTools()
    def note(self,name,outcome,ms=0):self.notes.append((name,outcome))


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    """Servers poll more often in tests, so stopping one takes milliseconds instead of half a second."""
    monkeypatch.setattr(mcp_http,'POLL_SECONDS',.05)


@pytest.fixture
def host():return Host()


@pytest.fixture
def server(host):
    value=mcp_http.start(0,host)
    yield value
    value.shutdown();value.close_connections();value.server_close()


def port_of(server):return server.server_address[1]


def post(server,body,headers=None,path='/mcp',method='POST',token=TOKEN):
    connection=http.client.HTTPConnection('127.0.0.1',port_of(server),timeout=10)
    try:
        sent={'Content-Type':'application/json'}
        if token is not None:sent['Authorization']=f'Bearer {token}'
        sent.update(headers or {})
        data=body if isinstance(body,bytes) else json.dumps(body).encode()
        connection.request(method,path,data,{k:v for k,v in sent.items() if v is not None})
        response=connection.getresponse();payload=response.read()
        return response.status,{k.lower():v for k,v in response.getheaders()},payload
    finally:connection.close()


def ping(ident=1):return {'jsonrpc':'2.0','id':ident,'method':'ping'}


def raw(server,data,timeout=5):
    """Send bytes on a fresh socket and read until the server closes it."""
    with socket.create_connection(('127.0.0.1',port_of(server)),timeout=timeout) as s:
        s.sendall(data);chunks=[]
        try:
            while chunk:=s.recv(65536):chunks.append(chunk)
        except (ConnectionResetError,ConnectionAbortedError):pass
        return b''.join(chunks)


def request_bytes(server,body=b'{}',headers=None,method='POST',path='/mcp',host=None):
    port=port_of(server)
    lines=[f'{method} {path} HTTP/1.1',f'Host: {f"127.0.0.1:{port}" if host is None else host}']
    lines+=[f'{k}: {v}' for k,v in (headers or {}).items()]
    return ('\r\n'.join(lines)+'\r\n\r\n').encode()+body


def test_binds_loopback_only_and_exclusively(server,host):
    assert server.server_address[0]=='127.0.0.1'
    assert post(server,ping())[0]==200
    with pytest.raises(OSError):mcp_http.McpHttpServer(port_of(server),host)
    names=set()
    try:names={info[4][0] for info in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET)}-{'127.0.0.1'}
    except OSError:pass
    for address in list(names)[:2]:
        with pytest.raises(OSError):socket.create_connection((address,port_of(server)),timeout=1).close()


def test_requires_bearer_token_and_ignores_query_tokens(server):
    status,headers,body=post(server,ping(),token=None)
    assert status==401 and headers['www-authenticate']=='Bearer realm="Grainy"' and body==b''
    assert post(server,ping(),path=f'/mcp?token={TOKEN}',token=None)[0]==401
    assert post(server,ping(),path=f'/mcp?access_token={TOKEN}',token='wrong')[0]==401
    assert post(server,ping(),token=TOKEN[:-1]+'0')[0]==401
    assert post(server,ping(),headers={'Authorization':f'Basic {TOKEN}'},token=None)[0]==401
    status,_,body=post(server,ping(),path='/mcp?x=1')
    assert status==200 and json.loads(body)['result']=={}


@pytest.mark.parametrize('token',[None,'','None','0123456789','g'*64,TOKEN.upper()])
def test_missing_or_short_token_always_401(server,host,token):
    host.token=token
    for sent in ('None','',str(token),TOKEN):
        assert post(server,ping(),headers={'Authorization':f'Bearer {sent}'},token=None)[0]==401


def test_bearer_scheme_is_case_insensitive_and_compare_uses_compare_digest(server,monkeypatch):
    import hmac
    seen=[];original=hmac.compare_digest
    def spy(a,b):seen.append((a,b));return original(a,b)
    monkeypatch.setattr(mcp_http.hmac,'compare_digest',spy)
    for scheme in ('Bearer','bearer','BEARER'):
        assert post(server,ping(),headers={'Authorization':f'{scheme} {TOKEN}'},token=None)[0]==200
    assert post(server,ping(),headers={'Authorization':f'Bearer   {TOKEN}  '},token=None)[0]==200
    assert len(seen)==4 and all(b==TOKEN.encode() for _,b in seen)


def test_rejects_foreign_host_and_origin_allows_same_origin(server):
    port=port_of(server)
    good={'Content-Type':'application/json','Authorization':f'Bearer {TOKEN}','Content-Length':'2'}
    for name in ('evil.example','127.0.0.1',f'127.0.0.1:{port+1}',f'192.168.0.2:{port}','',f'localhost.evil:{port}'):
        assert raw(server,request_bytes(server,headers=good,host=name)).startswith(b'HTTP/1.1 403')
    for name in (f'localhost:{port}',f'LOCALHOST:{port}',f'[::1]:{port}'):
        reply=raw(server,request_bytes(server,json.dumps(ping()).encode(),{**good,'Content-Length':str(len(json.dumps(ping()))),
                                                                       'Connection':'close'},host=name))
        assert reply.startswith(b'HTTP/1.1 200'),name
    for origin in ('http://evil.example','null',f'http://127.0.0.1:{port+1}',f'https://127.0.0.1:{port}'):
        assert post(server,ping(),headers={'Origin':origin})[0]==403
    for origin in (f'http://127.0.0.1:{port}',f'http://localhost:{port}'):
        assert post(server,ping(),headers={'Origin':origin})[0]==200


@pytest.mark.parametrize('method',['GET','PUT','DELETE','PATCH','OPTIONS'])
def test_non_post_methods_get_405_with_allow(server,method):
    status,headers,body=post(server,b'',method=method)
    assert status==405 and headers['allow']=='POST' and body==b''


def test_length_size_and_type_limits(server):
    reply=raw(server,request_bytes(server,b'',{'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json',
                                                   'Transfer-Encoding':'chunked'}))
    assert reply.startswith(b'HTTP/1.1 411')
    assert post(server,b'x'*(mcp_http.MAX_BODY+1))[0]==413
    for kind in ('text/plain','application/x-www-form-urlencoded','application/jsonx',None):
        assert post(server,ping(),headers={'Content-Type':kind})[0]==415
    for kind in ('application/json; charset=utf-8','application/json;charset=UTF-8','Application/JSON'):
        assert post(server,ping(),headers={'Content-Type':kind})[0]==200


@pytest.mark.parametrize('status,data',[
    (401,lambda s:request_bytes(s,b'{"jsonrpc":"2.0"}',{'Content-Length':'17','Content-Type':'application/json'})),
    (404,lambda s:request_bytes(s,b'{"jsonrpc":"2.0"}',{'Content-Length':'17'},path='/other')),
    (405,lambda s:request_bytes(s,b'{"jsonrpc":"2.0"}',{'Content-Length':'17'},method='PUT')),
    (411,lambda s:request_bytes(s,b'',{'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json'})),
    (415,lambda s:request_bytes(s,b'{"jsonrpc":"2.0"}',{'Authorization':f'Bearer {TOKEN}','Content-Length':'17','Content-Type':'text/plain'}))])
def test_early_replies_close_the_connection(server,host,status,data):
    """The unread body of a refused request is never parsed as the next request on the connection."""
    smuggled=request_bytes(server,json.dumps(ping(9)).encode(),{'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json',
                                                                    'Content-Length':str(len(json.dumps(ping(9))))})
    reply=raw(server,data(server)+smuggled)
    assert reply.startswith(f'HTTP/1.1 {status}'.encode()) and b'Connection: close' in reply
    assert reply.count(b'HTTP/1.1')==1 and b'"id":9' not in reply


def test_send_error_bodies_are_empty(server):
    reply=raw(server,request_bytes(server,headers={'Content-Length':'0'},method='FOO'))
    head,_,body=reply.partition(b'\r\n\r\n')
    assert head.startswith(b'HTTP/1.1 501') and b'Content-Length: 0' in head and body==b'' and b'<' not in reply
    reply=raw(server,b'GET /mcp HTTP/1.1 extra\r\nHost: x\r\n\r\n')
    assert b'<' not in reply and b'html' not in reply.lower()
    reply=raw(server,b'\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03\r\n\r\n')
    assert b'<' not in reply


@pytest.mark.parametrize('path',['/','/mcp/','/.well-known/oauth-authorization-server','/.well-known/oauth-protected-resource/mcp',
                                 '/register','/mcp/stdio/x','/MCP'])
def test_unknown_and_well_known_paths_are_404(server,path):
    status,_,body=post(server,ping(),path=path)
    assert status==404 and body==b''


def test_never_sends_cors_headers(server):
    port=port_of(server)
    for headers in ({'Origin':f'http://127.0.0.1:{port}'},{},{'Origin':'http://evil.example'}):
        for method in ('POST','OPTIONS'):
            _,received,_=post(server,ping(),headers=headers,method=method)
            assert not [k for k in received if k.startswith('access-control')]
            assert received.get('cache-control')=='no-store'


def test_nan_infinity_and_deep_nesting_are_parse_errors(server):
    for body in (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}',b'[Infinity]',b'{"a":-Infinity}',
                 b'['*100000+b']'*100000,b'{"jsonrpc":',b'\xff\xfe',b''):
        status,_,payload=post(server,body)
        assert status==400 and json.loads(payload)=={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}}
        status,_,payload=post(server,body,path='/mcp/stdio')
        assert status==200 and json.loads(payload)['error']['code']==-32700


def test_request_logging_never_touches_stderr(server,monkeypatch):
    monkeypatch.setattr(sys,'stderr',None)
    assert post(server,ping())[0]==200 and post(server,ping(),token=None)[0]==401
    raw(server,b'GARBAGE\r\n\r\n');raw(server,b'GET /mcp HTTP/1.1 extra\r\n\r\n');raw(server,request_bytes(server,method='FOO'))
    with socket.create_connection(('127.0.0.1',port_of(server)),timeout=2) as s:
        s.sendall(request_bytes(server,b'{"jsonrpc"',{'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json','Content-Length':'500'}))
    assert post(server,ping())[0]==200


def test_connection_cap_refuses_the_seventeenth(server):
    sockets=[]
    try:
        for _ in range(mcp_http.MAX_CONNECTIONS):
            sockets.append(socket.create_connection(('127.0.0.1',port_of(server)),timeout=5))
        deadline=time.monotonic()+5
        while len(server.phases)<mcp_http.MAX_CONNECTIONS and time.monotonic()<deadline:time.sleep(.02)
        assert len(server.phases)==mcp_http.MAX_CONNECTIONS
        with socket.create_connection(('127.0.0.1',port_of(server)),timeout=5) as extra:
            extra.sendall(request_bytes(server,json.dumps(ping()).encode(),{'Authorization':f'Bearer {TOKEN}',
                'Content-Type':'application/json','Content-Length':str(len(json.dumps(ping())))}))
            try:data=extra.recv(100)
            except (ConnectionResetError,ConnectionAbortedError):data=b''
            assert data==b''
        assert len(server.phases)==mcp_http.MAX_CONNECTIONS
    finally:
        for s in sockets:s.close()
    deadline=time.monotonic()+5
    while server.phases and time.monotonic()<deadline:time.sleep(.02)
    assert post(server,ping())[0]==200


def test_lingering_closes_count_against_the_connection_cap(server):
    """Early replies close by lingering (up to LINGER_SECONDS) while the client keeps its socket open. Those
    connections keep their place, so a flood of them never runs more handler threads than MAX_CONNECTIONS."""
    port=port_of(server);stop=threading.Event();peak={'threads':0,'phases':0}
    request=f'GET /x HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode()
    def flood():
        held=[]
        while not stop.is_set():
            try:
                connection=socket.create_connection(('127.0.0.1',port),timeout=2);held.append(connection)
                connection.sendall(request)
            except OSError:pass
            if len(held)>200:held.pop(0).close()
        for connection in held:connection.close()
    baseline=threading.active_count()
    flooders=[threading.Thread(target=flood,daemon=True) for _ in range(2)]
    for thread in flooders:thread.start()
    deadline=time.monotonic()+2.5
    while time.monotonic()<deadline:
        peak['threads']=max(peak['threads'],threading.active_count()-baseline-len(flooders))
        with server.lock:peak['phases']=max(peak['phases'],len(server.phases))
        time.sleep(.005)
    stop.set()
    for thread in flooders:thread.join(10)
    assert peak['phases']<=mcp_http.MAX_CONNECTIONS and peak['threads']<=mcp_http.MAX_CONNECTIONS+4,peak
    deadline=time.monotonic()+5
    while server.phases and time.monotonic()<deadline:time.sleep(.02)
    assert not server.phases and post(server,ping())[0]==200


def test_client_disconnects_are_not_errors_in_the_activity_list(server,host):
    """A client process that ends with its kept-alive connection open resets it; that is no error to show."""
    body=json.dumps(ping()).encode()
    with socket.create_connection(('127.0.0.1',port_of(server)),timeout=5) as s:
        s.sendall(request_bytes(server,body,{'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json',
                                             'Content-Length':str(len(body))}))
        reply=b''
        while not reply.endswith(b'"result":{}}'):reply+=s.recv(65536)
        # Closing with SO_LINGER 0 sends a reset, as Windows does when the client process ends.
        s.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('HH' if sys.platform=='win32' else 'ii',1,0))
    deadline=time.monotonic()+5
    while server.phases and time.monotonic()<deadline:time.sleep(.02)
    assert not server.phases and not [note for note in host.notes if note[0]=='http'],host.notes
    for error in (ConnectionResetError(10054,'reset'),ConnectionAbortedError(10053,'aborted'),BrokenPipeError(),TimeoutError()):
        try:raise error
        except OSError:server.handle_error(None,None)
    assert not [note for note in host.notes if note[0]=='http']
    try:raise ValueError('broken')
    except ValueError:server.handle_error(None,None)
    assert host.notes[-1]==('http','error: ValueError: broken')


def test_tools_get_the_gui_of_the_server_the_request_came_in_on(host):
    marker=object();server=mcp_http.start(0,host,marker)
    try:
        call={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'list_photos','arguments':{}}}
        assert post(server,call)[0]==200 and host.tools.guis==[marker]
    finally:server.shutdown();server.close_connections();server.server_close()


def test_header_and_idle_deadlines_close_tricklers(server,monkeypatch):
    monkeypatch.setattr(mcp_http,'HEADER_SECONDS',.5);monkeypatch.setattr(mcp_http,'IDLE_SECONDS',.6)
    monkeypatch.setattr(mcp_http,'BODY_SECONDS',.5)
    started=time.monotonic()
    with socket.create_connection(('127.0.0.1',port_of(server)),timeout=8) as s:
        s.sendall(b'POST /mcp HTTP/1.1\r\n')
        for _ in range(10):
            try:s.sendall(b'X-Slow: 1\r\n')
            except OSError:break
            time.sleep(.2)
        try:data=s.recv(100)
        except (ConnectionResetError,ConnectionAbortedError):data=b''
    assert data==b'' and time.monotonic()-started<6
    with socket.create_connection(('127.0.0.1',port_of(server)),timeout=8) as s:
        body=json.dumps(ping()).encode()
        s.sendall(request_bytes(server,body,{'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json','Content-Length':str(len(body))}))
        first=b''
        while not first.endswith(b'"result":{}}'):first+=s.recv(65536)
        assert first.startswith(b'HTTP/1.1 200') and b'Keep-Alive: timeout=55' in first
        started=time.monotonic()
        try:data=s.recv(100)
        except (ConnectionResetError,ConnectionAbortedError):data=b''
        assert data==b'' and time.monotonic()-started<4
    with socket.create_connection(('127.0.0.1',port_of(server)),timeout=8) as s:
        s.sendall(request_bytes(server,b'{"js',{'Authorization':f'Bearer {TOKEN}','Content-Type':'application/json','Content-Length':'40'}))
        started=time.monotonic()
        try:data=s.recv(100)
        except (ConnectionResetError,ConnectionAbortedError):data=b''
        assert data==b'' and time.monotonic()-started<4


def test_call_slots_wait_then_report_busy(server,host,monkeypatch):
    monkeypatch.setattr(mcp_http,'CALL_SLOT_SECONDS',.3)
    host.calls=threading.BoundedSemaphore(1);host.tools.gate=threading.Event()
    call={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'list_photos','arguments':{'text':'a'}}}
    first={}
    thread=threading.Thread(target=lambda:first.update(reply=post(server,call)),daemon=True);thread.start()
    assert host.tools.entered.wait(5)
    started=time.monotonic();status,_,body=post(server,{**call,'id':2})
    result=json.loads(body)['result']
    assert status==200 and result['isError'] and result['content'][0]['text']==mcp_http.SLOTS_BUSY
    assert .25<time.monotonic()-started<3 and len(host.tools.calls)==1
    assert post(server,ping(3))[0]==200            # the protocol's own replies need no slot
    host.tools.gate.set();thread.join(5)
    assert json.loads(first['reply'][2])['result']['isError'] is False


def test_stop_releases_port_and_closes_idle_connections(host):
    server=mcp_http.start(0,host);port=port_of(server)
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
    connection.request('POST','/mcp',json.dumps(ping()),{'Content-Type':'application/json','Authorization':f'Bearer {TOKEN}'})
    assert connection.getresponse().read()
    started=time.monotonic()
    server.shutdown();server.close_connections();server.server_close()
    assert time.monotonic()-started<2
    with pytest.raises((OSError,http.client.HTTPException)):
        connection.request('POST','/mcp',json.dumps(ping()),{'Content-Type':'application/json','Authorization':f'Bearer {TOKEN}'})
        connection.getresponse().read()
    connection.close()
    again=mcp_http.start(port,host)
    try:assert post(again,ping())[0]==200
    finally:again.shutdown();again.server_close()


def test_regenerated_token_applies_to_next_request(server,host):
    assert post(server,ping())[0]==200
    host.token='f'*64
    assert post(server,ping())[0]==401 and post(server,ping(),token='f'*64)[0]==200


def test_token_never_in_notes_or_errors(server,host):
    call={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_photo','arguments':{'ids':[1]}}}
    post(server,call);post(server,ping(),token='x'*64);post(server,ping(),path='/nope');post(server,b'{bad')
    raw(server,b'GARBAGE\r\n\r\n')
    assert ('get_photo','ok') in host.notes and ('http','refused 401') in host.notes
    assert TOKEN not in repr(host.notes) and 'x'*64 not in repr(host.notes)
    status,headers,body=post(server,ping(),token='y'*64)
    assert TOKEN.encode() not in body and 'y'*64 not in repr(headers)
