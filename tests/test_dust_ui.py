import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from copy import deepcopy
import threading,time
import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication,QDialog
from PySide6.QtCore import Qt,QTimer,QPointF
from PySide6.QtTest import QTest
from luma.app import MainWindow,configure_application
from luma.dust_dialog import DustDialog


def wait(predicate,timeout=15000):
    app=QApplication.instance();deadline=time.monotonic()+timeout/1000
    while time.monotonic()<deadline:
        app.processEvents()
        # A nested loop may dispatch another queued render after its quit
        # callback observed idle. Check after dispatch, before returning.
        if predicate():return
        time.sleep(.005)
    assert predicate()


@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([]);configure_application(app);return app


@pytest.fixture
def window(app,tmp_path):
    from test_dust import fixture
    _,dusty,_=fixture((320,480));path=tmp_path/'dust.png'
    Image.fromarray(np.uint8(np.repeat(dusty[...,None],3,-1)*255)).save(path)
    w=MainWindow(tmp_path/'catalog');ident=w.catalog.add(path);w.refresh_lists();w.activate(ident);w.show()
    wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
    yield w
    for dialog in w.findChildren(DustDialog):
        if not dialog.closed:dialog.reject()
    wait(lambda:not w.render_running and not w.jobs);w.close()


def ready(dialog):
    wait(lambda:dialog.result is not None and not dialog.busy and not dialog.preview_running and not dialog.view.item.pixmap().isNull())


def test_review_click_zoom_preview_selection_and_no_write(window,tmp_path):
    w=window;before=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id))
    d=DustDialog(w);d.show();ready(d)
    assert len(d.result['spots'])==4 and d.selected()==[0,1,2,3]
    spot=d.view.spots[0];r=d.view.sceneRect();point=d.view.mapFromScene(QPointF(spot['x']*(r.width()-1),spot['y']*(r.height()-1)))
    QTest.mouseClick(d.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    assert 0 not in d.selected()
    d.list.setCurrentRow(1);assert d.view.transform().m11()==1
    d.after.setChecked(True);wait(lambda:d.preview_note.text()=='체크된 후보 제거 미리보기' and not d.preview_running)
    d.select_all(False);assert not d.apply_button.isEnabled()
    d.select_all(True);assert d.apply_button.isEnabled()
    d.show_spots.setChecked(False);assert not d.view.show_spots
    assert w.settings==before and len(w.catalog.histories(w.current_id))==history
    d.reject();assert d.data is None and d.display.closed


def test_apply_one_history_with_undo_redo_xmp_and_no_auto_sync(window,tmp_path,monkeypatch):
    from luma.library import write_sidecar,read_sidecar
    w=window;before=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id));seen=[]
    original_exec=DustDialog.exec
    def run(dialog):
        poll=QTimer(dialog);poll.setInterval(10)
        def apply_when_ready():
            if dialog.result is not None and not dialog.busy:
                poll.stop();dialog.select_all(False);dialog.toggle_candidate(0)
                dialog.scale.setValue(75);dialog.add_candidate(.3,.5);dialog.scale.setValue(130)
                seen.extend(dialog.view.spots[i] for i in dialog.selected());dialog.apply_selection()
        poll.timeout.connect(apply_when_ready);poll.start();QTimer.singleShot(10000,dialog.reject)
        return original_exec(dialog)
    monkeypatch.setattr(DustDialog,'exec',run)
    sync_calls=[]
    original_commit=w.commit
    def commit(*args,**kwargs):
        if w.settings!=w.last_saved:sync_calls.append(w.manager.syncing)
        return original_commit(*args,**kwargs)
    monkeypatch.setattr(w,'commit',commit)
    w.studio.detect_dust();wait(lambda:not w.render_running)
    assert len(seen)==2 and w.settings['retouch'][-1]['spots']==seen
    assert seen[0]['scale']==75 and seen[1]['manual'] and seen[1]['scale']==130
    assert sync_calls==[True] and len(w.catalog.histories(w.current_id))==history+1
    saved=deepcopy(w.settings);sidecar=tmp_path/'dust.xmp';write_sidecar(w.catalog.photo(w.current_id),sidecar)
    assert read_sidecar(sidecar)['settings']['retouch']==saved['retouch']
    w.undo();wait(lambda:not w.render_running);assert w.settings==before
    w.redo();wait(lambda:not w.render_running);assert w.settings==saved
    assert w.catalog.preference(f'undo:{w.current_id}')['undo'][-1]['retouch']==before['retouch']


