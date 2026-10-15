import pytest


@pytest.fixture(autouse=True)
def hdr_feature(monkeypatch):
    """The HDR editing mode is switched off in the app (engine.HDR_FEATURE); its code stays tested.
    tests/test_hdr_off.py covers the shipped, switched-off state."""
    from luma import engine
    monkeypatch.setattr(engine, 'HDR_FEATURE', True)


@pytest.fixture(autouse=True)
def codex_feature(monkeypatch):
    """The Codex command window is hidden in the public build (features.codex); its code stays tested
    and never reads the developer's own settings file. tests/test_feedback.py covers the hidden state."""
    from luma import features
    monkeypatch.setattr(features, 'CODEX', True)


@pytest.fixture(autouse=True)
def feedback_records(monkeypatch, tmp_path_factory):
    """The feedback dialog keeps sent references in the per-user settings (feedback.records); in tests a file of
    their own, made on first use, so no test can touch the real one."""
    from luma import feedback
    from luma.preferences import Preferences
    path = []
    def records():
        if not path:path.append(tmp_path_factory.mktemp('feedback')/'settings.json')
        return Preferences(path[0])
    monkeypatch.setattr(feedback, 'records', records)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test reaches the network: feedback.send and the dialog look urllib.request.urlopen up when they send, and
    OpenerDirector.open also stops the urlopen that updater captured as a default argument."""
    import urllib.request
    def blocked(*a, **k):raise AssertionError('network access in a test')
    monkeypatch.setattr(urllib.request, 'urlopen', blocked); monkeypatch.setattr(urllib.request.OpenerDirector, 'open', blocked)
