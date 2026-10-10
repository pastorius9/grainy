import json
import sqlite3
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from luma.catalog import Catalog
from luma.engine import defaults,export_image,load_image,to_srgb
from luma.library import matches,move_files,move_folder,relink,duplicate_groups,restore_backup,write_sidecar,read_sidecar
from luma.colorio import profile


@pytest.fixture
def library(tmp_path):
    directory=tmp_path/'photos';directory.mkdir()
    path=directory/'one.png';Image.new('RGB',(80,60),(92,120,170)).save(path)
    cat=Catalog(tmp_path/'catalog');ident=cat.add(path)
    yield cat,ident,path
    cat.close()


def test_virtual_copy_snapshot_collection_and_smart_filter(library):
    cat,ident,path=library;cat.edit(ident,{**defaults(),'exposure':.6})
    copy=cat.virtual_copy(ident)
    assert copy!=ident and cat.photo(copy)['path']==str(path) and cat.add(path)==ident
    cat.edit(copy,{**defaults(),'exposure':-1});assert cat.photo(ident)['settings']['exposure']==.6
    cat.snapshot(copy,'dark');assert json.loads(cat.snapshots(copy)[0]['edits'])['exposure']==-1
    collection=cat.add_collection('選択');cat.collection_add(collection,[ident,copy,copy])
    assert cat.collection_ids(collection)=={ident,copy}
    smart=cat.add_collection('rated',{'rating_min':4,'keywords':'여행|일본'})
    cat.update(ident,rating=5,keywords='여행|일본, 필름')
    assert cat.collection_ids(smart)=={ident}
    cat.update(ident,rating=1);assert cat.collection_ids(smart)==set()


def test_file_move_updates_virtual_paths_and_rejects_overwrite(library,tmp_path):
    cat,ident,path=library;copy=cat.virtual_copy(ident);before=path.read_bytes()
    existing=path.with_name('occupied.png');existing.write_bytes(b'do not overwrite')
    with pytest.raises(ValueError):move_files(cat,{str(path):existing})
    assert path.read_bytes()==before and existing.read_bytes()==b'do not overwrite'
    target=tmp_path/'moved.png';move_files(cat,{str(path):target})
    assert not path.exists() and target.read_bytes()==before
    assert all(cat.photo(i)['path']==str(target) for i in (ident,copy))
    cat.remove_photos([copy]);assert target.exists() and cat.photo(ident)


def test_folder_move_and_relink(library,tmp_path):
    cat,ident,path=library;target=tmp_path/'relocated'
    move_folder(cat,path.parent,target);new=target/path.name
    assert new.exists() and cat.photo(ident)['path']==str(new)
    with pytest.raises(ValueError):move_folder(cat,target,target/'nested')
    replacement=tmp_path/'replacement.png';Image.new('RGB',(80,60)).save(replacement)
    relink(cat,ident,replacement);assert cat.photo(ident)['path']==str(replacement)


def test_backup_restore_and_missing_detection(library,tmp_path):
    cat,ident,path=library;backup=cat.backup(tmp_path/'backup.sqlite')
    cat.update(ident,rating=5);restore_backup(cat,backup)
    assert cat.photo(ident)['rating']==0
    assert not matches(cat.photo(ident),{'missing':True})
    path.rename(path.with_name('missing.png'))
    assert matches(cat.photo(ident),{'missing':True})


def test_duplicate_contents_and_xmp_roundtrip(library):
    cat,ident,path=library;dupe=path.with_name('different-name.png');dupe.write_bytes(path.read_bytes());cat.add(dupe)
    assert len(duplicate_groups(cat.photos())[0])==2
    cat.update(ident,rating=4,keywords='여행|일본, 필름',label='초록')
    cat.set_user_metadata(ident,{'creator':'촬영자','caption':'설명'})
    cat.edit(ident,{**defaults(),'exposure':.75})
    sidecar=path.with_suffix('.xmp');write_sidecar(cat.photo(ident),sidecar);write_sidecar(cat.photo(ident),sidecar)
    data=read_sidecar(sidecar)
    assert data['rating']==4 and data['keywords']=='여행|일본, 필름' and data['settings']['exposure']==.75
    assert data['user_metadata']['creator']=='촬영자'


