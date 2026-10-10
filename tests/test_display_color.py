from dataclasses import replace
from pathlib import Path
from threading import Event
import json
import os
import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog
from luma import display_profiles as profiles,colorio,engine
from luma.display_color import DisplayTarget,DisplayDialog
from luma.catalog import Catalog
from luma.app import MainWindow
from luma.widgets import PhotoView
from test_studio_ui import app,window,wait


def profile_file(tmp_path,name='Adobe RGB'):
    path=tmp_path/(name+'.icc');path.write_bytes(colorio.profile(name));return path


def current(w,mode):
    return w.display_color.main.state.mode==mode and not w.display_color.main.running and not w.display_color.main.timer.isActive() and not w.render_running


def test_windows_resolver_uses_effective_profile_and_no_profile_means_srgb(tmp_path,monkeypatch):
    path=profile_file(tmp_path);seen=[]
    def query(screen):seen.append(screen);return str(path) if screen=='A' else None
    monkeypatch.setattr(profiles,'windows_profile_path',query);monkeypatch.setattr(profiles,'MAC',False)
    a=profiles.resolve('auto','A');b=profiles.resolve('auto','B')
    assert a.data==path.read_bytes() and not a.error
    assert b.data is None and not b.error and b.description=='sRGB'
    assert seen==['A','B']
    assert profiles.resolve('off','A').data is None and seen==['A','B']


def test_manual_reload_missing_corrupt_non_rgb_and_oversize(tmp_path):
    import imagecodecs
    path=profile_file(tmp_path);a=profiles.resolve('manual','A',path)
    path.write_bytes(colorio.profile('Display P3'))
    b=profiles.resolve('manual','A',path)
    assert a.digest!=b.digest and b.data==path.read_bytes()
    for payload in [b'broken',imagecodecs.cms_profile('gray'),b'x'*(profiles.MAX_PROFILE_BYTES+1)]:
        path.write_bytes(payload);state=profiles.resolve('manual','A',path)
        assert state.error and state.data is None and 'sRGB' in state.message
    path.unlink();assert profiles.resolve('manual','A',path).error


def test_error_during_auto_detection_is_visible_and_does_not_keep_previous_profile(monkeypatch):
    def fail(screen):raise OSError('monitor disconnected')
    monkeypatch.setattr(profiles,'windows_profile_path',fail);monkeypatch.setattr(profiles,'MAC',False)
    s=profiles.resolve('auto','B');assert s.error and s.data is None and 'monitor disconnected' in s.message


def test_macos_resolver_takes_the_profile_itself_and_none_means_srgb(monkeypatch):
    data=colorio.profile('Display P3');seen=[]
    def query(screen):
        seen.append(screen)
        if screen=='C (3)':raise OSError('display went away')
        return data if screen=='A (1)' else b'broken' if screen=='D (4)' else None
    monkeypatch.setattr(profiles,'mac_profile_data',query);monkeypatch.setattr(profiles,'MAC',True)
    monkeypatch.setattr(profiles,'windows_profile_path',lambda screen:pytest.fail('not on macOS'))
    a=profiles.resolve('auto','A (1)');b=profiles.resolve('auto','B (2)')
    assert a.data==data and not a.error and a.description=='A' and a.path=='' and a.digest!='srgb'
    assert b.data is None and not b.error and b.description=='sRGB'
    for screen in ('C (3)','D (4)'):
        failed=profiles.resolve('auto',screen);assert failed.error and failed.data is None and 'sRGB' in failed.message
    assert seen==['A (1)','B (2)','C (3)','D (4)']
    assert profiles.resolve('off','A (1)').data is None and len(seen)==4


@pytest.mark.skipif(not profiles.MAC,reason='CoreGraphics')
def test_macos_reads_the_main_display_or_reports_none():
    import ctypes as ct
    cg=ct.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    cg.CGMainDisplayID.restype=ct.c_uint32;main=cg.CGMainDisplayID()
    assert profiles.mac_profile_data('no number') is None and profiles.mac_profile_data('') is None
    data=profiles.mac_profile_data(f'Main ({main})')        # None on a machine without a display (CI)
    if data is not None:
        state=profiles.resolve('auto',f'Main ({main})')
        assert not state.error and state.data==data and state.description=='Main'


