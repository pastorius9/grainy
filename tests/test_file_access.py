"""Originals the system will not let Grainy read (macOS resets Files and Folders permission when an ad hoc
signed update arrives) are told apart from missing ones. PermissionError is simulated for chosen folders,
the way macOS answers even a stat; Path.is_file() is False for them, as it is there."""
import errno
import json
import os
import threading
from threading import Event
import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox
from luma import engine, file_access, preview_store as store
from luma.catalog import Catalog
from luma.file_access import access, unreadable_folders, PRESENT, MISSING, DENIED
from luma.folder_locations import scan_folders
from luma.folders import path_key
from luma.i18n import language, set_language, tr
from luma.library import matches
from test_studio_ui import app, window, wait


def deny(monkeypatch, *folders):
    """macOS without the permission: stat and listing fail with EPERM at and below these paths."""
    keys = [os.path.normcase(os.path.abspath(str(f))) for f in folders]
    def blocked(path):
        if not isinstance(path, (str, bytes, os.PathLike)):return False
        key = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        return any(key == k or key.startswith(k.rstrip(os.sep)+os.sep) for k in keys)
    real_stat, real_scandir = os.stat, os.scandir
    real = {name: getattr(os.path, name) for name in ('isfile', 'isdir', 'exists')}
    def stat(path, *a, **k):
        if blocked(path):raise PermissionError(errno.EPERM, 'Operation not permitted', os.fsdecode(path))
        return real_stat(path, *a, **k)
    def scandir(path='.'):
        if blocked(path):raise PermissionError(errno.EPERM, 'Operation not permitted', os.fsdecode(path))
        return real_scandir(path)
    monkeypatch.setattr(os, 'stat', stat); monkeypatch.setattr(os, 'scandir', scandir)
    for name, function in real.items():   # on Windows these are native calls that skip os.stat
        monkeypatch.setattr(os.path, name, lambda path, function=function:False if blocked(path) else function(path))


def picture(path, color=(110, 140, 160)):
    path.parent.mkdir(parents=True, exist_ok=True); Image.new('RGB', (90, 60), color).save(path); return path


@pytest.fixture
def guidance(monkeypatch):
    """The message boxes shown, without blocking; a fresh session for the once-only message."""
    shown = []
    monkeypatch.setattr(file_access, 'shown', False); monkeypatch.setattr(file_access, 'pending', False)
    monkeypatch.setattr(QMessageBox, 'exec', lambda box: shown.append((box.text(), box.informativeText(), [b.text() for b in box.buttons()])))
    return shown


def test_access_tells_denied_apart_from_missing(tmp_path, monkeypatch):
    photo = picture(tmp_path/'open'/'a.png'); locked = picture(tmp_path/'locked'/'b.png')
    assert access(photo) == PRESENT and access(tmp_path/'open'/'gone.png') == MISSING
    assert access(tmp_path/'open') == MISSING and access(tmp_path/'open', folder=True) == PRESENT
    assert access(photo/'below-a-file.png') == MISSING
    deny(monkeypatch, tmp_path/'locked')
    assert not os.path.isfile(locked) and not os.path.exists(locked)    # what made the photos look offline
    with pytest.raises(PermissionError):locked.is_file()              # Python 3.13 lets EPERM through
    assert access(locked) == DENIED and access(tmp_path/'locked', folder=True) == DENIED and file_access.denied(locked)
    assert not file_access.denied(tmp_path/'open'/'gone.png') and access(photo) == PRESENT
    real = os.stat
    def slow(path, *a, **k):
        if str(path).endswith('network.png'):raise TimeoutError(errno.ETIMEDOUT, 'timed out')
        return real(path, *a, **k)
    monkeypatch.setattr(os, 'stat', slow)
    assert access(tmp_path/'network.png') == MISSING                 # other errors: as before
    assert unreadable_folders([str(tmp_path/'open'), str(tmp_path/'locked'), str(tmp_path/'nowhere')]) == [str(tmp_path/'locked')]


@pytest.fixture
def cached(tmp_path):
    y, x = np.mgrid[:80, :120]
    rgb = np.stack([.12+x/300, .15+y/220, np.full_like(x, .35, dtype=float)], -1)
    path = tmp_path/'card'/'source.png'; path.parent.mkdir(); Image.fromarray(np.uint8(rgb*255)).save(path)
    cat = Catalog(tmp_path/'library'); ident = cat.add(path)
    assert store.rebuild(cat.directory, ident) == '갱신 완료'
    yield cat, ident, path
    cat.close()


