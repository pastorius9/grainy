import json,sqlite3
from pathlib import Path
from threading import Event
import pytest
from PySide6.QtCore import Qt,QItemSelectionModel,QEventLoop,QTimer
from PySide6.QtWidgets import QApplication
from PIL import Image
from luma.catalog import Catalog
from luma.engine import defaults
from luma.library import matches,restore_backup
from luma.library_query import query,QueryCancelled
from test_studio_ui import app,window,wait


def query_catalog(cat,**options):return query(cat.directory/'catalog.sqlite',options,Event())


def test_index_search_sort_folders_rules_and_collections(tmp_path):
    c=Catalog(tmp_path/'data');root=tmp_path/'여행_%';ids=[]
    for n,folder,name in [(0,root,'Straße.jpg'),(1,root/'밤','NIGHT.JPG'),(2,tmp_path/'여행_A','other.jpg')]:
        ident=c.add(folder/name);ids.append(ident)
        c.update(ident,rating=n+3,flag=1 if n==0 else 0,keywords='인물' if n==1 else '풍경',
            metadata=json.dumps({'camera':'Nikon' if n<2 else 'Canon','iso':100*(n+1),'width':500,'height':300,
                'date':f'2026:09:{20-n:02} 10:00:00'}))
    assert query_catalog(c,search='STRASSE')['ids']==[ids[0]]
    assert query_catalog(c,folder=str(root),recursive=True)['ids']==ids[:2]
    assert query_catalog(c,folder=str(root),recursive=False)['ids']==ids[:1]
    assert query_catalog(c,mode='rated')['ids']==ids[1:]
    assert query_catalog(c,mode='picked')['ids']==ids[:1]
    assert query_catalog(c,sort='capture')['ids']==ids[::-1]
    assert query_catalog(c,sort='rating',descending=True)['ids']==ids[::-1]
    for rules in [{'any':[{'camera':'Nikon','iso_min':200},{'rating_min':5}]},
            {'all':[{'extension':'jpg'},{'orientation':'landscape'}]},
            {'date_from':'2026-09-19','date_to':'2026-09-20'},{'missing':True},{'virtual':False}]:
        expected=[p['id'] for p in c.photos() if matches(p,rules)]
        assert query_catalog(c,rules=rules)['ids']==expected
    collection=c.add_collection('smart',{'camera':'Nikon'});assert query_catalog(c,collection=collection)['ids']==ids[:2]
    regular=c.add_collection('selection');c.collection_add(regular,[ids[1]])
    assert query_catalog(c,collection=regular)['ids']==[ids[1]]
    c.stack(ids[:2]);assert query_catalog(c,collapsed=True)['ids']==[ids[0],ids[2]]
    assert query_catalog(c,collapsed=True,sort='rating',descending=True)['ids']==ids[:0:-1]
    c.close()


def test_derived_index_tracks_external_writes_deletes_and_restore(tmp_path):
    c=Catalog(tmp_path/'data');ident=c.add(tmp_path/'a.jpg');snapshot=c.backup(tmp_path/'snapshot.sqlite')
    original=c.photo(ident)['edits'];assert query_catalog(c)['ids']==[ident]
    with sqlite3.connect(c.directory/'catalog.sqlite') as writer:
        writer.execute('UPDATE photos SET name=?,keywords=?,path=? WHERE id=?',('changed.jpg','새 키워드',str(tmp_path/'moved'/'changed.jpg'),ident))
    assert query_catalog(c,search='새 키워드')['ids']==[ident]
    assert query_catalog(c,folder=str(tmp_path/'moved'))['ids']==[ident]
    assert c.photo(ident)['edits']==original
    c.remove_photos([ident]);assert query_catalog(c)['ids']==[]
    restore_backup(c,snapshot);assert query_catalog(c,search='a.jpg')['ids']==[ident]
    assert query_catalog(c,search='changed')['ids']==[];c.close()


