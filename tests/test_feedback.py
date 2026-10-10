import os
import sys
import urllib.error
import urllib.parse
import pytest
from test_studio_ui import app, wait
from luma import feedback, features


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the real form."""
    def blocked(*a, **k):raise AssertionError('network access in a test')
    monkeypatch.setattr(feedback.urllib.request, 'urlopen', blocked)


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
        assert calls == [('슬라이더가 느려요', dialog.system)] and 'offline' in dialog.status.text()
        assert dialog.message.toPlainText()                                    # kept for another try
        answer[0] = ''; dialog.submit(); wait(lambda: dialog.thread is None and dialog.send_button.isEnabled())
        assert len(calls) == 2 and not dialog.message.toPlainText()
        dialog.dialog.deleteLater()
    finally:
        w.close()
