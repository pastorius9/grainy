"""Release-binary self-test in an isolated temporary catalog, without account RPCs."""
def run():
    import json,os,tempfile,traceback
    from . import engine
    engine.HDR_FEATURE=True   # switched off in the app; the bundled HDR path is still verified
    from pathlib import Path
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QEventLoop,QTimer
    from PIL import Image
    import numpy as np
    from .app import MainWindow,configure_application
    from .engine import export_image,load_image
    from .extras import MapView
    output=Path(os.environ.get('LUMA_SELF_TEST_REPORT',str(Path(tempfile.gettempdir())/'luma-self-test.json')))
    report={};window=None;windows=os.name=='nt'
    try:
        app=QApplication.instance() or QApplication([]);configure_application(app)
        with tempfile.TemporaryDirectory(prefix='luma-release-test-',ignore_cleanup_errors=True) as root:
            # Resolved: TEMP can be an 8.3 short path (CI runners), which would not match stored photo paths.
            root=Path(root).resolve();photo=root/'fixture.png';Image.new('RGB',(600,400),(100,140,180)).save(photo)
            window=MainWindow(root/'catalog');errors=[];window.show_error=errors.append
            # The steps below place files themselves; automatic folder import would race them on slow machines.
            window.folder_sync.set_enabled(False)
            ident=window.catalog.add(photo);window.refresh_lists();window.activate(ident);window.show()
            loop=QEventLoop();timer=QTimer();timer.setInterval(30)
            timer.timeout.connect(lambda:loop.quit() if errors or window.source is not None and not window.render_running and not window.library_loading else None)
            timer.start();QTimer.singleShot(30000,loop.quit);loop.exec();timer.stop()
            assert not errors and window.source is not None and not window.render_running,str(errors)
            assert window.photo_model.rowCount()==1 and window.grid.model() is window.filmstrip.model()
            assert window.photo_model.cached_rows<=window.photo_model.PAGE_SIZE*window.photo_model.MAX_PAGES
            window.studio.new_layer();window.studio.mode('brush');window.studio.stroke([[.5,.5]],False)
            window.studio.local_changed('exposure',.5);window.commit();window.render()
            timer.start();QTimer.singleShot(30000,loop.quit);loop.exec();timer.stop()
            out=root/'out.tif';export_image(photo,out,window.settings,'TIFF 16-bit',color_space='Display P3',keep_metadata=True,user_metadata={'caption':'검증'})
            assert load_image(out)[0].shape==(400,600,3)
            import imagecodecs
            dense=np.repeat(np.arange(12000,16096,dtype=np.uint16)[None,:,None],3,-1)
            fixture=root/'sixteen.png';fixture.write_bytes(imagecodecs.png_encode(dense))
            exported=root/'sixteen-out.png'
            from .engine import defaults
            export_image(fixture,exported,defaults(),'PNG 16-bit',keep_metadata=True,user_metadata={'latitude':0.,'longitude':0.})
            assert np.array_equal(imagecodecs.png_decode(exported.read_bytes()),dense)
            assert load_image(exported)[1]['latitude']==0
            # AVIF (Pillow plugin) and JPEG XL (imagecodecs) encoders are bundled in the release.
            from PIL import Image
            export_image(photo,root/'out.avif',window.settings,'AVIF',keep_metadata=True)
            assert Image.open(root/'out.avif').format=='AVIF'
            export_image(photo,root/'out.jxl',window.settings,'JPEG XL',keep_metadata=True)
            assert imagecodecs.jpegxl_decode((root/'out.jxl').read_bytes()).shape==(400,600,3)
            window.tabs.setCurrentIndex(5)
            timer.start();QTimer.singleShot(30000,loop.quit);loop.exec();timer.stop()
            assert not errors and window.view.tool_overlay is not None,str(errors)
            from .engine import develop
            assert np.allclose(window.view.on_screen,develop(window.source,window.settings),atol=1e-6)
            from .optics import lens_records,cameras,make_profile,lens_shading,lens_geometry
            lens=next(r for r in lens_records() if '18-105' in r['model'])
            optics=defaults();optics.update(lensfun=make_profile(lens,1.523,18,3.5),lensfun_enabled=True)
            constant=np.full((120,180,3),.2,np.float32)
            shaded=lens_shading(constant,optics);corrected=lens_geometry(shaded,optics)
            assert shaded[0,0,0]>.3 and corrected.shape==constant.shape and np.isfinite(corrected).all()
            import cv2
            from .upright import estimate
            grid=np.full((400,600,3),.6,np.float32)
            for x in range(80,560,80):cv2.line(grid,(x,30),(x,370),(.02,.02,.02),3)
            for y in range(60,370,60):cv2.line(grid,(30,y),(570,y),(.02,.02,.02),3)
            tilted=cv2.warpAffine(grid,cv2.getRotationMatrix2D((299.5,199.5),-6,1),(600,400),borderValue=(.6,.6,.6))
            alignment=estimate(tilted,'level');assert abs(alignment['rotation']-6)<.2
            yy,xx=np.mgrid[:400,:600]
            horizon=np.where((yy>200+np.tan(np.deg2rad(6))*(xx-299.5))[...,None],
                np.array([.04,.14,.22]),np.array([.38,.6,.8])).astype(np.float32)
            single_horizon=estimate(horizon,'level');assert abs(single_horizon['rotation']-6)<.2
            assert window.live_cache.hits>0 and window.live_cache.bytes<=window.live_cache.max_bytes
            window.grab().save(str(output.with_suffix('.png')))
            window.tabs.setCurrentIndex(4);app.processEvents()
            window.tabs.currentWidget().ensureWidgetVisible(window.studio.auto_lens_button,0,20)
            app.processEvents();window.grab().save(str(output.with_name(output.stem+'-optics.png')))
            from .lens_dialog import LensDialog
            dialog=LensDialog(window,{'make':'NIKON CORPORATION','camera':'NIKON D7000',
                'lens':lens['model'],'focal_length':18,'aperture':3.5})
            dialog.show();app.processEvents();dialog.grab().save(str(output.with_name(output.stem+'-lens.png')))
            dialog.close()
            # Feedback needs HTTPS from the frozen app: the TLS module and Windows' root certificates must load.
            import ssl,urllib.request
            assert ssl.create_default_context().cert_store_stats()['x509_ca']>0 and urllib.request.HTTPSHandler
            from . import feedback
            assert feedback.configured()
            feedback_sent=None
            if os.environ.get('LUMA_SELF_TEST_FEEDBACK')=='1':   # an explicit, real submission; never by default
                feedback_sent=feedback.send('[self-test] connection check from the packaged app, please ignore')
                assert feedback_sent=='',feedback_sent
            mapview=MapView([],lambda _:None);assert len(mapview.scene().items())>100
            report={'status':'passed','gui':True,'tls':True,'feedback_sent':feedback_sent=='','mask_edit':True,'tiff_icc':True,'png16_precision':True,'exif_gps_zero':True,'offline_map':True,'photos_transmitted':False,'cached_preview_matches_export':True,'mask_overlay':True}
            report.update(lens_profiles=len(lens_records()),camera_profiles=len(cameras()),lens_correction=True,auto_level_degrees=alignment['rotation'])
            report.update(single_horizon_auto_level=single_horizon['algorithm']=='lines-horizon-v2')
            report.update(sparse_library=True,shared_selection=window.grid.selectionModel() is window.filmstrip.selectionModel())
            from .rawcolor import CameraSource,temperature_xyz,xyz_temperature
            from .dcp import load_profile
            dcp_path=Path(__file__).resolve().parents[1]/'assets'/'test-camera.dcp'
            native_info=dict(camera='Test Camera',xyz_to_camera=np.eye(3).tolist(),neutral=[.5,1.,.6],daylight_neutral=[.5,1.,.6])
            pixels=np.broadcast_to(np.array([.5,1.,.6],np.float32)*.2,(32,48,3)).copy()
            native=CameraSource(pixels,native_info);native_settings=defaults()
            native_settings.update(raw_mode='as_shot',dcp_profile=load_profile(dcp_path))
            profiled=develop(native,native_settings)
            assert np.max(np.ptp(profiled,axis=-1))<.0004 and np.isfinite(profiled).all()
            cct,tint=xyz_temperature(temperature_xyz(4500,20));assert abs(cct-4500)<1 and abs(tint-20)<.02
            report.update(raw_camera_wb=True,dcp_color_pipeline=True)
            from .dcp import tone_map
            curve_probe=np.array([[[0.,.5,1.]]],dtype=np.float32)
            short_diagonal=np.array([[.125,.125],[.875,.875]])
            np.testing.assert_array_equal(tone_map(curve_probe,short_diagonal),
                                          np.array([[[.125,.5,.875]]],dtype=np.float32))
            report.update(dcp_truncated_tone_endpoints=True)
            from . import illuminants
            from .dcp import compiled,weight
            triple_path=dcp_path.with_name('test-triple-illuminant.dcp')
            triple_settings=defaults();triple_settings.update(raw_mode='as_shot',dcp_profile=load_profile(triple_path))
            triple_profile=compiled(triple_settings['dcp_profile'])
            np.testing.assert_allclose(triple_profile['xy3'],[1/3,1/3],atol=1e-12)
            np.testing.assert_allclose(weight(triple_profile,5000,illuminants.xy_to_xyz(triple_profile['xy3'])),[0,0,1],atol=1e-12)
            triple_source,triple_info=load_image(triple_path.with_suffix('.dng'),raw_options=triple_settings)
            triple_pixels=develop(triple_source,triple_settings)
            assert np.isfinite(triple_pixels).all() and np.max(np.ptp(triple_pixels[20:-20,20:-20],axis=-1))<.0004
            report.update(triple_illuminant_profile=True,custom_spectral_illuminant=True,triple_dng_decode=True)
            from .catalog import Catalog
            from .catalog_maintenance import optimize
            from .library import restore_backup
            storage=Catalog(root/'profile-storage')
            profile_id=storage.add(root/'fixture.NEF',native_settings)
            storage.save_preference(f'undo:{profile_id}',{'undo':[native_settings]*3,'redo':[]})
            storage.save_preset('profile',native_settings)
            assert storage.db.execute('SELECT COUNT(*) FROM profile_assets').fetchone()[0]==1
            assert storage.photo(profile_id)['settings']==native_settings
            backup=storage.backup(root/'profile-backup.sqlite')
            storage.set_settings(profile_id,defaults());restore_backup(storage,backup)
            assert storage.photo(profile_id)['settings']==native_settings
            compact=optimize(storage.directory)
            assert compact['integrity']=='ok' and compact['profiles']==1
            storage.close()
            report.update(profile_asset_deduplication=True,profile_backup_restore=True,profile_optimization=True)
            from .profile_library import scan,compatible,load_selected
            from .raw_defaults import make_rule,resolve,iso_anchors
            found=scan([dcp_path.parent],window.catalog.directory/'test-profile-index.sqlite')
            matches=compatible(found['profiles'],native_info)
            assert len(matches)==2
            original_match=next(p for p in matches if p['sha256']==native_settings['dcp_profile']['sha256'])
            assert load_selected(original_match,native_info)==native_settings['dcp_profile']
            assert scan([dcp_path.parent],window.catalog.directory/'test-profile-index.sqlite')['parsed']==0
            anchors=iso_anchors([dict(path='test.NEF',info={**native_info,'make':'Test','iso':iso},
                settings={**defaults(),'noise_luma':value}) for iso,value in [(400,0),(1600,10)]])
            rule=make_rule({**native_info,'make':'Test'},native_settings,anchors=anchors)
            imported,notes=resolve({**native_info,'make':'Test','format':'NEF','iso':800},[rule])
            assert imported['noise_luma']==5 and imported['dcp_profile']==native_settings['dcp_profile'] and notes
            from .profile_dialog import RawDefaultsDialog
            default_dialog=RawDefaultsDialog(window);default_dialog.show();app.processEvents()
            default_dialog.grab().save(str(output.with_name(output.stem+'-defaults.png')));default_dialog.close()
            report.update(dcp_discovery=True,raw_import_defaults=True,iso_adaptive=True)
            from . import preview_store
            cache_photo=root/'cache-fixture.png';Image.new('RGB',(120,80),(40,90,150)).save(cache_photo)
            cache_id=window.catalog.add(cache_photo)
            assert preview_store.rebuild(window.catalog.directory,cache_id)=='갱신 완료'
            assert not preview_store.inspect(window.catalog.directory,cache_id)['rebuild']
            old_state=preview_store.snapshot(window.catalog.directory,cache_id)
            window.catalog.edit(cache_id,{'exposure':.5})
            assert not preview_store.publish(window.catalog.directory,old_state,Image.new('RGB',(120,80),'red'))
            assert preview_store.rebuild(window.catalog.directory,cache_id)=='갱신 완료'
            cache_photo.rename(root/'cache-fixture-unplugged.png')
            assert preview_store.inspect(window.catalog.directory,cache_id)['status']=='오프라인'
            report.update(versioned_thumbnails=True,stale_thumbnail_rejected=True,offline_thumbnail=True)
            from . import colorio
            from .display_color import DisplayDialog
            from .engine import output_rgb
            monitor=root/'display.icc';monitor.write_bytes(colorio.profile('Display P3'))
            before_display=window.view.on_screen.copy();window.display_color.configure('manual',monitor)
            color_loop=QEventLoop();color_timer=QTimer();color_timer.setInterval(20)
            def color_ready():return window.display_color.main.profile is not None and not window.render_running
            color_timer.timeout.connect(lambda:color_loop.quit() if color_ready() else None)
            color_timer.start();QTimer.singleShot(30000,color_loop.quit);color_loop.exec();color_timer.stop()
            assert color_ready() and np.array_equal(before_display,window.view.on_screen)
            display_dialog=DisplayDialog(window.display_color);display_dialog.show();app.processEvents()
            display_dialog.grab().save(str(output.with_name(output.stem+'-display.png')));display_dialog.close()
            p3=np.array([[[.94,.03,.02],[.02,.85,.03]]],np.float32)
            # Use tagged P3 encoded pixels for a direct gamut-preservation check.
            working=colorio.convert(p3,colorio.profile('Display P3'),colorio.profile('Luma Wide'))
            np.testing.assert_allclose(output_rgb(working,'ProPhoto',monitor.read_bytes()),p3,atol=3e-4)
            report.update(per_window_display_profiles=True,display_settings=True,wide_gamut_display=True)
            from . import native_color
            import imagecodecs
            # The native libraries ship with the Windows release only; elsewhere the Python paths are the engine.
            assert native_color.available() or not windows, 'Bundled native color library missing'
            if native_color.available():
                color_sample=np.random.default_rng(119).uniform(-.1,1.2,(385,401,3)).astype(np.float32)
                source_icc=colorio.profile('Luma Wide');target_icc=colorio.profile('Display P3')
                independent=imagecodecs.cms_transform(color_sample,source_icc,target_icc,
                    colorspace='rgb',outcolorspace='rgb',outdtype='float32',intent=1)
                for _ in range(2):np.testing.assert_array_equal(native_color.convert(color_sample,source_icc,target_icc),independent)
                assert native_color.cache_info()['hits']>0
                report.update(native_color_engine=True,native_color_exact_float=True,native_color_cache=True)
            else:report.update(native_color_engine=False)
            from . import native_dcp,dcp
            assert native_dcp.available() or not windows, 'Bundled native DCP library missing'
            if native_dcp.available():
                rng=np.random.default_rng(323)
                coords=[rng.uniform(0,d,(31,73)).astype(np.float32) for d in (4,12,7)]
                for dtype in (np.float32,np.float64):
                    table=rng.uniform(-30,2,(5,12,8,3)).astype(dtype)
                    np.testing.assert_array_equal(native_dcp.interpolate(coords,table),dcp._table_values_numpy(coords,table))
                report.update(native_dcp_engine=True,native_dcp_exact_float32=True,native_dcp_exact_float64=True)
            else:report.update(native_dcp_engine=False)
            from .lightroom_import import import_catalog,inspect_catalog
            import sqlite3
            lr=root/'reference.lrcat'
            with sqlite3.connect(lr) as db:
                db.executescript('''
                    CREATE TABLE AgLibraryRootFolder(id_local INTEGER,absolutePath TEXT);
                    CREATE TABLE AgLibraryFolder(id_local INTEGER,rootFolder INTEGER,pathFromRoot TEXT);
                    CREATE TABLE AgLibraryFile(id_local INTEGER,folder INTEGER,baseName TEXT,extension TEXT);
                    CREATE TABLE Adobe_images(id_local INTEGER,rootFile INTEGER,rating INTEGER,pick INTEGER,masterImage INTEGER);
                    INSERT INTO AgLibraryFolder VALUES(1,1,'');
                    INSERT INTO AgLibraryFile VALUES(1,1,'fixture','png');
                    INSERT INTO Adobe_images VALUES(1,1,4,1,NULL);
                    INSERT INTO Adobe_images VALUES(2,1,2,-1,1);
                    CREATE TABLE Adobe_imageDevelopSettings(id_local INTEGER,image INTEGER,text TEXT);
                    INSERT INTO Adobe_imageDevelopSettings VALUES(1,1,'s={Exposure2012=.75}');
                    CREATE TABLE Adobe_libraryImageDevelopHistoryStep(id_local INTEGER,image INTEGER,name TEXT,text TEXT,dateCreated REAL);
                    INSERT INTO Adobe_libraryImageDevelopHistoryStep VALUES(1,1,'Initial','s={Exposure2012=0}',1);
                    CREATE TABLE AgLibraryCollection(id_local INTEGER,name TEXT,creationId TEXT);
                    INSERT INTO AgLibraryCollection VALUES(1,'Four stars','com.adobe.ag.library.smart_collection');
                    CREATE TABLE AgLibraryCollectionContent(id_local INTEGER,collection INTEGER,owningModule TEXT,content TEXT);
                    INSERT INTO AgLibraryCollectionContent VALUES(1,1,'ag.library.smart_collection','s={{criteria="rating",operation=">=",value=4}}');
                ''')
                db.execute('INSERT INTO AgLibraryRootFolder VALUES(1,?)',(str(root),))
            db.close()
            assert inspect_catalog(lr)['counts']['Adobe_images']==2
            preserved=window.catalog.photo(ident)
            migration=import_catalog(lr,window.catalog.directory)
            assert migration['imported']==2 and migration['copies']==2
            assert window.catalog.photo(ident)==preserved
            repeated=import_catalog(lr,window.catalog.directory)
            assert repeated['imported']==0 and repeated['reused']==2
            report.update(lightroom_atomic_import=True,lightroom_virtual_copies=True,lightroom_idempotent=True)
            translated=import_catalog(lr,window.catalog.directory,convert_edits=True)
            assert translated['converted_photos']==translated['converted_history']==translated['converted_smart']==1
            assert window.catalog.photo(ident)==preserved
            imported_id=window.catalog.db.execute('SELECT MAX(id) FROM photos WHERE rating=4').fetchone()[0]
            assert window.catalog.photo(imported_id)['settings']['exposure']==.75
            assert window.catalog.preference(f'undo:{imported_id}')['undo'][0]['exposure']==0
            from .smart_rules import translate,KEY
            from .library_query import query
            from threading import Event
            descriptor=translate('s={{criteria="rating",operation=">=",value=4}}')
            assert imported_id in query(window.catalog.directory/'catalog.sqlite',{'rules':descriptor},Event())['ids']
            from .adobe_preset import read,resolve
            adobe_path=root/'test.lrtemplate';adobe_path.write_text('s={value={settings={Exposure2012=.5}}}',encoding='utf-8')
            adobe_preset=read(adobe_path);window.catalog.save_adobe_preset('Adobe Test',adobe_preset)
            partial=resolve(window.catalog.presets()['Adobe Test'],{**defaults(),'shadows':22},'test.jpg')
            assert partial['exposure']==.5 and partial['shadows']==22
            report.update(adobe_develop_translation=True,adobe_history_undo=True,adobe_dynamic_smart_collection=True,adobe_partial_preset=True)
            from . import pixel_jobs
            from .engine import to_linear,to_srgb
            pixels=np.random.default_rng(173).random((194,702,3),dtype=np.float32)
            expected=np.where(pixels<=.04045,pixels/12.92,((pixels+.055)/1.055)**2.4).astype(np.float32)
            np.testing.assert_array_equal(to_linear(pixels),expected)
            expected=np.where(pixels<=.0031308,pixels*12.92,1.055*pixels**(1/2.4)-.055).astype(np.float32)
            np.testing.assert_array_equal(to_srgb(pixels),expected)
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(4) as callers:
                jobs=[callers.submit(pixel_jobs.transform,pixels,
                    lambda tile:pixel_jobs.transform(tile,lambda part:part*2)) for _ in range(4)]
                for job in jobs:np.testing.assert_array_equal(job.result(timeout=10),pixels*2)
            report.update(parallel_pixel_math=True,parallel_pixel_exact=True,parallel_nested_requests=True)
            from .processing import layer_mask
            from .mask_dialog import MaskComponentDialog
            stroke=dict(type='brush',points=[[.5,.5]],radius=.2,feather=70,paint_version=2,flow=20,density=60)
            image=np.full((101,101,3),.5,np.float32)
            coverage=layer_mask(image,image.shape,dict(components=[stroke,stroke]),lambda a:a)
            assert abs(float(coverage[50,50])-.216)<1e-6
            dialog=MaskComponentDialog(window,stroke);dialog.fields['flow'].setValue(35)
            assert dialog.result_component()['flow']==35;dialog.close()
            assert all(key in window.studio.local_controls for key in ('whites','blacks','texture','dehaze','sharpen','noise_luma','noise_color'))
            report.update(mask_flow_density=True,mask_component_editor=True,local_detail_controls=True)
            from .processing import _local_region
            colors=np.full((40,60,3),[.2,.5,.7],np.float32);colors[:,30:]=[.8,.2,.1]
            gated={**stroke,'points':[[.3,.5],[.7,.5]],'seed':[.3,.5],'radius':.4,'flow':100,'density':100,'auto_mask':True,'auto_tolerance':.12}
            mask=layer_mask(colors,colors.shape,dict(components=[gated]),lambda a:a,source_image=colors)
            assert mask[20,18]>.99 and mask[20,40]==0
            red=np.full((10,10,3),[.8,.1,.1],np.float32)
            np.testing.assert_allclose(_local_region(red,np.ones((10,10),np.float32),dict(hue=120))[5,5],[.1,.8,.1],atol=2e-6)
            from PySide6.QtTest import QTest
            from PySide6.QtCore import Qt,QPointF
            window.tabs.setCurrentIndex(5);window.studio.mode('brush');window.commit()
            before=window.catalog.photo(window.current_id)['settings']
            point=window.view.mapFromScene(QPointF(window.view.image_rect.width()*.5,window.view.image_rect.height()*.5))
            QTest.mousePress(window.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
            assert window.studio.pending_stroke is not None and window.catalog.photo(window.current_id)['settings']==before
            QTest.keyClick(window.view,Qt.Key.Key_Escape)
            QTest.mouseRelease(window.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
            assert window.studio.pending_stroke is None and window.settings==before
            report.update(live_stroke_transaction=True,brush_color_boundary=True,local_curves_hue=True)
            from .point_color import sample as sample_color,preview as color_coverage
            color_settings=defaults();color_pixels=np.full((80,120,3),[.8,.2,.2],np.float32)
            color_settings['grading']=[[120,70,0]]*3;color_settings['monochrome']=True
            np.testing.assert_allclose(sample_color(to_linear(color_pixels),color_settings,[.5,.5]),[.8,.2,.2],atol=2e-6)
            color_settings=defaults();groups=[[0,0,0] for _ in range(8)];groups[0]=[0,-40,0]
            color_settings['masks']=[dict(components=[dict(type='brush',points=[[.5,.5]],radius=.25,feather=0)],adjustments=dict(hsl=groups))]
            selected_rgb=sample_color(to_linear(color_pixels),color_settings,[.5,.5],0)
            color_settings['masks'][0]['adjustments']['point_colors']=[dict(version=2,rgb=selected_rgb,range=.12,saturation_range=.6,lightness_range=.3,hue=80,saturation=0,lightness=0)]
            selected_mask=color_coverage(to_linear(color_pixels),color_settings,(0,0))
            assert selected_mask[40,60]>.99 and selected_mask[0,0]==0
            assert len(window.studio.local_hsl_controls)==3 and window.studio.local_hsl_channel.count()==8
            assert 'lightness_range' in window.studio.local_point_controls and 'saturation_range' in window.studio.point_controls
            report.update(local_hsl_mixer=True,stage_correct_color_picker=True,point_color_range_overlay=True)
            from .range_mask import sample_selection,color_coverage
            range_settings=defaults();range_settings['masks']=[dict(components=[],adjustments=dict(hue=120))]
            chosen=sample_selection(to_linear(color_pixels),range_settings,[[.5,.5]],0)
            np.testing.assert_allclose(chosen['colors'],[[.8,.2,.2]],atol=2e-6)
            multicolor=np.array([[[.8,.2,.2],[.2,.8,.2],[.2,.2,.8]]],np.float32)
            selection=color_coverage(multicolor,[[.8,.2,.2],[.2,.8,.2]],.1)
            np.testing.assert_array_equal(selection,[[1,1,0]])
            assert set(window.studio.range_controls)=={'low','high','softness','tolerance'}
            report.update(mask_input_sampler=True,multiple_color_range=True,live_range_controls=True)
            from .source_cache import DecodedSourceCache
            cache_path=root/'cache-fixture.png';Image.new('RGB',(48,32),(90,150,30)).save(cache_path)
            source_cache=DecodedSourceCache();cache_settings=defaults();decode_calls=[]
            def cached_read():
                decode_calls.append(True)
                return load_image(cache_path,working_space=cache_settings['working_space'],raw_options=cache_settings)
            cached_pixels,_=source_cache.load(cache_path,cache_settings,None,cached_read)
            cache_settings['exposure']=1
            cached_again,_=source_cache.load(cache_path,cache_settings,None,cached_read)
            assert cached_again is cached_pixels and len(decode_calls)==1 and not cached_again.flags.writeable
            np.testing.assert_array_equal(develop(cached_again,cache_settings),develop(cached_read()[0],cache_settings))
            Image.new('RGB',(49,32),(40,80,170)).save(cache_path)
            refreshed,_=source_cache.load(cache_path,cache_settings,None,cached_read)
            assert refreshed.shape==(32,49,3) and source_cache.bytes<=source_cache.max_bytes
            assert window.source_pool is not window.pool and window.source_pool.maxThreadCount()==2
            report.update(decoded_source_cache=True,source_cache_file_invalidation=True,source_cache_pixels_preserved=True,independent_source_workers=True)
            assert window.auto_level_button.text()=='자동 수평 맞추기' and window.auto_level_note.wordWrap()
            report.update(crop_auto_level_control=True)
            from copy import deepcopy
            window.set_setting('upright',alignment);window.set_setting('straighten',2)
            window.set_setting('crop',[.1,.1,.9,.9]);window.commit()
            framed=deepcopy(window.settings)
            window.crop_reset_button.click()
            assert window.settings=={**framed,'crop':None,'straighten':0,'upright':None}
            assert window.catalog.photo(window.current_id)['settings']==window.settings
            window.undo();assert window.settings==framed
            window.redo();assert window.settings['upright'] is None and window.settings['crop'] is None
            report.update(crop_reset_auto_level=True,crop_reset_undo=True)
            from .engine import resize_float,to_linear
            yy,xx=np.mgrid[:192,:288].astype(np.float32)
            rgb=np.repeat((.4+.08*np.sin(xx*.085)*np.cos(yy*.063)+(xx>130)*.15)[...,None],3,-1)
            pixels=to_linear(rgb);small=resize_float(pixels,144)
            detail=defaults();detail.update(sharpen=85,sharpen_radius=2.1,sharpen_detail=100)
            full=develop(pixels,detail)
            np.testing.assert_array_equal(full,develop(pixels,detail,original_size=(288,192)))
            effect=resize_float(full,144)-resize_float(develop(pixels,defaults()),144)
            neutral=develop(small,defaults())
            scaled=develop(small,detail,original_size=(288,192))
            previous=develop(small,detail)
            assert np.mean(abs(scaled-neutral-effect))<np.mean(abs(previous-neutral-effect))*.7
            report.update(preview_original_pixel_scale=True,full_resolution_filter_preserved=True)
            from . import __version__
            from PySide6.QtWidgets import QLabel
            assert any(label.text()==f'PHOTO STUDIO  /  {__version__}' for label in window.findChildren(QLabel))
            report.update(app_version=__version__)
            from .command_batch import capture,targets,apply,restore
            from .commands import validate_proposal,changed_settings
            command_folder=root/'command-fixture';command_folder.mkdir()
            command_ids=[]
            for index in range(2):
                path=command_folder/f'color-{index}.png'
                Image.new('RGB',(40,30),(100,60+index*50,200)).save(path)
                command_ids.append(window.catalog.add(path,{**defaults(),'exposure':index*.5}))
            command_snapshot=capture(window.catalog,command_ids[0],[command_ids[0]],str(command_folder))
            records=targets(command_snapshot,'folder')
            command=validate_proposal({'message':'test','action':'adjust','scope':'folder',
                'changes':[{'key':'monochrome','value':True,'operation':'set'}]})
            assert apply(window.catalog,records,[changed_settings(r['settings'],command) for r in records])==command_ids
            assert all(window.catalog.photo(i)['settings']['monochrome'] for i in command_ids)
            assert window.catalog.photo(command_ids[1])['settings']['exposure']==.5
            assert restore(window.catalog)==command_ids
            assert not any(window.catalog.photo(i)['settings']['monochrome'] for i in command_ids)
            assert restore(window.catalog,True)==command_ids
            from .command_dialog import CommandDialog
            assert not window.auth.connected
            command_dialog=CommandDialog(window.auth,window,window.command_context,window.apply_command,window.restore_command_batch)
            command_dialog.show();app.processEvents()
            assert command_dialog.scope.findData('folder')>=0
            assert command_dialog.batch_undo.isVisible()
            command_dialog.close()
            report.update(command_folder_scope=True,command_typed_monochrome=True,command_atomic_batch_undo=True)
            from .dust import detect,repair,Options,visualize
            from .dust_dialog import DustDialog
            from PySide6.QtWidgets import QPushButton
            yy,xx=np.mgrid[:180,:240];dusty=np.full((180,240),.5,np.float32)
            dusty[(xx-65)**2+(yy-80)**2<=16]=.15
            dusty[(xx-170)**2+(yy-110)**2<=25]=.9
            found=detect(dusty)
            assert len(found['spots'])==2
            cleaned=repair(np.repeat(to_linear(dusty)[...,None],3,-1),found['spots'])
            assert abs(float(to_srgb(cleaned)[80,65,0])-.5)<.015
            assert abs(float(to_srgb(cleaned)[110,170,0])-.5)<.015
            edged=np.full((220,300),.88,np.float32);ey,ex=np.mgrid[:220,:300]
            edged[ey<95]=.2;edged[(ex-150)**2+(ey-100)**2<=9]-=.24
            detailed=detect(edged,Options(detailed=True))
            assert any(s['review'] and abs(s['x']*299-150)<3 and abs(s['y']*219-100)<3 for s in detailed['spots'])
            assert visualize(dusty).max()>.1
            rgb=np.repeat(to_linear(dusty)[...,None],3,-1)
            scaled=repair(rgb,[{**found['spots'][0],'scale':60}])
            assert np.isfinite(scaled).all() and np.any(scaled!=rgb)
            assert any(button.text()=='먼지 자동 감지 · 후보 확인…' for button in window.findChildren(QPushButton))
            report.update(dust_detection_bright_dark=True,dust_local_repair=True,dust_review_runtime=True)
            report.update(dust_detailed_review=True,dust_emphasis_preview=True,dust_scaled_repair=True)
            from .dust import _median,_quantiles,_repeated_detail
            samples=np.random.default_rng(411).random(127,dtype=np.float32)
            assert _median(samples)==float(np.median(samples))
            np.testing.assert_array_equal(_quantiles(samples,.1,.6,.9),[np.percentile(samples,q) for q in (10,60,90)])
            pattern=np.full((120,120),.6,np.float32)
            for y in range(15,115,16):
                for x in range(15,115,16):cv2.circle(pattern,(x,y),2,.35,-1)
            assert _repeated_detail(cv2.GaussianBlur(pattern,(0,0),.65),45,45,5,5)
            cv2.circle(pattern,(47,47),2,.15,-1)
            assert not _repeated_detail(cv2.GaussianBlur(pattern,(0,0),.65),45,45,5,5)
            report.update(dust_fast_statistics=True,dust_repeated_pattern_intensity=True)
            dust_review=DustDialog(window)
            assert not dust_review.options().reduce_patterns and not dust_review.more_button.isVisible()
            assert not dust_review.options().soft and not dust_review.soft.isEnabled()
            dust_review.detailed.setChecked(True)
            assert dust_review.reduce_patterns.isEnabled() and dust_review.soft.isEnabled()
            dust_review.soft.setChecked(True)
            assert dust_review.options().soft
            assert dust_review.suppress_grain.isEnabled() and not dust_review.suppress_grain.isChecked()
            dust_review.suppress_grain.setChecked(True)
            assert dust_review.options().suppress_grain
            assert not dust_review.scratches.isChecked() and not dust_review.scratch_width.isEnabled()
            dust_review.scratches.setChecked(True);assert dust_review.scratch_width.isEnabled()
            dust_review.reject();dust_review.deleteLater();app.processEvents()
            report['dust_review_paging_runtime']=True
            sy,sx=np.mgrid[:240,:320]
            grain=(.5+np.random.default_rng(20261001).normal(0,.055,(240,320))).astype(np.float32)
            soft_truth=[(80,60,'dark'),(160,80,'bright'),(240,160,'dark'),(128,180,'bright')]
            for x,y,kind in soft_truth:
                grain+=(.14 if kind=='bright' else -.14)*np.exp(-((sx-x)**2+(sy-y)**2)/(2*3.3**2)).astype(np.float32)
            ordinary=detect(grain,Options(detailed=True),limit=5000)
            extra=detect(grain,Options(detailed=True,soft=True),limit=5000)
            assert extra['spots'][:len(ordinary['spots'])]==ordinary['spots']
            assert all(any(s['polarity']==kind and abs(s['x']*319-x)<=3 and abs(s['y']*239-y)<=3 for s in extra['spots']) for x,y,kind in soft_truth)
            assert all(s['review'] and s['detail_kind']=='soft' for s in extra['spots'][len(ordinary['spots']):])
            report.update(dust_soft_grain_detection=True,dust_soft_existing_preserved=True,dust_soft_explicit_review=True)
            strict=detect(grain,Options(detailed=True,soft=True,suppress_grain=True),limit=5000)
            assert strict['spots'][:len(ordinary['spots'])]==ordinary['spots']
            assert all(any(s['polarity']==kind and abs(s['x']*319-x)<=3 and abs(s['y']*239-y)<=3 for s in strict['spots']) for x,y,kind in soft_truth)
            report.update(dust_optional_grain_suppression=True)
            from .dust_shapes import ellipses as filament_ellipses,paint as paint_filament,manual as manual_filament
            filament_image=np.full((240,320),.6,np.float32)
            cv2.polylines(filament_image,[np.array([[40,135],[80,195],[180,175],[255,75]],np.int32)],False,.15,3)
            filament_result=detect(filament_image,Options(scratches=True),limit=5000)
            filaments=[s for s in filament_result['spots'] if s.get('detail_kind')=='scratch']
            assert len(filaments)==1 and filaments[0]['review'] and len(filaments[0]['parts'])>10
            filament_rgb=np.repeat(filament_image[...,None],3,-1)
            filament_fixed=repair(filament_rgb,filaments)
            filament_mask=np.zeros(filament_image.shape,np.uint8);paint_filament(filament_mask,filament_ellipses(filaments[0],filament_image.shape))
            np.testing.assert_array_equal(filament_fixed[filament_mask==0],filament_rgb[filament_mask==0])
            assert np.abs(filament_fixed[...,0]-.6)[filament_image<.3].mean()<.02
            manual_line=manual_filament([[.2,.2],[.4,.5],[.7,.3]],filament_image.shape,8)
            assert manual_line['manual'] and len(manual_line['parts'])>10
            report.update(dust_filament_detection=True,dust_filament_narrow_footprint=True,dust_manual_filament=True)
            texture_clean=(.5+np.random.default_rng(31591).normal(0,.035,(240,320,3))).astype(np.float32)
            texture_mask=np.zeros(texture_clean.shape[:2],np.uint8);paint_filament(texture_mask,filament_ellipses(manual_line,texture_clean.shape))
            texture_dirty=texture_clean.copy();texture_dirty[texture_mask>0]=.025
            texture_fixed=repair(texture_dirty,[manual_line],method='texture')
            texture_core=cv2.erode(texture_mask,np.ones((3,3),np.uint8))>0
            reference_noise=(texture_clean-cv2.GaussianBlur(texture_clean,(0,0),1.5))[texture_core].std()
            restored_noise=(texture_fixed-cv2.GaussianBlur(texture_fixed,(0,0),1.5))[texture_core].std()
            assert .7<restored_noise/reference_noise<1.3
            np.testing.assert_array_equal(texture_fixed[texture_mask==0],texture_dirty[texture_mask==0])
            report.update(dust_texture_repair=True,dust_texture_outside_preserved=True)
            from .dust_batch_dialog import DustBatchDialog
            from .dust_batch import apply_reviewed,restore as restore_dust
            import time
            dust_folder=root/'dust-batch';dust_folder.mkdir()
            dust_ids=[]
            for index in range(2):
                path=dust_folder/f'dust-{index}.png'
                sample=dusty if index==0 else np.flip(dusty,axis=1)
                Image.fromarray(np.uint8(np.repeat(sample[...,None],3,-1)*255)).save(path)
                dust_ids.append(window.catalog.add(path))
            previous_folder=window.folder_filter;window.folder_filter=str(dust_folder)
            dust_batch=DustBatchDialog(window);dust_batch.start()
            deadline=time.monotonic()+15
            while dust_batch.running or dust_batch.busy:
                app.processEvents();time.sleep(.005)
                assert time.monotonic()<deadline,'Batch detection timed out'
            assert len(dust_batch.entries)==2 and all(e['state']=='ready' and e['result']['total']==2 for e in dust_batch.entries)
            for entry in dust_batch.entries:
                review=DustDialog(window,record=entry['record'],cached=dict(result=entry['result'],options=entry['options']))
                deadline=time.monotonic()+15
                while review.result is None or review.busy:
                    app.processEvents();time.sleep(.005)
                    assert time.monotonic()<deadline,'Batch review timed out'
                review.apply_selection();entry.update(state='reviewed',operation=review.operation,review=review.review_state)
                review.deleteLater()
            assert dust_batch.persist(range(len(dust_batch.entries)))
            saved_id=dust_batch.session_id
            assert all(e['state']=='reviewed' for e in dust_batch.store.load(saved_id)[1])
            dust_batch.apply()
            assert all(e['state']=='applied' for e in dust_batch.store.load(saved_id)[1])
            assert all(window.catalog.photo(i)['settings']['retouch'] for i in dust_ids)
            dust_batch.restore(False)
            assert all(not window.catalog.photo(i)['settings']['retouch'] for i in dust_ids)
            dust_batch.restore(True)
            assert all(window.catalog.photo(i)['settings']['retouch'] for i in dust_ids)
            dust_batch.reject();dust_batch.deleteLater();window.folder_filter=previous_folder;app.processEvents()
            report.update(dust_batch_detection=True,dust_batch_review_runtime=True,dust_batch_atomic_undo=True)
            report.update(dust_review_checkpoints=True,dust_review_atomic_application=True)
            from .i18n import set_language,tr
            from .language_dialog import LanguageDialog
            from .preferences import Preferences
            prefs=Preferences(root/'preferences.json');assert prefs.language is None
            prefs.save_language('en');assert Preferences(root/'preferences.json').language=='en'
            set_language('en');assert tr('설정')=='Settings';assert tr('HDR 영역 표시')=='Visualize HDR ranges'
            chooser=LanguageDialog(first_run=True);assert chooser is not None;chooser.close()
            set_language('ko')
            assert window.windowTitle().startswith('Grainy') and not hasattr(window,'auth_button')
            from PySide6.QtWidgets import QDoubleSpinBox
            assert isinstance(window.adjustments['exposure'].value,QDoubleSpinBox)
            from .hdr import histogram
            hdr_settings={**defaults(),'hdr':True,'exposure':3}
            hdr_output=root/'hdr-output.tif'
            export_image(photo,hdr_output,hdr_settings,'TIFF HDR 32-bit')
            hdr_source,_=load_image(photo)
            hdr_expected=to_linear(develop(hdr_source,hdr_settings,output_space=None))
            hdr_decoded,_=load_image(hdr_output)
            np.testing.assert_allclose(hdr_decoded,hdr_expected,atol=1e-5,rtol=1e-5)
            assert hdr_decoded.max()>1
            window.histogram.hdr=True;window.histogram.set_image(develop(hdr_source,hdr_settings,output_space=None))
            assert sum(histogram(develop(hdr_source,hdr_settings,output_space=None))[0][64:])>0
            report.update(grainy_branding=True,language_assets=True,numeric_controls=True,
                          hdr_extended_pixels=True,hdr_tiff_roundtrip=True,hdr_histogram=True,
                          native_hdr_display_supported=__import__('luma.native_hdr',fromlist=['available']).available(),
                          physical_hdr_display_verified=False)
            from .processing import apply_retouch,brush_mask
            healing_source=np.full((160,240,3),.4,np.float32);healing_source[76:85,116:125]=0
            healing_operation=dict(type='heal',version=2,points=[[.5,.5],[.55,.5]],source=None,radius=.07,feather=40)
            healed=apply_retouch(healing_source,[healing_operation])
            np.testing.assert_allclose(healed[76:85,116:125],.4,atol=1e-5)
            healing_mask=brush_mask(healing_source.shape,healing_operation['points'],.07,40)
            np.testing.assert_array_equal(healed[healing_mask==0],healing_source[healing_mask==0])
            window.studio.mode('heal');window.studio.preview_stroke([[.5,.5]],False)
            assert window.studio.pending_stroke['staged']
            assert not window.studio.preview_settings(defaults())['retouch']
            healing_baseline=deepcopy(window.settings)
            window.studio.stroke([[.5,.5]],False);window.studio.stroke([[.7,.5]],False)
            assert len(window.studio.healing_strokes)==2 and window.settings==healing_baseline
            assert window.view.healing_overlay is not None
            window.studio.apply_healing()
            assert len(window.settings['retouch'])==len(healing_baseline['retouch'])+2
            window.undo();assert window.settings==healing_baseline
            report.update(automatic_healing_source=True,healing_boundary_match=True,
                          healing_outside_preserved=True,healing_batch_execute=True,healing_batch_undo=True)
            from .folder_locations import scan_folders,relink_plan,apply_relink
            from .folders import path_key
            from threading import Event
            location=root/'folder-location';location.mkdir()
            moved_photo=location/'sample.png';Image.new('RGB',(20,20)).save(moved_photo)
            moved_id=window.catalog.add(moved_photo);window.catalog.edit(moved_id,{'exposure':.75})
            seen=scan_folders([str(location)],[str(location)],{path_key(location)},{},Event())
            empty=location/'new-empty';empty.mkdir()
            assert str(empty) in scan_folders([str(location)],[str(location)],{path_key(location)},{},Event())['children']
            renamed=root/'renamed-folder';location.rename(renamed)
            discovered=scan_folders([str(location)],[str(location)],{path_key(location)},seen['identities'],Event())
            assert discovered['moves']==[(str(location),str(renamed))]
            assert apply_relink(window.catalog,relink_plan(window.catalog,location,renamed))==1
            assert window.catalog.photo(moved_id)['settings']['exposure']==.75
            assert str(location) not in window.catalog.folder_roots()
            assert window.folder_panel.timer.interval()==5000
            report.update(live_folder_discovery=True,folder_identity_rename=True,folder_relink_preserves_edits=True)
            from .folder_recovery import recover_folders,validate_recovery
            from .folder_locations import apply_relinks
            from .engine import thumbnail
            from PIL import ImageDraw
            legacy=root/'legacy-folder';legacy.mkdir();legacy_ids=[]
            for n in range(3):
                sample=Image.new('RGB',(240,160),(30+n*25,50,90+n*20));drawing=ImageDraw.Draw(sample)
                drawing.rectangle((n*20,20,80+n*35,95),fill=(220,160-n*35,30))
                drawing.ellipse((120-n*30,50,210-n*10,150),fill=(40,210,160-n*30))
                path=legacy/f'{n}.jpg';sample.save(path);legacy_id=window.catalog.add(path);legacy_ids.append(legacy_id)
                thumbnail(path).save(window.catalog.thumbs/f'{legacy_id}.jpg',quality=88)
            destination=root/'found-legacy-folder';legacy.rename(destination)
            found=recover_folders(window.catalog.directory)
            assert len(found['plans'])==1 and found['plans'][0]['proof']=='saved_previews'
            assert validate_recovery(window.catalog,found['plans'])
            assert apply_relinks(window.catalog,found['plans'])==3
            report.update(legacy_folder_auto_recovery=True,legacy_preview_evidence=True)
            from .photo_filter import apply as apply_filter,FILTERS
            probe=np.full((12,16,3),.4,np.float32)
            filtered=apply_filter(probe,{**defaults(),'photo_filter_enabled':True,'photo_filter':'warming85'})
            assert filtered[0,0,0]>filtered[0,0,2] and len(FILTERS)==17
            window.wb_button.click()
            from PySide6.QtCore import Qt
            assert window.view.viewport().cursor().shape()==Qt.CursorShape.BitmapCursor
            window.wb_button.click()
            from .photo_actions import build_menu
            assert any(a.data()=='quick-export' for a in build_menu(window).actions())
            quick_folder=root/'quick-export';quick_folder.mkdir()
            window.start_export([window.current_id],quick_folder,'Original',naming='{stem}')
            export_loop=QEventLoop();export_poll=QTimer();export_poll.setInterval(30)
            export_poll.timeout.connect(lambda:export_loop.quit() if not window.export_running else None)
            export_poll.start();QTimer.singleShot(30000,export_loop.quit);export_loop.exec();export_poll.stop()
            assert not window.export_running and not window.last_export['failures']
            quick_path=Path(window.last_export['outputs'][0]['path'])
            assert quick_path.read_bytes()==Path(window.catalog.photo(window.current_id)['path']).read_bytes()
            assert quick_path.with_suffix('.xmp').is_file()
            report.update(quick_export=True,original_export_sidecar=True,color_filter_presets=17,wb_eyedropper_cursor=True)
            before_panels=window.catalog.photo(window.current_id)['settings']
            window.tabs.open_panel('photo_filter');app.processEvents()
            assert window.tabs.is_open('photo_filter') and not window.tabs.is_open('basic')
            window.tabs.currentWidget().ensureWidgetVisible(window.adjustments['distortion']);app.processEvents()
            assert window.tabs.is_open('lens') and window.adjustments['distortion'].isVisible()
            assert window.adjustments['exposure'].property('compact')
            assert window.catalog.photo(window.current_id)['settings']==before_panels
            report.update(develop_accordion=True,solo_panels=True,compact_sliders=True,advanced_controls_reachable=True)
            from .native_hdr import available
            assert available() or not windows,'Missing native HDR renderer'
            window.tabs.open_panel('basic')
            window.settings={**defaults(),'hdr':True,'exposure':2};window.load_controls();window.render_version+=1;window.render()
            def wait_hdr():
                loop=QEventLoop();poll=QTimer();poll.setInterval(20)
                poll.timeout.connect(lambda:loop.quit() if not window.render_running and not window.refine_timer.isActive() else None)
                poll.start();QTimer.singleShot(15000,loop.quit);loop.exec();poll.stop()
                assert not window.render_running and not window.refine_timer.isActive()
            wait_hdr();assert not window.hdr_sdr_preview.isChecked()
            direct=window.view.on_screen.copy();extended=window.view.hdr_pixels.copy()
            np.testing.assert_allclose(direct,np.clip(extended,0,1),atol=1e-6)
            before=window.settings.copy();window.hdr_sdr_preview.click();wait_hdr()
            assert window.view.on_screen.mean()<direct.mean() and window.settings==before
            window.hdr_sdr_preview.click();wait_hdr();np.testing.assert_array_equal(window.view.on_screen,direct)
            assert window.curve.hdr and window.curve.axis(1)==.5
            report.update(hdr_preview_separate=True,hdr_sdr_white_preserved=True,hdr_curve_extended=True)
            window.close();window=None
            # The GPU module must ship; a PC without a usable GPU keeps the CPU path.
            from . import native_gpu
            from .engine import _develop_tone_pixels,normalized,defaults
            assert native_gpu.available() or not windows
            report['gpu_status']=native_gpu.status()
            if native_gpu.available() and not report['gpu_status'].startswith('unavailable'):
                sample=np.random.default_rng(7).random((64,96,3),dtype=np.float32)
                edits=normalized({**defaults(),'exposure':.4,'shadows':30,'contrast':20,'curve':[[0,0],[.5,.6],[1,1]],'tone_version':2})
                assert np.abs(native_gpu.tone(sample,edits)-_develop_tone_pixels(sample.copy(),edits)).max()<2e-6
                current={**edits,'tone_version':3,'highlights':-40}      # process 3 Highlights/Shadows (tone_response)
                assert np.abs(native_gpu.tone(sample,current)-_develop_tone_pixels(sample.copy(),current)).max()<2e-5
                report['gpu_tone_local_matches_cpu']=True
                report['gpu_tone_matches_cpu']=True
    except Exception:
        report={'status':'failed','error':traceback.format_exc()}
        if window:window.close()
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['status']=='passed' else 1
