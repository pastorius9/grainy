"""Own hidden test windows only: verify actual GPU float pixels, no desktop capture."""
import os,sys,json,argparse,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['QT_QPA_PLATFORM']='windows'
import numpy as np
from dataclasses import asdict
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import Qt
from luma.native_hdr import Renderer,probe,Display

def run():
    from . import engine
    engine.HDR_FEATURE=True
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True);host.resize(320,100);host.show();app.processEvents()
    state=probe(int(host.winId()));results={}
    for warp in (False,True):
        renderer=Renderer(int(host.winId()),warp=warp)
        try:
            frame=np.ones((4,5,4),np.float32);frame[...,:3]=np.array([.1,1,2,4,16])[None,:,None]
            renderer.upload(frame);overlay=np.zeros((40,55,4),np.uint8)
            renderer.draw(55,40,[0,0,55,40],overlay,200,1000,present=False)
            pixels=renderer.readback(55,40)
            samples=pixels[20,[5,16,27,38,49],0].astype(float)
            np.testing.assert_allclose(samples,[.25,2.5,5,10,12.5],rtol=.015)
            assert pixels.dtype==np.float16 and np.isfinite(pixels).all()
            overlay[:,:,:]=[255,0,0,255]
            renderer.draw(55,40,[0,0,55,40],overlay,200,1000,present=False)
            np.testing.assert_allclose(renderer.readback(55,40)[20,20,:3],[2.5,0,0],atol=.001)
            overlay=np.zeros((80,100,4),np.uint8)
            renderer.draw(100,80,[20,10,55,40],overlay,80,320,present=False)
            resized=renderer.readback(100,80)
            assert resized[30,58,0]==4 and resized[0,0,0]<.01
            results['warp' if warp else 'hardware']={'passed':True,'scrgb_samples':samples.tolist(),'overlay':True,'resize':True}
        finally:renderer.close()
    host.close();app.processEvents()
    # Exercise Qt's actual viewport integration and switch back to backing-store SDR.
    from luma.widgets import PhotoView
    from luma.engine import to_srgb
    view=PhotoView();view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True);view.resize(500,350);view.show()
    rgb=np.ones((120,180,3),np.float32)*.8;view.set_image(rgb)
    rgba=np.ones((120,180,4),np.float32);rgba[...,:3]=4
    view.hdr_surface.set_frame(rgba,Display(active=True,white=200,peak=1000))
    deadline=time.monotonic()+5
    while view.hdr_surface.frames==0 and time.monotonic()<deadline:app.processEvents()
    assert view.hdr_surface.frames>0,view.hdr_surface.error
    view.crop_mode=True;view.viewport().update();app.processEvents()
    view.resize(600,400);app.processEvents();assert not view.hdr_surface.error
    view.hdr_surface.set_frame(None,Display());app.processEvents()
    assert not view.viewport().gpu and view.hdr_surface.renderer is None
    view.close();app.processEvents();results['qt_viewport']={'passed':True,'frames':view.hdr_surface.frames}
    return dict(status='passed',display=asdict(state),physical_hdr_verified=False,checks=results)

def main():
    path=Path(os.environ.get('GRAINY_HDR_TEST_REPORT',str(ROOT/'validation/hdr-gpu.json')))
    try:report=run()
    except Exception:report=dict(status='failed',error=traceback.format_exc())
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
