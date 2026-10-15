import gc
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
import pytest
from test_studio_ui import app, wait
from luma import __version__, feedback, features
from luma.preferences import Preferences


@pytest.fixture
def form(monkeypatch):
    monkeypatch.setattr(feedback, 'FORM_ID', 'FORM')
    monkeypatch.setattr(feedback, 'FIELDS', {'version': '1', 'message': '2', 'system': '3'})


class Response:
    def __init__(self, status=200):self.status = status
    def __enter__(self):return self
    def __exit__(self, *a):return False


def test_send_posts_only_the_typed_text_version_and_system(form):
    seen = []
    def opener(request, timeout):
        seen.append((request.full_url, urllib.parse.parse_qs(request.data.decode()), timeout)); return Response()
    assert feedback.send('  창이 멈춤  ', 'Windows 11', opener=opener) == ''
    url, fields, timeout = seen[0]
    assert url == 'https://docs.google.com/forms/d/e/FORM/formResponse' and timeout == feedback.TIMEOUT
    from luma import __version__
    assert fields == {'entry.1': [__version__], 'entry.2': ['창이 멈춤'], 'entry.3': ['Windows 11']}
    assert len(feedback.payload('x'*9000, system='s')['entry.2']) == feedback.LIMIT
    info = feedback.system_info().lower()
    assert info.startswith('macos 1' if sys.platform == 'darwin' else 'windows')
    assert os.environ.get('USERNAME', '?').lower() not in info and os.environ.get('COMPUTERNAME', '?').lower() not in info


def test_send_reports_failures_without_raising(form):
    def offline(request, timeout):raise urllib.error.URLError('no route')
    def refused(request, timeout):raise urllib.error.HTTPError(request.full_url, 404, 'Not Found', None, None)
    assert 'no route' in feedback.send('x', opener=offline)
    assert feedback.send('x', opener=refused) == 'HTTP 404'
    assert feedback.send('x', opener=lambda request, timeout: Response(302)) == 'HTTP 302'


def test_shipped_form_is_configured_and_buttons_follow_the_switches(app, tmp_path, monkeypatch):
    from luma.app import MainWindow
    assert feedback.configured() and set(feedback.FIELDS) == {'version', 'message', 'system'}
    monkeypatch.setattr(features, 'CODEX', False)
    w = MainWindow(tmp_path/'data')
    try:
        w.show()
        assert not w.feedback_button.isHidden() and w.command_button.isHidden()      # the public build
        app.processEvents()      # beside the name, apart from the buttons that switch screens
        assert w.feedback_button.objectName() == 'feedback' and w.feedback_button.x() < w.width()/2 < w.library_button.x()
        w.update_button.setText('9.9.9'); w.update_button.show(); app.processEvents()      # the update notice follows it, a little apart
        assert 8 <= w.update_button.x()-w.feedback_button.geometry().right() and w.update_button.geometry().right() < w.width()/2
        w.update_button.hide()
        w.show_commands(); assert getattr(w, 'command_dialog', None) is None and w.auth_dialog is None
    finally:
        w.close()
    monkeypatch.setattr(feedback, 'FORM_ID', '')
    assert feedback.send('x') == 'not configured'
    w = MainWindow(tmp_path/'data2')
    try:
        w.show(); assert w.feedback_button.isHidden()
    finally:
        w.close()
    monkeypatch.setattr(features, 'CODEX', None)                 # the shipped default reads the settings file
    from luma.preferences import Preferences
    monkeypatch.setattr(Preferences, '__init__', lambda self, path=None: setattr(self, 'values', {}))
    assert features.codex() is False
    monkeypatch.setattr(Preferences, '__init__', lambda self, path=None: setattr(self, 'values', {'codex': True}))
    assert features.codex() is True