@pytest.mark.parametrize('legacy,expected',[('__unset__','auto'),(None,'off'),('manual.icc','manual')])
def test_migration_preserves_manual_and_explicit_off(app,tmp_path,legacy,expected):
    c=Catalog(tmp_path/'data')
    if legacy!='__unset__':c.save_preference('monitor_icc',legacy)
    c.close();w=MainWindow(tmp_path/'data')
    try:assert w.display_color.mode==expected
    finally:w.close()


def test_profile_changes_display_but_not_image_histogram_edits_thumbnails_or_export(window,tmp_path):
    import tifffile
    from luma import preview_store
    w=window;ident=w.current_id;target=w.display_color.main
    wait(lambda:not target.running and not target.timer.isActive() and not w.render_running)
    before=w.view.on_screen.copy();settings=json.dumps(w.settings,sort_keys=True)
    source=Path(w.catalog.photo(ident)['path']);source_bytes=source.read_bytes()
    first=tmp_path/'before.tif';second=tmp_path/'after.tif'
    engine.export_image(source,first,w.settings,'TIFF 16-bit')
    wait(lambda:not preview_store.inspect(w.catalog.directory,ident)['rebuild'])
    thumb=preview_store.files(w.catalog.directory,ident)[0].read_bytes()
    path=profile_file(tmp_path);w.display_color.configure('manual',path)
    wait(lambda:current(w,'manual') and target.profile is not None)
    np.testing.assert_array_equal(w.view.on_screen,before)
    expected=np.uint8(colorio.display_rgb(before,path.read_bytes())*255+.5)
    shown=w.view.photo_item.pixmap().toImage();x,y=70,40
    np.testing.assert_allclose(shown.pixelColor(x,y).getRgb()[:3],expected[y,x],atol=1)
    assert json.dumps(w.settings,sort_keys=True)==settings and not w.catalog.histories(ident)
    assert source.read_bytes()==source_bytes
    engine.export_image(source,second,w.settings,'TIFF 16-bit')
    np.testing.assert_array_equal(tifffile.imread(first),tifffile.imread(second))
    assert thumb==preview_store.files(w.catalog.directory,ident)[0].read_bytes()
    w.display_color.configure('off');wait(lambda:current(w,'off'))
    shown=w.view.photo_item.pixmap().toImage()
    np.testing.assert_allclose(shown.pixelColor(x,y).getRgb()[:3],np.uint8(before[y,x]*255+.5),atol=1)


def test_latest_monitor_wins_if_old_resolver_finishes_later(window,tmp_path,monkeypatch):
    w=window;target=w.display_color.main;wait(lambda:not target.running and not target.timer.isActive())
    a=profile_file(tmp_path);b=profile_file(tmp_path,'Display P3')
    started=Event();release=Event();screen=['A'];seen=[]
    native=profiles.resolve
    def slow(mode,name,path):
        seen.append(name)
        if name=='A':started.set();assert release.wait(10)
        return replace(native('manual',name,a if name=='A' else b),mode='auto')
    monkeypatch.setattr(profiles,'resolve',slow);monkeypatch.setattr(target,'screen_name',lambda:screen[0])
    target.schedule();wait(started.is_set)
    try:
        screen[0]='B';target.schedule();release.set()
        wait(lambda:target.state.screen=='B' and not target.running)
        assert target.profile==b.read_bytes() and seen==['A','B']
    finally:release.set()


