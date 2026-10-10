import json
from copy import deepcopy
import cv2
import numpy as np
from PIL import Image
from PySide6.QtWidgets import QDialog
from test_studio_ui import app,window,wait
from test_optics import grid_fixture
from luma.optics import lens_records,make_profile
from luma.lens_dialog import LensDialog


def info():
    lens=next(r for r in lens_records() if '18-105' in r['model'])
    return {'make':'NIKON CORPORATION','camera':'NIKON D7000','lens':lens['model'],
        'focal_length':18,'aperture':3.5,'source_orientation':1}


def test_profile_dialog_and_apply_undo_sync(window,tmp_path):
    w=window;metadata=info();w.catalog.update(w.current_id,metadata=json.dumps(metadata))
    dialog=LensDialog(w,metadata);dialog.apply()
    assert dialog.result()==QDialog.DialogCode.Accepted and dialog.result_profile['focal']==18
    invalid=LensDialog(w,{});invalid.lens.setEditText('not a lens');invalid.apply()
    assert invalid.result_profile is None
    invalid.close()
    w.studio.auto_lens();wait(lambda:not w.render_running)
    assert w.settings['lensfun_enabled'] and w.catalog.photo(w.current_id)['settings']['lensfun_enabled']
    assert all(control.isEnabled() for control in w.studio.lens_switches.values())
    w.undo();wait(lambda:not w.render_running);assert not w.settings['lensfun_enabled']
    w.redo();wait(lambda:not w.render_running);assert w.settings['lensfun_enabled']
    # Copy and sync use the target photo's zoom/aperture/orientation.
    path=tmp_path/'second.png';Image.new('RGB',(180,120),'gray').save(path)
    ident=w.catalog.add(path);other=info();other.update(focal_length=50,aperture=8,source_orientation=6)
    w.catalog.update(ident,metadata=json.dumps(other))
    w.refresh_lists();wait(lambda:not w.library_loading)
    w.set_mode(1);w.grid.selectAll();w.set_mode(0)
    w.manager.copy_keys={'lensfun','lensfun_enabled'};w.manager.sync_selected()
    assert w.catalog.photo(ident)['settings']['lensfun']['focal']==50
    w.copy_edits();w.paste_edits()
    assert w.catalog.photo(ident)['settings']['lensfun']['orientation']==6
    wait(lambda:not w.render_running)


def test_auto_alignment_async_commit_undo_and_stale_result(window,monkeypatch):
    w=window;image=grid_fixture()
    matrix=cv2.getRotationMatrix2D((399.5,299.5),-7,1)
    w.source=cv2.warpAffine(image,matrix,(800,600),borderValue=(.65,.65,.65))
    original=w.source.copy();w.tabs.setCurrentIndex(next(i for i in range(w.tabs.count()) if w.tabs.tabText(i)=='크롭'))
    assert w.auto_level_button.isVisible();w.auto_level_button.click()
    assert not w.auto_level_button.isEnabled()
    wait(lambda:w.settings['straighten']!=0 and not w.render_running)
    assert abs(w.settings['straighten']+7)<.2 and w.settings['upright'] is None      # the angle slider, not a perspective matrix
    assert w.auto_level_button.isEnabled() and '-7.' in w.auto_level_note.text()
    np.testing.assert_array_equal(w.source,original)
    w.undo();wait(lambda:not w.render_running);assert w.settings['straighten']==0 and w.settings['crop'] is None
    w.cancel_crop();wait(lambda:not w.render_running)
    captured=[]
    monkeypatch.setattr(w,'spawn',lambda work,ready,error:captured.append((work,ready,error)))
    before=deepcopy(w.settings);w.studio.auto_upright('level');result=captured[0][0]()
    w.load_version+=1;captured[0][1](result)
    assert w.settings==before and all(b.isEnabled() for b in w.studio.upright_buttons)
    w.source=np.full((120,180,3),.3,np.float32);captured.clear();w.studio.auto_upright('auto')
    try:captured[0][0]()
    except ValueError as error:captured[0][2](error)
    else:raise AssertionError('blank image should not be aligned')
    assert w.settings==before and all(b.isEnabled() for b in w.studio.upright_buttons)


def test_auto_level_worker_refusal_is_readable_and_keeps_edits(window):
    w=window;w.source=np.full((120,180,3),.3,np.float32)
    before=deepcopy(w.settings);w.auto_level_button.click()
    wait(lambda:w.auto_level_button.isEnabled() and not w.jobs)
    assert w.settings==before
    assert '기준' in w.auto_level_note.text() and 'Traceback' not in w.auto_level_note.text()
    assert '\n' not in w.auto_level_note.text()


def test_reusing_application_does_not_reload_fonts_and_restyle(app,monkeypatch):
    from luma.app import configure_application
    def unexpected(*args):raise AssertionError('Application configured again')
    monkeypatch.setattr(app,'setStyleSheet',unexpected)
    configure_application(app)
