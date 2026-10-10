import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
import pytest
from test_studio_ui import app, wait
from luma import updater, __version__


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):raise AssertionError('network access in a test')
    monkeypatch.setattr(updater.urllib.request, 'urlopen', blocked)
    monkeypatch.delenv('GRAINY_UPDATE_FEED', raising=False)


@pytest.fixture(autouse=True)
def windows_layout(monkeypatch):
    """The release-folder installer is the same code on every system; the macOS tests below switch to the bundle."""
    monkeypatch.setattr(updater, 'MAC', False)


@pytest.fixture
def mac_layout(monkeypatch, tmp_path, windows_layout):
    monkeypatch.setattr(updater, 'MAC', True)
    monkeypatch.setattr(updater, 'work_folder', lambda app: tmp_path/'Support'/'Update')
    return tmp_path/'Support'/'Update'


def release_zip(path, version='9.9.9', extra=()):
    with zipfile.ZipFile(path, 'w') as bundle:
        bundle.writestr('Grainy/Grainy.exe', f'exe {version}')
        bundle.writestr('Grainy/_internal/lib.dll', f'lib {version}')
        bundle.writestr('Grainy/LICENSE.txt', 'MIT')
        for name, data in extra:bundle.writestr(name, data)
    return path


def payload(archive, version='9.9.9', url=None, **changes):
    data = archive.read_bytes()
    asset = dict(name=f'Grainy-{version}-windows-x64.zip', size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest(),
                 browser_download_url=url or updater.DOWNLOADS+f'v{version}/Grainy-{version}-windows-x64.zip')
    asset.update(changes)
    return dict(tag_name='v'+version, draft=False, prerelease=False, body='notes', assets=[asset])


def test_versions_and_release_parsing(tmp_path):
    assert updater.newer('0.5.80', '0.5.79') and updater.newer('0.6', '0.5.99') and updater.newer('0.5.100', '0.5.99')
    assert not updater.newer(__version__) and not updater.newer('0.5.9', '0.5.10')
    archive = release_zip(tmp_path/'r.zip')
    good = updater.parse_release(payload(archive))
    assert good['version'] == '9.9.9' and len(good['sha256']) == 64 and good['notes'] == 'notes'
    assert updater.parse_release(payload(archive, url='https://example.com/Grainy-9.9.9-windows-x64.zip')) is None     # foreign host
    assert updater.parse_release(payload(archive, digest=None)) is None                                               # unverifiable
    assert updater.parse_release({**payload(archive), 'prerelease': True}) is None
    assert updater.parse_release(payload(archive, name='Grainy-9.9.9-mac.zip')) is None and updater.parse_release([]) is None


def test_download_verifies_and_stage_rejects_unsafe_archives(tmp_path):
    archive = release_zip(tmp_path/'source.zip')
    release = updater.parse_release(payload(archive, url=str(archive)), None)
    seen = []
    got = updater.download(release, tmp_path/'work'/'download', lambda done, total: seen.append((done, total)))
    assert got.read_bytes() == archive.read_bytes() and seen[-1][0] == release['size'] and not list(got.parent.glob('*.part'))
    with pytest.raises(ValueError):updater.download({**release, 'sha256': '0'*64}, tmp_path/'bad')
    assert not list((tmp_path/'bad').iterdir())                                 # nothing unverified is left behind
    staged = updater.stage(got, tmp_path/'work')
    assert (staged/'Grainy.exe').read_text() == 'exe 9.9.9' and (staged/'_internal'/'lib.dll').is_file()
    with pytest.raises(OSError):updater.stage(got, tmp_path/'work')             # an unfinished update is not overwritten
    for name in ('../evil.txt', 'Other/x.txt', 'Grainy/../../evil.txt'):
        unsafe = release_zip(tmp_path/'unsafe.zip', extra=[(name, 'x')])
        with pytest.raises(ValueError):updater.stage(unsafe, tmp_path/'w2')
    assert not (tmp_path/'evil.txt').exists()
    with zipfile.ZipFile(tmp_path/'empty.zip', 'w') as bundle:bundle.writestr('Grainy/readme.txt', 'x')
    with pytest.raises(ValueError):updater.stage(tmp_path/'empty.zip', tmp_path/'w3')


