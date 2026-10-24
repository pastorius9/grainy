"""photo_service: the GUI-free operations shared by the menus and the AI connection (MCP)."""
import json
import math
import os
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PIL import Image

from luma import photo_service as service
from luma.catalog import Catalog
from luma.commands import EDIT_FIELDS,validate_proposal
from luma.engine import defaults,normalized
from luma.photo_service import FIELDS,ServiceError,Busy


@pytest.fixture
def catalog(tmp_path):
    value=Catalog(tmp_path/'data');yield value;value.close()


def photo(catalog,path,color=(120,130,140),size=(96,64),**settings):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    yy,xx=np.mgrid[:size[1],:size[0]]
    pixels=np.stack([xx*255//size[0],yy*255//size[1],np.full_like(xx,color[2])],-1).astype(np.uint8)
    Image.fromarray(pixels).save(path)
    return catalog.add(path,{**defaults(),**settings})


def frozen(catalog,ids):
    records,errors=service.freeze(catalog,ids);assert not errors;return records


# --- validation and composition -------------------------------------------------------------------

def test_parse_changes_accepts_every_field_at_its_limits():
    for key,(_,low,high) in FIELDS.items():
        if key in ('monochrome','flip'):
            assert service.parse_changes({key:True})==({key:True},{key:'set'});continue
        if key=='film_look':
            assert service.parse_changes({key:''})==({key:''},{key:'set'});continue      # names: test_film_look.py
        for value in (low,high):
            assert service.parse_changes({key:value})==({key:float(value)},{key:'set'})
        assert service.parse_changes(None,{key:high-low})==({key:float(high-low)},{key:'add'})
        assert service.parse_changes(None,{key:low-high})[0][key]==low-high


@pytest.mark.parametrize('set_values,add_values,message',[
    ({'exposure':True},None,'exposure must be a number'),
    ({'exposure':math.nan},None,'exposure must be a number'),
    ({'exposure':math.inf},None,'exposure must be a number'),
    ({'path':1},None,'path is not an edit value'),
    ({'exposure':5.01},None,'exposure must be between -5 and 5'),
    ({'sharpen':-1},None,'sharpen must be between 0 and 150'),
    (None,{'contrast':201},'contrast change must be between -200 and 200'),
    ({'exposure':1},{'exposure':1},'exposure is given in both set and add'),
    ({},{},'give at least one value'),
    (None,None,'give at least one value'),
    ({'exposure':'1'},None,'exposure must be a number')])
def test_parse_changes_rejects_bool_nan_unknown_out_of_range_and_duplicates(set_values,add_values,message):
    with pytest.raises(ServiceError,match=message):service.parse_changes(set_values,add_values)


@pytest.mark.parametrize('key,value',[(k,v) for k,(_,lo,hi) in EDIT_FIELDS.items() if k!='monochrome'
    for v in (lo,hi,(lo+hi)/2,lo-1,hi+1,True)]+[('monochrome',True),('monochrome',False),('monochrome',1)])
def test_parse_changes_agrees_with_codex_validation_for_set(key,value):
    """Absolute values the Codex command accepts are accepted here, and the same values are refused."""
    try:codex=validate_proposal({'message':'','action':'adjust','scope':'current',
                                 'changes':[{'key':key,'value':value,'operation':'set'}]})
    except ValueError:codex=None
    try:ours=service.parse_changes({key:value})
    except ServiceError:ours=None
    assert (codex is None)==(ours is None)
    if ours is not None:assert ours==(codex['changes'],codex['operations'])


def test_add_deltas_may_be_negative_for_zero_based_sliders():
    """Codex checks a relative change against the absolute range, which refuses sharpen add -20."""
    with pytest.raises(ValueError):validate_proposal({'message':'','action':'adjust','scope':'current',
        'changes':[{'key':'sharpen','value':-20,'operation':'add'}]})
    changes,operations=service.parse_changes(None,{'sharpen':-20,'grain':-100})
    assert changes=={'sharpen':-20.,'grain':-100.} and set(operations.values())=={'add'}
    result=service.edited({**defaults(),'sharpen':50.},changes,operations,{},1)
    assert result['sharpen']==30 and result['grain']==0


def test_monochrome_is_set_only_and_boolean():
    with pytest.raises(ServiceError,match='only be set'):service.parse_changes(None,{'monochrome':True})
    with pytest.raises(ServiceError,match='true or false'):service.parse_changes({'monochrome':1})
    changes,operations=service.parse_changes({'monochrome':True})
    assert service.edited(defaults(),changes,operations,{},1)['monochrome'] is True


def test_edited_clamps_relative_changes_per_photo():
    changes,operations=service.parse_changes({'contrast':10},{'exposure':.5,'whites':-300})
    for exposure,expected in ((0.,.5),(4.8,5.),(-5.,-4.5)):
        result=service.edited({**defaults(),'exposure':exposure,'whites':-150.},changes,operations,{},1)
        assert result['exposure']==pytest.approx(expected) and result['whites']==-200 and result['contrast']==10
        assert isinstance(result['contrast'],float)
    assert service.compose(defaults(),[service.parse_changes({'exposure':1}),service.parse_changes(None,{'exposure':.25})],{},1)['exposure']==1.25


def test_edited_moves_tone_version_2_to_3_like_the_slider():
    old={**defaults(),'tone_version':2,'shadows':10.}
    assert service.edited(old,*service.parse_changes(None,{'exposure':.1}),{},1)['tone_version']==2
    assert service.edited(old,*service.parse_changes(None,{'shadows':5}),{},1)['tone_version']==3
    assert service.edited(old,*service.parse_changes({'highlights':-20}),{},1)['tone_version']==3


@pytest.mark.parametrize('rotation',[0,1])
def test_edited_straighten_refits_crop_like_the_editor(rotation):
    from luma import straighten
    info={'width':600,'height':400};w,h=(400,600) if rotation else (600,400)
    settings={**defaults(),'rotation':rotation}
    result=service.edited(settings,*service.parse_changes({'straighten':6}),info,1)
    expected=straighten.refit(None,0.,6.,w,h,None)
    assert result['straighten']==6 and result['crop']==pytest.approx(expected)
    placed={**settings,'straighten':6.,'crop':[.1,.1,.6,.6]}
    again=service.edited(placed,*service.parse_changes(None,{'straighten':4}),info,1)
    assert again['crop']==pytest.approx(straighten.refit([.1,.1,.6,.6],6.,10.,w,h,None))
    upright=service.edited({**settings,'upright':{'mode':'level'}},*service.parse_changes({'straighten':6}),{},1)
    assert upright['crop'] is None


def test_edited_straighten_without_size_is_a_photo_error():
    with pytest.raises(ServiceError,match='photo 31: its size is not known yet; open it once in Grainy'):
        service.edited(defaults(),*service.parse_changes({'straighten':3}),{},31)


def test_negative_fields_need_an_inverted_negative():
    with pytest.raises(ServiceError,match='photo 4 is not an inverted negative'):
        service.edited(defaults(),*service.parse_changes(None,{'negative_density':.5}),{},4)
    result=service.edited({**defaults(),'negative':True},*service.parse_changes({'negative_range':8},{'negative_density':.5}),{},4)
    assert result['negative_range']==8 and result['negative_density']==.5


# --- targets --------------------------------------------------------------------------------------

def test_where_options_maps_filters_rejects_empty_and_recursive_without_folder():
    options=service.where_options({'text':'roll','folder':'D:/Film','recursive':True,'flag':'picked','rating_min':3,
                                   'label':'red','collection':4},sort='capture',descending=True)
    assert options['search']=='roll' and options['folder']=='D:/Film' and options['recursive'] is True
    assert options['mode']=='picked' and options['rules']=={'rating_min':3,'label':'빨강'} and options['collection']==4
    assert options['collapsed'] is False and options['stack_orders']=={} and options['sort']=='capture' and options['descending']
    assert service.where_options({'flag':'unflagged'})['rules']=={'flag':0}
    assert service.where_options({'folder':'x'})['recursive'] is False
    with pytest.raises(ServiceError,match='where needs at least one filter'):service.where_options({})
    assert service.where_options({},allow_empty=True)['rules']=={}
    with pytest.raises(ServiceError,match='recursive needs folder'):service.where_options({'recursive':True,'text':'a'})


def test_query_ids_filters_folder_rating_flag_label_text_collection(catalog,tmp_path):
    a=photo(catalog,tmp_path/'A'/'one.png');b=photo(catalog,tmp_path/'A'/'sub'/'two.png');c=photo(catalog,tmp_path/'B'/'three.png')
    catalog.update(a,rating=4,flag=1,label='빨강',keywords='가족, roll12');catalog.update(b,rating=2,flag=-1);catalog.update(c,rating=5)
    collection=catalog.add_collection('pick');catalog.collection_add(collection,[b,c])
    q=lambda where,**k:service.query_ids(catalog.directory,where,**k)
    assert q({'folder':str(tmp_path/'A')})==[a]
    assert q({'folder':str(tmp_path/'A'),'recursive':True})==[a,b]
    assert q({'rating_min':4})==[a,c] and q({'flag':'picked'})==[a] and q({'flag':'rejected'})==[b]
    assert q({'flag':'unflagged'})==[c] and q({'label':'red'})==[a]
    assert q({'text':'roll12'})==[a] and q({'text':'three'})==[c] and q({'text':'가족'})==[a]
    assert q({'collection':collection})==[b,c] and q({'collection':collection,'rating_min':5})==[c]
    assert q({},allow_empty=True)==[a,b,c] and q({},allow_empty=True,sort='rating',descending=True)==[c,a,b]
    cancel=Event();cancel.set()
    with pytest.raises(ServiceError,match='took too long'):service.query_ids(catalog.directory,{'rating_min':1},cancel=cancel)


def test_page_rows_are_compact_and_omit_defaults(catalog,tmp_path):
    a=photo(catalog,tmp_path/'A'/'one.png');b=photo(catalog,tmp_path/'B'/'two.png');c=photo(catalog,tmp_path/'A'/'gone.png')
    catalog.update(b,rating=3,flag=1,label='파랑');copy=catalog.virtual_copy(b)
    Path(catalog.photo(c)['path']).unlink()
    dirs,rows=service.page_rows(catalog.directory,[b,a,copy,c,999])
    assert dirs==[str(tmp_path/'B'),str(tmp_path/'A')]
    assert rows==[{'id':b,'name':'two.png','r':3,'f':1,'l':'blue'},{'id':a,'name':'one.png','d':1},
                  {'id':copy,'name':'two.png · 사본','r':3,'f':1,'l':'blue','v':1},{'id':c,'name':'gone.png','d':1,'x':1}]
    assert service.folder_rows(catalog.directory)==[{'path':str(tmp_path/'A'),'n':2},{'path':str(tmp_path/'B'),'n':2}]
    assert service.folder_rows(catalog.directory,str(tmp_path/'B'))==[{'path':str(tmp_path/'B'),'n':2}]


# --- summaries ------------------------------------------------------------------------------------

def test_summary_lists_only_non_default_values_and_classifies_every_default_key():
    grouped={key for keys in service.GROUPS.values() for key in keys}
    for key in defaults():
        assert key in FIELDS or key in service.CURVES or key in grouped or key in service.COUNTED or key in service.IGNORED,key
    settings={**defaults(),'exposure':.304,'contrast':12.,'monochrome':True,'crop':[.1,.1,.9,.9],
              'masks':[{'a':1},{'b':2}],'hsl':[[1.,0.,0.]]+[[0.,0.,0.]]*7,'negative':True,'tone_version':1}
    record={'id':12,'name':'R12_01.NEF','path':'D:/Film/R12_01.NEF','rating':3,'flag':1,'label':'빨강','keywords':'롤12',
            'settings':normalized(settings),'info':{'width':6048,'height':4024,'format':'NEF','camera':'Z 6','iso':400.,
            'aperture':2.8,'shutter':'1/250','focal_length':50.,'date':'2026:10:01 10:00:00','lens':'50mm'}}
    result=service.summary(record)
    assert result['edits']=={'exposure':.3,'contrast':12,'monochrome':True}
    assert result['other']==['crop','color_mix','negative','masks:2']
    assert result['size']==[6048,4024] and result['iso']==400 and result['focal']==50 and result['aperture']==2.8
    assert result['r']==3 and result['f']==1 and result['l']=='red' and result['keywords']=='롤12'
    plain=service.summary({**record,'settings':defaults(),'rating':0,'flag':0,'label':'','keywords':'','info':{}})
    assert plain=={'id':12,'name':'R12_01.NEF','path':'D:/Film/R12_01.NEF'}


def test_summary_never_returns_gps_serial_or_other_metadata():
    info={'width':10,'height':8,'latitude':37.5,'longitude':127.,'serial':'SN123','creator':'Owner','copyright':'(c)',
          'make':'Nikon','maker_note':'x','subject_distance':3.,'camera':'Z 6'}
    result=service.summary({'id':1,'name':'a','path':'p','settings':defaults(),'info':info})
    text=json.dumps(result)
    for value in ('37.5','127','SN123','Owner','(c)','Nikon','maker','subject'):assert value not in text
    assert set(result)=={'id','name','path','size','camera'}


def test_fmt_rounds_and_keeps_integers_integral():
    assert service.fmt(.304)==.3 and service.fmt(12.)==12 and isinstance(service.fmt(12.),int)
    assert service.fmt(True) is True and service.fmt(False) is False
    assert service.fmt(-0.004)==0 and isinstance(service.fmt(-0.004),int) and service.fmt(2.346)==2.35 and service.fmt(7)==7


# --- batch stack ----------------------------------------------------------------------------------

def batch(catalog,ids,change,snapshotted=None,tool='apply_edits'):
    records=frozen(catalog,ids)
    values=[service.compose(r['settings'],[service.parse_changes(None,change)],r['info'],r['id']) for r in records]
    return service.apply_batch(catalog,records,values,label='AI 보정',tool=tool,snapshot_name='before AI',
                               snapshotted=set() if snapshotted is None else snapshotted)


def test_apply_batch_index_depth_drops_oldest_and_its_key(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/f'{n}.png') for n in range(2)]
    made=[batch(catalog,ids,{'exposure':.1})[0] for _ in range(service.MCP_DEPTH+1)]
    index=service.batch_index(catalog)
    assert [e['id'] for e in index]==made[1:] and all(e['state']=='applied' and e['count']==2 for e in index)
    assert catalog.preference(service.BATCH_PREFIX+made[0]) is None
    assert all(catalog.preference(service.BATCH_PREFIX+b)['id']==b for b in made[1:])
    assert all(len(b)==7 and b.startswith('b') for b in made) and len(set(made))==len(made)
    assert service.last_batch(catalog)['id']==made[-1] and index[-1]['tool']=='apply_edits' and index[-1]['label']=='AI 보정'


def test_apply_batch_adds_one_pre_ai_snapshot_per_photo_and_run(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/f'{n}.png',exposure=n*.1) for n in range(2)]
    before={i:catalog.photo(i)['settings'] for i in ids};snapshotted=set()
    batch(catalog,ids,{'exposure':.2},snapshotted)
    assert snapshotted==set(ids)
    for ident in ids:
        (snap,)=catalog.snapshots(ident)
        assert snap['name']=='before AI' and normalized(snap['settings'])==before[ident]
    batch(catalog,ids,{'exposure':.2},snapshotted)
    assert all(len(catalog.snapshots(i))==1 for i in ids)
    third=photo(catalog,tmp_path/'third.png')
    catalog.db.execute(f"CREATE TRIGGER fail_batch BEFORE UPDATE OF edits ON photos WHEN NEW.id={third} BEGIN SELECT RAISE(ABORT,'injected'); END;")
    with pytest.raises(Exception,match='injected'):batch(catalog,[ids[0],third],{'contrast':5},snapshotted)
    assert snapshotted==set(ids) and not catalog.snapshots(third) and len(service.batch_index(catalog))==2


