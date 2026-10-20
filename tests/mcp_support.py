"""Shared helpers for the AI connection's tests: a DesktopSession on a temporary library with the server on an
ephemeral port, test photos, and JSON-RPC calls made from a worker thread while the GUI thread runs Qt."""
import http.client
import json
import os
import threading
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import numpy as np
from PIL import Image

from test_studio_ui import wait
from luma.desktop_session import DesktopSession
from luma.preferences import Preferences


def make_session(app,tmp_path,*,start=True,language='ko',settings=None):
    preferences=Preferences(tmp_path/'settings.json');preferences.save_language(language);preferences.save('update_check',False)
    for key,value in (settings or {}).items():preferences.save(key,value)
    session=DesktopSession(app,tmp_path/'library',preferences)
    session.apply_language(language);session.open_window()
    if start:assert session.mcp.start(0),session.mcp.error
    return session


def close_session(app,session):
    session.mcp.stop();session.switching=True
    if session.window is not None and not session.window.closing:session.window.close()
    try:app.lastWindowClosed.disconnect(session.last_window_closed)
    except (RuntimeError,TypeError):pass


def gradient(path,size=(240,160),tint=(1.,1.,1.),low=.15,high=.85):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    yy,xx=np.mgrid[:size[1],:size[0]]
    base=low+(high-low)*(xx/size[0]*.7+yy/size[1]*.3)
    pixels=np.stack([base*tint[0],base*tint[1],base*tint[2]],-1)
    Image.fromarray(np.uint8(np.clip(pixels,0,1)*255)).save(path)
    return path


def add_photos(window,folder,count,**options):
    ids=[window.catalog.add(gradient(Path(folder)/f'photo-{n}.png',**options)) for n in range(count)]
    window.refresh_lists();wait(lambda:not window.library_loading)
    return ids


def post(port,token,body,*,path='/mcp',headers=None,timeout=70):
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=timeout)
    try:
        sent={'Content-Type':'application/json'}
        if token is not None:sent['Authorization']=f'Bearer {token}'
        sent.update(headers or {})
        connection.request('POST',path,json.dumps(body),sent)
        response=connection.getresponse();data=response.read()
        return response.status,json.loads(data) if data else None
    finally:connection.close()


class Pending:
    """A request running in a worker thread; result() spins the GUI event loop until it is answered."""
    def __init__(self,port,token,body,**options):
        self.value=None;self.error=None;self.started=time.monotonic();self.seconds=None
        def run():
            try:self.value=post(port,token,body,**options)
            except BaseException as error:self.error=error
            self.seconds=time.monotonic()-self.started
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()

    def done(self):return not self.thread.is_alive()

    def result(self,timeout=60):
        wait(self.done,int(timeout*1000))
        if self.error is not None:raise self.error
        return self.value


def rpc(session,method,params=None,ident=1,**options):
    body={'jsonrpc':'2.0','id':ident,'method':method}
    if params is not None:body['params']=params
    return Pending(session.mcp.bound_port,session.mcp.token,body,**options).result(options.get('timeout',60))


def start_call(session,name,arguments=None,**options):
    body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments or {}}}
    return Pending(session.mcp.bound_port,session.mcp.token,body,**options)


class Result:
    def __init__(self,reply):
        status,body=reply;assert status==200,reply
        self.raw=body['result'];self.error=self.raw['isError']
        self.images=[c for c in self.raw['content'] if c['type']=='image']
        self.text=[c for c in self.raw['content'] if c['type']=='text'][-1]['text']
        self.value=None if self.error else json.loads(self.text)

    def __repr__(self):return f'Result(error={self.error}, text={self.text[:300]!r})'


def call(session,name,arguments=None,timeout=60):
    return Result(start_call(session,name,arguments).result(timeout))


def finish(pending,timeout=60):return Result(pending.result(timeout))