def test_cancelled_first_index_recovers_without_changing_photos(tmp_path):
    c=Catalog(tmp_path/'data');edit=json.dumps(defaults())
    with c.db:c.db.executemany('INSERT INTO photos(path,name,edits,imported) VALUES(?,?,?,?)',
        [(str(tmp_path/f'{n}.jpg'),f'{n}.jpg',edit,'2026-09-20') for n in range(3000)])
    class Cancel:
        calls=0
        def is_set(self):self.calls+=1;return self.calls>3
    with pytest.raises(QueryCancelled):query(c.directory/'catalog.sqlite',{},Cancel())
    indexed=c.db.execute('SELECT COUNT(*) FROM browser_index').fetchone()[0]
    assert 0<indexed<3000
    assert len(query_catalog(c)['ids'])==3000
    assert c.db.execute('SELECT COUNT(*) FROM photos WHERE edits=?',(edit,)).fetchone()[0]==3000
    assert c.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';c.close()


def test_shared_selection_sort_and_latest_search_wins(window,tmp_path):
    w=window
    for name in ['zebra.jpg','alpha.jpg','middle.jpg']:
        path=tmp_path/name;Image.new('RGB',(60,40),'gray').save(path);w.catalog.add(path)
    w.refresh_lists();wait(lambda:not w.library_loading)
    ids=w.visible_ids.copy();w.set_mode(1)
    selected=ids[1::2];w.grid.restore_selection(selected)
    w.set_mode(0);assert set(w.selected_ids())==set(selected)
    w.sort_order.setCurrentIndex(w.sort_order.findData('name'));wait(lambda:not w.library_loading)
    assert set(w.selected_ids())==set(selected)
    expected=sorted(ids,key=lambda i:w.catalog.photo(i)['name'].casefold())
    assert w.visible_ids==expected
    for text in ['zebra','middle','no-match','alpha']:w.search.setText(text)
    wait(lambda:not w.library_loading and w.source is not None and not w.render_running)
    assert len(w.visible_ids)==1 and w.catalog.photo(w.visible_ids[0])['name']=='alpha.jpg'
    assert w.current_id==w.visible_ids[0]


def test_browsing_never_reads_full_edit_records_or_eager_thumbnails(window,tmp_path,monkeypatch):
    w=window;edit=json.dumps(defaults())
    with w.catalog.db:w.catalog.db.executemany('INSERT INTO photos(path,name,edits,imported) VALUES(?,?,?,?)',
        [(str(tmp_path/f'{n}.jpg'),f'{n}.jpg',edit,'2026-09-20') for n in range(10000)])
    monkeypatch.setattr(w.catalog,'photos',lambda:pytest.fail('full catalog read during browsing'))
    w.set_mode(1);w.refresh_lists();wait(lambda:not w.library_loading)
    assert len(w.visible_ids)==10001
    wait(lambda:not w.photo_model.pending_pages and not w.photo_model.pending_thumbs)
    assert w.photo_model.cached_rows<=256 and w.photo_model.thumbnail_reads<80
    assert w.photo_model.thumbnail_bytes<=w.photo_model.MAX_THUMB_BYTES
    # Scroll directly far beyond the first page; the row IDs remain addressable.
    last=w.photo_model.index(9999);w.grid.scrollTo(last)
    wait(lambda:w.photo_model.entry(9999,request=False) is not None)
    assert w.photo_model.cached_rows<=w.photo_model.PAGE_SIZE*w.photo_model.MAX_PAGES
    assert len(w.photo_model.thumbnails)<=256