def test_dialog_sends_in_the_background_and_shows_the_result(app, tmp_path, form):
    from luma.app import MainWindow
    w = MainWindow(tmp_path/'data')
    try:
        w.show()
        calls = []; answer = ['offline']
        dialog = feedback.FeedbackDialog(w, sender=lambda *a: calls.append(a) or answer[0])
        dialog.submit(); assert not calls and dialog.status.text()            # empty message: nothing sent
        dialog.message.setPlainText('슬라이더가 느려요')
        dialog.submit(); wait(lambda: dialog.thread is None and dialog.send_button.isEnabled())
        assert calls == [('슬라이더가 느려요', feedback.tagged(dialog.system, dialog.reference))] and 'offline' in dialog.status.text()
        assert dialog.message.toPlainText()                                    # kept for another try
        answer[0] = ''; dialog.submit(); wait(lambda: dialog.thread is None and dialog.send_button.isEnabled())
        assert len(calls) == 2 and not dialog.message.toPlainText()
        ref = calls[1][1].rsplit(' · #', 1)[1]
        assert [entry['ref'] for entry in feedback.records().values[feedback.SENT]] == [ref]     # tests/conftest.py: not the real file
        dialog.dialog.deleteLater()
    finally:
        w.close()


def finish(dialog, text=None):
    if text is not None:dialog.message.setPlainText(text)
    dialog.submit(); wait(lambda: dialog.thread is None and dialog.send_button.isEnabled())


def test_reference_is_five_unambiguous_characters_drawn_with_secrets(monkeypatch):
    assert sorted(feedback.ALPHABET) == sorted('ABCDEFGHJKMNPQRSTUVWXYZ23456789') and not set('01OIL') & set(feedback.ALPHABET)
    refs = {feedback.reference() for _ in range(300)}
    assert len(refs) > 290 and all(len(ref) == 5 and set(ref) <= set(feedback.ALPHABET) for ref in refs)
    drawn = []
    monkeypatch.setattr(feedback.secrets, 'choice', lambda letters: drawn.append(letters) or 'Q')
    assert feedback.reference() == 'QQQQQ' and drawn == [feedback.ALPHABET]*5


def test_the_reference_travels_only_at_the_end_of_the_system_answer(app, tmp_path, form, monkeypatch):
    monkeypatch.setattr(feedback, 'reference', lambda: 'K7Q2M')
    seen = []
    def opener(request, timeout):seen.append(request); return Response()
    dialog = feedback.FeedbackDialog(None, sender=lambda message, system: feedback.send(message, system, opener=opener),
                                     preferences=Preferences(tmp_path/'settings.json'))
    assert dialog.note.text() == f'함께 보내는 정보: Grainy {__version__} · {dialog.system} · 보낼 때 정하는 무작위 접수 번호'     # told before sending
    finish(dialog, '커브가 끊겨요 #1')
    request, = seen; fields = urllib.parse.parse_qs(request.data.decode())
    assert fields == {'entry.1': [__version__], 'entry.2': ['커브가 끊겨요 #1'], 'entry.3': [dialog.system+' · #K7Q2M']}
    assert 'K7Q2M' not in request.full_url and 'K7Q2M' not in str(request.header_items())
    assert dialog.status.text() == '보냈습니다. 고맙습니다. 접수 번호는 #K7Q2M입니다. 반영되면 업데이트한 뒤 Grainy가 알려 드립니다.'
    assert feedback.payload('x', feedback.tagged('Windows 11', 'K7Q2M')) == {'entry.1': __version__, 'entry.2': 'x', 'entry.3': 'Windows 11 · #K7Q2M'}
    dialog.dialog.deleteLater()


def test_reference_kept_after_a_failure_renewed_after_success_and_recorded_only_then(app, tmp_path, monkeypatch):
    refs = iter(['AAAAA', 'BBBBB', 'CCCCC', 'DDDDD']); monkeypatch.setattr(feedback, 'reference', lambda: next(refs))
    path = tmp_path/'settings.json'; preferences = Preferences(path)
    calls = []; answer = ['offline']
    dialog = feedback.FeedbackDialog(None, sender=lambda *a: calls.append(a) or answer[0], preferences=preferences)
    used = lambda: [system.rsplit(' · #', 1)[1] for _, system in calls]
    finish(dialog, '느려요'); finish(dialog)
    assert used() == ['AAAAA', 'AAAAA'] and feedback.SENT not in Preferences(path).values      # the same text again: the same number
    finish(dialog, '느려요. 큰 사진에서')
    assert used()[-1] == 'BBBBB'                                                                # edited after the failure: a new one
    answer[0] = ''; finish(dialog)
    assert used()[-1] == 'BBBBB' and '#BBBBB' in dialog.status.text()
    finish(dialog, '하나 더')
    assert used()[-1] == 'CCCCC'                                                                # a new message after a success
    entries = Preferences(path).values[feedback.SENT]
    assert entries == [{'ref': ref, 'version': __version__, 'sent': date.today().isoformat()} for ref in ('BBBBB', 'CCCCC')]
    preferences.save(feedback.SENT, [{'ref': f'OLD{n:02d}', 'version': '0.5.94', 'sent': '2026-10-16'} for n in range(60)])
    finish(dialog, '마지막')
    entries = Preferences(path).values[feedback.SENT]
    assert len(entries) == feedback.KEEP and entries[0]['ref'] == 'OLD11' and entries[-1]['ref'] == 'DDDDD'     # at most 50, newest last
    preferences.save(feedback.SENT, 'damaged'); feedback.record(preferences, 'EEEEE', version='1.0', today=date(2026, 10, 16))
    assert Preferences(path).values[feedback.SENT] == [{'ref': 'EEEEE', 'version': '1.0', 'sent': '2026-10-16'}]
    dialog.dialog.deleteLater()


