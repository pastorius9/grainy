from copy import deepcopy
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QFileDialog
from test_studio_ui import app, window, wait
from test_photo_navigation_ui import menu_actions
from luma import workflow_data as data
from luma.engine import defaults, load_image, develop
from luma.photo_actions import build_menu


def settled(w):wait(lambda:w.source is not None and not w.render_running and not w.jobs)


def add_photos(w,tmp_path,colors):
    ids=[]
    for n,color in enumerate(colors):
        path=tmp_path/f'flow-{n}.png';Image.new('RGB',(320,200),color).save(path);ids.append(w.catalog.add(path))
    w.refresh_lists();wait(lambda:not w.library_loading)
    return ids


def neutral_error(path,settings):
    source,info=load_image(str(path),640,settings['working_space'],raw_options=settings)
    out=develop(source,settings,output_space=None,original_size=(info['width'],info['height']))
    mean=out.reshape(-1,3).mean(axis=0)
    return float(np.max(np.abs(np.log2(mean/mean[1]))))


def cast_photo(tmp_path,cast):
    grey=np.clip(np.random.default_rng(3).normal(.5,.08,(200,300,1)),.1,.9)*np.array(cast)
    path=tmp_path/'cast.png';Image.fromarray(np.uint8(np.clip(grey,0,1)*255)).save(path);return path


@pytest.mark.parametrize('cast',[(.88,1,1.12),(1.12,1,.85),(1,.9,1)])
def test_auto_white_balance_neutralizes_cast(tmp_path,cast):
    path=cast_photo(tmp_path,cast)
    settings=defaults();source,_=load_image(str(path),640,settings['working_space'],raw_options=settings)
    values=data.automatic_white_balance(source,settings)
    assert neutral_error(path,settings)>.14
    assert neutral_error(path,values)<.01
    assert values['exposure']==settings['exposure'] and not values['kelvin_enabled']


def test_auto_white_balance_stays_in_slider_range_and_rejects_empty_frames(tmp_path):
    path=cast_photo(tmp_path,(.78,1,1.22));settings=defaults()
    source,_=load_image(str(path),640,settings['working_space'],raw_options=settings)
    values=data.automatic_white_balance(source,settings)
    assert values['temperature']==100 and neutral_error(path,values)<neutral_error(path,settings)
    black=tmp_path/'black.png';Image.new('RGB',(64,64),(0,0,0)).save(black)
    source,_=load_image(str(black),640,settings['working_space'],raw_options=settings)
    with pytest.raises(ValueError):data.automatic_white_balance(source,settings)


def test_match_exposure_uses_capture_ev_and_requires_metadata():
    reference={'info':{'shutter':'1/100','aperture':4,'iso':100},'settings':{**defaults(),'exposure':.5}}
    target={'info':{'shutter':'1/50','aperture':4,'iso':100},'settings':defaults()}
    assert data.match_exposure(reference,target)['exposure']==pytest.approx(-.5)
    target['info']={'shutter':'1/100','aperture':2.8,'iso':400}
    assert data.match_exposure(reference,target)['exposure']==pytest.approx(.5-np.log2(4/2.8**2*16))
    with pytest.raises(ValueError):data.match_exposure(reference,{'info':{},'settings':defaults()})


def test_arrange_stacks_keeps_cover_first_and_members_together():
    rows=[{'id':1,'stack_id':None},{'id':2,'stack_id':2},{'id':3,'stack_id':None},{'id':4,'stack_id':2},{'id':5,'stack_id':2}]
    assert data.arrange_stacks(rows,{},False)==[1,2,3,4,5]
    assert data.arrange_stacks(rows,{},True)==[1,2,3]
    orders={'2':[5,2]}
    assert data.arrange_stacks(rows,orders,True)==[1,5,3]
    assert data.arrange_stacks(rows,orders,False)==[1,5,2,4,3]


def test_menu_previous_edits_orientation_and_undo(window,tmp_path):
    w=window;first=w.current_id;ids=add_photos(w,tmp_path,[(90,120,150),(150,120,90)])
    titles=[a.text() for a in w.menuBar().actions()]
    assert '사진 작업' in titles
    w.set_setting('exposure',.8);w.activate(ids[0]);settled(w)
    assert w.workflow.previous_id==first
    menu=build_menu(w);actions=menu_actions(menu)
    assert actions['previous-edits'].isEnabled() and actions['auto-white-balance'].isEnabled()
    actions['previous-edits'].trigger();settled(w)
    assert w.catalog.photo(ids[0])['settings']['exposure']==pytest.approx(.8)
    assert w.settings['exposure']==pytest.approx(.8)
    w.undo();w.commit()
    assert w.catalog.photo(ids[0])['settings']['exposure']==0
    # Orientation reset touches only transformed photos and keeps crops of the others.
    crop=(.1,.1,.8,.8);w.catalog.set_settings(ids[1],{**w.catalog.photo(ids[1])['settings'],'crop':crop})
    w.catalog.set_settings(ids[0],{**w.catalog.photo(ids[0])['settings'],'rotation':1,'flip':True})
    w.filmstrip.restore_selection(ids,ids[0]);w.workflow.camera_orientation();settled(w)
    assert w.catalog.photo(ids[0])['settings']['rotation']==0 and not w.catalog.photo(ids[0])['settings']['flip']
    assert tuple(w.catalog.photo(ids[1])['settings']['crop'])==crop


