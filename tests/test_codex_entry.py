from PySide6.QtWidgets import QApplication,QPushButton
from test_studio_ui import app,window,wait


def test_one_codex_entry_routes_login_then_opens_commands_and_cancellation_stays_closed(window,monkeypatch):
    w=window
    monkeypatch.setattr(w.auth,'refresh',lambda:None)
    monkeypatch.setattr('luma.command_dialog.CommandSession.models',lambda self:None)
    assert not hasattr(w,'auth_button')
    assert w.command_button.text()=='Codex'
    w.command_button.click()
    assert w.auth_dialog.isVisible() and w.command_dialog is None and w.codex_login_pending
    w.auth_dialog.reject();assert not w.codex_login_pending
    w.auth.account={'type':'chatgpt','email':'test@example.invalid'};w.auth.changed.emit()
    QApplication.processEvents();assert w.command_dialog is None
    w.command_button.click();assert w.command_dialog.isVisible() and not w.auth_dialog.isVisible()
    w.command_dialog.hide();w.auth.account=None
    w.command_button.click();assert w.auth_dialog.isVisible()
    w.auth.account={'type':'chatgpt','email':'test@example.invalid'};w.auth.changed.emit()
    wait(lambda:w.command_dialog.isVisible())
    assert not w.auth_dialog.isVisible() and not w.codex_login_pending