def test_each_window_has_its_own_monitor_and_compare_recolors_without_changing_pixels(window,tmp_path,monkeypatch):
    w=window;a=profile_file(tmp_path);b=profile_file(tmp_path,'Display P3');native=profiles.resolve
    monkeypatch.setattr(profiles,'resolve',lambda mode,name,path:replace(native('manual',name,a if name=='A' else b),mode='auto'))
    main=w.display_color.main;monkeypatch.setattr(main,'screen_name',lambda:'A');main.schedule()
    w.manager.compare(2,before=True);dialog=w.manager.dialogs[-1];other=next(t for t in w.display_color.targets if t.window is dialog)
    screen=['B'];monkeypatch.setattr(other,'screen_name',lambda:screen[0]);other.schedule()
    views=dialog.findChildren(PhotoView)
    wait(lambda:main.state.screen=='A' and other.state.screen=='B' and all(v.on_screen is not None for v in views))
    wait(lambda:w.display_pool.activeThreadCount()==0)
    pixels=views[1].on_screen.copy();expected=np.uint8(colorio.display_rgb(pixels,b.read_bytes())*255+.5)
    wait(lambda:views[1].photo_item.pixmap().toImage().pixelColor(70,40).getRgb()[:3]==tuple(expected[40,70]))
    assert main.profile==a.read_bytes() and other.profile==b.read_bytes()
    screen[0]='A';other.schedule();wait(lambda:other.state.screen=='A')
    expected=np.uint8(colorio.display_rgb(pixels,a.read_bytes())*255+.5)
    wait(lambda:views[1].photo_item.pixmap().toImage().pixelColor(70,40).getRgb()[:3]==tuple(expected[40,70]))
    np.testing.assert_array_equal(views[1].on_screen,pixels)
    dialog.close();assert other.closed and other not in w.display_color.targets and dialog not in w.manager.dialogs


def test_new_configuration_persists_and_dialog_controls_are_working(window,tmp_path):
    w=window;path=profile_file(tmp_path);dialog=DisplayDialog(w.display_color);dialog.show()
    dialog.mode.setCurrentIndex(dialog.mode.findData('manual'));assert dialog.choose.isEnabled()
    dialog.path.setText(str(path));dialog.apply();wait(lambda:current(w,'manual'))
    assert w.catalog.preference('display_color')==dict(mode='manual',path=str(path))
    assert 'Adobe' in dialog.status.text()
    dialog.mode.setCurrentIndex(dialog.mode.findData('auto'));assert not dialog.choose.isEnabled()
    dialog.apply();wait(lambda:current(w,'auto'));assert not w.catalog.preference('monitor_icc')
    dialog.close()


def test_profile_file_refresh_and_failure_recovers(window,tmp_path):
    w=window;path=profile_file(tmp_path);target=w.display_color.main
    w.display_color.configure('manual',path);wait(lambda:current(w,'manual') and target.profile is not None)
    initial=target.revision;path.write_bytes(b'broken');target.refresh()
    wait(lambda:target.state.error);assert target.profile is None and target.revision>initial
    assert '확인 필요' in w.display_color.button.text()
    path.write_bytes(colorio.profile('Display P3'));target.refresh()
    wait(lambda:not target.state.error and target.profile is not None)
    assert target.profile==path.read_bytes()


@pytest.mark.skipif(os.name!='nt',reason='Win32 ABI')
@pytest.mark.parametrize('outcome',['ok','grow','no_profile','error','too_large'])
def test_windows_unicode_buffer_and_dc_cleanup(monkeypatch,outcome):
    import ctypes as ct
    from ctypes import wintypes as wt
    from types import SimpleNamespace
    from unittest.mock import Mock
    name='C:\\색상\\화면 프로파일.icc';calls=[];query_count=[0]
    def query(dc,pointer,buffer):
        assert dc==123
        size=ct.cast(pointer,ct.POINTER(wt.DWORD)).contents
        if buffer is None:
            if outcome=='no_profile':ct.set_last_error(2015);size.value=0;return False
            if outcome=='error':ct.set_last_error(5);size.value=0;return False
            size.value=40000 if outcome=='too_large' else len(name)+1
            return True
        query_count[0]+=1
        if outcome=='grow' and query_count[0]==1:
            size.value+=10;ct.set_last_error(122);return False
        buffer.value=name;return True
    dll=SimpleNamespace(CreateDCW=Mock(return_value=123),GetICMProfileW=Mock(side_effect=query),DeleteDC=Mock(return_value=True))
    monkeypatch.setattr(ct,'WinDLL',lambda *a,**k:dll)
    if outcome in ('error','too_large'):
        with pytest.raises((OSError,ValueError)):profiles.windows_profile_path(r'\\.\DISPLAY2')
    else:assert profiles.windows_profile_path(r'\\.\DISPLAY2')==(None if outcome=='no_profile' else name)
    dll.CreateDCW.assert_called_once_with(r'\\.\DISPLAY2',r'\\.\DISPLAY2',None,None)
    dll.DeleteDC.assert_called_once_with(123)


