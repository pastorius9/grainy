from pathlib import Path
from threading import Event
import json,sqlite3,shutil,os
import pytest
from PIL import Image,ImageDraw
from luma.catalog import Catalog
from luma.engine import thumbnail
from luma.preview_store import signature
from luma.folder_recovery import recover_folders,validate_recovery
from luma.folder_locations import apply_relinks


def patterned(path,number=0):
    path.parent.mkdir(parents=True,exist_ok=True)
    image=Image.new('RGB',(240,160),(30+number*25,50,90+number*20));draw=ImageDraw.Draw(image)
    draw.rectangle((number*20,20,80+number*35,95),fill=(220,160-number*35,30))
    draw.ellipse((120-number*30,50,210-number*10,150),fill=(40,210,160-number*30))
    image.save(path);return path


def library(tmp_path,*,metadata=True,previews=False,count=3):
    c=Catalog(tmp_path/'catalog');old=tmp_path/'old';ids=[]
    for i in range(count):
        path=patterned(old/f'{i}.jpg',i);ident=c.add(path,metadata={'file_fingerprint':signature(path)} if metadata else {})
        if previews:thumbnail(path).save(c.thumbs/f'{ident}.jpg',quality=88)
        ids.append(ident)
    c.edit(ids[0],{'exposure':.4});copy=c.virtual_copy(ids[0]);c.update(ids[0],rating=5)
    return c,old,ids,copy


def test_saved_file_information_finds_folder_renamed_before_first_observation(tmp_path):
    c,old,ids,copy=library(tmp_path);new=tmp_path/'new';old.rename(new)
    try:
        result=recover_folders(c.directory);assert not result['incomplete'] and len(result['plans'])==1
        plan=result['plans'][0];assert plan['proof']=='file_stats' and len(plan['updates'])==4
        assert validate_recovery(c,result['plans']);apply_relinks(c,result['plans'])
        assert c.photo(ids[0])['settings']['exposure']==.4 and c.photo(ids[0])['rating']==5
        assert c.photo(copy)['path']==str(new/'0.jpg') and str(old) not in c.folder_roots()
    finally:c.close()


def test_legacy_saved_previews_find_folder_with_no_file_fingerprints(tmp_path):
    c,old,ids,_=library(tmp_path,metadata=False,previews=True);new=tmp_path/'new';old.rename(new)
    try:
        result=recover_folders(c.directory);assert len(result['plans'])==1
        assert result['plans'][0]['proof']=='saved_previews'
        assert result['plans'][0]['anchors']==ids
    finally:c.close()


def test_blank_first_frame_is_skipped_as_identity_evidence(tmp_path):
    c,old,ids,_=library(tmp_path,metadata=False,previews=True,count=4)
    blank=old/'0.jpg';Image.new('RGB',(240,160),(20,20,20)).save(blank)
    thumbnail(blank).save(c.thumbs/f'{ids[0]}.jpg',quality=88)
    old.rename(tmp_path/'new')
    try:
        plans=recover_folders(c.directory)['plans'];assert len(plans)==1
        assert ids[0] not in plans[0]['anchors'] and len(plans[0]['anchors'])==3
    finally:c.close()


def test_all_blank_previews_are_not_identity_evidence(tmp_path):
    c,old,ids,_=library(tmp_path,metadata=False,previews=True)
    for ident in ids:
        path=Path(c.photo(ident)['path']);Image.new('RGB',(240,160),(20,20,20)).save(path)
        thumbnail(path).save(c.thumbs/f'{ident}.jpg',quality=88)
    old.rename(tmp_path/'new')
    try:assert not recover_folders(c.directory)['plans']
    finally:c.close()