def test_undo_batch_walks_back_and_redo_forward(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/f'{n}.png') for n in range(2)]
    first,_=batch(catalog,ids,{'exposure':.5});second,_=batch(catalog,ids[:1],{'contrast':20},tool='auto_tone')
    assert service.undo_batch(catalog)==(second,'auto_tone',ids[:1])
    assert catalog.photo(ids[0])['settings']['contrast']==0 and catalog.photo(ids[0])['settings']['exposure']==.5
    assert service.undo_batch(catalog)==(first,'apply_edits',ids)
    assert all(catalog.photo(i)['settings']['exposure']==0 for i in ids)
    with pytest.raises(ServiceError,match='there is no batch to undo'):service.undo_batch(catalog)
    assert service.undo_batch(catalog,redo=True)==(first,'apply_edits',ids)
    assert service.undo_batch(catalog,redo=True)[0]==second and catalog.photo(ids[0])['settings']['contrast']==20
    with pytest.raises(ServiceError,match='there is no batch to redo'):service.undo_batch(catalog,redo=True)


@pytest.mark.parametrize('mutation',['edit','remove','undo'])
def test_undo_batch_refuses_after_member_edit_or_removal_in_english(catalog,tmp_path,mutation):
    ids=[photo(catalog,tmp_path/f'{n}.png') for n in range(2)];bid,_=batch(catalog,ids,{'exposure':.5})
    if mutation=='edit':catalog.edit(ids[1],{**catalog.photo(ids[1])['settings'],'exposure':1})
    elif mutation=='remove':catalog.remove_photos([ids[1]])
    else:catalog.save_preference(f'undo:{ids[1]}',{'undo':[],'redo':[]})
    with pytest.raises(ServiceError) as caught:service.undo_batch(catalog)
    assert str(caught.value)==(f'{bid} can no longer be undone: some of its photos were edited or removed afterwards. '
                               'Undo those photos one by one in Grainy instead.')
    assert catalog.photo(ids[0])['settings']['exposure']==.5 and service.last_batch(catalog)['id']==bid