@pytest.mark.parametrize('space',['sRGB','Adobe RGB','Display P3','ProPhoto RGB'])
def test_icc_tiff_preserves_precision_and_metadata(library,tmp_path,space):
    import tifffile
    cat,ident,path=library;target=tmp_path/f'{space}.tif'
    export_image(path,target,defaults(),'TIFF 16-bit',color_space=space,keep_metadata=True,user_metadata={'caption':'시험 사진'},keywords='필름')
    with tifffile.TiffFile(target) as tf:
        assert tf.pages[0].dtype==np.uint16 and tf.pages[0].tags[34675].value
        assert '시험 사진' in tf.pages[0].tags[700].value.decode('utf-8')
    rgb,_=load_image(target)
    np.testing.assert_allclose(to_srgb(rgb),to_srgb(load_image(path)[0]),atol=.001)


def test_old_catalog_migration_keeps_edits_and_history(tmp_path):
    directory=tmp_path/'old';directory.mkdir();db=sqlite3.connect(directory/'catalog.sqlite')
    db.execute('CREATE TABLE photos(id INTEGER PRIMARY KEY,path TEXT UNIQUE NOT NULL,name TEXT NOT NULL,rating INTEGER NOT NULL DEFAULT 0,flag INTEGER NOT NULL DEFAULT 0,keywords TEXT NOT NULL DEFAULT "",edits TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT "{}",imported TEXT NOT NULL)')
    db.execute('INSERT INTO photos VALUES(1,?,?,?,?,?,?,?,?)',(str(tmp_path/'photo.png'),'photo.png',4,1,'keep',json.dumps({'exposure':1.2}),'{}','2026'))
    db.commit();db.close()
    cat=Catalog(directory)
    try:
        assert cat.photo(1)['settings']['exposure']==1.2 and cat.photo(1)['rating']==4
        assert (directory/'backups'/'before-library-v2.sqlite').exists()
        assert cat.virtual_copy(1)!=1
    finally:cat.close()


def test_wide_working_space_preserves_outside_srgb_colour(tmp_path):
    import tifffile
    import imagecodecs
    from luma.engine import develop
    pixels=np.array([[[.1,.95,.2],[.9,.2,.05],[.3,.2,.8]]],np.float32)
    source=tmp_path/'p3.tif';icc=profile('Display P3')
    tifffile.imwrite(source,np.uint16(pixels*65535),photometric='rgb',extratags=[(34675,'B',len(icc),icc,False)])
    linear,info=load_image(source,working_space='ProPhoto')
    s=defaults();s['working_space']='ProPhoto'
    preserved=develop(linear,s,output_space='Display P3')
    np.testing.assert_allclose(preserved,pixels,atol=.002)
    target=tmp_path/'preserved.tif';export_image(source,target,s,'TIFF 16-bit',color_space='Display P3')
    np.testing.assert_allclose(tifffile.imread(target)/65535,pixels,atol=.002)


def test_videos_are_not_opened_and_catalog_reader(tmp_path):
    from luma.engine import IMAGE_EXTENSIONS
    from luma.extras import lightroom_records
    video=tmp_path/'video.avi';video.write_bytes(b'RIFF')
    assert not {'.mp4','.mov','.avi','.mkv','.m4v'}&IMAGE_EXTENSIONS          # no longer importable
    with pytest.raises(ValueError):load_image(video)                           # an entry from an older catalog
    catalog=tmp_path/'test.lrcat'
    with sqlite3.connect(catalog) as db:
        db.executescript('CREATE TABLE AgLibraryRootFolder(id_local INTEGER,absolutePath TEXT);CREATE TABLE AgLibraryFolder(id_local INTEGER,rootFolder INTEGER,pathFromRoot TEXT);CREATE TABLE AgLibraryFile(id_local INTEGER,folder INTEGER,baseName TEXT,extension TEXT);CREATE TABLE Adobe_images(rootFile INTEGER,rating INTEGER,pick INTEGER);')
        db.execute('INSERT INTO AgLibraryRootFolder VALUES(1,?)',(str(tmp_path),));db.execute("INSERT INTO AgLibraryFolder VALUES(1,1,'')")
        db.execute("INSERT INTO AgLibraryFile VALUES(1,1,'video','avi')");db.execute('INSERT INTO Adobe_images VALUES(1,4,1)')
    rows=lightroom_records(catalog);assert rows==[{'path':str(video),'rating':4,'flag':1}]
