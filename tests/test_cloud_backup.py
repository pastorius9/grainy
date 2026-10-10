import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
import pytest
from test_studio_ui import app, wait
from luma.catalog import Catalog
from luma.cloud_backup import PREFIX, snapshot, upload, surplus, trim


def photos(path):
    with closing(sqlite3.connect(path)) as db:return db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]


def test_upload_verifies_skips_unchanged_and_cleans_staging(tmp_path):
    catalog = Catalog(tmp_path/'data'); catalog.save_preference('x', 1)
    folder = tmp_path/'drive'/'Grainy 백업'
    staged = snapshot(catalog)
    target, value = upload(staged, folder, now=datetime(2026, 10, 1, 9))
    assert target.name == PREFIX+'20261001-090000.sqlite' and not staged.exists() and photos(target) == 0
    assert not list(folder.glob('*.part')) and not list((tmp_path/'data'/'backups'/'cloud-staging').iterdir())
    again, same = upload(snapshot(catalog), folder, value)                  # unchanged: nothing uploaded
    assert again is None and same == value and len(list(folder.iterdir())) == 1
    catalog.save_preference('x', 2)
    changed, other = upload(snapshot(catalog), folder, value, now=datetime(2026, 10, 1, 9))
    assert changed.name == PREFIX+'20261001-090000-1.sqlite' and other != value
    catalog.close()


def test_old_backups_thin_to_one_per_month_and_go_to_recycle_bin(tmp_path, monkeypatch):
    names = [f'{PREFIX}2026{m:02d}{d:02d}-120000.sqlite' for m in (7, 8, 9) for d in (1, 15)]
    assert surplus(names, keep=2) == [f'{PREFIX}20260801-120000.sqlite', f'{PREFIX}20260701-120000.sqlite']
    for name in names+['other.sqlite']:(tmp_path/name).write_bytes(b'x')
    trashed = []
    import send2trash
    monkeypatch.setattr(send2trash, 'send2trash', lambda p: trashed.append(Path(p).name))
    trim(tmp_path, keep=2)
    assert sorted(trashed) == sorted(surplus(names, keep=2)) and (tmp_path/'other.sqlite').exists()


def test_window_backs_up_on_demand_and_on_close(app, tmp_path):
    from luma.app import MainWindow
    folder = tmp_path/'drive'
    w = MainWindow(tmp_path/'data')
    closed = False
    try:
        assert not w.cloud_backup.start(False)                              # off by default: nothing written
        w.cloud_backup.save(enabled=True, folder=str(folder))
        messages = []
        assert w.cloud_backup.start(True, messages.append)
        wait(lambda: messages)
        assert len(list(folder.glob(PREFIX+'*.sqlite'))) == 1 and w.cloud_backup.settings()['last']
        w.catalog.save_preference('changed', True)
        w.close(); closed = True
        assert len(list(folder.glob(PREFIX+'*.sqlite'))) == 2
        titles = [a.text() for m in w.menuBar().findChildren(type(w.menuBar().actions()[0].menu())) for a in m.actions()]
        assert any('구글 드라이브 백업' in t for t in titles)
    finally:
        if not closed:w.close()


@pytest.mark.skipif(os.name == 'nt', reason='Windows finds the Google Drive volume first')
def test_google_drive_for_desktop_on_macos_is_found_under_cloud_storage(tmp_path, monkeypatch):
    from luma import cloud_backup
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert cloud_backup.google_drive() is None and cloud_backup.default_folder() == ''
    storage = tmp_path/'Library'/'CloudStorage'
    (storage/'OneDrive-Personal'/'My Drive').mkdir(parents=True)             # another provider's folder is not it
    assert cloud_backup.google_drive() is None
    (storage/'GoogleDrive-b@example.com'/'내 드라이브').mkdir(parents=True)
    (storage/'GoogleDrive-a@example.com'/'My Drive').mkdir(parents=True)
    assert cloud_backup.google_drive() == storage/'GoogleDrive-a@example.com'/'My Drive'
    assert cloud_backup.default_folder() == str(storage/'GoogleDrive-a@example.com'/'My Drive'/cloud_backup.FOLDER_NAME)