def test_preview_status_says_access_needed_not_offline(cached, monkeypatch):
    cat, ident, path = cached
    pixels, info = engine.load_image(path, 1800)
    previews = cat.directory/'previews'; previews.mkdir()
    np.savez_compressed(previews/f'{ident}.npz', pixels=pixels, info=json.dumps(info))
    with monkeypatch.context() as patch:
        deny(patch, path.parent)
        result = store.inspect(cat.directory, ident)
        assert result['snapshot']['denied'] and result['image'] is not None
        assert result['status'] == '접근 권한 필요'                   # instead of '오프라인'
        (previews/f'{ident}.npz').unlink(); cat.edit(ident, {'exposure': 1})
        result = store.inspect(cat.directory, ident)
        assert not result['rebuild'] and result['status'] == '접근 권한 필요 · 이전 미리보기'
        assert store.rebuild(cat.directory, ident, force=True) == '접근 권한 필요'
    result = store.inspect(cat.directory, ident)                      # access allowed again
    assert not result['snapshot']['denied'] and result['rebuild'] and result['status'] == '갱신 중'
    assert store.rebuild(cat.directory, ident) == '갱신 완료' and store.inspect(cat.directory, ident)['status'] == ''
    path.rename(path.with_suffix('.unplugged'))                       # a missing original: exactly as before
    result = store.inspect(cat.directory, ident)
    assert not result['snapshot']['denied'] and result['status'] == '오프라인'
    cat.edit(ident, {'exposure': .5}); assert store.inspect(cat.directory, ident)['status'] == '원본 없음 · 이전 미리보기'
    assert store.rebuild(cat.directory, ident, force=True) == '원본 없음'
    previous = language()
    try:
        set_language('en'); assert tr('접근 권한 필요') == 'Access needed' and tr('오프라인') == 'Offline'
    finally:set_language(previous)


def test_missing_filter_and_folder_scan_do_not_count_denied(tmp_path, monkeypatch):
    cat = Catalog(tmp_path/'library')
    try:
        locked = cat.add(picture(tmp_path/'locked'/'a.png')); gone = picture(tmp_path/'card'/'b.png'); moved = cat.add(gone)
        here = cat.add(picture(tmp_path/'here'/'c.png'))
        gone.rename(gone.with_name('elsewhere.png'))
        deny(monkeypatch, tmp_path/'locked')
        assert [i for i in (locked, moved, here) if matches(cat.photo(i), {'missing': True})] == [moved]
        assert [i for i in (locked, moved, here) if matches(cat.photo(i), {'missing': False})] == [locked, here]
        (tmp_path/'card'/'elsewhere.png').unlink(); (tmp_path/'card').rmdir()
        folders = [tmp_path/'locked', tmp_path/'card', tmp_path/'here']
        result = scan_folders([str(f) for f in folders], [], set(), {}, Event())
        assert path_key(tmp_path/'card') in result['missing']           # gone: offered for relinking as before
        assert path_key(tmp_path/'locked') not in result['missing'] and path_key(tmp_path/'here') not in result['missing']
    finally:cat.close()


def test_opening_a_denied_photo_explains_once_and_does_not_look_for_a_moved_folder(window, guidance, monkeypatch):
    w = window; first = w.current_id
    locked = [w.catalog.add(picture(w.catalog.directory.parent/'locked'/f'{n}.png', (40*n, 90, 120))) for n in (1, 2)]
    gone = picture(w.catalog.directory.parent/'card'/'gone.png'); missing = w.catalog.add(gone)
    w.refresh_lists(); wait(lambda: not w.library_loading)
    searches = []; monkeypatch.setattr(w.folder_panel, 'recover', lambda explicit=False: searches.append(explicit))
    monkeypatch.setattr(file_access, 'MAC', False)                    # the Windows wording on every system
    deny(monkeypatch, w.catalog.directory.parent/'locked')
    w.activate(locked[0]); wait(lambda: not w.jobs and w.info_label.text() == tr('사진을 읽을 수 없습니다.'))
    assert len(guidance) == 1 and not searches
    text, details, buttons = guidance[0]
    assert text == tr('Grainy가 사진 폴더를 읽을 권한이 없습니다.') and str(w.catalog.directory.parent/'locked') in details
    assert tr('시스템 설정 열기') not in buttons                        # Windows: no settings button
    assert w.statusBar().currentMessage() == tr('원본 폴더에 접근할 권한이 없습니다. 접근을 허용하면 사진이 그대로 돌아옵니다.')
    w.activate(first); wait(lambda: w.source is not None and not w.jobs and not w.render_running)
    w.activate(locked[1]); wait(lambda: not w.jobs and w.info_label.text() == tr('사진을 읽을 수 없습니다.'))
    assert len(guidance) == 1 and not searches                        # once per session
    gone.unlink(); w.activate(missing); wait(lambda: not w.jobs and searches)
    assert len(guidance) == 1 and searches == [False]                 # a missing original: as before
    assert w.statusBar().currentMessage() == tr('원본 위치를 찾고 있습니다. 폴더 우클릭에서 자동 찾기나 위치 지정을 사용할 수 있습니다.')
    reports = []; monkeypatch.setattr(QMessageBox, 'information', lambda *a: reports.append(a[2]))
    w.manager.integrity(); wait(lambda: reports)
    assert reports == [tr('데이터베이스: {0}\n사진 {1}장 / 누락된 원본 참조 {2}개', 'ok', '4', '1')
        +tr('\n접근 권한이 필요한 원본 {0}개 (누락 아님)', '2')]          # denied ones are not missing