def test_an_unwritable_settings_file_does_not_break_sending(app, tmp_path, monkeypatch):
    import sys
    blocked = tmp_path/'blocked'; blocked.write_text('', encoding='utf-8')         # a file where the settings folder would be
    preferences = Preferences(blocked/'settings.json')
    with pytest.raises(OSError):preferences.save('x', 1)
    raised = []; monkeypatch.setattr(sys, 'excepthook', lambda *error: raised.append(error))      # an error in a Qt slot lands here
    calls = []
    dialog = feedback.FeedbackDialog(None, sender=lambda *a: calls.append(a) or '', preferences=preferences)
    finish(dialog, '첫 번째')
    assert dialog.status.text().startswith('보냈습니다.') and dialog.reference is None and not dialog.message.toPlainText()
    finish(dialog, '두 번째')
    assert len(calls) == 2 and calls[0][1] != calls[1][1] and dialog.status.text().startswith('보냈습니다.')
    assert not raised
    dialog.dialog.deleteLater()


def test_closing_the_window_while_sending_still_records_the_reference(app, monkeypatch):
    """feedback.show keeps nothing once the window closes; the message still goes, so its reference must be kept too."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPlainTextEdit, QPushButton
    monkeypatch.setattr(feedback, 'reference', lambda: 'CL5SE')
    going, sent = threading.Event(), []
    def sender(message, system):going.wait(5); sent.append(system); return ''
    monkeypatch.setattr(feedback, 'send', sender)
    def close_while_sending():
        d = QApplication.activeModalWidget(); d.findChild(QPlainTextEdit).setPlainText('닫아도 보내져요')
        next(b for b in d.findChildren(QPushButton) if b.text() == '보내기').click(); d.reject()
    QTimer.singleShot(0, close_while_sending); feedback.show(None)
    gc.collect(); assert not sent                                                 # closed before the form answered
    going.set(); wait(lambda: feedback.SENT in feedback.records().values, 3000)
    assert sent[0].endswith(' · #CL5SE') and [entry['ref'] for entry in feedback.records().values[feedback.SENT]] == ['CL5SE']
    wait(lambda: not feedback.SENDING, 3000)                                      # then let go


def test_the_network_guard_covers_the_default_opener_and_sender(app, tmp_path, form):
    """tests/conftest.py blocks urllib; the defaults are looked up when used, so a test that forgets a fake cannot post."""
    with pytest.raises(AssertionError, match='network access in a test'):feedback.send('x')
    dialog = feedback.FeedbackDialog(None, preferences=Preferences(tmp_path/'settings.json'))
    finish(dialog, 'x'); assert 'network access in a test' in dialog.status.text() and feedback.SENT not in Preferences(tmp_path/'settings.json').values
    with pytest.raises(AssertionError, match='network access in a test'):urllib.request.build_opener().open('https://example.com')
    dialog.dialog.deleteLater()


def test_a_session_saving_later_keeps_the_references_the_dialog_recorded(tmp_path):
    session = Preferences(tmp_path/'settings.json'); session.save('language', 'ko')      # read when Grainy started
    feedback.record(Preferences(tmp_path/'settings.json'), 'K7Q2M')                      # the dialog's own copy
    session.save('update_checked', 1.0)
    values = Preferences(tmp_path/'settings.json').values
    assert values['language'] == 'ko' and values['update_checked'] == 1.0 and values[feedback.SENT][0]['ref'] == 'K7Q2M'
    assert session.values == values
