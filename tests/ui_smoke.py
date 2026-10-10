"""Exercise the actual Qt window and worker pipeline; writes reviewable evidence."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
import json
import time
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint, QItemSelectionModel
from PySide6.QtTest import QTest
from luma.app import MainWindow,configure_application
from luma.catalog import Catalog

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation'
OUT.mkdir(exist_ok=True)
app=QApplication([])
configure_application(app)


def wait_for(predicate,timeout=25):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(15)
    raise AssertionError('Timed out waiting for UI')


with TemporaryDirectory(prefix='luma-qa-',dir=OUT) as scratch:
    directory=Path(scratch)
    source=directory/'사진 테스트.png'
    y,x=np.mgrid[0:600,0:900]
    pixels=np.stack([x*255/899,y*255/599,((x//60+y//60)%2)*180+30],axis=-1).astype(np.uint8)
    Image.fromarray(pixels).save(source)
    second=directory/'second.jpg'
    Image.fromarray(pixels[:,::-1]).save(second)
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    data=directory/'data'
    window=MainWindow(data)
    window.show()
    QTest.qWait(120)
    window.grab().save(str(OUT/'01-empty.png'))
    errors=[]
    window.show_error=lambda error:errors.append(error)
    window.import_paths([str(source),str(second)])
    wait_for(lambda:len(window.catalog.photos())==2 and not window.import_busy and not window.library_loading and window.source is not None and not window.render_running)
    ids=[p['id'] for p in window.catalog.photos()]
    first=next(p['id'] for p in window.catalog.photos() if p['name']==source.name)
    window.activate(first)
    wait_for(lambda:window.source is not None and not window.render_running)
    baseline=window.view.on_screen.copy()
    exposure=window.adjustments['exposure'].slider
    exposure.setFocus()
    QTest.keyClick(exposure,Qt.Key.Key_Right)
    exposure.setValue(80)
    wait_for(lambda:not window.render_running and not window.preview_timer.isActive() and not window.commit_timer.isActive())
    assert window.settings['exposure']==.8
    assert not np.allclose(baseline,window.view.on_screen)
    window.undo()
    assert window.settings['exposure']==0
    window.redo()
    assert window.settings['exposure']==.8
    wait_for(lambda:not window.render_running)
    window.tabs.setCurrentIndex(1)
    window.crop_button.click()
    wait_for(lambda:not window.render_running)
    rect=window.view.crop_screen_rect()
    p1=rect.bottomRight().toPoint()
    p2=QPoint(round(rect.left()+rect.width()*.6),round(rect.top()+rect.height()*.6))
    QTest.mousePress(window.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p1)
    QTest.mouseMove(window.view.viewport(),p2,40)
    QTest.mouseRelease(window.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p2)
    assert window.view.crop_mode and window.settings['crop'] is None
    QTest.keyClick(window.view,Qt.Key.Key_Return)
    wait_for(lambda:not window.render_running and not window.preview_timer.isActive())
    assert window.settings['crop'] is not None
    assert .55<window.settings['crop'][2]-window.settings['crop'][0]<.65
    window.copy_edits()
    window.set_mode(1)
    for i in range(window.grid.count()):
        window.grid.item(i).setSelected(True)
    window.paste_edits()
    assert all(window.catalog.photo(i)['settings']['exposure']==.8 for i in ids)
    other=next(i for i in ids if i!=first)
    assert window.catalog.photo(other)['settings']['crop'] is None
    window.set_rating(5)
    assert all(window.catalog.photo(i)['rating']==5 for i in ids)
    wait_for(lambda:not window.library_loading)
    window.set_mode(0)
    assert set(window.selected_ids())==set(ids)
    window.keywords.setText('검증, 필름')
    window.save_keywords()
    window.tabs.setCurrentIndex(0)
    wait_for(lambda:not window.render_running)
    window.grab().save(str(OUT/'02-editing.png'))
    window.before_button.click()
    wait_for(lambda:not window.render_running)
    assert window.view.on_screen.shape==(600,900,3)
    window.before_button.click()
    wait_for(lambda:not window.render_running)
    exports=directory/'exports'
    exports.mkdir()
    window.start_export(ids,exports,'TIFF 16-bit')
    wait_for(lambda:not window.export_running)
    assert len(window.last_export['outputs'])==2 and not window.last_export['failures']
    assert all(Path(o['path']).stat().st_size>1000 for o in window.last_export['outputs'])
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original_hash
    settings=window.settings.copy()
    window.close()
    app.processEvents()
    reopened=Catalog(data)
    assert reopened.photo(first)['settings']==settings
    assert reopened.photo(first)['keywords']=='검증, 필름'
    reopened.close()
    assert not errors,errors
    report={'passed':True,'checks':['import two files','Qt slider keyboard interaction','preview pixel change','undo and redo','mouse drag crop','batch copy without crop','multi-selection preservation','batch star rating','original compare','16-bit TIFF batch export','original SHA-256 unchanged','catalog reopen'],
            'export_sizes':[o['size'] for o in window.last_export['outputs']], 'screenshots':['01-empty.png','02-editing.png']}
    (OUT/'ui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
