import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3
from threading import Event
import pytest
from luma.catalog import Catalog
from luma.engine import normalized
from luma import dcp, preview_store
from luma.catalog_maintenance import optimize
from luma.profile_store import ProfileCodec, verify_references
from luma.library import restore_backup, write_sidecar, read_sidecar
from test_rawcolor import make_dcp, record


@pytest.fixture
def catalog(tmp_path):
    cat=Catalog(tmp_path/'catalog')
    yield cat
    cat.close()


def settings(name='Luma test', exposure=0):
    return normalized(dict(dcp_profile={**record(make_dcp()),'name':name},exposure=exposure))


def count(cat):
    return cat.db.execute('SELECT COUNT(*) FROM profile_assets').fetchone()[0]


def test_one_asset_all_owners_and_portable_roundtrip(catalog,tmp_path):
    cat=catalog;s=settings();other=settings('별칭',.5)
    ident=cat.add(tmp_path/'a.nef',s);second=cat.add(tmp_path/'b.nef',other)
    cat.edit(ident,other);cat.snapshot(ident,'remember');copy=cat.virtual_copy(ident)
    cat.save_preset('preset',other)
    undo={'undo':[s,other]*20,'redo':[s]};cat.save_preference(f'undo:{ident}',undo)
    rules=[{'values':other,'iso':[{'values':s}]}];cat.save_preference('raw_defaults_v1',rules)
    assert count(cat)==1
    assert cat.photo(ident)['settings']==cat.photo(second)['settings']==cat.photo(copy)['settings']==other
    assert cat.histories(ident)[0]['settings']==s
    assert cat.snapshots(ident)[0]['settings']==other
    assert cat.presets()['preset']==other
    assert cat.preference(f'undo:{ident}')==undo
    assert cat.preference('raw_defaults_v1')==rules
    assert preview_store.snapshot(cat.directory,ident,True)['settings']==other
    assert 'data_ref' in cat.photo(ident)['edits'] and '"data":' not in cat.photo(ident)['edits']
    assert dcp.compiled(cat.photo(ident)['settings']['dcp_profile'])['camera']=='Test Camera'
    sidecar=tmp_path/'photo.xmp';write_sidecar(cat.photo(ident),sidecar)
    imported=read_sidecar(sidecar)['settings'];assert imported==other
    with closing(Catalog(tmp_path/'portable')) as destination:
        new=destination.add(tmp_path/'new.nef',imported)
        preset=json.loads(json.dumps(cat.presets()['preset']))
        destination.save_preset('imported',preset)
        assert destination.photo(new)['settings']==other and count(destination)==1
    backup=cat.backup(tmp_path/'backup.sqlite');cat.set_settings(ident,s)
    restore_backup(cat,backup)
    assert cat.photo(ident)['settings']==other and cat.preference(f'undo:{ident}')==undo


@pytest.mark.parametrize('mutation',[
    lambda p:p.update(data='bad'), lambda p:p.update(sha256='0'*64),
    lambda p:p.update(data_ref=p['sha256']),lambda p:p.pop('data'),
    lambda p:p.update(sha256='invalid'),lambda p:p.update(data=''),
])
def test_invalid_public_profile_rolls_back_owner_and_asset(catalog,tmp_path,mutation):
    s=settings();mutation(s['dcp_profile'])
    with pytest.raises((ValueError,TypeError)):catalog.add(tmp_path/'bad.nef',s)
    assert not catalog.photos() and count(catalog)==0


