from copy import deepcopy
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from test_studio_ui import app,window,wait
from luma.engine import DEFAULTS,defaults,develop,normalized,export_image,load_image,to_srgb
from luma.panel_keys import PANEL_KEYS,ALWAYS,OWNER,OFF_PANELS,SWITCHES,effective,edited,is_on,switch_on,carried,panels_of
from luma.develop_panels import PANELS,Panel
from luma.widgets import Adjustment


def owner(widget):
    while widget is not None and not isinstance(widget,Panel):widget=widget.parentWidget()
    return widget.key if widget else None


def plain(settings,panel):
    """settings with the panel's values at their defaults and every panel on: the 'no edit in this panel' look."""
    return {**settings,'panels_off':[],**{k:deepcopy(DEFAULTS[k]) for k in PANEL_KEYS[panel]}}


def test_every_default_key_belongs_to_at_most_one_panel():
    keys=[key for owned in PANEL_KEYS.values() for key in owned]
    assert len(keys)==len(set(keys)) and not set(keys)&set(ALWAYS)
    assert set(keys)|set(ALWAYS)==set(DEFAULTS) and DEFAULTS['panels_off']==[]
    assert set(PANEL_KEYS)<={key for key,_ in PANELS}-{'crop','metadata'}
    assert OFF_PANELS==tuple(p for p in PANEL_KEYS if p!='photo_filter') and SWITCHES=={'photo_filter':'photo_filter_enabled'}
    # One order and known names only, so equal states compare equal.
    assert normalized({'panels_off':['effects','basic','photo_filter','nope']})['panels_off']==['basic','effects']
    assert normalized({'panels_off':'basic'})['panels_off']==[]


# One visible edit per panel; switching the panel off must render exactly as if it were never made.
EDITS={'basic':{'exposure':.6,'vibrance':30},'curve':{'parametric':[30.,0.,0.,-20.]},
       'mixer':{'hsl':[[0.,-60.,10.]]*8},'grading':{'grading':[[30.,60.,0.],[0.,0.,0.],[200.,40.,0.]]},
       'detail':{'sharpen':120},'lens':{'distortion':40.,'lens_vignette':-50.},'transform':{'perspective_v':40.},
       'effects':{'vignette':60.,'grain':40.},'calibration':{'calibration':[[40.,50.],[0.,0.],[0.,-40.]]},
       'masks':{'masks':[dict(name='m',enabled=True,opacity=1,components=[dict(type='brush',points=[[.5,.5]],radius=.3,feather=0)],
                              adjustments={'exposure':1})]},
       'healing':{'retouch':[dict(type='heal',version=2,points=[[.5,.5]],source=[.2,.3],radius=.1,feather=30)]}}


@pytest.mark.parametrize('panel',[*EDITS,'film'])
def test_off_panel_renders_as_without_its_edits(panel):
    yy,xx=np.mgrid[:72,:108];image=np.stack([.15+xx/150,.2+yy/110,.45+.3*np.sin(xx/7)],-1).astype(np.float32)
    if panel=='film':
        from luma.film_look import catalog
        values={'film_look':catalog()[0]['id'],'film_strength':100.}
    else:values=EDITS[panel]
    s=normalized({**defaults(),**values});off=normalized({**s,'panels_off':[panel]})
    assert edited(s,panel) and is_on(s,panel) and not is_on(off,panel) and edited(off,panel)
    result=develop(image,off)
    np.testing.assert_array_equal(result,develop(image,plain(s,panel)))
    assert not np.allclose(result,develop(image,s),atol=1e-4)
    assert all(off[k]==s[k] for k in PANEL_KEYS[panel])          # the values are kept
    assert effective(s) is s and effective(off)['panels_off']==[panel]