def test_rescan_required_after_options_changed(window):
    d=DustDialog(window);d.show();ready(d)
    d.maximum.setValue(30);assert d.result is None and not d.selected() and not d.apply_button.isEnabled()
    d.scan();ready(d);assert d.detect_options['maximum']==30
    d.minimum.setValue(50);d.scan();assert not d.busy and '최소' in d.status.text()
    d.reject()


@pytest.mark.parametrize('change',['settings','source'])
def test_apply_refuses_changed_settings_or_source(window,change):
    w=window;d=DustDialog(w);d.show();ready(d)
    if change=='settings':w.set_setting('exposure',.2)
    else:
        path=w.catalog.photo(w.current_id)['path'];Image.new('RGB',(480,320),'gray').save(path)
    d.apply_selection();assert d.operation is None and d.result is not None and '바뀌' in d.status.text()
    assert not w.settings['retouch'];d.reject()


def test_cancel_and_close_discard_inflight_result(window,monkeypatch):
    import luma.dust_dialog as module
    entered=threading.Event();release=threading.Event();real=module.detect
    def slow(*args,**kwargs):
        entered.set();assert release.wait(5);return real(*args,**kwargs)
    monkeypatch.setattr(module,'detect',slow)
    d=DustDialog(window);d.show();wait(entered.is_set)
    d.cancel_scan();d.reject();release.set();wait(lambda:not window.jobs)
    assert d.result is None and d.operation is None and d.closed and not window.settings['retouch']


def test_preview_changes_coalesce_and_late_after_preview_is_discarded(window,monkeypatch):
    import luma.dust_dialog as module
    d=DustDialog(window);d.show();ready(d)
    entered=threading.Event();release=threading.Event();real=module.repair
    def slow(*args,**kwargs):
        entered.set();assert release.wait(5);return real(*args,**kwargs)
    monkeypatch.setattr(module,'repair',slow)
    d.after.setChecked(True);wait(entered.is_set)
    d.after.setChecked(False);d.select_all(False)
    release.set();wait(lambda:not d.preview_running and not d.preview_timer.isActive() and d.preview_note.text()=='제거 전 보기')
    assert not d.after.isChecked() and not d.selected();d.reject()


def test_detailed_extra_candidate_is_unchecked_until_explicitly_selected(window):
    from test_dust import edge_fixture
    w=window;_,dirty=edge_fixture();path=w.catalog.photo(w.current_id)['path']
    Image.fromarray(np.uint8(np.repeat(dirty[...,None],3,-1)*255)).save(path)
    w.activate(w.current_id,force=True);wait(lambda:w.source is not None and not w.render_running)
    d=DustDialog(w);d.detailed.setChecked(True);d.show();ready(d)
    extras=[i for i,s in enumerate(d.view.spots) if s.get('review')]
    assert extras and not (set(extras)&set(d.selected()))
    assert all(not d.view.spots[i]['review'] for i in d.selected())
    d.toggle_candidate(extras[0]);assert extras[0] in d.selected()
    d.apply_selection();assert d.operation['detection']['detailed']
    assert any(s['review'] for s in d.operation['spots'])


