"""The settings each Develop panel holds, for the panels' on/off switches (no Qt).

A panel switched off keeps its values; everything that renders (preview, thumbnails, export) uses the
defaults instead (effective). Keys outside every panel (ALWAYS) always apply: they decide how the file is
read (RAW colour, camera profile, working space, negative inversion), frame it (crop tool) or are modes
and process versions, and switching them per panel would mean reading the file again.
"""
from copy import deepcopy

PANEL_KEYS={
    'basic':('temperature','tint','kelvin','wb_reference','kelvin_enabled','exposure','contrast','highlights',
             'shadows','whites','blacks','texture','clarity','dehaze','vibrance','saturation','monochrome',
             'hdr_limit','hdr_brightness','sdr_exposure','sdr_compression','tone_mapping'),
    'film':('film_look','film_strength'),
    'curve':('curve','parametric','rgb_curves'),
    'mixer':('mixer_mode','hsl','point_colors'),
    'grading':('grading','grading_balance'),
    'detail':('sharpen','sharpen_radius','sharpen_detail','sharpen_mask','noise_luma','noise_color'),
    'lens':('lensfun','lensfun_enabled','lensfun_distortion','lensfun_tca','lensfun_vignette','lensfun_scale',
            'distortion','distortion_k2','distortion_k3','lens_vignette','ca_red','ca_blue','defringe','lens_profile'),
    'transform':('upright','upright_crop','perspective_v','perspective_h','aspect_scale','shift_x','shift_y',
                 'transform_scale','guides'),
    'effects':('vignette','vignette_midpoint','vignette_roundness','vignette_feather','vignette_highlights',
               'grain','grain_size','grain_roughness'),
    'photo_filter':('photo_filter','photo_filter_enabled','photo_filter_density','photo_filter_luminosity'),
    'calibration':('calibration','camera_matrix'),
    'masks':('masks',),
    'healing':('retouch',),
}
ALWAYS=('crop','rotation','straighten','flip','video_color','video_time','hdr','tone_version','detail_version',
        'working_space','raw_mode','raw_kelvin','raw_tint','raw_neutral','dcp_profile','dcp_huesat','dcp_look',
        'dcp_tone','dcp_exposure','negative','negative_base','negative_peak','negative_range','negative_density',
        'panels_off')
# The Colour Filter panel always had its own on/off value: its switch is that value, not an entry in panels_off.
SWITCHES={'photo_filter':'photo_filter_enabled'}
OFF_PANELS=tuple(panel for panel in PANEL_KEYS if panel not in SWITCHES)    # what panels_off can name, in order
OWNER={key:panel for panel,keys in PANEL_KEYS.items() for key in keys}


def is_on(settings,panel):
    switch=SWITCHES.get(panel)
    return bool(settings.get(switch)) if switch else panel not in (settings.get('panels_off') or ())


def edited(settings,panel):
    """Whether any of the panel's values differs from the defaults (on or off)."""
    from .engine import DEFAULTS
    return any(settings.get(key,DEFAULTS[key])!=DEFAULTS[key] for key in PANEL_KEYS[panel])


def effective(settings):
    """The settings as rendered: the values of switched-off panels replaced by their defaults (in a new dict;
    the same dict when every panel is on)."""
    off=settings.get('panels_off')
    if not off:return settings
    from .engine import DEFAULTS
    result=dict(settings)
    for panel in off:
        for key in PANEL_KEYS.get(panel,()):result[key]=deepcopy(DEFAULTS[key])
    return result


def switch_on(settings,keys):
    """Switch on, in place, the panels that own the changed keys: an edit made in a panel that is off shows,
    as in Lightroom. A panel's own switch value does not count as an edit."""
    for panel in {OWNER[key] for key in keys if key in OWNER and key not in SWITCHES.values()}:
        if is_on(settings,panel):continue
        if panel in SWITCHES:settings[SWITCHES[panel]]=True
        else:settings['panels_off']=[p for p in settings.get('panels_off') or () if p!=panel]
    return settings


def changed_keys(before,after):
    return [key for key in OWNER if before.get(key)!=after.get(key)]


def panels_of(keys):
    keys=set(keys)
    return {panel for panel,owned in PANEL_KEYS.items() if keys.intersection(owned)}


def carried(target,source,panels):
    """target's panels_off after pasting the values of `panels` from source: each pasted panel takes the
    source's on/off state, so a pasted look appears as it did on the source photo."""
    off=set(target.get('panels_off') or ());source_off=set(source.get('panels_off') or ())
    for panel in panels:
        if panel in source_off:off.add(panel)
        else:off.discard(panel)
    return [panel for panel in OFF_PANELS if panel in off]