def test_undo_batch_checks_expected_id(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/'a.png')];first,_=batch(catalog,ids,{'exposure':.1});second,_=batch(catalog,ids,{'exposure':.1})
    with pytest.raises(ServiceError,match=f'{first} is not the most recent batch; the most recent is {second} \\(apply_edits\\)'):
        service.undo_batch(catalog,first)
    assert service.undo_batch(catalog,second)[0]==second
    assert service.undo_batch(catalog,second,redo=True)[0]==second


def test_new_batch_after_undo_drops_redo(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/'a.png')];first,_=batch(catalog,ids,{'exposure':.1});second,_=batch(catalog,ids,{'exposure':.1})
    service.undo_batch(catalog);third,_=batch(catalog,ids,{'contrast':4})
    assert [e['id'] for e in service.batch_index(catalog)]==[first,third]
    assert catalog.preference(service.BATCH_PREFIX+second) is None
    with pytest.raises(ServiceError,match='no batch to redo'):service.undo_batch(catalog,redo=True)


def test_noop_batch_returns_no_id_and_keeps_index(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/'a.png')];first,_=batch(catalog,ids,{'exposure':.1})
    records=frozen(catalog,ids)
    assert service.apply_batch(catalog,records,[r['settings'] for r in records],label='AI 보정',tool='apply_edits',
                               snapshot_name='x',snapshotted=set())==(None,[])
    assert [e['id'] for e in service.batch_index(catalog)]==[first]