def test_switch_on_carried_and_batch_edits(tmp_path):
    s={**defaults(),'panels_off':['basic','effects']}
    assert switch_on(deepcopy(s),('exposure','crop','photo_filter_enabled'))['panels_off']==['effects']
    assert switch_on({**defaults()},('photo_filter_density',))['photo_filter_enabled']
    # Pasting a panel's values brings its on/off state; panels not pasted keep the target's.
    source={**defaults(),'panels_off':['effects']};target={**defaults(),'panels_off':['basic','detail']}
    assert carried(target,source,panels_of(['vignette','exposure']))==['detail','effects']
    # Batch edits (menus, Codex, AI connection) switch on the panels whose values they change, as the editor does;
    # settings copied from another photo (switch=False) keep their own states.
    from PIL import Image
    from luma.catalog import Catalog
    from luma.command_batch import apply
    from luma import photo_service as service
    path=tmp_path/'a.png';Image.new('RGB',(32,24),(120,130,140)).save(path)
    catalog=Catalog(tmp_path/'data');ident=catalog.add(path);catalog.set_settings(ident,normalized(s))
    record=lambda:{'id':ident,'path':catalog.photo(ident)['path'],'settings':catalog.photo(ident)['settings']}
    current=record();apply(catalog,[current],[{**current['settings'],'exposure':.5}],remember=False)
    assert catalog.photo(ident)['settings']['panels_off']==['effects']
    current=record();apply(catalog,[current],[{**current['settings'],'grain':10.}],remember=False,switch=False)
    assert catalog.photo(ident)['settings']['panels_off']==['effects'] and catalog.photo(ident)['settings']['grain']==10
    summary=service.summary({'id':1,'name':'a.png','path':str(path),'settings':catalog.photo(ident)['settings']})
    assert summary['off']==['effects'] and 'off' not in service.summary({**record(),'name':'a','settings':defaults()})
    catalog.close()


def test_switch_controls_match_the_controls_placed_in_each_panel(window):
    w=window;s=w.studio;p=w.tabs
    placed=[(c,key) for key,c in w.adjustments.items()]
    placed+=[(c,key) for key,_,controls in s.group_controls for c in controls]
    placed+=[(w.curve,'curve'),(s.channel_curve,'rgb_curves'),*[(c,'parametric') for c in s.parametric],
             *[(c,'hsl') for c in w.hsl_widgets],(s.mixer,'mixer_mode'),(s.points,'point_colors'),(w.bw,'monochrome'),
             (s.kelvin,'kelvin_enabled'),(s.film_choice,'film_look'),(s.photo_filter_choice,'photo_filter'),
             (s.photo_filter_luminosity,'photo_filter_luminosity'),(s.upright_crop,'upright_crop'),(s.working,'working_space'),
             *[(c,key) for key,c in s.lens_switches.items()],*[(c,key) for key,c in s.dcp_switches.items()]]
    for control,key in placed:
        # A control's value is switched by the panel it sits in, or by no panel (how the file is read, the crop).
        assert OWNER.get(key,owner(control))==owner(control) and (key in OWNER or key in ALWAYS),(key,owner(control))
    switches=[p.sections[key].switch for key in PANEL_KEYS]
    assert all(c is not None for c in switches) and p.sections['crop'].switch is None and p.sections['metadata'].switch is None
    assert len({(c.size().width(),c.size().height()) for c in switches})==1
    assert not hasattr(s,'photo_filter_enabled')      # one on/off per panel: the Colour Filter's is its header switch
    visible=[p.sections[key] for key in PANEL_KEYS if p.sections[key].isVisible()]
    assert len({panel.header.width()-panel.switch.geometry().right() for panel in visible})==1


