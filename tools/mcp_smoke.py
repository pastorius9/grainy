"""HTTP smoke flow for tools/try_mcp.py --check. Never prints the connection key."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time

from PIL import Image
from PySide6.QtCore import QObject,QTimer,Signal


class Smoke(QObject):
    finished=Signal(object)

    def __init__(self,session,root,report):
        super().__init__();self.session=session;self.root=root;self.report=report;self.result=None
        self.finished.connect(self.close)
        self.timer=QTimer(self);self.timer.setInterval(100);self.timer.timeout.connect(self.start);self.timer.start()

    def start(self):
        w=self.session.window
        if w.import_busy or w.import_scans or w.library_loading:return
        self.timer.stop()
        # Capture only what the HTTP client needs. Catalog access below is exclusively through MCP.
        port=self.session.mcp.bound_port;token=self.session.mcp.token
        threading.Thread(target=self.run,args=(port,token),daemon=True).start()

    def close(self,result):
        self.result=result
        self.report.parent.mkdir(parents=True,exist_ok=True)
        self.report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'status':result['status'],'report':str(self.report)},ensure_ascii=False),flush=True)
        self.session.mcp.stop();self.session.window.close();self.session.app.exit(0 if result['status']=='passed' else 1)

    def run(self,port,token):
        results={};serial=0
        def rpc(method,params=None):
            nonlocal serial
            serial+=1;connection=http.client.HTTPConnection('127.0.0.1',port,timeout=60)
            try:
                connection.request('POST','/mcp',json.dumps({'jsonrpc':'2.0','id':serial,'method':method,'params':params or {}}),
                    {'Content-Type':'application/json','Authorization':'Bearer '+token})
                response=connection.getresponse();body=json.loads(response.read())
                assert response.status==200 and 'error' not in body,(response.status,body)
                return body['result']
            finally:connection.close()
        def call(name,args):
            response=rpc('tools/call',{'name':name,'arguments':args})
            assert not response['isError'],response['content'][-1]['text']
            value=json.loads(response['content'][-1]['text'])
            end=time.monotonic()+60
            while value.get('state')=='running':
                assert time.monotonic()<end,'job did not finish'
                value=call('get_job',{'job':value['job'],'wait':1})[0]
            assert value.get('state') not in ('failed','cancelled'),value
            return value,response
        try:
            rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'grainy-smoke','version':'1'}})
            listed=rpc('tools/list');results['tools_list_chars']=len(json.dumps(listed,separators=(',',':')))
            files=sorted((self.root/'photos').glob('*.png'))
            original={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
            listed,_=call('list_photos',{});ids=[p['id'] for p in listed['photos']];assert len(ids)==6
            before,_=call('get_photo',{'ids':ids})
            results['crop_auto'],_=call('apply_edits',{'edits':[{'ids':[ids[0]],'auto_straighten':True,
                                                             'auto_crop':True,'crop':[.08,.08,.92,.92]}]})
            assert results['crop_auto']['changed']==1
            details,_=call('get_photo',{'ids':[ids[0]]});photo=details['photos'][0]
            assert photo['crop']==[.08,.08,.92,.92] and photo['auto_level'] is not None
            results['geometry']=photo
            films,_=call('list_films',{});variants=[{'film_look':p['name']} for p in films['films'][:6]]
            results['sheet'],sheet=call('contact_sheet',{'ids':[ids[0]],'variants':variants,'save':True,
                'path':str(self.root/'review'),'size':2400})
            path=Path(results['sheet']['saved']['path'])
            with Image.open(path) as image:assert max(image.size)<=2400 and image.width>1000
            inline=self.root/'inline-sheet.jpg';inline.write_bytes(base64.b64decode(sheet['content'][0]['data']))
            results['inline_sheet']=str(inline)
            assert call('list_photos',{})[0]['total']==6
            entries=[{'ids':[ident],'rating':n%6,'flag':'picked' if n%2 else 'rejected','label':'green'} for n,ident in enumerate(ids)]
            results['labels'],_=call('set_labels',{'entries':entries})
            changed,_=call('get_photo',{'ids':ids})
            assert [p.get('r',0) for p in changed['photos']]==list(range(6))
            results['undo'],_=call('undo_last_batch',{});assert results['undo']['tool']=='set_labels'
            restored,_=call('get_photo',{'ids':ids});assert not any(p.get('r') or p.get('f') or p.get('l') for p in restored['photos'])
            assert restored['photos'][0]['crop']==[.08,.08,.92,.92]
            export={'ids':ids,'path':str(self.root/'exports'),'longest':200,'format':'JPEG'}
            results['dry_run'],_=call('export_photos',{**export,'dry_run':True})
            assert results['dry_run']['planned']==6 and not (self.root/'exports').exists()
            results['export'],_=call('export_photos',export)
            assert results['export']['exported']==6
            for planned,actual in zip(results['dry_run']['files'],results['export']['files']):
                assert (planned['name'],planned['w'],planned['h'])==(actual['name'],actual['w'],actual['h'])
                with Image.open(actual['path']) as image:assert image.size==(actual['w'],actual['h'])
                assert Path(actual['path']).stat().st_size==actual['bytes']>0
            scans=self.root/'scans';(scans/'nested').mkdir(parents=True)
            Image.new('RGB',(64,48),(140,150,160)).save(scans/'nested'/'good.png')
            (scans/'nested'/'unreadable.png').write_bytes(b'not an image')
            results['import'],_=call('import_folder',{'path':str(scans),'recursive':True})
            assert results['import']['imported']==1 and results['import']['not_imported']==1
            assert results['import']['failures']==['unreadable.png']
            assert call('set_labels',{'ids':results['import']['ids'],'rating':3})[0]['changed']==1
            assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in original.items())
            results.update(status='passed',originals_unchanged=6,temp_root=str(self.root))
        except Exception as error:
            # No headers, environment or credential variables are ever included in this report.
            results.update(status='failed',error=str(error),temp_root=str(self.root))
        self.finished.emit(results)
