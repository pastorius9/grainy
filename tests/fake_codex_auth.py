"""Local protocol peer for auth tests. Never opens a browser or contacts OpenAI."""
import json
import os
import sys
from pathlib import Path

home=Path(os.environ['CODEX_HOME'])
behavior=os.environ.get('LUMA_FAKE_AUTH','success')
marker=home/'fake-signed-in'
logged_in=marker.exists()
login_id='test-login-1'


def send(message):
    raw=(json.dumps(message,ensure_ascii=False)+'\n').encode()
    # Exercise split JSON / UTF-8 reads without treating stderr as protocol data.
    sys.stdout.buffer.write(raw[:7]); sys.stdout.buffer.flush()
    sys.stdout.buffer.write(raw[7:]); sys.stdout.buffer.flush()


for line in sys.stdin.buffer:
    request=json.loads(line)
    method=request.get('method')
    if not method:
        continue
    with (home/'calls.jsonl').open('a',encoding='utf-8') as log:
        log.write(json.dumps({'method':method,'params':request.get('params'),
            'api_key_inherited':any(os.environ.get(key) for key in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN'))})+'\n')
    ident=request.get('id')
    if method=='initialize':
        if behavior=='initialize_error':
            send({'id':ident,'error':{'message':'private-token-must-not-leak'}})
        else:
            send({'id':ident,'result':{'userAgent':'test'}})
    elif method=='account/read':
        if behavior=='read_crash':
            sys.exit(1)
        if behavior=='read_timeout':
            continue
        account={'type':'chatgpt','email':'테스트@example.com','planType':'plus','accessToken':'must-not-retain'} if logged_in else None
        send({'id':ident,'result':{'account':account,'requiresOpenaiAuth':True}})
    elif method=='account/login/start':
        if behavior=='login_error':
            sys.stderr.write('private-token-must-not-leak\n');sys.stderr.flush()
            send({'id':ident,'error':{'message':'private-token-must-not-leak'}})
            continue
        url='https://auth.openai.com/oauth/authorize?state=fake'
        if behavior=='bad_url':
            url='https://auth.openai.com.attacker.invalid/oauth/authorize'
        send({'id':ident,'result':{'type':'chatgpt','loginId':login_id,'authUrl':url}})
        if behavior in ('wait','bad_url'):
            continue
        if behavior=='login_failure':
            send({'method':'account/login/completed','params':{'loginId':login_id,'success':False,'error':'private-token-must-not-leak'}})
        else:
            logged_in=True
            marker.write_text('test',encoding='utf-8')
            send({'method':'account/login/completed','params':{'loginId':login_id,'success':True}})
            send({'method':'account/updated','params':{'authMode':'chatgpt','planType':'plus'}})
    elif method=='account/rateLimits/read':
        send({'id':ident,'result':{'rateLimits':{'primary':{'usedPercent':99}},'rateLimitsByLimitId':{
            'codex':{'limitId':'codex','primary':{'usedPercent':25,'windowDurationMins':300,'resetsAt':1800000000},
                     'secondary':{'usedPercent':60,'windowDurationMins':10080,'resetsAt':1800003600}}}}})
    elif method=='account/login/cancel':
        send({'id':ident,'result':{'status':'canceled'}})
        send({'method':'account/login/completed','params':{'loginId':login_id,'success':False}})
    elif method=='account/logout':
        logged_in=False
        marker.unlink(missing_ok=True)
        send({'id':ident,'result':{}})
        send({'method':'account/updated','params':{'authMode':None}})
    elif method=='model/list':
        send({'id':ident,'result':{'data':[{'model':'test-model','displayName':'Test model'}]}})
    elif method=='thread/start':
        send({'id':ident,'result':{'thread':{'id':'test-thread'}}})
    elif method=='turn/start':
        send({'id':ident,'result':{'turn':{'id':'test-turn','status':'inProgress'}}})
        if behavior=='turn_wait':continue
        if behavior=='turn_error':
            send({'method':'turn/completed','params':{'threadId':'test-thread','turn':{'id':'test-turn','status':'failed','error':{'message':'secret'}}}})
            continue
        result={'message':'노출을 올리는 보정을 제안합니다.','action':'adjust','changes':[{'key':'exposure','value':.5}]}
        if behavior=='folder_bw':
            result={'message':'현재 폴더 전체를 흑백으로 전환하는 보정입니다.','action':'adjust','scope':'folder',
                    'changes':[{'key':'monochrome','value':True,'operation':'set'}]}
        content=json.dumps(result,ensure_ascii=False)
        if behavior=='bad_proposal':content='unstructured text'
        for chunk in (content[:18],content[18:]):
            send({'method':'item/agentMessage/delta','params':{'threadId':'test-thread','turnId':'test-turn','itemId':'msg','delta':chunk}})
        send({'method':'item/completed','params':{'threadId':'test-thread','turnId':'test-turn','item':{'id':'msg','type':'agentMessage','text':content}}})
        send({'method':'turn/completed','params':{'threadId':'test-thread','turn':{'id':'test-turn','status':'completed'}}})
    elif method=='turn/interrupt':
        send({'id':ident,'result':{}})
        send({'method':'turn/completed','params':{'threadId':'test-thread','turn':{'id':'test-turn','status':'interrupted'}}})