def installed(folder, version='1.0'):
    (folder/'_internal').mkdir(parents=True); (folder/'Grainy.exe').write_text(f'exe {version}')
    (folder/'_internal'/'lib.dll').write_text(f'lib {version}'); (folder/'my-notes.txt').write_text('mine')
    return folder


def test_finish_swaps_versions_keeps_the_previous_one_and_other_files(tmp_path):
    app_dir = installed(tmp_path/'app')
    staged = updater.stage(release_zip(tmp_path/'new.zip'), app_dir/'.update')
    log = io.StringIO()
    assert updater.finish(app_dir, staged, log=log) == ''
    assert (app_dir/'Grainy.exe').read_text() == 'exe 9.9.9' and (app_dir/'_internal'/'lib.dll').read_text() == 'lib 9.9.9'
    assert (app_dir/'LICENSE.txt').is_file() and (app_dir/'my-notes.txt').read_text() == 'mine'
    previous = list((app_dir/'.update').glob('previous-*'))
    assert len(previous) == 1 and (previous[0]/'Grainy.exe').read_text() == 'exe 1.0' and 'new version copied' in log.getvalue()


def test_finish_restores_the_previous_version_when_copying_fails(tmp_path, monkeypatch):
    app_dir = installed(tmp_path/'app')
    staged = updater.stage(release_zip(tmp_path/'new.zip'), app_dir/'.update')
    def broken(source, target, *a, **k):
        Path(target).mkdir(); (Path(target)/'half.dll').write_text('partial'); raise OSError('disk full')
    monkeypatch.setattr(updater.shutil, 'copytree', broken)
    assert 'disk full' in updater.finish(app_dir, staged)
    assert (app_dir/'Grainy.exe').read_text() == 'exe 1.0' and (app_dir/'_internal'/'lib.dll').read_text() == 'lib 1.0'
    assert not (app_dir/'_internal'/'half.dll').exists() and list((app_dir/'.update').glob('failed-*'))


def test_cleanup_recycles_leftovers_but_keeps_the_newest_previous_version(tmp_path, monkeypatch):
    work = tmp_path/'app'/'.update'
    for name in ('new', 'download', 'failed-1', 'previous-20260101-000000', 'previous-20260201-000000'):(work/name).mkdir(parents=True)
    trashed = []
    import send2trash
    monkeypatch.setattr(send2trash, 'send2trash', lambda p: trashed.append(Path(p).name))
    updater.cleanup(tmp_path/'app')
    assert sorted(trashed) == ['download', 'failed-1', 'new', 'previous-20260101-000000']


def bundle(folder, version='1.0'):
    """A stand-in Grainy.app with what a real bundle has: a program, a link into Resources, Info.plist."""
    app_bundle = folder/'Grainy.app'; (app_bundle/'Contents'/'MacOS').mkdir(parents=True); (app_bundle/'Contents'/'Resources').mkdir()
    program = app_bundle/'Contents'/'MacOS'/'Grainy'; program.write_text(f'#!/bin/sh\necho {version}\n'); program.chmod(0o755)
    (app_bundle/'Contents'/'Info.plist').write_text(f'<plist>{version}</plist>')
    (app_bundle/'Contents'/'Resources'/'data.txt').write_text(f'data {version}')
    (app_bundle/'Contents'/'MacOS'/'data.txt').symlink_to('../Resources/data.txt')
    return app_bundle


def bundle_zip(path, source):
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--keepParent', str(source), str(path)], check=True)
    return path


def test_each_system_takes_its_own_release_file(tmp_path, monkeypatch):
    archive = release_zip(tmp_path/'r.zip'); data = archive.read_bytes()
    def asset(name):return dict(name=name, size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest(), browser_download_url=updater.DOWNLOADS+'v9.9.9/'+name)
    names = ['Grainy-9.9.9-windows-x64.zip', 'Grainy-9.9.9-macos-arm64.zip', 'Grainy-9.9.9-macos-x64.zip', 'Grainy-9.9.9-macos-arm64.dmg']
    release = dict(draft=False, prerelease=False, body='', assets=[asset(n) for n in names])
    assert updater.parse_release(release)['name'] == names[0]
    monkeypatch.setattr(updater, 'MAC', True)
    for machine, expected in (('arm64', names[1]), ('x86_64', names[2])):
        monkeypatch.setattr(updater.platform, 'machine', lambda machine=machine: machine)
        assert updater.parse_release(release)['name'] == expected
    assert updater.parse_release({**release, 'assets': [asset(names[0])]}) is None        # a Windows-only release offers macOS nothing


