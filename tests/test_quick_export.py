from pathlib import Path
import hashlib
import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QFileDialog, QMessageBox
from test_studio_ui import app, window, wait
from test_photo_navigation_ui import menu_actions
from luma.app import ExportDialog
from luma.photo_actions import build_menu
from luma.quick_export import PREFERENCE, previous, copy_original
from luma.library import read_sidecar


def done(w):
    wait(lambda:not w.export_running)
    assert not w.last_export['failures']
    return [Path(v['path']) for v in w.last_export['outputs']]


def test_quick_menu_selection_pixels_size_previous_and_cancel(window,tmp_path,monkeypatch):
    w=window;source=tmp_path/'large.png';Image.new('RGB',(2400,1200),(110,70,40)).save(source)
    ident=w.catalog.add(source);unused=w.catalog.add(tmp_path/'missing.png')
    w.refresh_lists();wait(lambda:not w.library_loading)
    w.filmstrip.restore_selection([w.current_id,ident],w.current_id)
    w.set_setting('monochrome',True)  # Export must commit the pending active edit.
    folder=tmp_path/'exports';folder.mkdir()
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a:str(folder))
    menu=build_menu(w);actions=menu_actions(menu)
    assert not actions['export-previous'].isEnabled()
    actions['quick-export-small'].trigger();outputs=done(w)
    assert len(outputs)==2 and max(max(Image.open(p).size) for p in outputs)==2048
    small=next(p for p in outputs if p.stem=='test')
    # Monochrome edits export as one-channel gray files by default (0.5.53).
    assert Image.open(small).mode=='L'
    assert previous(w.catalog)['folder']==str(folder) and previous(w.catalog)['longest']==2048
    before={p:p.read_bytes() for p in outputs}
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a:pytest.fail('Previous must reuse folder'))
    w.quick_export('previous');more=done(w)
    assert len(more)==2 and not set(more)&set(outputs)
    assert all(p.read_bytes()==data for p,data in before.items())
    dialog=ExportDialog(2,w)
    assert dialog.longest.value()==2048 and dialog.folder.text()==str(folder);dialog.close()
    saved=previous(w.catalog)
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a:'')
    w.quick_export('large');assert not w.export_running and previous(w.catalog)==saved
    assert len(list(folder.glob('*.jpg')))==4 and not (folder/'missing.jpg').exists()
    menu.close()


def test_large_and_original_copy_keep_source_and_sidecar_collisions(window,tmp_path,monkeypatch):
    w=window;record=w.catalog.photo(w.current_id);source=Path(record['path']);digest=hashlib.sha256(source.read_bytes()).hexdigest()
    folder=tmp_path/'out';folder.mkdir();(folder/'test.xmp').write_text('keep',encoding='utf-8')
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a:str(folder))
    w.quick_export('large');outputs=done(w);assert Image.open(outputs[0]).size==(180,120)
    w.set_setting('photo_filter_enabled',True);w.set_setting('photo_filter','cooling80')
    w.quick_export('original');output=done(w)[0]
    assert output.name=='test_2.png' and output.read_bytes()==source.read_bytes()
    assert read_sidecar(output.with_suffix('.xmp'))['settings']['photo_filter']=='cooling80'
    assert (folder/'test.xmp').read_text(encoding='utf-8')=='keep'
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    assert previous(w.catalog)['format']=='Original'
    dialog=ExportDialog(1,w)
    assert dialog.format.currentData()=='Original' and not dialog.longest.isEnabled();dialog.close()


def test_original_sidecar_race_cleans_only_new_copy(window,tmp_path):
    record=window.catalog.photo(window.current_id);target=tmp_path/'race.png'
    target.with_suffix('.xmp').write_bytes(b'existing')
    with pytest.raises(FileExistsError):copy_original(record,target)
    assert not target.exists() and target.with_suffix('.xmp').read_bytes()==b'existing'