def test_open_transaction_is_busy_not_korean_runtime_error(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/'a.png')];batch(catalog,ids,{'exposure':.1});records=frozen(catalog,ids)
    catalog.db.execute('BEGIN')
    try:
        with pytest.raises(Busy,match='Grainy is saving something else'):
            service.apply_batch(catalog,records,[{**records[0]['settings'],'exposure':2.}],label='AI 보정',tool='t',snapshot_name='x',snapshotted=set())
        with pytest.raises(Busy,match='Grainy is saving something else'):service.undo_batch(catalog)
    finally:catalog.db.rollback()


def test_mcp_batch_profile_references_survive_maintenance(catalog,tmp_path):
    from test_rawcolor import make_dcp,record
    from luma.catalog_maintenance import optimize
    from luma.profile_store import verify_references
    profile=record(make_dcp());values={**defaults(),'dcp_profile':profile}
    ids=[catalog.add(tmp_path/f'{i}.nef',values) for i in range(2)]
    bid,_=batch(catalog,ids,{'exposure':.3})
    raw=catalog.db.execute('SELECT value FROM preferences WHERE key=?',(service.BATCH_PREFIX+bid,)).fetchone()[0]
    assert '"data":' not in raw and 'data_ref' in raw
    snapshot=catalog.db.execute('SELECT edits FROM snapshots').fetchone()[0];assert '"data":' not in snapshot
    assert len(verify_references(catalog.db))==1
    optimize(catalog.directory)
    service.undo_batch(catalog)
    assert all(catalog.photo(i)['settings']['dcp_profile']==profile and catalog.photo(i)['settings']['exposure']==0 for i in ids)
    service.undo_batch(catalog,redo=True);assert all(catalog.photo(i)['settings']['exposure']==.3 for i in ids)


