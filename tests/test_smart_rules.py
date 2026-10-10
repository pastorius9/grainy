from datetime import datetime
import json
import os
from threading import Event
import pytest
from luma.smart_rules import KEY,translate,evaluate,sql_condition,validate,time_dependent
from luma.lua_data import DataError
from luma.library_query import query
from luma.catalog import Catalog


def photo(**values):
    return dict(path='C:/photos/서울/Trip001.NEF',name='Trip001.NEF',rating=4,flag=1,label='빨강',
                keywords='여행|서울, 봄꽃',metadata='{}',extras=json.dumps(values))


def rule(criteria,operation,value=None,**kwargs):
    return validate(dict(criteria=criteria,operation=operation,value=value,**kwargs))


def test_nested_conditions_exclude_and_whole_rule_rejection():
    rules=translate('''s={ {criteria="rating",operation=">=",value=4},
      { {criteria="keywords",operation="any",value="바다 서울"},
        {criteria="fileFormat",operation="==",value="JPG"},combine="union" },
      { {criteria="labelColor",operation="==",value=2},combine="exclude" }, combine="intersect" }''')
    assert evaluate(rules[KEY],photo())
    assert not evaluate(rules[KEY],{**photo(),'label':'노랑'})
    for text in ('s={{criteria="rating",operation=">",value=3},{criteria="unknown",operation="==",value=1}}',
                 's={{criteria="rating",operation=">",value=3,extra=true}}'):
        with pytest.raises(DataError):translate(text)
    assert sql_condition(rules[KEY]) is None


@pytest.mark.parametrize('criteria,operation,value,extra,expected',[
    ('filename','beginsWith','trip',{},True),('folder','endsWith','서울',{},True),
    ('keywords','all','여행 서울',{},True),('keywords','words','서울',{},True),
    ('keywords','words','울',{},False),('keywords','noneOf','부산 바다',{},True),
    ('labelText','==','red',{},True),('labelColor','==','none',{},False),
    ('labelColor','==',1,{},True),('labelColor','!=','custom',{},True),
    ('fileFormat','==','RAW',{},True),('fileFormat','==','DNG',{},False),
    ('hasGPSData','isTrue',None,{'latitude':0,'longitude':0},True),
    ('hasGPSData','isFalse',None,{'latitude':100,'longitude':0},True),
    ('isoSpeedRating','>=',800,{'iso':'800'},True),
    ('cameraSN','==','abc-123',{'serial':'ABC-123'},True),
    ('copyname','==','흑백',{'_copy_name':'흑백'},True),
])
def test_metadata_comparisons(criteria,operation,value,extra,expected):
    assert evaluate(rule(criteria,operation,value),photo(**extra)) is expected


def test_fixed_and_relative_dates_leap_month_and_query_clock():
    now=datetime(2024,3,31,12)
    last_month=rule('captureTime','inLast',1,value_units='months')
    assert evaluate(last_month,photo(DateTimeOriginal='2024:02:29 01:02:03'),now=now)
    assert not evaluate(last_month,photo(DateTimeOriginal='2024:02:28 23:59:59'),now=now)
    assert not evaluate(last_month,photo(DateTimeOriginal='2024:04:01 00:00:00'),now=now)
    assert evaluate(rule('captureTime','in','2024-02-28',value2='2024-02-29'),photo(date='2024-02-29T11:22:33'),now=now)
    assert evaluate(rule('touchTime','inLast',2,value_units='hours'),photo(_last_edit_time='2024-03-31T10:00:00'),now=now)
    assert not evaluate(rule('touchTime','inLast',2,value_units='hours'),photo(_last_edit_time='2024-03-31T09:59:59'),now=now)
    assert evaluate(rule('captureTime','notInLast',1,value_units='months'),photo(date='2024-01-01'),now=now)
    assert not evaluate(rule('captureTime','notInLast',1,value_units='months'),photo(),now=now)
    assert time_dependent({KEY:last_month}) and not time_dependent({KEY:rule('rating','>=',4)})


@pytest.mark.parametrize('leaf',[
    dict(criteria='rating',operation='==',value=True),
    dict(criteria='rating',operation='in',value=5,value2=2),
    dict(criteria='labelColor',operation='==',value=0),
    dict(criteria='fileFormat',operation='==',value={}),
    dict(criteria='captureTime',operation='==',value='2024-02-30'),
    dict(criteria='captureTime',operation='inLast',value=1.5,value_units='days'),
])
def test_invalid_rule_types_and_values_rejected(leaf):
    with pytest.raises(ValueError):validate(leaf)


def test_live_collection_native_sql_matches_python_and_preserves_additional_filters(tmp_path,monkeypatch):
    import luma.library_query as queries
    cat=Catalog(tmp_path/'catalog')
    try:
        ids=[]
        for i in range(42):
            ident=cat.add(tmp_path/f'photo{i}.jpg');ids.append(ident)
            cat.update(ident,rating=i%6,flag=i%3-1,label=['','빨강','노랑','custom'][i%4])
        descriptors=[translate('s={{criteria="rating",operation=">=",value=4},{criteria="labelColor",operation="==",value="custom"},combine="union"}'),
            translate('s={{criteria="pick",operation="in",value=-1,value2=0},{criteria="labelColor",operation="==",value=1},combine="exclude"}')]
        def must_not_use_python(*args,**kwargs):raise AssertionError('common rule should run in SQLite')
        original=queries.matches
        for rules in descriptors:
            expected=[p['id'] for p in cat.photos() if evaluate(rules[KEY],p)]
            collection=cat.add_collection('Smart',rules)
            monkeypatch.setattr(queries,'matches',must_not_use_python)
            assert query(cat.directory/'catalog.sqlite',{'collection':collection},Event())['ids']==expected
        monkeypatch.setattr(queries,'matches',original)
        rules=translate('s={{criteria="rating",operation=">=",value=5}}')
        collection=cat.add_collection('Five',rules)
        assert ids[0] not in query(cat.directory/'catalog.sqlite',{'collection':collection},Event())['ids']
        cat.update(ids[0],rating=5)
        assert ids[0] in query(cat.directory/'catalog.sqlite',{'collection':collection},Event())['ids']
        rules['name']='photo0'
        assert query(cat.directory/'catalog.sqlite',{'rules':rules},Event())['ids']==[ids[0]]
    finally:cat.close()


def test_filter_dialog_keeps_complex_conditions_until_explicitly_removed():
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from luma.manager import FilterDialog
    app=QApplication.instance() or QApplication([])
    original=translate('s={{criteria="keywords",operation="empty"},{criteria="rating",operation=">=",value=4},combine="union"}')
    dialog=FilterDialog(None,original)
    assert dialog.rules()==original
    dialog.fields['camera'].setText('Nikon')
    assert dialog.rules()=={**original,'camera':'Nikon'}
    dialog.keep_complex.setChecked(False)
    assert dialog.rules()=={'camera':'Nikon'}
    dialog.close()


def test_edit_timestamp_records_real_edits_only(tmp_path):
    cat=Catalog(tmp_path/'catalog')
    try:
        ident=cat.add(tmp_path/'test.jpg')
        cat.edit(ident,{})
        assert '_last_edit_time' not in cat.photo(ident)['user_metadata']
        cat.edit(ident,{'exposure':1})
        first=cat.photo(ident)['user_metadata']['_last_edit_time']
        assert datetime.fromisoformat(first).tzinfo is not None
        cat.set_settings(ident,{'exposure':0})
        assert cat.photo(ident)['user_metadata']['_last_edit_time']>=first
    finally:cat.close()
