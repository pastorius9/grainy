from copy import deepcopy
from threading import Event
import os
from pathlib import Path
import numpy as np
import pytest
from luma import dcp,profile_library as library,raw_defaults as rd
from luma.engine import defaults,read_metadata
from luma.catalog import Catalog
from test_rawcolor import make_dcp


def profile(path,camera='Test Camera',name='Neutral',extra=()):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(make_dcp([(50708,2,camera+'\0'),(50936,2,name+'\0'),*extra]));return path


def test_profile_index_dedup_cache_modify_remove_and_selection(tmp_path):
    root=tmp_path/'profiles';a=profile(root/'a.dcp');copy=root/'copy.DCP';copy.write_bytes(a.read_bytes())
    profile(root/'b.dcp','Other Camera');cache=tmp_path/'cache.sqlite'
    first=library.scan([root,root],cache)
    assert first['files']==3 and first['parsed']==3 and len(first['profiles'])==2
    rows=library.compatible(first['profiles'],{'camera':'Test Camera'})
    assert len(rows)==1 and len(rows[0]['paths'])==2
    assert library.load_selected(rows[0],{'camera':'Test Camera'})['name']=='Neutral'
    assert library.scan([root],cache)['parsed']==0
    profile(a,name='Changed');copy.unlink()
    with pytest.raises(ValueError,match='프로파일'):library.load_selected(rows[0],{'camera':'Test Camera'})
    second=library.scan([root],cache);assert second['parsed']==1 and second['files']==2
    assert library.compatible(second['profiles'],{'camera':'Test Camera'},'changed')[0]['name']=='Changed'
    a.unlink();assert not library.compatible(library.scan([root],cache)['profiles'],{'camera':'Test Camera'})
    cache.unlink()  # Windows must not retain a live SQLite file handle after a scan.


def test_index_unsupported_corrupt_oversized_and_favorites(tmp_path):
    root=tmp_path/'p';profile(root/'supported.dcp')
    profile(root/'newer.dcp',extra=[(52529,3,[21])])
    (root/'bad.dcp').write_bytes(b'not a DCP')
    with (root/'huge.dcp').open('wb') as f:f.truncate(dcp.MAX_PROFILE_BYTES+1)
    report=library.scan([root],tmp_path/'index.sqlite')
    assert len(report['profiles'])==4 and sum(bool(r['error']) for r in report['profiles'])==3
    row=next(r for r in report['profiles'] if not r['error'])
    assert library.compatible(report['profiles'],{'camera':'Test Camera'},favorites=[row['sha256']],only_favorites=True)==[row]
    assert not library.compatible([row],{'camera':'Other'})


def test_index_cancel_cap_and_restart(tmp_path):
    for i in range(4):profile(tmp_path/f'{i}.dcp',name=str(i))
    cache=tmp_path/'cache.sqlite';token=Event();token.set()
    assert library.scan([tmp_path],cache,token)['cancelled']
    partial=library.scan([tmp_path],cache,limit=2);assert partial['truncated'] and partial['files']==2
    full=library.scan([tmp_path],cache);assert full['files']==4 and full['parsed']==2


def test_load_selected_validates_camera_and_deleted_duplicate(tmp_path):
    a=profile(tmp_path/'a.dcp');b=tmp_path/'b.dcp';b.write_bytes(a.read_bytes())
    row=library.scan([tmp_path],tmp_path/'index.sqlite')['profiles'][0];a.unlink()
    assert library.load_selected(row,{'camera':'Test Camera'})
    with pytest.raises(ValueError,match='카메라'):library.load_selected(row,{'camera':'Other'})


INFO={'make':'Test','camera':'Test Camera','format':'NEF','iso':800,'serial':'00123'}


def test_raw_rules_priority_disabled_and_missing_serial():
    base=rd.make_rule({},dict(exposure=1),scope='master',groups=['빛'])
    model=rd.make_rule(INFO,dict(exposure=2),groups=['빛'])
    body=rd.make_rule(INFO,dict(exposure=3),scope='serial',groups=['빛'])
    rules=[base,model,body]
    assert rd.resolve(INFO,rules)[0]['exposure']==3
    assert rd.resolve({**INFO,'serial':'123'},rules)[0]['exposure']==2
    assert rd.resolve({**INFO,'serial':''},rules)[0]['exposure']==2
    assert rd.resolve({**INFO,'camera':'Other'},rules)[0]['exposure']==1
    body['enabled']=False;assert rd.resolve(INFO,rules)[0]['exposure']==2
    assert rd.resolve({**INFO,'format':'JPG'},rules)[0]==defaults()
    with pytest.raises(ValueError,match='일련번호'):rd.make_rule({**INFO,'serial':''},{},scope='serial')


def test_camera_keys_and_scope_replacement():
    a={'make':'NIKON CORPORATION','camera':'NIKON D3S'};b={'make':'Nikon','camera':'D3s'}
    assert rd.camera_key(a)==rd.camera_key(b)
    assert rd.camera_key(a)!=rd.camera_key({'make':'Other','camera':'D3S'})
    assert rd.make_rule(a,{})['id']==rd.make_rule(b,{'exposure':1})['id']