# --- automatic values -----------------------------------------------------------------------------

def test_auto_values_strict_raises_and_lenient_collects_reasons(catalog,tmp_path):
    from luma.engine import auto_tone_settings
    ids=[photo(catalog,tmp_path/f'{n}.png',size=(160,100)) for n in range(3)]
    records=frozen(catalog,ids);Path(records[1]['path']).unlink()
    compute=lambda source,settings:auto_tone_settings(source,settings)[0]
    with pytest.raises(Exception):service.auto_values(records,compute,catalog.directory,strict=True)
    seen=[]
    values,errors=service.auto_values(records,compute,catalog.directory,strict=False,progress=seen.append)
    assert values[1] is None and values[0] is not None and values[2] is not None and seen==[1,2,3]
    assert errors==[{'id':ids[1],'error':'original missing and no offline preview'}]
    cancel=Event();cancel.set()
    assert service.auto_values(records,compute,catalog.directory,cancel=cancel,strict=False)==(None,[])
    assert service.reason(ValueError('x'),records[0]['path'])=='could not read or compute (ValueError)'
    assert service.reason(service.OriginalChanged('x'),records[0]['path'])=='the original changed while it was read'


# --- export ---------------------------------------------------------------------------------------

def test_export_records_names_collisions_and_signatures_match_previous_behaviour(catalog,tmp_path):
    from luma.workflow_data import edit_signature
    a=photo(catalog,tmp_path/'A'/'roll.png');b=photo(catalog,tmp_path/'B'/'roll.png',exposure=.4)
    out=tmp_path/'out';out.mkdir();(out/'roll_grainy.jpg').write_bytes(b'taken')
    records=[catalog.photo(a),catalog.photo(b)];done=[]
    result=service.export_records(records,out,'JPEG',80,0,progress=done.append)
    assert [Path(o['path']).name for o in result['outputs']]==['roll_grainy_2.jpg','roll_grainy_3.jpg']
    assert (out/'roll_grainy.jpg').read_bytes()==b'taken' and result['failures']==[] and result['skipped']==0
    assert [o['edit_signature'] for o in result['outputs']]==[edit_signature(r['settings']) for r in records]
    assert sorted(done)==[1,2] and [o['photo_id'] for o in result['outputs']]==[a,b]
    numbered=service.export_records(records,out,'PNG',naming='R12_{n:03d}')
    assert [Path(o['path']).name for o in numbered['outputs']]==['R12_001.png','R12_002.png']


def test_export_records_cancel_skips_remaining(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/f'{n}.png') for n in range(3)];out=tmp_path/'out';out.mkdir()
    cancel=Event();cancel.set()
    result=service.export_records([catalog.photo(i) for i in ids],out,cancel=cancel)
    assert result=={'outputs':[],'failures':[],'skipped':3} and not list(out.iterdir())


