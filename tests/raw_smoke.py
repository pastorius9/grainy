import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys
import json
import time
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
import rawpy
import tifffile
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from luma.app import MainWindow,configure_application
from luma.engine import load_image,defaults,export_image

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation'
source=OUT/'iss030e122639.NEF'
original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
preview,info=load_image(source,1800)
assert max(preview.shape[:2])<=1800
full,metadata=load_image(source)
full_size=(full.shape[1],full.shape[0])
assert (info['width'],info['height'])==full_size, 'Preview must report original resolution'
del full
settings=defaults()
settings.update(exposure=.35,highlights=-25,shadows=18,vibrance=12)
target=OUT/'raw-verified-16bit.tif'
if target.exists():
    target=OUT/f'raw-verified-16bit-{int(time.time())}.tif'
assert export_image(source,target,settings,'TIFF 16-bit')==full_size
with tifffile.TiffFile(target) as tf:
    assert tf.pages[0].shape==full_size[::-1]+(3,)
    assert str(tf.pages[0].dtype)=='uint16'
assert hashlib.sha256(source.read_bytes()).hexdigest()==original_hash
app=QApplication([])
configure_application(app)
with TemporaryDirectory(prefix='raw-qa-',dir=OUT) as directory:
    window=MainWindow(directory)
    window.show()
    errors=[]
    window.show_error=lambda error:errors.append(error)
    window.import_paths([str(source)])
    end=time.monotonic()+60
    while time.monotonic()<end and (window.source is None or window.import_busy or window.render_running or not window.catalog.photos()):
        app.processEvents()
        QTest.qWait(20)
    assert window.source is not None and not window.render_running
    window.settings=settings
    window.load_controls()
    window.commit()
    window.render_version+=1
    window.render()
    end=time.monotonic()+30
    while time.monotonic()<end and window.render_running:
        app.processEvents()
        QTest.qWait(20)
    window.grab().save(str(OUT/'03-raw-editing.png'))
    assert not errors,errors
    window.close()
    app.processEvents()
report={'passed':True,'sample_source':'https://github.com/letmaik/rawpy/blob/main/test/iss030e122639.NEF',
        'input_bytes':source.stat().st_size,'input_sha256':original_hash,'rawpy':rawpy.__version__,
        'libraw':rawpy.libraw_version,'preview_size':[preview.shape[1],preview.shape[0]],
        'full_resolution':full_size,'export_file':target.name,'export_bits_per_channel':16,
        'original_unchanged':True,'screenshot':'03-raw-editing.png'}
(OUT/'raw-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report))
