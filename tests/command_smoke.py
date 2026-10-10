"""Live, text-only conversation using the Luma login, without reading credentials."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEventLoop,QTimer
from luma.auth import CodexAuth
from luma.commands import CommandSession

def wait(predicate,timeout=90000):
    loop=QEventLoop();poll=QTimer();poll.setInterval(30)
    poll.timeout.connect(lambda:loop.quit() if predicate() else None)
    deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(loop.quit)
    poll.start();deadline.start(timeout)
    if not predicate():loop.exec()
    poll.stop();deadline.stop();assert predicate(),'Timed out'

app=QCoreApplication([]);client=CodexAuth();answers=[];errors=[];events=[]
try:
    client.start();wait(lambda:client.phase in ('connected','signed_out','error'))
    assert client.connected,'Luma is not signed in'
    session=CommandSession(client)
    session.completed.connect(answers.append);session.failed.connect(errors.append)
    client.notification.connect(lambda method,params:events.append(method))
    session.send('연결 테스트입니다. 사진이나 파일을 읽거나 변경하지 말고 "명령창 연결 완료"라고 한 문장으로만 답해주세요.')
    wait(lambda:not session.running,180000)
    assert not errors,errors
    assert answers and answers[0]['action']=='chat' and not answers[0]['changes']
    report={'signed_in':True,'live_response':answers[0]['message'],'streamed':'item/agentMessage/delta' in events,
            'photos_transmitted':False,'settings_modified':False}
    (Path(__file__).resolve().parents[1]/'validation'/'command-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
finally:client.shutdown()