@pytest.mark.parametrize('name',['..','.','a/b','a\\b','CON','con.txt','LPT1','x:','end.','end ','',' '*3,'tab\there','a'*81,'q?','<x>'])
def test_export_folder_accepts_one_segment_only(tmp_path,name):
    with pytest.raises(ServiceError):service.export_folder(tmp_path,name)


def test_export_folder_makes_the_folder_inside_the_root(tmp_path):
    assert service.export_folder(tmp_path/'Pictures'/'Grainy','Roll 12')==(tmp_path/'Pictures'/'Grainy'/'Roll 12').resolve()
    assert (tmp_path/'Pictures'/'Grainy'/'Roll 12').is_dir()
    assert service.export_folder(tmp_path/'root',None)==(tmp_path/'root').resolve() and (tmp_path/'root').is_dir()


@pytest.mark.parametrize('naming,ok',[('{stem}_grainy',True),('R12_{n:03d}',True),('{stem}-{n}',True),('{n:06d}',True),
    ('사진_{stem}',True),('{n:>999999999}',False),('{stem.__class__}',False),('{0}',False),('{}',False),('a/b',False),
    ('x:',False),('end.',False),('end ',False),('bad\x07',False),('a'*81,False),('{n:07d}',False),('{n:3d}',False),('CON',False)])
def test_check_naming_whitelist(naming,ok):
    if ok:service.check_naming(naming)
    else:
        with pytest.raises(ServiceError,match='naming may contain only'):service.check_naming(naming)


def test_rotation_and_flip_turn_as_the_editor_does():
    from luma.engine import defaults
    base={**defaults(),'crop':[.1,.1,.9,.9]}
    turned=service.edited(base,*service.parse_changes({'rotation':90}),{},1)
    assert turned['rotation']==1 and turned['crop'] is None                      # a quarter turn clockwise; the crop starts over
    back=service.edited(turned,*service.parse_changes(None,{'rotation':-90}),{},1)
    assert back['rotation']==0
    assert service.edited({**base,'rotation':3},*service.parse_changes(None,{'rotation':180}),{},1)['rotation']==1
    same=service.edited(base,*service.parse_changes({'rotation':0}),{},1)
    assert same['crop']==[.1,.1,.9,.9]                                           # no turn: the crop stays
    assert service.edited(base,*service.parse_changes({'flip':True}),{},1)['flip'] is True
    for bad in ({'rotation':45},{'rotation':360},{'flip':1}):
        with pytest.raises(service.ServiceError):service.parse_changes(bad)
    with pytest.raises(service.ServiceError):service.parse_changes(None,{'flip':True})
    record={'id':1,'name':'a.jpg','path':'D:/a.jpg','settings':{**defaults(),'rotation':3,'flip':True}}
    assert service.summary(record)['edits']=={'rotation':270,'flip':True}


# --- curves, presets and copied settings ----------------------------------------------------------

def test_curves_are_set_only_point_lists_and_reach_get_photo():
    curve=[[0,0],[.25,.2],[.75,.8],[1,1]]
    changes,operations=service.parse_changes({'curve':curve,'rgb_curves':[curve,[[0,0],[1,.9]],[[0,.1],[1,1]]]})
    assert changes['curve']==[[0.,0.],[.25,.2],[.75,.8],[1.,1.]] and operations=={'curve':'set','rgb_curves':'set'}
    result=service.edited({**defaults(),'panels_off':['curve','effects']},changes,operations,{},1)
    assert result['curve']==changes['curve'] and result['rgb_curves'][1]==[[0.,0.],[1.,.9]]
    assert result['panels_off']==['effects']                     # an edit in a switched-off panel switches it on
    edits=service.summary({'id':1,'name':'a','path':'p','settings':result})['edits']
    assert edits=={'curve':[[0,0],[.25,.2],[.75,.8],[1,1]],'rgb_curves':result['rgb_curves']}
    for bad in ([[0,0]],[[0,0],[.5,.5]],[[.1,0],[1,1]],[[0,0],[.5,.5],[.5,.6],[1,1]],[[0,0],[1,1.2]],[[0,0],[1,True]],
                [[0,0,0],[1,1]],[[0,0]]*17,'x'):
        with pytest.raises(ServiceError,match='curve must be 2 to 16'):service.parse_changes({'curve':bad})
    with pytest.raises(ServiceError,match='rgb_curves must be 3 curves'):service.parse_changes({'rgb_curves':[curve]})
    with pytest.raises(ServiceError,match=r'rgb_curves\[2\] must be'):service.parse_changes({'rgb_curves':[curve,curve,[[0,0]]]})
    with pytest.raises(ServiceError,match='curve can only be set'):service.parse_changes(None,{'curve':curve})