def test_thumbnail_monitor_conversion_and_invalidation(window,monkeypatch):
    import numpy as np
    from luma import colorio
    from luma import preview_store
    from dataclasses import replace
    w=window;model=w.photo_model;ident=w.current_id
    # The first render stores its own thumbnail in two steps (resize on the render pool, then write on the
    # thumbnail pool); both must be over, or that write lands after the image published below.
    def idle():return not w.render_running and not w.preview_queue.running and w.pool.activeThreadCount()==0 and w.thumbnail_pool.activeThreadCount()==0
    wait(idle);QApplication.processEvents();wait(idle)
    assert preview_store.publish(w.catalog.directory,preview_store.snapshot(w.catalog.directory,ident),
        Image.new('RGB',(80,60),(200,110,60)))
    before=(w.catalog.thumbs/f'{ident}.jpg').read_bytes()
    monitor=colorio.profile('Adobe RGB')
    monkeypatch.setattr(w.display_color.main,'state',replace(w.display_color.main.state,data=monitor))
    model.invalidate_thumbnails([ident]);model.thumbnail(ident)
    wait(lambda:ident in model.thumbnails)
    pix=model.thumbnails[ident][0];actual=pix.toImage().pixelColor(40,30).getRgb()[:3]
    source=np.array(Image.open(w.catalog.thumbs/f'{ident}.jpg'))[30,40]/255
    expected=colorio.display_rgb(source[None,None].astype(np.float32),monitor)[0,0]*255
    np.testing.assert_allclose(actual,expected,atol=1)
    assert before==(w.catalog.thumbs/f'{ident}.jpg').read_bytes()
    monkeypatch.setattr(w.display_color.main,'state',replace(w.display_color.main.state,data=None));model.invalidate_thumbnails();model.thumbnail(ident)
    wait(lambda:ident in model.thumbnails)
    actual=model.thumbnails[ident][0].toImage().pixelColor(40,30).getRgb()[:3]
    np.testing.assert_allclose(actual,source*255,atol=1)


def test_import_cancel_keeps_completed_photos_and_resumes_without_duplicates(app,tmp_path,monkeypatch):
    import hashlib
    import luma.app as module
    paths=[]
    for n in range(4):
        p=tmp_path/'photos'/f'{n}.jpg';p.parent.mkdir(exist_ok=True)
        Image.new('RGB',(80,60),(80+n*20,120,160)).save(p);paths.append(p)
    hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    native=module.thumbnail;started=Event();release=Event();count=[0]
    def slow(path,**kwargs):
        count[0]+=1
        if count[0]==2:started.set();assert release.wait(10)
        return native(path,**kwargs)
    monkeypatch.setattr(module,'thumbnail',slow)
    w=module.MainWindow(tmp_path/'data');w.show();w.import_paths([str(paths[0].parent)])
    try:
        wait(started.is_set);assert len(w.catalog.photos())==1
        w.import_cancel_button.click();release.set()
        wait(lambda:not w.import_busy and not w.import_scans and not w.library_loading)
        assert len(w.catalog.photos())==1 and not w.import_queue
        assert not w.import_cancel_button.isVisible()
        w.import_paths([str(paths[0].parent)])
        wait(lambda:not w.import_busy and not w.import_scans and not w.library_loading)
        assert len(w.catalog.photos())==4
        assert hashes==[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    finally:release.set();w.close()


def test_import_scan_cancellation_does_not_add_partial_scan(app,tmp_path,monkeypatch):
    import luma.app as module
    p=tmp_path/'photos'/'one.jpg';p.parent.mkdir();Image.new('RGB',(40,30),'gray').save(p)
    started=Event();release=Event();native=Path.rglob
    def paused(self,pattern):
        if self==p.parent:
            started.set();assert release.wait(10)
        yield from native(self,pattern)
    monkeypatch.setattr(Path,'rglob',paused)
    w=module.MainWindow(tmp_path/'data');w.import_paths([str(p.parent)])
    try:
        wait(started.is_set);w.cancel_import();release.set()
        wait(lambda:not w.import_scans and not w.import_busy and not w.library_loading)
        assert w.catalog.photos()==[] and p.is_file()
    finally:release.set();w.close()
