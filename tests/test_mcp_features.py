"""Crop, comparison sheets, culling and export plans through HTTP on temporary catalogs."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import threading

import numpy as np
import pytest
from PIL import Image,ImageDraw

from test_studio_ui import app,wait
from test_mcp_tools import session,korean,settings_of
from mcp_support import add_photos,call,gradient
from luma import photo_service,photo_images,mcp_protocol,mcp_tools


def test_crop_geometry_is_one_batch_and_summary_roundtrips(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',1)
    w.catalog.update(ids[0],metadata=json.dumps({'width':240,'height':160}))
    original=settings_of(w,ids)
    value=call(session,'apply_edits',{'edits':[{'ids':ids,'set':{'rotation':90,'flip':True,'straighten':3},
                                             'crop':[.2,.15,.8,.85]}]})
    assert not value.error and value.value['changed']==1
    changed=settings_of(w,ids)
    assert changed[0]['rotation']==1 and changed[0]['flip'] is True and changed[0]['crop']==[.2,.15,.8,.85]
    details=call(session,'get_photo',{'ids':ids}).value['photos'][0]
    assert details['crop']==[.2,.15,.8,.85] and details['edits']['straighten']==3 and 'auto_level' not in details
    assert not call(session,'undo_last_batch',{}).error and settings_of(w,ids)==original
    assert not call(session,'undo_last_batch',{'redo':True}).error and settings_of(w,ids)==changed
    assert call(session,'apply_edits',{'edits':[{'ids':ids,'crop':None}]}).value['changed']==1
    assert settings_of(w,ids)[0]['crop'] is None
    for crop in ([.8,0,.2,1],[0,0,1,0],[0,0,1,1.2],[0,0,1],False):
        assert call(session,'apply_edits',{'edits':[{'ids':ids,'crop':crop}]}).error


def test_auto_level_and_scan_crop_use_existing_algorithms_off_gui(session,tmp_path,monkeypatch):
    from luma import upright,scan_border
    w=session.window;ids=add_photos(w,tmp_path/'photos',1)
    calls=[]
    level={'mode':'level','matrix':[[1,0,0],[0,1,0],[0,0,1]],'rotation':2.,'confidence':.9,'crop_scale':1.}
    def estimate(image,mode):
        calls.append(('level',mode,threading.current_thread()));return level
    def border(image,settings):
        assert settings['upright']['rotation']==2
        calls.append(('border',None,threading.current_thread()));return (.1,.2,.9,.8)
    monkeypatch.setattr(upright,'estimate',estimate);monkeypatch.setattr(scan_border,'crop_for',border)
    result=call(session,'apply_edits',{'edits':[{'ids':ids,'auto_straighten':True,'auto_crop':True}]})
    assert not result.error and result.value['changed']==1,result
    assert [(c[0],c[1]) for c in calls]==[('level','level'),('border',None)]
    assert all(c[2] is not threading.main_thread() for c in calls)
    details=call(session,'get_photo',{'ids':ids}).value['photos'][0]
    assert details['crop']==[.1,.2,.9,.8] and details['auto_level']==2 and 'straighten' not in details.get('edits',{})
    assert call(session,'undo_last_batch',{}).value['photos']==1
    assert settings_of(w,ids)[0]['upright'] is None and settings_of(w,ids)[0]['crop'] is None


def test_auto_crop_detects_real_scan_border(session,tmp_path):
    path=tmp_path/'scan.png';pixels=np.full((240,360,3),150,np.uint8)
    pixels[:10]=0;pixels[-10:]=0;pixels[:,:10]=0;pixels[:,-10:]=0;Image.fromarray(pixels).save(path)
    ident=session.window.catalog.add(path)
    result=call(session,'apply_edits',{'edits':[{'ids':[ident],'auto_crop':True}]})
    assert not result.error and result.value['changed']==1
    crop=session.window.catalog.photo(ident)['settings']['crop']
    assert 0<crop[0]<.1 and 0<crop[1]<.1 and .9<crop[2]<1 and .9<crop[3]<1


def test_variant_sheet_six_looks_and_saved_names_without_catalog_changes(session,tmp_path,monkeypatch):
    from luma import film_look
    w=session.window;ids=add_photos(w,tmp_path/'photos',1,size=(600,400));before=settings_of(w,ids)
    films=[f['name'] for f in film_look.catalog()[:6]]
    labels=[];original=photo_images.contact_sheet
    def sheet(cells,*a,**k):labels.append([c[2] for c in cells]);return original(cells,*a,**k)
    monkeypatch.setattr(photo_images,'contact_sheet',sheet)
    folder=tmp_path/'saved'
    args={'ids':ids,'variants':[{'film_look':f} for f in films],'save':True,'path':str(folder),'size':1800}
    result=call(session,'contact_sheet',args)
    assert not result.error and result.value['variants']==films and len(result.images)==1,result
    assert labels[0]==films and all('photo-0.png' in text and film in text for text,film in zip(labels[1],films))
    saved=result.value['saved'];path=Path(saved['path']);before_file=path.read_bytes()
    with Image.open(path) as image:assert max(image.size)<=1800 and image.size==(saved['w'],saved['h'])
    assert len(before_file)==saved['bytes']
    again=call(session,'contact_sheet',args)
    assert again.value['saved']['path']!=saved['path'] and path.read_bytes()==before_file
    assert len(w.catalog.photos())==1 and settings_of(w,ids)==before and photo_service.batch_index(w.catalog)==[]


def test_variant_presets_and_invalid_combinations(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',2)
    result=call(session,'contact_sheet',{'ids':ids[:1],'variants':[{'preset':'Soft daylight','label':'soft'},{'film_look':''}]})
    assert not result.error and result.value['variants']==['soft','No film']
    for args in ({'ids':ids,'variants':[{'film_look':''}]},
                 {'ids':ids[:1],'variants':[{'film_look':'','preset':'Soft daylight'}]},
                 {'ids':ids[:1],'variants':[{'label':'missing'}]},
                 {'ids':ids[:1],'variants':[{'film_look':''}]*13},
                 {'ids':ids,'path':str(tmp_path/'out')}):
        assert call(session,'contact_sheet',args).error


def test_labels_mixed_batch_stack_and_rating_max(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',3)
    edit=call(session,'apply_edits',{'edits':[{'ids':ids,'set':{'exposure':.5}}]}).value['batch']
    result=call(session,'set_labels',{'entries':[{'ids':[ids[0]],'rating':5,'flag':'picked','label':'green'},
        {'ids':ids[1:],'rating':2},{'ids':[ids[2]],'rating':0,'flag':'rejected'}]})
    assert not result.error and result.value['changed']==3 and result.value['batch']!=edit
    assert [r['rating'] for r in map(w.catalog.photo,ids)]==[5,2,0]
    assert [p['id'] for p in call(session,'list_photos',{'rating_max':0}).value['photos']]==[ids[2]]
    assert [p['id'] for p in call(session,'list_photos',{'rating_max':2}).value['photos']]==ids[1:]
    assert call(session,'undo_last_batch',{}).value['tool']=='set_labels'
    assert [r['rating'] for r in map(w.catalog.photo,ids)]==[0,0,0]
    assert all(s['exposure']==.5 for s in settings_of(w,ids))
    assert call(session,'undo_last_batch',{}).value['undone']==edit
    assert call(session,'undo_last_batch',{'redo':True}).value['tool']=='apply_edits'
    assert call(session,'undo_last_batch',{'redo':True}).value['tool']=='set_labels'
    assert [r['rating'] for r in map(w.catalog.photo,ids)]==[5,2,0]


def test_label_conflict_is_atomic_and_new_edits_discard_label_redo(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',2)
    call(session,'set_labels',{'ids':ids,'rating':3})
    w.catalog.update(ids[1],rating=4)
    result=call(session,'undo_last_batch',{})
    assert result.error and 'can no longer be undone' in result.text
    assert [r['rating'] for r in map(w.catalog.photo,ids)]==[3,4]
    call(session,'set_labels',{'ids':ids,'rating':5})
    assert not call(session,'undo_last_batch',{}).error
    call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'contrast':2}}]})
    assert call(session,'undo_last_batch',{'redo':True}).error


def test_labels_rollback_database_failure(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',2)
    w.catalog.db.execute(f"CREATE TEMP TRIGGER fail_labels BEFORE UPDATE OF rating ON photos WHEN NEW.id={ids[1]} "
                         "BEGIN SELECT RAISE(ABORT,'write failed'); END")
    result=call(session,'set_labels',{'ids':ids,'rating':5})
    assert result.error and [r['rating'] for r in map(w.catalog.photo,ids)]==[0,0]
    assert photo_service.batch_index(w.catalog)==[] and not w.catalog.db.in_transaction


def test_export_dry_run_sizes_names_and_real_file_metadata(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',2,size=(240,160))
    call(session,'apply_edits',{'edits':[{'ids':ids,'set':{'rotation':90},'crop':[.25,0,.75,1]}]})
    folder=tmp_path/'new-output';args={'ids':ids,'path':str(folder),'width':60,'height':60,'format':'PNG'}
    history=w.catalog.preference('export_history',[])
    plan=call(session,'export_photos',{**args,'dry_run':True})
    assert not plan.error and plan.value['planned']==2 and not folder.exists(),plan
    assert w.catalog.preference('export_history',[])==history
    assert all((f['w'],f['h'],f['bytes'])==(20,60,None) for f in plan.value['files'])
    result=call(session,'export_photos',args)
    assert not result.error and result.value['exported']==2
    assert [f['name'] for f in result.value['files']]==[f['name'] for f in plan.value['files']]
    for f in result.value['files']:
        path=Path(f['path'])
        with Image.open(path) as image:assert image.size==(f['w'],f['h'])==(20,60)
        assert path.stat().st_size==f['bytes']>0
    plan2=call(session,'export_photos',{**args,'dry_run':True})
    assert all(f['name'].endswith('_2.png') for f in plan2.value['files']) and len(list(folder.iterdir()))==2


def test_file_results_are_capped():
    result=photo_service.output_files([{'photo_id':i,'path':f'/photos/{i}.jpg','size':(10,20),'bytes':30} for i in range(107)])
    assert len(result['files'])==100 and result['more_files']==7


@pytest.mark.parametrize('tool',['contact_sheet','export_photos'])
def test_saved_outputs_reject_all_settings_folders(session,tmp_path,tool):
    ids=add_photos(session.window,tmp_path/'photos',1)
    for folder in (session.preferences.path.parent,session.window.catalog.directory):
        args={'ids':ids,'path':str(folder/'new')}
        if tool=='contact_sheet':args['save']=True
        result=call(session,tool,args)
        assert result.error and 'manages itself' in result.text and not (folder/'new').exists()


def test_contact_sheet_notes_unsaved_edits_when_person_is_adjusting(session,tmp_path,monkeypatch):
    w=session.window;ids=add_photos(w,tmp_path/'photos',1);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    w.set_setting('exposure',.5);monkeypatch.setattr(w,'interacting',lambda:True)
    result=call(session,'contact_sheet',{'ids':ids})
    assert not result.error and 'last saved' in result.value['note']


def test_dry_run_reads_unsaved_crop_without_saving_catalog(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',1);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    w.set_setting('crop',[0,0,.5,1]);w.commit_timer.stop()
    before=w.catalog.photo(ids[0]);depth=len(w.undo_stack)
    result=call(session,'export_photos',{'ids':ids,'dry_run':True,'path':str(tmp_path/'out')})
    assert not result.error and (result.value['files'][0]['w'],result.value['files'][0]['h'])==(120,160)
    assert w.catalog.photo(ids[0])==before and len(w.undo_stack)==depth and not (tmp_path/'out').exists()


def test_label_batch_survives_undo_of_later_edit(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'photos',1)
    call(session,'set_labels',{'ids':ids,'rating':4})
    call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.2}}]})
    assert call(session,'undo_last_batch',{}).value['tool']=='apply_edits'
    assert call(session,'undo_last_batch',{}).value['tool']=='set_labels'
    assert w.catalog.photo(ids[0])['rating']==0


@pytest.mark.parametrize('tool',['import_folder','contact_sheet'])
def test_network_folder_resolution_does_not_block_gui(session,tmp_path,monkeypatch,tool):
    from luma import mcp_protocol
    from PySide6.QtCore import QTimer
    from mcp_support import start_call,finish
    w=session.window;ids=add_photos(w,tmp_path/'photos',1);folder=tmp_path/'network'
    release=threading.Event();entered=threading.Event();ticks=[];original=Path.resolve
    def slow(path,*a,**k):
        if path==folder:entered.set();release.wait(3)
        return original(path,*a,**k)
    monkeypatch.setattr(Path,'resolve',slow);monkeypatch.setattr(mcp_protocol,'DEADLINE_SECONDS',.5)
    timer=QTimer();timer.setInterval(20);timer.timeout.connect(lambda:ticks.append(1));timer.start()
    args={'path':str(folder)}
    if tool=='contact_sheet':args.update(ids=ids,save=True)
    try:
        pending=start_call(session,tool,args);wait(entered.is_set,2000);result=finish(pending,2)
        assert result.error and 'time limit' in result.text and len(ticks)>3 and pending.seconds<1.5
        assert not folder.exists()
    finally:release.set();timer.stop()
