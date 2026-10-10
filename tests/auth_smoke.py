"""Validate installed Codex account RPCs; cancel OAuth without signing anyone in."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEventLoop,QTimer,QProcess
from luma.auth import CodexAuth,find_codex,valid_login_url


def wait_for(predicate,timeout=35000):
    loop=QEventLoop()
    poll=QTimer();poll.setInterval(20)
    poll.timeout.connect(lambda:loop.quit() if predicate() else None)
    poll.start()
    deadline=QTimer();deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit);deadline.start(timeout)
    if not predicate():
        loop.exec()
    poll.stop();deadline.stop()
    assert predicate(),'Live account RPC timed out'


app=QCoreApplication([])
executable=find_codex()
assert executable,'Codex executable missing'
with tempfile.TemporaryDirectory(prefix='luma-auth-smoke-') as directory:
    client=CodexAuth(Path(directory)/'codex',executable=executable)
    urls=[]
    client.browserRequested.connect(urls.append)
    try:
        client.start()
        wait_for(lambda:client.phase in {'signed_out','error'})
        assert client.phase=='signed_out',client.message
        client.login()
        wait_for(lambda:client.phase in {'waiting','error'})
        assert client.phase=='waiting',client.message
        assert len(urls)==1 and valid_login_url(urls[0])
        assert client.login_id
        client.cancel_login()
        wait_for(lambda:client.phase in {'signed_out','error'})
        assert client.phase=='signed_out',client.message
        assert not client.connected and not client.login_url
        client.shutdown()
        client.start()
        wait_for(lambda:client.phase in {'signed_out','error'})
        assert client.phase=='signed_out',client.message
    finally:
        client.shutdown()
    assert client.process.state()==QProcess.ProcessState.NotRunning
    # Windows may release native process working-directory handles just after
    # exit notification. Give that cleanup an event-loop turn before rmtree.
    cleanup_loop=QEventLoop()
    QTimer.singleShot(1500,cleanup_loop.quit)
    cleanup_loop.exec()
report={'installed_codex_account_read':True,'official_oauth_url_received':True,
        'login_cancelled':True,'restart_signed_out':True,'owned_process_stopped':True,
        'user_sign_in_performed':False,'photos_transmitted':False}
out=Path(__file__).resolve().parents[1]/'validation'/'auth-report.json'
out.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report))
