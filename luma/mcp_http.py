"""The AI connection's HTTP endpoint: 127.0.0.1 only, a key on every request, bounded in every phase.

POST /mcp is the Streamable HTTP transport as Claude Code and Codex use it (JSON replies only, no
sessions, no event streams). POST /mcp/stdio serves the stdio bridges (assets/mcp) in the legacy era.
"""
import hmac
import http.server
import json
import re
import socket
import socketserver
import sys
import threading
import time
import traceback

from . import mcp_protocol

MAX_BODY=1_048_576
MAX_CONNECTIONS=16
HEADER_SECONDS=10     # first request on a connection: request line + headers
IDLE_SECONDS=60       # after a reply on a kept-alive connection, until the next request's headers are complete
BODY_SECONDS=10
CALL_SLOT_SECONDS=5   # waiting for one of host.calls
LINGER_SECONDS=1      # closing a connection: how long the client's remaining bytes are read and discarded
POLL_SECONDS=.5       # serve_forever's poll: phase deadlines are checked, and stop() returns, this often
TOKEN=re.compile(r'[0-9a-f]{64}')
PATHS=('/mcp','/mcp/stdio')
SLOTS_BUSY="Grainy is handling other requests; nothing was changed. Try again in a moment."
PARSE_ERROR={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}}


class McpHttpServer(socketserver.ThreadingMixIn,http.server.HTTPServer):
    daemon_threads=True
    block_on_close=False          # stop() waits (bounded) for requests itself
    # Windows: SO_REUSEADDR would let another program take a port in use, so server_bind sets SO_EXCLUSIVEADDRUSE
    # instead. Elsewhere SO_REUSEADDR only lets the port be bound again over connections a stopped server left
    # in TIME_WAIT (about 30 s on macOS): without it switching the connection off and on, or Grainy restarting
    # for an update, cannot use the port again for that long. A port another program listens on stays refused.
    allow_reuse_address=not hasattr(socket,'SO_EXCLUSIVEADDRUSE')
    request_queue_size=16

    def __init__(self,port,host,gui=None):
        # gui: what host.tools.call gets with every request on this server (McpService: this server's GuiCall)
        self.host=host;self.gui=gui;self.phases={};self.lock=threading.Lock();self.in_flight=0
        super().__init__(('127.0.0.1',port),Handler)

    def server_bind(self):
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        socketserver.TCPServer.server_bind(self)          # without HTTPServer.server_bind's getfqdn() reverse lookup
        self.server_name,self.server_port='127.0.0.1',self.server_address[1]

    def set_phase(self,request,name,seconds=None):
        with self.lock:
            if request in self.phases:self.phases[request]=(name,None if seconds is None else time.monotonic()+seconds)

    def process_request(self,request,address):
        with self.lock:
            refused=len(self.phases)>=MAX_CONNECTIONS
            if not refused:self.phases[request]=('headers',time.monotonic()+HEADER_SECONDS)
        if refused:
            socketserver.TCPServer.shutdown_request(self,request)     # at once, without a thread
            return
        super().process_request(request,address)

    def shutdown_request(self,request):
        """A lingering close: closing with unread bytes (the body of a refused request) would send a reset,
        and the client could lose the reply. Half-close, then discard what the client still sends (bounded).
        The connection keeps its place in phases until it is closed: lingering connections count against
        MAX_CONNECTIONS too, so there are never more handler threads than that."""
        self.set_phase(request,'closing')
        try:
            request.shutdown(socket.SHUT_WR)
            request.settimeout(LINGER_SECONDS);deadline=time.monotonic()+LINGER_SECONDS;drained=0
            while drained<=2*MAX_BODY and time.monotonic()<deadline:
                data=request.recv(65536)
                if not data:break
                drained+=len(data)
        except OSError:pass
        try:self.close_request(request)
        finally:
            with self.lock:self.phases.pop(request,None)

    def service_actions(self):
        """Every poll of serve_forever: close connections whose headers, body or idle time ran out.
        A request being handled ('busy') is bounded by its own deadline instead."""
        now=time.monotonic()
        with self.lock:late=[r for r,(name,deadline) in self.phases.items() if name!='busy' and deadline is not None and deadline<now]
        for request in late:
            try:request.shutdown(socket.SHUT_RDWR)
            except OSError:pass

    def close_connections(self):
        with self.lock:requests=list(self.phases)
        for request in requests:
            try:request.shutdown(socket.SHUT_RDWR)
            except OSError:pass

    def handle_error(self,request,address):
        # A client that went away (a reset when its process ended with the connection open, or a timeout) is
        # a normal disconnect, not an error for the activity list.
        if isinstance(sys.exc_info()[1],(ConnectionError,TimeoutError)):return
        # Never stderr: the windowed build has none.
        try:self.host.note('http','error: '+traceback.format_exc().strip().splitlines()[-1][:200])
        except Exception:pass


def start(port,host,gui=None):
    """Bind (OSError propagates) and serve in a daemon thread."""
    server=McpHttpServer(port,host,gui)
    threading.Thread(target=server.serve_forever,kwargs={'poll_interval':POLL_SECONDS},daemon=True,name='GrainyMcpServer').start()
    return server


