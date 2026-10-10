import threading
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt,QPoint,QTimer
from PySide6.QtGui import QContextMenuEvent,QImage
from PySide6.QtWidgets import QApplication,QMenu
from PySide6.QtTest import QTest
from test_studio_ui import app,window,wait
from luma.photo_actions import build_menu
from copy import deepcopy
from luma.engine import geometry
from PySide6.QtWidgets import QMessageBox


def photos(w,tmp_path):
    ids=[w.current_id]
    for n in range(2):
        path=tmp_path/f'photo-{n}.png';Image.new('RGB',(2400,1600),(70+n*50,110,140)).save(path)
        ids.append(w.catalog.add(path))
    w.refresh_lists();wait(lambda:not w.library_loading)
    return ids


def point(view,ident):
    index=view.model().index(view.model().positions[ident]);view.scrollTo(index)
    QApplication.processEvents();return view.visualRect(index).center()


def settled(w):wait(lambda:w.source is not None and not w.render_running and not w.jobs)


def menu_actions(menu):
    result={}
    for action in menu.actions():
        if action.data():result[action.data()]=action
        if action.menu():result.update(menu_actions(action.menu()))
    return result


def test_library_single_click_opens_large_develop_but_modifiers_keep_selection(window,tmp_path):
    w=window;ids=photos(w,tmp_path);w.set_mode(1)
    QTest.mouseClick(w.grid.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ControlModifier,point(w.grid,ids[1]))
    assert w.stack.currentIndex()==1 and ids[1] in w.selected_ids()
    QTest.mouseClick(w.grid.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w.grid,ids[1]))
    settled(w)
    assert w.current_id==ids[1] and w.stack.currentIndex()==0 and w.view.fit_mode and w.filmstrip.isVisible()
    assert w.view.image_rect.width()==2400


def test_develop_click_requests_native_pixels_second_click_fits_and_drag_pans(window,tmp_path):
    w=window;ids=photos(w,tmp_path);w.activate(ids[1]);w.set_mode(0);settled(w)
    center=w.view.viewport().rect().center();requests=[];w.view.fullResolutionRequested.connect(lambda:requests.append(True))
    assert w.view.fit_mode and w.view.transform().m11()<1
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,center)
    wait(lambda:w.full_source is not None and w.presented_quality=='quality' and not w.render_running)
    assert not w.view.fit_mode and w.view.transform().m11()==1 and requests
    assert w.full_source.shape[:2]==(1600,2400)
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,center)
    QTest.mouseMove(w.view.viewport(),center+QPoint(80,25))
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,center+QPoint(80,25))
    assert not w.view.fit_mode and w.view.transform().m11()==1
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,center)
    assert w.view.fit_mode


@pytest.mark.parametrize('mode',['crop','sample','brush'])
def test_editing_tools_do_not_trigger_click_zoom(window,mode):
    w=window;w.view.fit_photo()
    if mode=='crop':w.crop_button.click()
    elif mode=='sample':w.view.sample_mode=True
    else:w.studio.new_layer();w.studio.mode('brush')
    settled(w);p=w.view.mapFromScene(w.view.image_rect.center())
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p)
    settled(w);assert w.view.fit_mode