def test_failed_owner_update_rolls_back_new_blob(catalog,tmp_path):
    ident=catalog.add(tmp_path/'one.nef')
    catalog.db.execute("CREATE TRIGGER fail_edit BEFORE UPDATE OF edits ON photos BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):catalog.edit(ident,settings())
    assert count(catalog)==0 and not catalog.histories(ident)
    assert catalog.photo(ident)['settings']['dcp_profile'] is None


def test_failed_reset_of_legacy_photo_rolls_back_history_asset(catalog,tmp_path):
    ident=catalog.add(tmp_path/'one.nef');s=settings()
    with catalog.db:catalog.db.execute('UPDATE photos SET edits=? WHERE id=?',(json.dumps(s),ident))
    catalog.db.execute("CREATE TRIGGER fail_reset BEFORE UPDATE OF edits ON photos BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):catalog.edit(ident,normalized({}))
    assert count(catalog)==0 and not catalog.histories(ident)
    assert catalog.photo(ident)['settings']==s


def test_duplicate_and_missing_photo_do_not_add_assets(catalog,tmp_path):
    path=tmp_path/'one.nef';ident=catalog.add(path)
    assert catalog.add(path,settings())==ident
    catalog.edit(999,settings());catalog.set_settings(999,settings())
    assert count(catalog)==0


def test_new_copies_and_history_of_legacy_photo_are_compact(catalog,tmp_path):
    s=settings();ident=catalog.add(tmp_path/'old.nef')
    with catalog.db:catalog.db.execute('UPDATE photos SET edits=? WHERE id=?',(json.dumps(s),ident))
    duplicate=catalog.virtual_copy(ident);catalog.snapshot(ident,'legacy')
    catalog.edit(ident,settings(exposure=1))
    for table in ('photos','history','snapshots'):
        for row in catalog.db.execute(f'SELECT edits FROM {table}'):
            assert 'data_ref' in row[0] and '"data":' not in row[0]
    assert catalog.photo(duplicate)['settings']==s
    assert catalog.histories(ident)[0]['settings']==s and count(catalog)==1


def test_invalid_legacy_profile_can_be_removed_without_losing_history(catalog,tmp_path):
    ident=catalog.add(tmp_path/'old.nef');bad=settings();bad['dcp_profile']['data']='damaged'
    payload=json.dumps(bad)
    with catalog.db:catalog.db.execute('UPDATE photos SET edits=? WHERE id=?',(payload,ident))
    catalog.edit(ident,normalized({}),'프로파일 해제')
    assert catalog.photo(ident)['settings']['dcp_profile'] is None
    assert catalog.histories(ident)[0]['edits']==payload
    assert count(catalog)==0


@pytest.mark.parametrize('change',["DELETE FROM profile_assets", "UPDATE profile_assets SET data=x'0102'", "UPDATE profile_assets SET size=0"])
def test_cache_detects_external_and_local_corruption(catalog,tmp_path,change):
    ident=catalog.add(tmp_path/'a.nef',settings())
    assert catalog.photo(ident)['settings']==settings()
    with closing(sqlite3.connect(catalog.directory/'catalog.sqlite')) as db:
        with db:db.execute(change)
    with pytest.raises(ValueError):catalog.photo(ident)
    with pytest.raises(ValueError):preview_store.snapshot(catalog.directory,ident,True)


def test_uncommitted_cache_never_survives_rollback(catalog):
    with catalog.db:
        text=catalog.profiles.dumps(settings())
    catalog.db.execute('BEGIN')
    assert catalog.profiles.loads(text)==settings()
    catalog.db.rollback()
    with catalog.db:catalog.db.execute('DELETE FROM profile_assets')
    with pytest.raises(ValueError):catalog.profiles.loads(text)


def test_oversized_corrupt_blob_is_not_loaded_into_python(catalog,tmp_path):
    ident=catalog.add(tmp_path/'a.nef',settings());sha=settings()['dcp_profile']['sha256']
    with catalog.db:catalog.db.execute('UPDATE profile_assets SET data=zeroblob(?),size=?',(8*1024**2,8*1024**2))
    assert catalog.profiles._asset_row(sha)[0] is None
    with pytest.raises(ValueError):catalog.photo(ident)


def legacy_fixture(cat,tmp_path,n=18):
    s=settings();s['unknown_future_value']={'keep':[1,2,3]}
    ids=[cat.add(tmp_path/f'{i}.nef') for i in range(n)]
    encoded=json.dumps(s,ensure_ascii=False)
    with cat.db:
        cat.db.executemany('UPDATE photos SET edits=?,rating=4,keywords=? WHERE id=?',[(encoded,'사진',i) for i in ids])
        cat.db.execute('INSERT INTO history(photo_id,label,edits,created) VALUES(?,?,?,?)',(ids[0],'old',encoded,'2026'))
        cat.db.execute('INSERT INTO snapshots(photo_id,name,edits,created) VALUES(?,?,?,?)',(ids[0],'old',encoded,'2026'))
        cat.db.execute('INSERT INTO presets VALUES(?,?)',('old',encoded))
        cat.db.execute('INSERT INTO preferences VALUES(?,?)',(f'undo:{ids[0]}',json.dumps({'undo':[s]*8,'redo':[s]})))
        cat.db.execute('INSERT INTO preferences VALUES(?,?)',('raw_defaults_v1',json.dumps([{'values':s}])))
    return ids,s


def semantic_rows(db):
    codec=ProfileCodec(db);out={}
    for table,column in [('photos','edits'),('history','edits'),('snapshots','edits'),('presets','edits'),('preferences','value')]:
        rows=[dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
        for row in rows:row[column]=codec.loads(row[column])
        out[table]=rows
    return out


def test_optimizer_preserves_all_semantics_and_preview_tokens_and_backup(catalog,tmp_path):
    ids,s=legacy_fixture(catalog,tmp_path)
    before=semantic_rows(catalog.db)
    tokens=list(catalog.db.execute('SELECT * FROM preview_state ORDER BY 1'))
    result=optimize(catalog.directory,batch_size=3)
    assert result['converted']==len(ids)+5 and result['integrity']=='ok' and result['vacuumed']
    assert count(catalog)==1 and semantic_rows(catalog.db)==before
    assert list(catalog.db.execute('SELECT * FROM preview_state ORDER BY 1'))==tokens
    with closing(sqlite3.connect(result['backup'])) as db:
        db.row_factory=sqlite3.Row;assert semantic_rows(db)==before
    assert optimize(catalog.directory)['converted']==0
    # No history entry solely because storage changed.
    catalog.edit(ids[0],s)
    assert len(catalog.histories(ids[0]))==1


def test_optimizer_cancel_resume_and_failed_rows_keep_assets(catalog,tmp_path):
    ids,s=legacy_fixture(catalog,tmp_path)
    before=semantic_rows(catalog.db);cancel=Event()
    def progress(value):
        if value['stage']=='중복 프로파일 정리 중':cancel.set()
    report=optimize(catalog.directory,cancel,progress,batch_size=2)
    assert report['cancelled'] and report['converted']==2 and report['backup']
    assert semantic_rows(catalog.db)==before
    assert optimize(catalog.directory)['converted']==len(ids)+3
    assert semantic_rows(catalog.db)==before
    # Orphaned assets are removed only when every payload can be inspected.
    orphan=record(make_dcp([(50936,2,'orphan\0')]))
    with catalog.db:
        catalog.profiles.dumps({'dcp_profile':orphan})
        catalog.db.execute("INSERT INTO presets VALUES('broken','not json')")
    report=optimize(catalog.directory)
    assert report['warning_count'] and count(catalog)==2 and report['removed_assets']==0
    with catalog.db:catalog.db.execute("DELETE FROM presets WHERE name='broken'")
    report=optimize(catalog.directory)
    assert report['removed_assets']==1 and count(catalog)==1


def test_cancel_during_batch_rolls_back_batch(catalog,tmp_path,monkeypatch):
    ids,s=legacy_fixture(catalog,tmp_path,4);before=semantic_rows(catalog.db);cancel=Event()
    pack=ProfileCodec.pack;calls=[]
    def stopping(self,*args,**kwargs):
        result=pack(self,*args,**kwargs);calls.append(1)
        if len(calls)==2:cancel.set()
        return result
    monkeypatch.setattr(ProfileCodec,'pack',stopping)
    report=optimize(catalog.directory,cancel,batch_size=4)
    assert report['cancelled'] and report['converted']==0 and count(catalog)==0
    assert semantic_rows(catalog.db)==before


def test_invalid_legacy_profile_preserved_without_partial_assets(catalog,tmp_path):
    ids,s=legacy_fixture(catalog,tmp_path,2);bad=deepcopy(s);bad['dcp_profile']['data']='bad'
    # Mixed valid and invalid profiles in a single undo row.
    with catalog.db:catalog.db.execute('UPDATE preferences SET value=? WHERE key LIKE ?',
        (json.dumps({'undo':[s,bad]}),'undo:%'))
    original=catalog.db.execute("SELECT value FROM preferences WHERE key LIKE 'undo:%'").fetchone()[0]
    report=optimize(catalog.directory)
    assert report['warning_count']==1
    assert catalog.db.execute("SELECT value FROM preferences WHERE key LIKE 'undo:%'").fetchone()[0]==original
    assert count(catalog)==1


def test_restore_rejects_missing_asset_before_touching_current(catalog,tmp_path):
    ident=catalog.add(tmp_path/'a.nef',settings())
    backup=catalog.backup(tmp_path/'bad.sqlite')
    with closing(sqlite3.connect(backup)) as db:
        with db:db.execute('DELETE FROM profile_assets')
    catalog.update(ident,rating=5);before=semantic_rows(catalog.db)
    with pytest.raises(ValueError):restore_backup(catalog,backup)
    assert semantic_rows(catalog.db)==before and count(catalog)==1


def test_legacy_backup_schema_upgrade_is_non_destructive(catalog,tmp_path):
    ids,s=legacy_fixture(catalog,tmp_path,2);before=semantic_rows(catalog.db)
    backup=catalog.backup(tmp_path/'legacy.sqlite')
    with closing(sqlite3.connect(backup)) as db:
        with db:db.execute('DROP TABLE profile_assets')
    catalog.update(ids[0],rating=0);restore_backup(catalog,backup)
    assert semantic_rows(catalog.db)==before and count(catalog)==0
    assert (catalog.directory/'backups'/'before-profile-assets-v1.sqlite').is_file()
    assert optimize(catalog.directory)['converted']==7


def test_unknown_preferences_keep_referenced_assets(catalog,tmp_path):
    ident=catalog.add(tmp_path/'one.nef',settings())
    stored=json.loads(catalog.photo(ident)['edits'])
    catalog.save_preference('future_saved_tool',{'values':stored})
    catalog.remove_photos([ident])
    report=optimize(catalog.directory)
    assert count(catalog)==1 and report['removed_assets']==0
    assert catalog.preference('future_saved_tool')['values']==settings()


def test_vacuum_failure_keeps_converted_catalog_and_backup(catalog,tmp_path,monkeypatch):
    from luma import catalog_maintenance
    ids,s=legacy_fixture(catalog,tmp_path,3);before=semantic_rows(catalog.db)
    connect=sqlite3.connect
    class Connection(sqlite3.Connection):
        def execute(self,sql,*args):
            if sql=='VACUUM':raise sqlite3.OperationalError('fixture disk full')
            return super().execute(sql,*args)
    monkeypatch.setattr(catalog_maintenance.sqlite3,'connect',lambda *a,**kw:connect(*a,**kw,factory=Connection))
    result=optimize(catalog.directory)
    assert not result['vacuumed'] and result['warning_count']==1
    assert result['converted']==8 and result['backup'] and result['integrity']=='ok'
    assert semantic_rows(catalog.db)==before