def test_switching_off_and_on_renders_undoes_exports_and_persists(window,tmp_path):
    w=window;p=w.tabs;section=p.sections['effects']
    assert all(not p.sections[k].dot.isVisibleTo(p.sections[k]) for k in PANEL_KEYS)
    w.set_setting('exposure',.6);w.set_setting('vignette',55);w.finish_interaction();wait(lambda:not w.render_running)
    shown=w.view.on_screen.copy();steps=len(w.undo_stack)
    dots={k for k in PANEL_KEYS if p.sections[k].dot.isVisibleTo(p.sections[k])}
    assert dots=={'basic','effects'} and section.switch.isChecked() and not section.header.property('panelOff')
    expanded=[k for k,s in p.sections.items() if s.header.isChecked()]
    section.switch.click();wait(lambda:not w.render_running)
    assert [k for k,s in p.sections.items() if s.header.isChecked()]==expanded     # the switch does not fold the panel
    assert w.settings['panels_off']==['effects'] and w.settings['vignette']==55 and len(w.undo_stack)==steps+1
    assert not section.switch.isChecked() and section.header.property('panelOff') and section.dot.isVisibleTo(section)
    np.testing.assert_allclose(w.view.on_screen,develop(w.source,plain(w.settings,'effects')),atol=1e-6)
    assert not np.allclose(w.view.on_screen,shown,atol=1e-3)
    record=w.catalog.photo(w.current_id);assert record['settings']['panels_off']==['effects']
    out=tmp_path/'off.tif';export_image(record['path'],out,record['settings'],'TIFF 16-bit')
    np.testing.assert_allclose(to_srgb(load_image(out)[0]),w.view.on_screen,atol=3e-5)
    from luma.photo_images import render_preview
    np.testing.assert_array_equal(render_preview(record,512,w.catalog.directory),
                                  render_preview({**record,'settings':plain(record['settings'],'effects')},512,w.catalog.directory))
    w.undo();wait(lambda:not w.render_running)
    assert w.settings['panels_off']==[] and section.switch.isChecked();np.testing.assert_allclose(w.view.on_screen,shown,atol=1e-6)
    w.redo();wait(lambda:not w.render_running);assert not section.switch.isChecked()
    # Turning on again restores the edits; then off for the reload below.
    section.switch.click();wait(lambda:not w.render_running)
    np.testing.assert_allclose(w.view.on_screen,shown,atol=1e-6);assert len(w.undo_stack)==steps+2
    section.switch.click();wait(lambda:not w.render_running)
    from luma.app import MainWindow
    restored=MainWindow(w.catalog.directory)
    try:
        restored.show();wait(lambda:not restored.library_loading)
        restored.activate(w.current_id);wait(lambda:restored.source is not None and not restored.render_running)
        again=restored.tabs.sections['effects']
        assert restored.settings['panels_off']==['effects'] and restored.settings['vignette']==55
        assert not again.switch.isChecked() and again.header.property('panelOff') and again.dot.isVisibleTo(again)
        np.testing.assert_allclose(restored.view.on_screen,w.view.on_screen,atol=1e-6)
    finally:restored.close()


def test_editing_a_switched_off_panel_switches_it_on_in_the_same_step(window):
    w=window;p=w.tabs;section=p.sections['effects']
    w.set_setting('vignette',40);w.finish_interaction();section.switch.click();wait(lambda:not w.render_running)
    steps=len(w.undo_stack)
    w.adjustments['vignette'].slider.setValue(70);w.finish_interaction();wait(lambda:not w.render_running)
    assert section.switch.isChecked() and w.settings['panels_off']==[] and len(w.undo_stack)==steps+1
    np.testing.assert_allclose(w.view.on_screen,develop(w.source,w.settings),atol=1e-6)
    w.undo();wait(lambda:not w.render_running)
    assert w.settings['panels_off']==['effects'] and w.settings['vignette']==40 and not section.switch.isChecked()
    # Switching a panel without edits: no dot, title dimmed. Masks and healing show their dot on the tool strip.
    p.sections['detail'].switch.click();QApplication.processEvents()
    detail=p.sections['detail'];assert detail.header.property('panelOff') and not detail.dot.isVisibleTo(detail)
    assert all(dot.isHidden() for dot in p.tool_dots.values()) and set(p.tool_dots)=={'masks','healing'}
    w.studio.new_layer();w.studio.add_component(dict(type='brush',points=[[.5,.5]],radius=.2,feather=0,operation='add'))
    w.commit();assert not p.tool_dots['masks'].isHidden() and p.tool_dots['healing'].isHidden()
    # Copy and paste bring each pasted panel's on/off state along.
    w.copy_edits();section.switch.click();assert w.settings['panels_off']==['detail']
    w.paste_edits();wait(lambda:not w.render_running)
    assert w.settings['panels_off']==['detail','effects'] and not section.switch.isChecked()
