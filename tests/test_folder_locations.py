from pathlib import Path
from threading import Event
import sqlite3
import pytest
from PIL import Image
from luma.catalog import Catalog
from luma.folders import path_key
from luma.folder_locations import scan_folders,directory_identity,relink_plan,apply_relink


def photo(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    Image.new('RGB',(60,40),(110,140,160)).save(path)
    return path


def scan(paths,roots,identities=None,expanded=None):
    return scan_folders(list(map(str,paths)),list(map(str,roots)),set(map(path_key,expanded or roots)),identities or {},Event())


def test_new_empty_and_populated_subfolders_are_discovered_without_image_reads(tmp_path):
    root=tmp_path/'photos';root.mkdir()
    assert scan([root],[root])['children']==[]
    empty=root/'새 폴더';empty.mkdir();p=photo(root/'new shoot'/'a.jpg')
    result=scan([root],[root])
    assert set(result['children'])=={str(empty),str(p.parent)}
    deep=empty/'더 깊은 폴더';deep.mkdir()
    assert str(deep) not in scan([root,empty],[root])['children']
    assert str(deep) in scan([root,empty],[root],expanded=[root,empty])['children']


def test_saved_identity_detects_rename_but_never_guesses_by_matching_name(tmp_path):
    old=tmp_path/'old';p=photo(old/'child'/'a.jpg')
    before=scan([p.parent],[old])
    renamed=tmp_path/'이름 변경';old.rename(renamed)
    after=scan([p.parent],[old],before['identities'])
    assert after['moves']==[(str(old),str(renamed))]
    assert path_key(old) in after['missing']
    assert scan([p.parent],[old])['moves']==[]
    impostor=photo(tmp_path/'other'/'child'/'a.jpg')
    assert directory_identity(impostor.parent)!=before['identities'][path_key(p.parent)]['id']


def test_ancestor_rename_is_detected_when_only_inner_folder_was_registered(tmp_path):
    old=tmp_path/'old';inner=old/'child';photo(inner/'a.jpg')
    before=scan([inner],[inner]);new=tmp_path/'new';old.rename(new)
    assert scan([inner],[inner],before['identities'])['moves']==[(str(old),str(new))]


@pytest.fixture
def catalog(tmp_path):
    cat=Catalog(tmp_path/'catalog')
    yield cat
    cat.close()


def test_relink_preserves_ids_edits_history_copies_collections_and_watch_paths(catalog,tmp_path):
    c=catalog;old=tmp_path/'old';p=photo(old/'child'/'a.jpg');ident=c.add(p)
    c.edit(ident,{'exposure':.75});copy=c.virtual_copy(ident);c.snapshot(ident,'saved')
    c.update(ident,rating=5,keywords='필름');collection=c.add_collection('favorites');c.collection_add(collection,[ident,copy])
    c.save_preference(f'undo:{ident}',{'undo':[{'exposure':.2}]})
    c.save_preference('watch_folders',[str(old/'child')])
    c.save_preference('folder_panel',{'selected':str(old/'child'),'expanded':[path_key(old)],'current_photo':ident})
    c.register_folder(old)
    sibling=photo(tmp_path/'old2'/'b.jpg');sibling_id=c.add(sibling)
    before={table:[tuple(r) for r in c.db.execute(f'SELECT * FROM {table}')] for table in ('history','snapshots','collection_members')}
    pixels=p.read_bytes();new=tmp_path/'new';old.rename(new)
    plan=relink_plan(c,old,new);assert plan['found']==2 and plan['missing']==0
    assert apply_relink(c,plan)==2
    assert c.photo(ident)['path']==str(new/'child'/'a.jpg')==c.photo(copy)['path']
    assert c.photo(sibling_id)['path']==str(sibling)
    assert c.photo(ident)['settings']['exposure']==.75 and c.photo(ident)['rating']==5
    assert c.preference(f'undo:{ident}')=={'undo':[{'exposure':.2}]}
    assert c.preference('watch_folders')==[str(new/'child')]
    assert c.preference('folder_panel')['selected']==str(new/'child')
    assert str(old) not in c.folder_roots() and str(old/'child') not in c.folder_roots()
    assert (new/'child'/'a.jpg').read_bytes()==pixels
    for table,rows in before.items():assert [tuple(r) for r in c.db.execute(f'SELECT * FROM {table}')]==rows


def test_partial_missing_retargets_all_paths_and_cleans_old_root(catalog,tmp_path):
    c=catalog;old=tmp_path/'old';a=c.add(photo(old/'a.jpg'));b=c.add(photo(old/'b.jpg'))
    new=tmp_path/'new';old.rename(new);(new/'b.jpg').rename(new/'renamed.jpg')
    plan=relink_plan(c,old,new);assert plan['found']==1 and plan['missing']==1
    apply_relink(c,plan)
    assert c.photo(a)['path']==str(new/'a.jpg') and c.photo(b)['path']==str(new/'b.jpg')
    assert c.folder_roots()==[str(new)]


def test_wrong_or_already_cataloged_location_does_not_change_catalog(catalog,tmp_path):
    c=catalog;old=tmp_path/'old';ident=c.add(photo(old/'a.jpg'))
    wrong=tmp_path/'wrong';wrong.mkdir()
    with pytest.raises(ValueError):relink_plan(c,old,wrong)
    new=tmp_path/'new';c.add(photo(new/'a.jpg'))
    with pytest.raises(ValueError):relink_plan(c,old,new)
    assert c.photo(ident)['path']==str(old/'a.jpg')


def test_relink_transaction_rolls_back_paths_roots_and_preferences(catalog,tmp_path):
    c=catalog;old=tmp_path/'old';ident=c.add(photo(old/'a.jpg'));new=tmp_path/'new';old.rename(new)
    c.db.execute("CREATE TRIGGER fail_root BEFORE DELETE ON folder_roots BEGIN SELECT RAISE(ABORT,'fixture'); END;")
    with pytest.raises(sqlite3.IntegrityError):apply_relink(c,relink_plan(c,old,new))
    assert c.photo(ident)['path']==str(old/'a.jpg') and c.folder_roots()==[str(old)]
    assert c.preference('folder_panel') is None


def test_cancel_does_not_scan(tmp_path):
    event=Event();event.set()
    result=scan_folders([str(tmp_path)],[str(tmp_path)],{path_key(tmp_path)},{},event)
    assert not result['children'] and not result['moves'] and result['incomplete']
