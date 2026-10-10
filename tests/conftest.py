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