def test_denied_photo_with_offline_preview_opens_and_mac_button_opens_settings(window, guidance, monkeypatch):
    w = window; first = w.current_id
    path = picture(w.catalog.directory.parent/'locked'/'a.png'); ident = w.catalog.add(path)
    w.activate(ident); wait(lambda: w.source is not None and not w.jobs and not w.render_running)
    previews = w.catalog.directory/'previews'; previews.mkdir(exist_ok=True)
    np.savez_compressed(previews/f'{ident}.npz', pixels=w.source, info=json.dumps(w.catalog.photo(ident)['info']))
    w.activate(first); wait(lambda: w.source is not None and not w.jobs and not w.render_running)
    opened = []; monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    monkeypatch.setattr(file_access, 'MAC', True)
    def click(box):
        guidance.append(box.text())
        next(b for b in box.buttons() if b.text() == tr('시스템 설정 열기')).click()
    monkeypatch.setattr(QMessageBox, 'exec', click)
    deny(monkeypatch, path.parent)
    w.activate(ident); wait(lambda: w.source is not None and not w.jobs and not w.render_running and guidance)
    assert w.catalog.photo(ident)['info']['offline_preview'] and w.tabs.isEnabled()
    assert guidance == [tr('macOS가 Grainy의 사진 폴더 접근을 막고 있습니다.')]
    assert opened == [file_access.SETTINGS_URL]


def test_guidance_waits_for_an_open_dialog(window, guidance, monkeypatch):
    from PySide6.QtWidgets import QApplication
    modal = [object()]; monkeypatch.setattr(QApplication, 'activeModalWidget', staticmethod(lambda: modal[0]))
    assert not file_access.guide(window, 'D:\\사진') and not file_access.guide(window, 'D:\\사진')
    QTest.qWait(1200); assert not guidance                           # still covered by the other dialog
    modal[0] = None; wait(lambda: guidance)
    QTest.qWait(1200); assert len(guidance) == 1 and 'D:\\사진' in guidance[0][1]


def test_startup_check_lists_folder_roots_and_explains_once(window, guidance, monkeypatch):
    w = window
    fine = picture(w.catalog.directory.parent/'fine'/'a.png').parent; locked = picture(w.catalog.directory.parent/'locked'/'b.png').parent
    w.catalog.register_folder(fine); roots = w.catalog.folder_roots()
    listed = []; real = os.scandir
    def scandir(path='.'):
        if threading.current_thread().name == 'GrainyFolderAccess':listed.append(str(path))
        return real(path)
    monkeypatch.setattr(os, 'scandir', scandir)
    thread = file_access.check_folders(w, interval=10); wait(lambda: not thread.is_alive()); QTest.qWait(100)
    assert not guidance and listed == roots                           # roots only, never the photos in them
    w.catalog.register_folder(locked); stored = next(r for r in w.catalog.folder_roots() if r not in roots)
    deny(monkeypatch, locked, stored)
    thread = file_access.check_folders(w, interval=10); wait(lambda: guidance)
    assert len(guidance) == 1 and stored in guidance[0][1]
    assert file_access.check_folders(w) is None                       # once per session
    assert not file_access.guide(w, str(locked)) and len(guidance) == 1
    imports = []; monkeypatch.setattr(w, 'import_paths', lambda paths, **k: imports.append(paths))
    w.sync_folder(stored)                                             # right-click › folder sync
    assert not imports and len(guidance) == 1
    assert w.statusBar().currentMessage() == tr('원본 폴더에 접근할 권한이 없습니다. 접근을 허용하면 사진이 그대로 돌아옵니다.')
