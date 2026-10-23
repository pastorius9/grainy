"""Select Sky: a classical sky mask found from colour, smoothness and position."""
from copy import deepcopy
import numpy as np
import pytest
from PIL import Image
from luma.sky_mask import sky_mask,DEFAULTS
from luma.processing import layer_mask
from luma.engine import develop,defaults,geometry,to_linear,export_image
from test_studio_ui import app,window,wait

SKY={'type':'sky',**DEFAULTS}


def landscape(h=240,w=360,seed=0,water=False):
    """sRGB frame: a blue sky paling towards hills over textured ground, or over a tree line and calm
    water that mirrors the sky."""
    rng=np.random.default_rng(seed);y,x=np.mgrid[:h,:w].astype(np.float32);t=y/h
    sky=np.stack([.30+.45*t,.50+.38*t,.88+.08*t],-1)
    ground=np.array([.22,.30,.12],np.float32)+rng.normal(0,.07,(h,w,3)).astype(np.float32)
    if not water:
        horizon=(.45+.04*np.sin(x[0]/23)+.02*np.sin(x[0]/7))*h
        return np.clip(np.where(y[...,None]<horizon[None,:,None],sky,ground),0,1).astype(np.float32),horizon
    mirror=np.stack([.30+.45*(1-t),.50+.38*(1-t),.88+.08*(1-t)],-1)*.8
    image=np.where((t<.35)[...,None],sky,np.where((t<.5)[...,None],ground*.6,mirror))
    return np.clip(image,0,1).astype(np.float32),None


def test_sky_found_with_a_soft_edge_that_follows_the_horizon():
    image,horizon=landscape();mask=sky_mask(image,SKY)
    assert mask.dtype==np.float32 and mask.shape==image.shape[:2] and mask.min()>=0 and mask.max()<=1
    y=np.arange(image.shape[0])[:,None]
    above=y<horizon[None,:]-4;below=y>horizon[None,:]+4
    assert mask[above].mean()>.95 and mask[below].mean()<.02
    # The edge follows the hills column by column, and is soft rather than a step.
    for column in range(0,image.shape[1],37):
        edge=int(horizon[column])
        assert mask[edge-4,column]>.8 and mask[edge+4,column]<.2
    soft=lambda m:((m>.05)&(m<.95)).sum()
    assert soft(mask)>0 and soft(sky_mask(image,{**SKY,'feather':100}))>soft(sky_mask(image,{**SKY,'feather':0}))


def test_reflection_below_a_tree_line_is_not_sky():
    image,_=landscape(water=True);mask=sky_mask(image,SKY);h=image.shape[0]
    assert mask[:int(.3*h)].mean()>.95
    assert mask[int(.55*h):].max()<.05


@pytest.mark.parametrize('kind',['texture','indoor','dark'])
def test_frames_without_sky_give_an_empty_mask(kind):
    rng=np.random.default_rng(3);h,w=240,360;y,x=np.mgrid[:h,:w].astype(np.float32)
    if kind=='texture':
        image=rng.uniform(.1,.9,(h,w,3))
    elif kind=='indoor':
        # Warm walls and a darker ceiling, a lit window low in the frame and a lamp.
        image=np.empty((h,w,3));image[:]=[.55,.42,.30];image[:60]=[.30,.24,.18]
        image[150:200,220:300]=[.85,.9,.95];image[90:110,60:80]=[1,.95,.8]
        image=image+rng.normal(0,.01,(h,w,3))
    else:
        image=np.full((h,w,3),.05)+rng.normal(0,.01,(h,w,3))
    mask=sky_mask(np.clip(image,0,1).astype(np.float32),SKY)
    assert mask.mean()<.005


def test_deterministic_and_sensitivity_reaches_further():
    image,_=landscape(seed=5)
    assert np.array_equal(sky_mask(image,SKY),sky_mask(image.copy(),SKY))
    # A grey haze band over the hills joins the sky only at high sensitivity.
    hazy=image.copy();hazy[90:105]=hazy[90:105]*.5+np.float32([.36,.38,.40])
    low=sky_mask(hazy,{**SKY,'sensitivity':0});high=sky_mask(hazy,{**SKY,'sensitivity':100})
    assert high.mean()>=low.mean() and high[93:102].mean()>low[93:102].mean()


def test_rotation_and_crop_follow_the_shown_frame():
    image,_=landscape();source=np.ascontiguousarray(np.rot90(image,1))     # sky on the left of the source
    s={**defaults(),'rotation':1}
    shown=layer_mask(geometry(source,s),source.shape,{'components':[SKY]},lambda m:geometry(m,s),source)
    assert shown.shape==image.shape[:2] and shown[:60].mean()>.95 and shown[-60:].mean()<.02
    # Cropped to the ground there is no sky to find.
    s={**defaults(),'crop':[0,.6,1,1]}
    cropped=layer_mask(geometry(image,s),image.shape,{'components':[SKY]},lambda m:geometry(m,s),image)
    assert cropped.max()<.05