def test_switching_photo_never_paints_the_opening_message(window,tmp_path,monkeypatch):
    w=window;ids=photos(w,tmp_path);entered=threading.Event();release=threading.Event();real=w.source_cache.load
    def blocked(*args,**kwargs):entered.set();assert release.wait(10);return real(*args,**kwargs)
    monkeypatch.setattr(w.source_cache,'load',blocked)
    try:
        w.activate(ids[1]);wait(entered.is_set)
        assert not w.view.has_photo and w.view.loading_photo
        image=w.view.viewport().grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        pixels=np.frombuffer(image.constBits(),np.uint8).reshape(image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4)
        # The center previously contained the large welcome text during decode.
        center=pixels[image.height()//2-90:image.height()//2+70,30:-30,:3]
        assert np.max(center)-np.min(center,axis=(0,1)).max()<=5
        assert np.all(center==[16,18,21])
    finally:release.set()
    settled(w);assert not w.view.loading_photo and w.view.has_photo


def test_filmstrip_context_preserves_group_or_targets_right_clicked_photo(window,tmp_path):
    w=window;ids=photos(w,tmp_path);w.set_mode(0);w.filmstrip.restore_selection(ids[:2],ids[0]);settled(w)
    seen=[]
    def capture():
        menu=QApplication.activePopupWidget()
        if isinstance(menu,QMenu):
            seen.append((set(w.selected_ids()),{a.data() for a in menu.actions()}));menu.close()
    closer=QTimer();closer.setInterval(10);closer.timeout.connect(capture);closer.start()
    for target,expected in [(ids[1],set(ids[:2])),(ids[2],{ids[2]})]:
        p=point(w.filmstrip,target)
        QTest.mouseClick(w.filmstrip.viewport(),Qt.MouseButton.RightButton,Qt.KeyboardModifier.NoModifier,p)
        assert set(w.selected_ids())==expected and w.current_id==target
        event=QContextMenuEvent(QContextMenuEvent.Reason.Mouse,p,w.filmstrip.viewport().mapToGlobal(p))
        QApplication.sendEvent(w.filmstrip.viewport(),event)
        assert seen[-1][0]==expected and {'export','rotate-right','rotate-left','flip','copy','paste','virtual-copy','show-folder'}<=seen[-1][1]
    closer.stop()
    settled(w)


def test_context_rotation_and_export_use_selected_photos_with_individual_undo(window,tmp_path,monkeypatch):
    w=window;ids=photos(w,tmp_path);w.set_mode(0);w.filmstrip.restore_selection(ids[:2],ids[0]);settled(w)
    calls=[];monkeypatch.setattr(w,'export_dialog',lambda:calls.append(w.selected_ids()))
    menu=build_menu(w);actions={a.data():a for a in menu.actions()}
    actions['export'].trigger();assert set(calls[0])==set(ids[:2])
    actions['rotate-right'].trigger();settled(w)
    assert [w.catalog.photo(i)['settings']['rotation'] for i in ids]==[1,1,0]
    assert w.settings['rotation']==1
    w.undo();settled(w);assert w.settings['rotation']==0
    w.activate(ids[1]);settled(w);w.undo();settled(w);assert w.settings['rotation']==0
    assert [w.catalog.photo(i)['settings']['rotation'] for i in ids]==[0,0,0]
    menu.close()


def test_context_batch_monochrome_reset_and_undo_preserve_unselected_photo(window,tmp_path):
    w=window;ids=photos(w,tmp_path);w.set_mode(0);w.filmstrip.restore_selection(ids[:2],ids[0]);settled(w)
    w.set_setting('exposure',.5);w.commit();original=deepcopy(w.settings)
    menu=build_menu(w);actions=menu_actions(menu)
    actions['monochrome'].trigger();settled(w)
    assert [w.catalog.photo(i)['settings']['monochrome'] for i in ids]==[True,True,False]
    assert w.settings['exposure']==.5
    before_reset=deepcopy(w.settings);actions['reset'].trigger();settled(w)
    assert w.settings['exposure']==0 and not w.settings['monochrome']
    w.undo();settled(w);assert w.settings==before_reset
    w.undo();settled(w);assert w.settings==original
    w.activate(ids[1]);settled(w);w.undo();settled(w);assert w.settings['monochrome']
    w.undo();settled(w);assert not w.settings['monochrome']
    menu.close()


def test_context_vertical_flip_matches_pixels_and_can_be_undone(window):
    w=window;w.set_setting('rotation',1);w.set_setting('flip',True);w.commit();w.render();settled(w)
    original=deepcopy(w.settings);pixels=geometry(w.source,w.settings)
    menu=build_menu(w);menu_actions(menu)['flip-vertical'].trigger();settled(w)
    assert np.array_equal(geometry(w.source,w.settings),pixels[::-1])
    w.undo();settled(w);assert w.settings==original
    menu.close()


def test_context_batch_labels_stacks_and_collection_removal(window,tmp_path):
    w=window;ids=photos(w,tmp_path);w.set_mode(0);w.filmstrip.restore_selection(ids[:2],ids[0]);settled(w)
    def trigger(key):
        menu=build_menu(w);menu_actions(menu)[key].trigger();menu.close();wait(lambda:not w.library_loading);settled(w)
    for key in ['rating-4','flag-1','label-초록','stack']:trigger(key)
    rows=[w.catalog.photo(i) for i in ids]
    assert [(p['rating'],p['flag'],p['label']) for p in rows]==[(4,1,'초록'),(4,1,'초록'),(0,0,'')]
    assert rows[0]['stack_id']==rows[1]['stack_id'] and rows[0]['stack_id'] is not None and rows[2]['stack_id'] is None
    menu=build_menu(w);actions=menu_actions(menu)
    assert all(actions[k].isChecked() for k in ['rating-4','flag-1','label-초록']) and actions['unstack'].isEnabled()
    menu.close();trigger('unstack');assert all(w.catalog.photo(i)['stack_id'] is None for i in ids)
    collection=w.catalog.add_collection('선택 사진');w.catalog.collection_add(collection,ids)
    w.manager.collection=collection
    trigger('collection-remove')
    assert w.catalog.collection_ids(collection)==[ids[2]] or set(w.catalog.collection_ids(collection))=={ids[2]}
    assert all(w.catalog.photo(i) for i in ids)


def test_context_disabled_states_and_cancelled_removal_leave_files_intact(window,tmp_path,monkeypatch):
    w=window;ids=photos(w,tmp_path);w.filmstrip.restore_selection([ids[0]],ids[0]);settled(w)
    menu=build_menu(w);actions=menu_actions(menu)
    assert all(not actions[k].isEnabled() for k in ['paste','sync','stack','unstack','compare','survey','restore-snapshot','collection-remove'])
    assert {'metadata','capture-time','write-xmp','read-xmp','rename','move','smart-preview','collection-new','reset'}<=actions.keys()
    confirmations=[]
    def cancel(*args):confirmations.append(args[2]);return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox,'question',cancel)
    from pathlib import Path
    paths=[Path(w.catalog.photo(i)['path']) for i in ids];originals=[p.read_bytes() for p in paths]
    actions['remove'].trigger();actions['trash'].trigger()
    assert len(confirmations)==2 and len(w.catalog.photos())==3
    assert [p.read_bytes() for p in paths]==originals
    menu.close()