def test_rule_excludes_spatial_edits_and_pins_profile(tmp_path):
    path=profile(tmp_path/'p.dcp');record=dcp.load_profile(path)
    settings=defaults();settings.update(dcp_profile=record,raw_mode='sample',raw_kelvin=4567,raw_neutral=[1,2,3],
        crop=[.1,.1,.8,.8],masks=[{'bad':'must not copy'}],rotation=2,retouch=[{}],perspective_v=10)
    rule=rd.make_rule(INFO,settings);path.unlink()
    resolved,notes=rd.resolve(INFO,[rule])
    assert resolved['dcp_profile']==record and dcp.compiled(record)
    assert resolved['raw_mode']=='custom' and resolved['raw_neutral'] is None and resolved['raw_kelvin']==4567
    assert resolved['crop'] is None and not resolved['masks'] and resolved['rotation']==0 and resolved['perspective_v']==0
    assert notes
    master=rd.make_rule(INFO,settings,scope='master')
    other,notes=rd.resolve({**INFO,'camera':'Other Camera'},[master])
    # Built-in RAW development (0.5.58), the master rule, and the excluded DCP.
    assert other['dcp_profile'] is None and len(notes)==3 and notes[0]=='RAW 기본 현상'


def anchors():
    return rd.iso_anchors([{'path':'a.NEF','info':{**INFO,'iso':iso},'settings':{**defaults(),'noise_luma':noise,'sharpen':noise}}
                           for iso,noise in [(400,0),(1600,10),(6400,30)]])


@pytest.mark.parametrize('iso,expected',[(100,0),(400,0),(800,5),(1600,10),(3200,20),(6400,30),(12800,30)])
def test_iso_log_interpolation_and_clamped_endpoints(iso,expected):
    rule=rd.make_rule(INFO,{},groups=['디테일'],anchors=anchors())
    s,_=rd.resolve({**INFO,'iso':iso},[rule]);assert s['noise_luma']==expected and s['sharpen']==expected


@pytest.mark.parametrize('iso',[None,'not a value',0,-1,float('nan'),float('inf')])
def test_missing_iso_keeps_saved_detail(iso):
    rule=rd.make_rule(INFO,{'noise_luma':7},groups=['디테일'],anchors=anchors())
    s,notes=rd.resolve({**INFO,'iso':iso},[rule]);assert s['noise_luma']==7 and 'ISO가 없어' in notes[-1]


def test_iso_rejects_ambiguous_or_unrelated_photos():
    p=dict(path='a.NEF',info=INFO,settings=defaults());q=deepcopy(p)
    with pytest.raises(ValueError,match='서로 다른 ISO'):rd.iso_anchors([p,q])
    q['settings']['noise_luma']=30
    with pytest.raises(ValueError,match='서로 다른 보정값'):rd.iso_anchors([p,q])
    q['info']['camera']='Other'
    with pytest.raises(ValueError,match='같은 카메라'):rd.iso_anchors([p,q])


def test_manual_application_preserves_unselected_and_spatial():
    base={**defaults(),'exposure':2,'crop':[.1,.1,.8,.8],'rotation':2,'temperature':10}
    rule=rd.make_rule(INFO,{'noise_luma':30},groups=['디테일'])
    s,_=rd.resolve(INFO,[rule],base=base)
    assert s['noise_luma']==30 and s['crop']==base['crop'] and s['rotation']==2 and s['temperature']==10 and s['exposure']==2


def test_catalog_new_defaults_atomic_and_reimport_preserves_edits(tmp_path):
    catalog=Catalog(tmp_path/'catalog');rule=rd.make_rule(INFO,{'exposure':1},groups=['빛'])
    catalog.save_preference(rd.PREFERENCE,[rule]);s,_=rd.resolve(INFO,[rule])
    ident=catalog.add(tmp_path/'a.NEF',s,INFO);catalog.edit(ident,{**s,'exposure':2},'User edit')
    again=catalog.add(tmp_path/'a.NEF',defaults(),{})
    assert again==ident and catalog.photo(ident)['settings']['exposure']==2 and catalog.photo(ident)['info']==INFO
    catalog.close();catalog=Catalog(tmp_path/'catalog')
    assert catalog.preference(rd.PREFERENCE)==[rule] and len(catalog.histories(ident))==1
    catalog.close()


@pytest.mark.parametrize('extension',['jpg','png'])
def test_exif_body_serial_is_preserved_as_text(tmp_path,extension):
    from PIL import Image
    image=Image.new('RGB',(20,20));exif=Image.Exif();exif[271]='Test';exif[272]='Camera';exif[34665]={42033:'0012345',34855:800}
    path=tmp_path/f'photo.{extension}';image.save(path,exif=exif)
    info=read_metadata(path);assert info['serial']=='0012345' and info['iso']==800