def test_invert_subtract_and_intersect_like_other_components():
    image,_=landscape();shape=image.shape;identity=lambda m:m
    sky=layer_mask(image,shape,{'components':[SKY]},identity,image)
    radial={'type':'radial','points':[[.2,.0],[.8,.6]],'feather':50}
    shape_mask=layer_mask(image,shape,{'components':[radial]},identity,image)
    np.testing.assert_allclose(layer_mask(image,shape,{'components':[SKY],'invert':True},identity,image),1-sky,atol=1e-6)
    np.testing.assert_allclose(layer_mask(image,shape,{'components':[{**SKY,'invert':True}]},identity,image),1-sky,atol=1e-6)
    np.testing.assert_allclose(layer_mask(image,shape,{'components':[radial,{**SKY,'operation':'subtract'}]},identity,image),shape_mask*(1-sky),atol=1e-6)
    np.testing.assert_allclose(layer_mask(image,shape,{'components':[radial,{**SKY,'operation':'intersect'}]},identity,image),shape_mask*sky,atol=1e-6)


def test_global_edits_do_not_move_the_sky_and_the_mask_is_cached():
    from luma.render_cache import DevelopmentCache
    image,_=landscape();source=to_linear(image);source.setflags(write=False)
    layer={'enabled':True,'components':[SKY],'adjustments':{'exposure':-1}}
    masks=[];cache=DevelopmentCache(256*1024*1024)
    for exposure in (0,1.5):
        s={**defaults(),'exposure':exposure,'masks':[layer]}
        develop(source,s,cache=cache,on_mask=lambda i,m:masks.append(m))
    assert np.array_equal(masks[0],masks[1]) and masks[0][:40].mean()>.95


def test_gpu_preview_chain_takes_the_sky_mask_like_a_shape_mask():
    from luma import native_gpu
    from luma.render_cache import DevelopmentCache
    if not native_gpu.enabled():pytest.skip(native_gpu.status())
    image,_=landscape();source=to_linear(image);source.setflags(write=False)
    s={**defaults(),'exposure':.2,'masks':[{'enabled':True,'components':[SKY],'adjustments':{'exposure':-.8,'saturation':20}}]}
    seen={}
    result=develop(source,s,cache=DevelopmentCache(256*1024*1024),on_mask=lambda i,m:seen.__setitem__(i,m))
    assert np.array_equal(result,develop(source,s)) and seen[0][:60].mean()>.95


def test_sky_mask_edit_renders_on_export(tmp_path):
    image,horizon=landscape(480,720);path=tmp_path/'sky.png'
    Image.fromarray(np.uint8(np.round(image*255))).save(path)
    s=deepcopy(defaults());plain=tmp_path/'plain.png';edited=tmp_path/'edited.png'
    export_image(path,plain,s,format='PNG')
    s['masks']=[{'name':'하늘','enabled':True,'opacity':1,'components':[dict(SKY)],'adjustments':{'exposure':-1}}]
    export_image(path,edited,s,format='PNG')
    before=np.asarray(Image.open(plain),np.float32);after=np.asarray(Image.open(edited),np.float32)
    y=np.arange(480)[:,None]
    sky=y<horizon[None,:]-8;ground=y>horizon[None,:]+8
    assert (before-after)[sky].mean()>30 and np.abs(before-after)[ground].mean()<1


def test_select_sky_from_the_masks_tool(window,tmp_path,monkeypatch):
    from luma.mask_dialog import MaskComponentDialog
    w=window;s=w.studio;errors=[];monkeypatch.setattr(w,'show_error',errors.append)
    image,_=landscape();path=tmp_path/'landscape.png';Image.fromarray(np.uint8(np.round(image*255))).save(path)
    ident=w.catalog.add(path);w.refresh_lists();w.activate(ident)
    wait(lambda:w.current_id==ident and w.source is not None and not w.render_running and not w.library_loading)
    w.tabs.setCurrentIndex(5)
    for button in (s.create_button,s.add_button,s.subtract_button):
        assert [a.text() for a in button.menu().actions() if not a.isSeparator()][0]=='하늘 선택'
    history=len(w.catalog.histories(w.current_id))
    s.create_mask('sky');wait(lambda:not w.render_running and w.presented_version==w.render_version)
    component=w.settings['masks'][0]['components'][0]
    assert component=={'type':'sky','operation':'add',**DEFAULTS} and w.view.tool_mode==''
    assert len(w.catalog.histories(w.current_id))==history+1 and not s.sky_editor.isHidden()
    assert s.mask_tree.topLevelItem(0).child(0).text(0)=='+  하늘 1'
    overlay=w.view.tool_overlay;assert overlay is not None
    # The overlay covers the sky and leaves the ground.
    assert overlay.pixelColor(overlay.width()//2,2).alpha()>90 and overlay.pixelColor(overlay.width()//2,overlay.height()-3).alpha()<10
    s.sky_controls['sensitivity'].slider.setValue(80);s.sky_controls['feather'].slider.setValue(10);w.finish_interaction()
    wait(lambda:not w.render_running)
    assert s.current_component()['sensitivity']==80 and s.current_component()['feather']==10
    s.extend_mask('sky','subtract');wait(lambda:not w.render_running)
    assert [(c['type'],c['operation']) for c in w.settings['masks'][0]['components']]==[('sky','add'),('sky','subtract')]
    s.select_component(1);dialog=MaskComponentDialog(w,s.current_component())
    dialog.fields['sensitivity'].setValue(20);dialog.fields['feather'].setValue(70);dialog.operation.setCurrentIndex(2)
    assert dialog.result_component()=={'type':'sky','operation':'intersect','enabled':True,'invert':False,'sensitivity':20,'feather':70}
    dialog.deleteLater()
    w.undo();wait(lambda:not w.render_running)
    assert [c['type'] for c in w.settings['masks'][0]['components']]==['sky']
    assert not errors,errors