def test_auto_white_balance_menu_runs_in_worker_and_is_undoable(window,tmp_path):
    w=window;ids=add_photos(w,tmp_path,[(100,120,160)]);w.activate(ids[0]);settled(w)
    w.filmstrip.restore_selection([ids[0]],ids[0]);assert w.selected_ids()==[ids[0]]
    w.workflow.auto_white();wait(lambda:w.workflow.cancel is None and not w.jobs)
    settled(w);values=w.catalog.photo(ids[0])['settings']
    assert values['temperature']>5 and w.settings['temperature']==values['temperature']
    assert not w.maintenance_running
    w.undo();w.commit();assert w.catalog.photo(ids[0])['settings']['temperature']==0


def test_quick_collection_stack_cover_and_keyword(window,tmp_path):
    w=window;ids=add_photos(w,tmp_path,[(10,20,30),(40,50,60),(70,80,90)])
    w.filmstrip.restore_selection(ids[:2],ids[0]);w.workflow.quick_toggle()
    quick=w.catalog.preference('quick_collection')
    assert set(w.catalog.collection_ids(quick))==set(ids[:2])
    wait(lambda:not w.library_loading);w.filmstrip.restore_selection(ids[:2],ids[0]);w.workflow.quick_toggle();assert not w.catalog.collection_ids(quick)
    w.catalog.stack(ids);w.manager.stack_action.setChecked(True);w.manager.toggle_stacks();wait(lambda:not w.library_loading)
    assert ids[0] in w.visible_ids and not {ids[1],ids[2]}&set(w.visible_ids)
    w.activate(ids[2]);w.workflow.stack_cover();wait(lambda:not w.library_loading)
    assert ids[2] in w.visible_ids and not {ids[0],ids[1]}&set(w.visible_ids)
    w.manager.stack_action.setChecked(False);w.manager.toggle_stacks();wait(lambda:not w.library_loading)
    position=w.visible_ids.index(ids[2]);assert w.visible_ids[position:position+3]==[ids[2],ids[0],ids[1]]
    w.catalog.save_preference('shortcut_keyword','여행|일본')
    for _ in range(2):
        w.filmstrip.restore_selection(ids[:2],ids[0]);w.workflow.apply_keyword();wait(lambda:not w.library_loading)
    assert all(w.catalog.photo(i)['keywords']=='여행|일본' for i in ids[:2])
    assert w.catalog.photo(ids[2])['keywords']==''


def test_export_history_tracks_later_edits_and_reset(window,tmp_path,monkeypatch):
    w=window;ident=w.current_id;folder=tmp_path/'out';folder.mkdir()
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a:str(folder))
    w.quick_export('small');wait(lambda:not w.export_running)
    history=data.export_history(w.catalog,[ident])
    assert len(history)==1 and not history[0]['edited_since'] and Path(history[0]['path']).is_file()
    w.set_setting('exposure',.4);w.commit()
    assert data.export_history(w.catalog,[ident])[0]['edited_since']
    w.workflow.reset_exports();assert data.export_history(w.catalog,[ident])==[]


def test_metadata_preset_validates_and_applies_without_touching_edits(window):
    w=window;ident=w.current_id;before=deepcopy(w.catalog.photo(ident)['settings'])
    with pytest.raises(ValueError):data.save_metadata_preset(w.catalog,'bad',{'latitude':'91'})
    with pytest.raises(ValueError):data.save_metadata_preset(w.catalog,' ',{'creator':'A'})
    data.save_metadata_preset(w.catalog,'studio',{'creator':'Studio','copyright':'© Studio','keywords':'촬영'})
    data.apply_metadata_preset(w.catalog,[ident],data.metadata_presets(w.catalog)['studio'])
    photo=w.catalog.photo(ident)
    assert photo['user_metadata']['creator']=='Studio' and photo['keywords']=='촬영' and photo['settings']==before


def test_secondary_window_follows_current_photo_and_closes_with_app(window,tmp_path):
    w=window;ids=add_photos(w,tmp_path,[(200,40,40)])
    w.workflow.second_window();second=w.workflow.windows[-1]
    wait(lambda:second.view.image_rect.width()>0 and not w.jobs)
    first_key=second.shown_key
    w.activate(ids[0]);settled(w);wait(lambda:second.shown_key!=first_key and second.shown_key[0]==ids[0] and not w.jobs)
    w.workflow.reference();reference=w.workflow.windows[-1]
    assert not reference.follow and reference.reference['id']==ids[0]
    w.workflow.shutdown();assert second.closed and reference.closed and not w.workflow.windows