class SlotTools:
    """host.tools with one of host.calls held around each tools/call, which gets the server's gui."""
    def __init__(self,host,deadline,gui=None):
        self.host,self.deadline,self.gui=host,deadline,gui

    def definitions(self):return self.host.tools.definitions()

    def call(self,name,arguments,*,modern,deadline):
        if not self.host.calls.acquire(timeout=max(0.,min(CALL_SLOT_SECONDS,deadline-time.monotonic()))):
            return {'content':[{'type':'text','text':SLOTS_BUSY}],'isError':True}
        try:return self.host.tools.call(name,arguments,modern=modern,deadline=deadline,gui=self.gui)
        finally:self.host.calls.release()


def _reject_constant(name):
    raise ValueError(f'{name} is not JSON')


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='Grainy'
    sys_version=''

    @property
    def timeout(self):return IDLE_SECONDS            # per socket read; the phase deadlines bound the totals

    def log_message(self,format,*args):pass
    def log_request(self,code='-',size='-'):pass
    def log_error(self,format,*args):pass

    def phase(self,name,seconds=None):self.server.set_phase(self.request,name,seconds)

    def parse_request(self):
        parsed=super().parse_request()
        if parsed:self.phase('busy')
        return parsed

    def send_error(self,code,message=None,explain=None):
        """The status alone: no HTML page, and the connection is closed (its rest is never read)."""
        self.close_connection=True
        try:
            self.send_response(code)
            self.send_header('Content-Length','0');self.send_header('Connection','close');self.end_headers()
        except OSError:pass

    def early(self,status,headers=None):
        """A reply sent before the body was read: the connection closes, so a body is never taken as a request."""
        self.close_connection=True
        self.send_response(status)
        for key,value in (headers or {}).items():self.send_header(key,value)
        self.send_header('Content-Length','0');self.send_header('Cache-Control','no-store');self.send_header('Connection','close')
        self.end_headers()
        if status in (401,403):self.server.host.note('http',f'refused {status}')

    def reply(self,status,body=None):
        payload=b'' if body is None else mcp_protocol.dumps(body)
        self.send_response(status)
        if payload:self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store')
        if self.close_connection:self.send_header('Connection','close')
        else:self.send_header('Keep-Alive','timeout=55')
        self.end_headers()
        if payload:self.wfile.write(payload)
        self.wfile.flush()

    def do_POST(self):self.route()
    do_GET=do_PUT=do_DELETE=do_PATCH=do_HEAD=do_OPTIONS=do_POST

    def route(self):
        server=self.server;host=server.host;port=server.server_address[1]
        path=self.path.split('?',1)[0]
        if path not in PATHS:return self.early(404)
        if (self.headers.get('Host') or '').lower() not in (f'127.0.0.1:{port}',f'localhost:{port}',f'[::1]:{port}'):
            return self.early(403)
        origin=self.headers.get('Origin')
        if origin is not None and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}'):return self.early(403)
        if self.command!='POST':return self.early(405,{'Allow':'POST'})
        token=host.token
        challenge={'WWW-Authenticate':'Bearer realm="Grainy"'}
        if not token or not isinstance(token,str) or not TOKEN.fullmatch(token):return self.early(401,challenge)
        scheme,_,value=(self.headers.get('Authorization') or '').partition(' ')
        if scheme.lower()!='bearer' or not hmac.compare_digest(value.strip().encode('utf-8','replace'),token.encode()):
            return self.early(401,challenge)
        length=self.headers.get('Content-Length')
        if length is None:return self.early(411)
        try:size=int(length)
        except ValueError:return self.early(400)
        if size<0:return self.early(400)
        if size>MAX_BODY:return self.early(413)
        kind=(self.headers.get('Content-Type') or '').split(';',1)[0].strip().lower()
        if kind!='application/json':return self.early(415)
        self.phase('body',BODY_SECONDS)
        body=self.rfile.read(size)
        if len(body)<size:self.close_connection=True;return
        self.phase('busy')
        flavour='stdio' if path=='/mcp/stdio' else 'http'
        try:message=json.loads(body.decode('utf-8'),parse_constant=_reject_constant)
        except (ValueError,UnicodeDecodeError,RecursionError):
            self.reply(200 if flavour=='stdio' else 400,PARSE_ERROR);self.idle();return
        deadline=time.monotonic()+mcp_protocol.DEADLINE_SECONDS
        started=time.monotonic()
        with server.lock:server.in_flight+=1
        try:
            outcome=mcp_protocol.handle(message,self.headers,flavour=flavour,tools=SlotTools(host,deadline,server.gui),deadline=deadline)
            self.reply(outcome.status,outcome.body)
        finally:
            with server.lock:server.in_flight-=1
        if outcome.status!=202:self.note(message,outcome,started)
        self.idle()

    def idle(self):
        if not self.close_connection:self.phase('idle',IDLE_SECONDS)

    def note(self,message,outcome,started):
        name=message.get('method') if isinstance(message,dict) else None
        if not isinstance(name,str):name='request'
        params=message.get('params') if isinstance(message,dict) else None
        if name=='tools/call' and isinstance(params,dict) and isinstance(params.get('name'),str):name=params['name'][:64]
        body=outcome.body or {}
        if 'error' in body:result='error '+str(body['error'].get('code'))
        elif isinstance(body.get('result'),dict) and body['result'].get('isError'):result='error'
        else:result='ok'
        try:self.server.host.note(name[:64],result,int((time.monotonic()-started)*1000))
        except Exception:pass
