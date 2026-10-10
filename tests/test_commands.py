import json
import os
import sys
from pathlib import Path
from copy import deepcopy
import numpy as np
import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from luma.auth import CodexAuth
from luma.commands import CommandSession,validate_proposal,streamed_message
from luma.command_dialog import CommandDialog
from luma.engine import defaults
from luma.app import MainWindow


@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])


def wait(predicate):
    loop=QEventLoop();poll=QTimer();poll.setInterval(5)
    poll.timeout.connect(lambda:loop.quit() if predicate() else None)
    deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(loop.quit)
    poll.start();deadline.start(5000)
    if not predicate():loop.exec()
    poll.stop();deadline.stop();assert predicate()


@pytest.fixture
def client(app,tmp_path):
    home=tmp_path/'auth';home.mkdir();(home/'fake-signed-in').write_text('test')
    c=CodexAuth(home,command=[sys.executable,str(Path(__file__).with_name('fake_codex_auth.py'))])
    yield c
    c.shutdown()


def start(c):
    c.start();wait(lambda:c.connected)


def test_streaming_and_same_conversation(client):
    start(client);s=CommandSession(client);answers=[];parts=[]
    s.completed.connect(answers.append);s.messageChanged.connect(parts.append)
    assert s.send('노출을 올려줘',defaults())
    wait(lambda:not s.running)
    assert answers[0]['changes']=={'exposure':.5}
    assert parts[-1]=='노출을 올리는 보정을 제안합니다.'
    assert s.send('다시 설명해줘');wait(lambda:not s.running)
    calls=[json.loads(x) for x in (client.home/'calls.jsonl').read_text().splitlines()]
    assert sum(x['method']=='thread/start' for x in calls)==1
    assert len(answers)==2
    assert all(x['type']=='text' for c in calls if c['method']=='turn/start' for x in c['params']['input'])


def test_stop_and_new_chat(client,monkeypatch):
    monkeypatch.setenv('LUMA_FAKE_AUTH','turn_wait');start(client);s=CommandSession(client)
    s.send('test');wait(lambda:s.turn_id is not None)
    assert not s.send('double submission')
    s.cancel();wait(lambda:not s.running)
    assert s.status=='중단했습니다.'
    s.new_chat();assert s.thread_id is None


@pytest.mark.parametrize('behavior',['turn_error','bad_proposal'])
def test_failures_are_actionable_and_do_not_apply(client,monkeypatch,behavior):
    monkeypatch.setenv('LUMA_FAKE_AUTH',behavior);start(client)
    s=CommandSession(client);failures=[];answers=[]
    s.failed.connect(failures.append);s.completed.connect(answers.append)
    s.send('test');wait(lambda:not s.running)
    assert failures and not answers and 'secret' not in failures[0]


@pytest.mark.parametrize('changes',[[{'key':'path','value':1}],[{'key':'exposure','value':50}],
    [{'key':'exposure','value':float('nan')}],[{'key':'exposure','value':True}],
    [{'key':'exposure','value':.5},{'key':'exposure','value':1}]])
def test_untrusted_proposals_fail_closed(changes):
    with pytest.raises(ValueError):validate_proposal({'message':'test','action':'adjust','changes':changes})


def test_partial_unicode_and_escaped_streams():
    assert streamed_message('{"message":"반가워요')=='반가워요'
    assert streamed_message('{"message":"line\\n\\uD55C"}')=='line\n한'
    assert streamed_message('{"message":"a\\')=='a'


def test_real_command_controls(client):
    start(client);applied=[]
    dialog=CommandDialog(client,context=lambda:{'photo_id':1,'settings':defaults()},apply=lambda *a:applied.append(a))
    dialog.show();dialog.input.setPlainText('노출 올려줘')
    QTest.mouseClick(dialog.send_button,Qt.MouseButton.LeftButton)
    wait(lambda:dialog.apply_button.isVisible())
    assert '노출' in dialog.proposal_label.text()
    QTest.mouseClick(dialog.apply_button,Qt.MouseButton.LeftButton)
    assert len(applied)==1 and not dialog.apply_button.isEnabled()
    dialog.close()


def test_apply_photo_guard_undo_and_catalog(app,tmp_path):
    from PIL import Image
    p=tmp_path/'sample.png';Image.new('RGB',(100,80),'gray').save(p)
    w=MainWindow(tmp_path/'data');ident=w.catalog.add(p);w.refresh_lists();w.activate(ident)
    wait(lambda:w.source is not None)
    snapshot=w.command_context();proposal={'message':'test','action':'adjust','changes':{'exposure':.5}}
    w.apply_command(snapshot,proposal)
    assert w.settings['exposure']==.5 and w.catalog.photo(ident)['settings']['exposure']==.5
    with pytest.raises(ValueError):w.apply_command(snapshot,proposal)
    w.undo();assert w.settings['exposure']==0
    changed=deepcopy(snapshot);changed['photo_id']=ident+1
    with pytest.raises(ValueError):w.apply_command(changed,proposal)
    w.close()