def test_unknown_film_without_close_names_points_to_list_films():
    with pytest.raises(ServiceError,match="no film named 'zzzz qqqq'; call list_films for the film names"):
        service.parse_changes({'film_look':'zzzz qqqq'})


def test_preset_settings_apply_like_the_presets_panel_and_keep_the_framing():
    base={**defaults(),'crop':[.1,.1,.9,.9],'rotation':1,'straighten':2.,'flip':True,'exposure':1.}
    preset=normalized({'contrast':12,'exposure':.15})
    result=service.preset_settings(preset,base,{'path':'D:/a.jpg','info':{'format':'JPEG'}})
    assert result['contrast']==12 and result['exposure']==.15
    assert [result[k] for k in service.GEOMETRY]==[[.1,.1,.9,.9],1,2.,True]


def test_copied_settings_paste_like_the_editor_with_its_groups():
    from luma.manager import EDIT_GROUPS
    assert set(service.COPY_GROUPS.values())==set(EDIT_GROUPS)-{'HDR'}
    every=service.copy_keys()
    assert every==set(defaults())-set(EDIT_GROUPS['크롭·회전'])-{'panels_off'}      # LibraryManager's default copy items
    assert service.copy_keys(['curves','light'])==set(EDIT_GROUPS['커브'])|set(EDIT_GROUPS['빛'])
    curve=[[0.,0.],[.5,.6],[1.,1.]]
    source={**defaults(),'exposure':.7,'curve':curve,'crop':[.2,.2,.8,.8],'panels_off':['curve'],'temperature':9.}
    target={**defaults(),'contrast':5.,'crop':[.1,.1,.9,.9]};info={'format':'JPEG'}
    pasted=service.copied_settings(source,target,info,every)
    assert pasted['exposure']==.7 and pasted['curve']==curve and pasted['contrast']==0 and pasted['crop']==[.1,.1,.9,.9]
    assert pasted['panels_off']==['curve'] and pasted['temperature']==9      # a pasted panel keeps the source's off state
    only=service.copied_settings(source,target,info,service.copy_keys(['curves']))
    assert only['curve']==curve and only['exposure']==0 and only['contrast']==5 and only['panels_off']==['curve']
    framed=service.copied_settings(source,target,info,service.copy_keys(['crop']))
    assert framed['crop']==[.2,.2,.8,.8] and framed['exposure']==0 and framed['panels_off']==[]


def test_batch_remembers_the_preset_and_undo_and_redo_set_it(catalog,tmp_path):
    ids=[photo(catalog,tmp_path/f'{n}.png') for n in range(2)]+[photo(catalog,tmp_path/'off.png',panels_off=['basic'])]
    records=frozen(catalog,ids)
    values=[{**r['settings'],'exposure':.4} for r in records]
    service.apply_batch(catalog,records,values,label='AI 보정',tool='apply_edits',snapshot_name='x',snapshotted=set(),
                        switch=False,presets={ids[0]:'Soft daylight'})
    assert catalog.photo(ids[0])['user_metadata'][service.PRESET_KEY]=='Soft daylight'
    assert service.PRESET_KEY not in catalog.photo(ids[1])['user_metadata']
    assert catalog.photo(ids[2])['settings']['panels_off']==['basic']           # switch=False: the values bring their panels
    assert service.name_values(catalog.photo(ids[0]))['preset']=='Soft daylight'
    service.undo_batch(catalog)
    assert service.PRESET_KEY not in catalog.photo(ids[0])['user_metadata'] and catalog.photo(ids[0])['settings']['exposure']==0
    service.undo_batch(catalog,redo=True)
    assert catalog.photo(ids[0])['user_metadata'][service.PRESET_KEY]=='Soft daylight'


# --- export naming, sizes and folders -------------------------------------------------------------

@pytest.mark.parametrize('naming,values,expected',[
    ('{stem}_{film}',{'film':'Kodak Portra 400'},'roll_Kodak Portra 400'),('{stem}_{film}',{},'roll'),
    ('{film}_{stem}',{},'roll'),('{date}-{camera}-{n:03d}',{'date':'2026-10-01'},'2026-10-01-007'),
    ('{film} {preset}',{},'roll'),('{film}_{preset}_{stem}',{'preset':'Soft'},'Soft_roll'),('{stem}',{},'roll'),
    ('{camera}.{date}',{'camera':'Z 6'},'Z 6'),('R12_{n:02d}',{},'R12_07')])
def test_file_stem_drops_an_empty_token_with_one_separator(naming,values,expected):
    assert service.file_stem(naming,'roll',7,values)==expected