@pytest.mark.skipif(sys.platform != 'darwin', reason='ditto')
def test_macos_stage_keeps_links_and_permissions_and_rejects_unsafe_archives(tmp_path, mac_layout):
    archive = bundle_zip(tmp_path/'new.zip', bundle(tmp_path/'source', '9.9.9'))
    staged = updater.stage(archive, mac_layout)
    assert staged == mac_layout/'new'/'Grainy.app' and os.access(staged/'Contents'/'MacOS'/'Grainy', os.X_OK)
    assert (staged/'Contents'/'MacOS'/'data.txt').is_symlink() and (staged/'Contents'/'MacOS'/'data.txt').read_text() == 'data 9.9.9'
    with pytest.raises(OSError):updater.stage(archive, mac_layout)              # an unfinished update is not overwritten
    for names in (['Grainy.app/Contents/MacOS/Grainy', 'Grainy.app/Contents/Info.plist', '../evil.txt'],
                  ['Grainy.app/Contents/MacOS/Grainy', 'Grainy.app/Contents/Info.plist', 'Other.app/x'],
                  ['Grainy.app/Contents/Info.plist'], ['Grainy/Grainy.exe', 'Grainy/_internal/lib.dll']):
        with zipfile.ZipFile(tmp_path/'unsafe.zip', 'w') as unsafe:
            for name in names:unsafe.writestr(name, 'x')
        with pytest.raises(ValueError):updater.stage(tmp_path/'unsafe.zip', tmp_path/'w2')
    assert not (tmp_path/'evil.txt').exists() and not (tmp_path/'w2'/'new').exists()


@pytest.mark.skipif(sys.platform != 'darwin', reason='ditto')
def test_macos_finish_swaps_the_bundle_and_keeps_the_previous_one(tmp_path, mac_layout):
    home = tmp_path/'Applications'; app_bundle = bundle(home); (home/'Other.app').mkdir()
    staged = updater.stage(bundle_zip(tmp_path/'new.zip', bundle(tmp_path/'source', '9.9.9')), mac_layout)
    log = io.StringIO()
    assert updater.finish(app_bundle, staged, log=log) == ''
    assert subprocess.run([str(app_bundle/'Contents'/'MacOS'/'Grainy')], capture_output=True, text=True).stdout.strip() == '9.9.9'
    assert (home/'Other.app').is_dir() and not staged.exists() and 'new version in place' in log.getvalue()
    previous = list(mac_layout.glob('previous-*'))
    assert len(previous) == 1 and (previous[0]/'Grainy.app'/'Contents'/'Resources'/'data.txt').read_text() == 'data 1.0'
    assert updater.finish(app_bundle, mac_layout/'new'/'Grainy.app') == 'staged version is incomplete'


@pytest.mark.skipif(sys.platform != 'darwin', reason='ditto')
def test_macos_finish_puts_the_previous_bundle_back_when_the_move_fails(tmp_path, mac_layout, monkeypatch):
    app_bundle = bundle(tmp_path/'Applications')
    staged = updater.stage(bundle_zip(tmp_path/'new.zip', bundle(tmp_path/'source', '9.9.9')), mac_layout)
    place = updater._place; calls = []
    def failing(source, target):
        calls.append(target)
        if len(calls) == 2:raise OSError('disk full')
        place(source, target)
    monkeypatch.setattr(updater, '_place', failing)
    assert 'disk full' in updater.finish(app_bundle, staged)
    assert (app_bundle/'Contents'/'Resources'/'data.txt').read_text() == 'data 1.0' and os.access(app_bundle/'Contents'/'MacOS'/'Grainy', os.X_OK)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process numbers')