def test_direct_wide_gamut_display_preserves_colors_lost_by_srgb_intermediate(tmp_path):
    samples=np.array([[[.94,.03,.02],[.02,.85,.03],[.4,.2,.6]]],np.float32)
    # Independent tagged source pixels provide the expected monitor coordinates.
    path=tmp_path/'p3.png';Image.fromarray(np.uint8(samples*255+.5)).save(path,icc_profile=colorio.profile('Display P3'))
    source,_=engine.load_image(path,working_space='ProPhoto');settings=engine.defaults();settings['working_space']='ProPhoto'
    working=engine.develop(source,settings,output_space=None)
    direct=engine.output_rgb(working,'ProPhoto',colorio.profile('Display P3'))
    np.testing.assert_allclose(direct,np.uint8(samples*255+.5)/255,atol=3e-4)
    clipped=colorio.display_rgb(engine.output_rgb(working,'ProPhoto'),colorio.profile('Display P3'))
    assert np.max(np.abs(clipped-direct))>.1
    np.testing.assert_array_equal(engine.develop(source,settings),engine.output_rgb(working,'ProPhoto'))


def test_wide_gamut_editor_thumbnail_and_compare_match_tagged_reference(window,tmp_path):
    from luma import preview_store
    w=window;source=tmp_path/'p3.png';rgb=np.empty((80,120,3),np.uint8);rgb[:]=[240,8,5]
    Image.fromarray(rgb).save(source,icc_profile=colorio.profile('Display P3'))
    ident=w.catalog.add(source,settings={'working_space':'ProPhoto'});w.refresh_lists();w.activate(ident)
    monitor=profile_file(tmp_path,'Display P3');w.display_color.configure('manual',monitor)
    wait(lambda:current(w,'manual') and w.source is not None and not w.library_loading)
    actual=w.view.photo_item.pixmap().toImage().pixelColor(60,40).getRgb()[:3]
    np.testing.assert_allclose(actual,[240,8,5],atol=1)
    wait(lambda:not preview_store.inspect(w.catalog.directory,ident)['rebuild'])
    packet=preview_store.inspect(w.catalog.directory,ident);assert packet['color_space']=='ProPhoto RGB'
    with Image.open(preview_store.files(w.catalog.directory,ident)[0]) as jpeg:
        import imagecodecs
        assert imagecodecs.cms_info(jpeg.info['icc_profile'])['colorspace']=='rgb'
        assert len(jpeg.info['icc_profile'])<4096
    w.photo_model.invalidate_thumbnails([ident]);w.photo_model.thumbnail(ident)
    wait(lambda:ident in w.photo_model.thumbnails and w.photo_model.thumbnails[ident][0] is not None)
    actual=w.photo_model.thumbnails[ident][0].toImage().pixelColor(60,40).getRgb()[:3]
    np.testing.assert_allclose(actual,[240,8,5],atol=7)
    w.manager.compare(2);dialog=w.manager.dialogs[-1];view=dialog.findChildren(PhotoView)[0]
    target=next(t for t in w.display_color.targets if t.window is dialog)
    wait(lambda:view.on_screen is not None and target.profile is not None and not target.running)
    def correct():return max(abs(int(x)-int(y)) for x,y in zip(view.photo_item.pixmap().toImage().pixelColor(60,40).getRgb()[:3],[240,8,5]))<=1
    wait(correct);dialog.close()


def test_color_transform_matches_independent_pillow_cms(tmp_path):
    from PIL import ImageCms
    import io
    rng=np.random.default_rng(4);rgb=rng.integers(0,256,(20,30,3),np.uint8)
    destination=colorio.profile('Adobe RGB')
    reference=ImageCms.profileToProfile(Image.fromarray(rgb),ImageCms.ImageCmsProfile(io.BytesIO(colorio.profile('sRGB'))),
        ImageCms.ImageCmsProfile(io.BytesIO(destination)),renderingIntent=1,outputMode='RGB')
    actual=np.uint8(colorio.display_rgb(rgb/255,destination)*255+.5)
    np.testing.assert_allclose(actual,np.asarray(reference),atol=1)