def test_manual_add_resize_emphasis_preview_and_exported_operation(window):
    d=DustDialog(window);d.show();ready(d);original=len(d.view.spots)
    d.select_all(False);d.manual_size.setValue(16);d.add_mode.setChecked(True)
    r=d.view.sceneRect();point=d.view.mapFromScene(QPointF(r.width()*.35,r.height()*.4))
    QTest.mouseClick(d.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    assert len(d.view.spots)==original+1 and d.view.spots[-1]['manual']
    assert d.selected()==[original] and d.view.spots[-1]['diameter']==16
    d.scale.setValue(65);assert d.view.spots[-1]['scale']==65
    d.add_mode.setChecked(False);d.toggle_candidate(0);assert d.scale.value()==100
    d.toggle_candidate(original);assert d.scale.value()==65
    d.toggle_candidate(original);d.after.setChecked(True);d.emphasis.setChecked(True)
    wait(lambda:d.preview_note.text()=='먼지 강조 · 체크된 후보 제거 미리보기' and not d.preview_running)
    d.emphasis.setChecked(False);wait(lambda:d.preview_note.text()=='체크된 후보 제거 미리보기')
    d.apply_selection();assert d.operation['version']==4
    saved=[s for s in d.operation['spots'] if s.get('manual')]
    assert len(saved)==1 and saved[0]['scale']==65
    assert 'emphasis' not in d.operation and not window.settings['retouch']


def test_pattern_filter_and_last_detection_options_persist_without_photo_edits(window):
    w=window;before=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id))
    d=DustDialog(w);d.show();ready(d)
    assert not d.reduce_patterns.isChecked() and not d.reduce_patterns.isEnabled()
    d.detailed.setChecked(True);assert d.reduce_patterns.isEnabled()
    d.reduce_patterns.setChecked(True);d.sensitivity.setValue(55);d.maximum.setValue(35)
    assert d.result is None and not d.apply_button.isEnabled()
    d.scan();ready(d)
    assert d.detect_options['reduce_patterns'] and d.detect_options['sensitivity']==55
    d.reject();wait(lambda:not w.jobs)
    reopened=DustDialog(w);reopened.show();ready(reopened)
    assert reopened.detailed.isChecked() and reopened.reduce_patterns.isChecked()
    assert reopened.sensitivity.value()==55 and reopened.maximum.value()==35
    assert w.settings==before and len(w.catalog.histories(w.current_id))==history
    reopened.reject()


def test_invalid_saved_detection_options_fall_back_to_defaults(window):
    window.catalog.save_preference('dust_detection_options',{'sensitivity':'broken','detailed':True})
    d=DustDialog(window)
    assert d.sensitivity.value()==50 and not d.detailed.isChecked() and not d.reduce_patterns.isChecked()
    d.reject()