def test_macos_waits_for_the_old_copy_to_close(mac_layout):
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(.4)'])
    assert not updater.wait_for_exit(child.pid, .05)
    child.wait()                                                                 # reaped: the process number no longer exists
    assert updater.wait_for_exit(child.pid, 2) and updater.wait_for_exit(0)


class Preferences:
    def __init__(self, **values):self.values = dict(values)
    def save(self, key, value):self.values[key] = value


def test_systems_without_an_installer_get_no_update_commands(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from luma.app import MainWindow
    monkeypatch.setattr(updater, 'SUPPORTED', False)
    monkeypatch.setattr(updater, 'cleanup', lambda *a: pytest.fail('nothing to clean up'))
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: pytest.fail('no question'))
    w = MainWindow(tmp_path/'data'); prefs = Preferences(update_check=True)
    try:
        w.show()
        controller = updater.UpdateController(w, prefs, fetcher=lambda: pytest.fail('no check'), packaged=True)
        assert not controller.auto_action.isVisible() and not controller.check_action.isVisible()
        controller.startup(); app.processEvents()
        assert controller.thread is None and w.update_button.isHidden()
    finally:
        w.close()


def test_controller_asks_once_checks_when_due_and_shows_the_button(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QMenu
    from luma.app import MainWindow
    asked = []
    monkeypatch.setattr(updater, 'cleanup', lambda *a: None)
    monkeypatch.setattr(updater, 'SUPPORTED', True)                      # the controller itself is the same everywhere
    def answer(self):
        asked.append(self.text()); self.clickedButton = lambda: next(b for b in self.buttons() if b.text() == '자동 확인 켜기'); return 0
    monkeypatch.setattr(QMessageBox, 'exec', answer)
    infos = []; monkeypatch.setattr(QMessageBox, 'information', lambda *a, **k: infos.append(a[2]))
    w = MainWindow(tmp_path/'data'); prefs = Preferences()
    try:
        w.show()
        assert [a.text() for a in w.edit_menu.actions() if not a.isSeparator()][:2] == ['실행 취소\tCtrl+Z', '다시 실행\tCtrl+Shift+Z']
        assert w.menuBar().actions()[1].menu() is w.edit_menu and w.update_button.isHidden()
        calls = []
        release = dict(version='99.0.0', name='Grainy-99.0.0-windows-x64.zip', url='x', size=10, sha256='0'*64, notes='n')
        controller = updater.UpdateController(w, prefs, fetcher=lambda: calls.append(1) or release, packaged=True)
        settings = [a.text() for a in w.findChild(QMenu, 'settingsMenu').actions()]
        assert '새 버전 자동 확인' in settings and '업데이트 확인…' in settings and not controller.auto_action.isChecked()
        controller.startup()                                             # first start: the question, then a check
        assert len(asked) == 1 and prefs.values['update_check'] is True and controller.auto_action.isChecked()
        wait(lambda: controller.thread is None and calls)
        assert not w.update_button.isHidden() and '99.0.0' in w.update_button.text() and 'update_checked' in prefs.values
        controller.startup(); app.processEvents()                        # not asked again, not checked again within a day
        assert len(asked) == 1 and len(calls) == 1
        controller.fetcher = lambda: {**release, 'version': __version__}
        controller.check(True); wait(lambda: controller.thread is None and infos)
        assert __version__ in infos[-1] and w.update_button.isHidden()   # manual check: says it is up to date
        def offline():raise OSError('offline')
        controller.fetcher = offline; controller.check(True); wait(lambda: controller.thread is None and len(infos) == 2)
        assert 'offline' in infos[-1]
        declined = Preferences(update_check=False); quiet = updater.UpdateController(w, declined, fetcher=lambda: calls.append(2), packaged=True)
        quiet.startup(); app.processEvents(); assert 2 not in calls and len(asked) == 1     # switched off: no request at all
        source = updater.UpdateController(w, Preferences(), fetcher=lambda: calls.append(3), packaged=False)
        source.startup(); assert 3 not in calls and len(asked) == 1                         # run from source: nothing
    finally:
        w.close()