def test_matching_filenames_with_different_pictures_never_reconnect(tmp_path):
    c,old,_,_=library(tmp_path,metadata=False,previews=True)
    # The real originals are moved outside the searched parent.
    archive=tmp_path/'archive';archive.mkdir();old.rename(archive/'original')
    for i in range(3):patterned(tmp_path/'impostor'/f'{i}.jpg',2-i)
    try:assert not recover_folders(c.directory)['plans']
    finally:c.close()


def test_two_matching_copies_are_ambiguous(tmp_path):
    c,old,_,_=library(tmp_path);new=tmp_path/'new';old.rename(new);shutil.copytree(new,tmp_path/'duplicate')
    try:
        result=recover_folders(c.directory);assert result['plans']==[]
        assert result['unresolved'][0]['reason']=='ambiguous'
    finally:c.close()


def test_virtual_copies_cannot_supply_three_independent_photos(tmp_path):
    c,old,ids,_=library(tmp_path,metadata=False,previews=True,count=1)
    c.virtual_copy(ids[0]);old.rename(tmp_path/'new')
    try:assert not recover_folders(c.directory)['plans']
    finally:c.close()


def test_modified_missing_or_already_cataloged_target_is_not_accepted(tmp_path):
    c,old,ids,_=library(tmp_path);new=tmp_path/'new';old.rename(new)
    try:
        c.add(new/'0.jpg')
        result=recover_folders(c.directory);assert not result['plans']
        assert result['unresolved'][0]['reason']=='already_cataloged'
        c.remove_photos([c.add(new/'0.jpg')])
        (new/'2.jpg').rename(new/'different.jpg')
        assert not recover_folders(c.directory)['plans']
    finally:c.close()


def test_stale_result_checks_catalog_and_source_files(tmp_path):
    c,old,ids,_=library(tmp_path);new=tmp_path/'new';old.rename(new)
    try:
        plans=recover_folders(c.directory)['plans'];assert validate_recovery(c,plans)
        path=new/'0.jpg';old_stat=path.stat();os.utime(path,ns=(old_stat.st_atime_ns,old_stat.st_mtime_ns+1000000000))
        assert not validate_recovery(c,plans)
        os.utime(path,ns=(old_stat.st_atime_ns,old_stat.st_mtime_ns))
        assert validate_recovery(c,plans)
        c.db.execute('UPDATE photos SET path=? WHERE id=?',(str(tmp_path/'elsewhere.jpg'),ids[0]));c.db.commit()
        assert not validate_recovery(c,plans)
    finally:c.close()


def test_new_duplicate_after_scan_invalidates_parent_guard(tmp_path):
    c,old,_,_=library(tmp_path);new=tmp_path/'new';old.rename(new)
    try:
        plans=recover_folders(c.directory)['plans'];shutil.copytree(new,tmp_path/'late-copy')
        assert not validate_recovery(c,plans)
    finally:c.close()


def test_cancel_discards_all_proposals(tmp_path):
    c,old,_,_=library(tmp_path);old.rename(tmp_path/'new');cancel=Event();cancel.set()
    try:
        result=recover_folders(c.directory,cancel);assert result['cancelled'] and result['plans']==[]
    finally:c.close()


def test_batch_relink_failure_rolls_back_every_folder(tmp_path):
    c,old,ids,_=library(tmp_path);new=tmp_path/'new';old.rename(new)
    extra=tmp_path/'second';extra_id=c.add(patterned(extra/'other.jpg'));target=tmp_path/'second-new';extra.rename(target)
    try:
        first=recover_folders(c.directory)['plans'][0]
        from luma.folder_locations import relink_plan
        second=relink_plan(c,extra,target)
        c.db.execute(f"CREATE TRIGGER fail_second BEFORE UPDATE OF path ON photos WHEN OLD.id={extra_id} BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with pytest.raises(sqlite3.IntegrityError):apply_relinks(c,[first,second])
        assert c.photo(ids[0])['path']==str(old/'0.jpg') and str(old) in c.folder_roots()
        assert c.photo(extra_id)['path']==str(extra/'other.jpg')
    finally:c.close()