def test_more_candidates_keep_selection_manual_size_and_preview_without_rescan(window,monkeypatch):
    import luma.dust_dialog as module
    calls=[]
    def many(gray,options,**kwargs):
        calls.append(kwargs['limit']);h,w=gray.shape
        spots=[dict(x=.05+(i%40)*.022,y=.05+(i//40)*.03,rx=.002,ry=.003,diameter=3,
                    polarity='dark',review=i>=600,score=1,spot_id=i) for i in range(1003)]
        return dict(spots=spots,total=1003,truncated=False,width=w,height=h,seconds=.1)
    monkeypatch.setattr(module,'detect',many)
    d=DustDialog(window);d.show();ready(d)
    assert len(d.view.spots)==500 and len(d.selected())==500 and d.more_button.isVisible()
    d.select_all(False);d.toggle_candidate(0);d.add_candidate(.45,.45);d.scale.setValue(60)
    manual=d.view.spots[500];assert manual['manual']
    QTest.mouseClick(d.more_button,Qt.MouseButton.LeftButton)
    assert len(d.view.spots)==1001 and len(d.pending_spots)==3 and d.view.spots[500] is manual
    assert d.list.currentRow()==500 and d.scale.value()==60 and d.selected()[:2]==[0,500]
    assert len(d.selected())==102 and all(not d.view.spots[i].get('review') for i in d.selected())
    d.more_button.click();assert len(d.view.spots)==1004 and not d.more_button.isVisible()
    assert len({s['spot_id'] for s in d.view.spots if 'spot_id' in s})==1003
    assert calls==[5000] and '목록 1,003개' in d.status.text()
    d.select_all(False);d.toggle_candidate(500);d.after.setChecked(True)
    wait(lambda:d.preview_note.text()=='체크된 후보 제거 미리보기' and not d.preview_running)
    d.apply_selection();assert d.operation['spots']==[manual] and manual['scale']==60


def test_soft_candidates_require_explicit_selection_and_option_is_remembered(window):
    from test_dust_soft import soft_fixture
    w=window;_,dirty,_=soft_fixture();path=w.catalog.photo(w.current_id)['path']
    Image.fromarray(np.uint8(np.clip(np.repeat(dirty[...,None],3,-1),0,1)*255)).save(path)
    w.activate(w.current_id,force=True);wait(lambda:w.source is not None and not w.render_running)
    d=DustDialog(w);assert not d.soft.isChecked() and not d.soft.isEnabled()
    d.detailed.setChecked(True);assert d.soft.isEnabled();d.soft.setChecked(True);d.show();ready(d)
    new=[i for i,s in enumerate(d.view.spots) if s.get('detail_kind')=='soft']
    assert new and not set(new)&set(d.selected())
    assert all('옅은 점' in d.list.item(i).text() for i in new)
    d.select_all(False);d.toggle_candidate(new[0]);d.apply_selection()
    assert d.operation['detection']['soft'] and len(d.operation['spots'])==1
    assert not w.settings['retouch']
    reopened=DustDialog(w);assert reopened.soft.isChecked() and reopened.detailed.isChecked();reopened.reject()


def test_grain_suppression_gating_saved_setting_and_old_cached_review(window,monkeypatch):
    from dataclasses import asdict
    from luma.dust import Options
    from luma.dust_batch import capture_record
    import luma.dust_dialog as module
    w=window;d=DustDialog(w);d.show();ready(d)
    assert not d.suppress_grain.isEnabled() and not d.suppress_grain.isChecked()
    d.detailed.setChecked(True);assert not d.suppress_grain.isEnabled()
    d.soft.setChecked(True);assert d.suppress_grain.isEnabled()
    d.suppress_grain.setChecked(True);d.scan();ready(d)
    assert d.detect_options['suppress_grain'] and w.catalog.preference('dust_detection_options')['suppress_grain']
    d.soft.setChecked(False);assert not d.suppress_grain.isEnabled();d.reject()
    reopened=DustDialog(w);assert reopened.suppress_grain.isChecked();reopened.reject()
    # Work saved before the new flag existed must reopen without re-detection.
    record=capture_record(w.catalog,w.current_id);opts=asdict(Options())
    for key in ('suppress_grain','scratches','scratch_width'):opts.pop(key)
    result=module.detect(w.source[...,0],Options());cached=deepcopy(result)
    monkeypatch.setattr(module,'detect',lambda *a,**k:pytest.fail('Old compatible cache must not be scanned again'))
    old=DustDialog(w,record=record,cached=dict(result=result,options=opts));old.show();ready(old)
    assert old.result==cached and not old.suppress_grain.isChecked();old.reject()


def test_small_review_window_keeps_controls_readable_and_preview_accessible(window):
    d=DustDialog(window);d.resize(850,650);d.show();ready(d)
    assert d.list.height()>=150
    for control in (d.sensitivity,d.maximum,d.suppress_grain,d.soft,d.after):
        assert control.height()>=control.minimumSizeHint().height()
    scroll=d.controls_scroll;assert scroll.verticalScrollBar().maximum()>0
    scroll.ensureWidgetVisible(d.after);QApplication.processEvents()
    assert scroll.viewport().rect().contains(d.after.mapTo(scroll.viewport(),d.after.rect().center()))
    assert d.apply_button.isVisible() and d.view.isVisible();d.reject()


def test_scratch_candidates_show_narrow_shapes_and_freehand_strokes_can_be_reviewed(window):
    from test_dust_scratches import fixture
    w=window;_,dirty,_=fixture();path=w.catalog.photo(w.current_id)['path']
    Image.fromarray(np.uint8(np.clip(np.repeat(dirty[...,None],3,-1),0,1)*255)).save(path)
    w.activate(w.current_id,force=True);wait(lambda:w.source is not None and not w.render_running)
    d=DustDialog(w);assert not d.scratches.isChecked() and not d.scratch_width.isEnabled()
    d.scratches.setChecked(True);assert d.scratch_width.isEnabled() and d.reduce_patterns.isEnabled();d.show();ready(d)
    indices=[i for i,s in enumerate(d.view.spots) if s.get('detail_kind')=='scratch']
    assert len(indices)==4 and not set(indices)&set(d.selected())
    assert indices==list(range(4))
    curved=next(i for i in indices if d.view.spots[i]['polarity']=='dark' and d.view.spots[i]['y']>.3)
    path=d.view.candidate_path(d.view.spots[curved]);assert not path.contains(QPointF(170,300))
    d.select_all(False);d.view.fit();part=d.view.spots[curved]['parts'][0];r=d.view.sceneRect()
    point=d.view.mapFromScene(QPointF(part['x']*(r.width()-1),part['y']*(r.height()-1)))
    QTest.mouseClick(d.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    assert curved in d.selected()
    d.stroke_mode.setChecked(True);assert d.view.stroke_mode and not d.add_mode.isChecked()
    positions=[d.view.mapFromScene(QPointF(x,y)) for x,y in [(50,530),(300,550),(410,500)]]
    QTest.mousePress(d.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,positions[0])
    QTest.mouseMove(d.view.viewport(),positions[1]);QTest.mouseRelease(d.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,positions[2])
    manual=d.view.spots[-1];assert manual['manual'] and len(manual['parts'])>10 and len(d.selected())==2
    d.scale.setValue(65);assert manual['scale']==65
    d.after.setChecked(True);wait(lambda:d.preview_note.text()=='체크된 후보 제거 미리보기' and not d.preview_running)
    d.add_mode.setChecked(True);assert not d.stroke_mode.isChecked() and not d.view.stroke_mode
    d.apply_selection();assert d.operation['version']==4 and len(d.operation['spots'])==2
    assert d.operation['detection']['scratches'] and not w.settings['retouch']
    reopened=DustDialog(w);assert reopened.scratches.isChecked();reopened.reject()


def test_narrowed_stroke_overlay_still_draws_at_its_far_endpoint(window):
    from PySide6.QtGui import QPainter,QImage
    from PySide6.QtCore import QRectF
    from luma.dust_shapes import manual
    d=DustDialog(window);d.show();ready(d);d.view.actual()
    shape=d.data[1].shape;spot=manual([[.1,.5],[.9,.5]],shape,12);spot['scale']=25
    d.view.spots=[spot];d.view.checked={0};d.view.current=0
    x=.1*(shape[1]-1);y=.5*(shape[0]-1)
    image=QImage(32,32,QImage.Format.Format_RGBA8888);image.fill(Qt.GlobalColor.transparent)
    painter=QPainter(image);painter.translate(16-x,16-y)
    d.view.drawForeground(painter,QRectF(x-16,y-16,32,32));painter.end()
    values=np.frombuffer(image.bits(),np.uint8).reshape(32,image.bytesPerLine())
    assert np.any(values[:,3:128:4]>0);d.reject()


def test_repair_method_switches_preview_without_detection_and_restores_review(window,monkeypatch):
    import luma.dust_dialog as module
    from luma.dust_batch import capture_record
    w=window;before=deepcopy(w.settings);d=DustDialog(w);d.show();ready(d)
    assert d.repair_method.currentData()=='texture'
    selected=d.selected();result=d.result;seen=[];real=module.repair
    def capture(*args,**kwargs):seen.append(kwargs['method']);return real(*args,**kwargs)
    monkeypatch.setattr(module,'repair',capture)
    d.after.setChecked(True);wait(lambda:seen and not d.preview_running and not d.preview_timer.isActive())
    assert seen[-1]=='texture'
    d.repair_method.setCurrentIndex(d.repair_method.findData('smooth'))
    wait(lambda:seen[-1]=='smooth' and not d.preview_running and not d.preview_timer.isActive())
    assert d.result is result and d.selected()==selected
    cached=dict(result=deepcopy(result),options=deepcopy(d.detect_options));d.apply_selection()
    assert d.operation['repair_method']=='smooth' and d.review_state['repair_method']=='smooth'
    assert w.catalog.preference('dust_repair_method')=='smooth' and w.settings==before
    reopened=DustDialog(w);assert reopened.repair_method.currentData()=='smooth';reopened.reject()
    w.catalog.save_preference('dust_repair_method','texture')
    old_review=deepcopy(d.review_state);old_review.pop('repair_method')
    restored=DustDialog(w,record=capture_record(w.catalog,w.current_id),cached=cached,review_state=old_review)
    restored.show();ready(restored);assert restored.repair_method.currentData()=='smooth'
    restored.apply_selection();assert restored.operation['repair_method']=='smooth'