def test_name_values_are_file_safe_and_empty_when_unknown():
    from luma import film_look
    film=film_look.find('Kodak Portra 400')[0]
    record={'settings':{**defaults(),'film_look':film},'info':{'camera':'X/Y:Z','date':'2026:10:01 10:00:00'},
            'user_metadata':{service.PRESET_KEY:'a/b'}}
    assert service.name_values(record)=={'film':'Kodak Portra 400','preset':'a_b','camera':'X_Y_Z','date':'2026-10-01'}
    shifted={**record,'user_metadata':{'DateTimeOriginal':'2025:01:02 03:04:05'}}
    assert service.name_values(shifted)['date']=='2025-01-02' and service.name_values(shifted)['preset']==''
    assert service.name_values({'settings':defaults(),'info':{},'user_metadata':{}})=={'film':'','preset':'','camera':'','date':''}


@pytest.mark.parametrize('naming,ok',[('{stem}_{film}',True),('{date}_{camera}_{preset}_{n:03d}',True),('{film}',True),
    ('{films}',False),('{date:%Y}',False),('{camera.x}',False),('{preset!r}',False)])
def test_check_naming_takes_the_metadata_tokens(naming,ok):
    if ok:service.check_naming(naming)
    else:
        with pytest.raises(ServiceError,match='naming may contain only'):service.check_naming(naming)


@pytest.mark.parametrize('box,expected',[((1080,0,'inside'),1080),((0,1350,'inside'),2025),((1080,1080,'inside'),1080),
    ((1080,1080,'cover'),1620),((8000,0,'inside'),6000),((0,0,'inside'),6000),((1000,1000,'cover'),1500),(2000,2000)])
def test_export_longest_fits_a_box_and_never_enlarges(box,expected):
    from luma.engine import export_longest
    assert export_longest((4000,6000,3),box)==expected


def test_export_longest_keeps_the_box_after_rounding():
    from luma.engine import export_longest
    for h,w in ((2999,4001),(4000,6000),(1001,3333),(5472,3648),(37,4999)):
        for limits in ((1000,0),(0,1000),(1001,999),(333,777),(7,5)):
            for fit in ('inside','cover'):
                longest=export_longest((h,w,3),(*limits,fit));factor=longest/max(w,h)
                size=(max(1,round(w*factor)),max(1,round(h*factor)))
                bounded=[(side,limit) for side,limit in zip(size,limits) if limit]
                if fit=='inside':assert all(side<=limit for side,limit in bounded),(h,w,limits,size)
                elif factor<1:assert all(side>=limit for side,limit in bounded),(h,w,limits,size)


def test_export_records_fill_tokens_and_fit_a_box(catalog,tmp_path):
    from luma import film_look
    a=photo(catalog,tmp_path/'A'/'roll.png',size=(96,64),film_look=film_look.find('Kodak Portra 400')[0])
    out=tmp_path/'out';out.mkdir()
    result=service.export_records([catalog.photo(a)],out,'PNG',longest=(48,40,'inside'),naming='{stem}_{film}_{camera}')
    path=Path(result['outputs'][0]['path'])
    assert result['failures']==[] and path.name=='roll_Kodak Portra 400.png'
    with Image.open(path) as image:assert image.size==(48,32)
    cover=service.export_records([catalog.photo(a)],out,'PNG',longest=(40,40,'cover'),naming='{stem}')
    with Image.open(cover['outputs'][0]['path']) as image:assert image.size==(60,40)


def test_named_folder_needs_an_absolute_path_outside_grainys_own_folders(tmp_path):
    own=[(tmp_path/'library').resolve(),(tmp_path/'mcp').resolve()]
    assert service.named_folder(str(tmp_path/'out'/'new'),own)==(tmp_path/'out'/'new').resolve()
    for bad in ('relative/folder','.','','   ','\\no drive' if os.name=='nt' else 'no/slash'):
        with pytest.raises(ServiceError,match='path must be'):service.named_folder(bad,own)
    for inside in (tmp_path/'library',tmp_path/'library'/'thumbnails',tmp_path/'mcp'/'..'/'library'/'x'):
        with pytest.raises(ServiceError,match='a folder Grainy manages itself'):service.named_folder(str(inside),own)
    with pytest.raises(ServiceError,match='a folder Grainy manages itself'):service.export_folder(tmp_path,'library',own)
    assert not (tmp_path/'library').exists()


def test_image_files_lists_what_the_import_takes(tmp_path):
    for name in ('a.jpg','b.PNG','notes.txt','sub/c.tif','sub/deep/d.nef'):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'x')
    names=lambda files:[Path(f).relative_to(tmp_path).as_posix() for f in files]
    assert names(service.image_files(tmp_path,False,Event(),10))==['a.jpg','b.PNG']
    assert names(service.image_files(tmp_path,True,Event(),10))==['a.jpg','b.PNG','sub/c.tif','sub/deep/d.nef']
    assert service.image_files(tmp_path,True,Event(),3)['error'].startswith('more than 3 image files')
    cancel=Event();cancel.set();assert service.image_files(tmp_path,True,cancel,10) is None