def test_missing_previous_location_cancel_busy_and_failed_export(window,tmp_path,monkeypatch):
    w=window;folder=tmp_path/'out';folder.mkdir()
    w.start_export([w.current_id],folder);done(w);saved=previous(w.catalog)
    w.catalog.save_preference(PREFERENCE,{**saved,'folder':str(tmp_path/'gone')})
    calls=[];monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a:calls.append(a[1]) or '')
    w.quick_export('previous');assert calls and not w.export_running
    w.export_running=True
    assert not menu_actions(build_menu(w))['quick-export'].isEnabled()
    w.quick_export('large');assert len(calls)==1;w.export_running=False
    w.catalog.save_preference(PREFERENCE,saved)
    import luma.app as app_module
    def fail(*a,**k):raise OSError('test disk error')
    monkeypatch.setattr(app_module,'export_image',fail)
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:None)
    w.start_export([w.current_id],folder,quality=77);wait(lambda:not w.export_running)
    assert w.last_export['failures'] and previous(w.catalog)==saved


def test_previous_options_survive_reopen_and_invalid_values_fall_back(window,tmp_path):
    from luma.catalog import Catalog
    w=window;folder=tmp_path/'out';folder.mkdir()
    w.start_export([w.current_id],folder,'TIFF 16-bit',91,90,'Display P3',True,'{stem}_{n:03d}')
    done(w)
    with Catalog.open_reader(w.catalog.directory) as reader:
        assert previous(reader)==dict(folder=str(folder),format='TIFF 16-bit',quality=91,longest=90,
                                    color_space='Display P3',keep_metadata=True,naming='{stem}_{n:03d}',grayscale=True,dither=True)
    w.catalog.save_preference(PREFERENCE,{'format':'broken'})
    assert previous(w.catalog) is None


def test_parallel_batch_export_keeps_order_unique_names_and_reports_failures(window, tmp_path, monkeypatch):
    from luma.quick_export import export_concurrency
    warnings = [];monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[2]))
    w = window;folder = tmp_path/'batch';folder.mkdir()
    ids = []
    for n in range(5):
        source = tmp_path/f'same-{n}'/'photo.png';source.parent.mkdir()
        Image.new('RGB', (300, 200), (40*n, 90, 150)).save(source);ids.append(w.catalog.add(source))
    missing = w.catalog.add(tmp_path/'gone.png')
    (folder/'photo_grainy.jpg').write_bytes(b'existing')          # an existing file keeps its name
    w.refresh_lists();wait(lambda: not w.library_loading)
    w.start_export(ids+[missing], folder, 'JPEG', naming='{stem}_grainy')
    wait(lambda: not w.export_running)
    outputs = [Path(o['path']) for o in w.last_export['outputs']]
    assert [o['photo_id'] for o in w.last_export['outputs']] == ids
    assert len(set(outputs)) == 5 and all(p.is_file() and p.stat().st_size > 500 for p in outputs)
    assert (folder/'photo_grainy.jpg').read_bytes() == b'existing'
    assert [p.name for p in outputs] == [f'photo_grainy_{i}.jpg' for i in range(2, 7)]
    assert len(w.last_export['failures']) == 1 and 'gone.png' in w.last_export['failures'][0] and warnings
    records = [w.catalog.photo(i) for i in ids]
    assert 1 <= export_concurrency(records) <= 4 and export_concurrency(records[:1]) == 1


@pytest.mark.skipif(__import__('os').name=='nt',reason='Windows cannot hold such a file name')
def test_names_only_macos_allows_are_exported_under_a_portable_name(window,tmp_path):
    w=window;source=tmp_path/'2026:10:09 "여행" <1>.png'              # Finder shows the colons as slashes
    Image.new('RGB',(60,40),(120,90,60)).save(source);ident=w.catalog.add(source)
    target=tmp_path/'out';target.mkdir()
    w.start_export([ident],target,'JPEG',naming='{stem}')
    wait(lambda:not w.export_running)
    assert not w.last_export['failures']
    assert [p.name for p in target.iterdir()]==['2026_10_09 _여행_ _1_.jpg']
