"""Compose an AI batch off the GUI thread, in entry order, without catalog writes."""
from copy import deepcopy

from . import photo_service as photos


def crop_value(value):
    if value is None:return None
    if not isinstance(value,list) or len(value)!=4 or not all(photos._number(v) and 0<=v<=1 for v in value):
        raise photos.ServiceError('crop must be null or [left, top, right, bottom] in 0..1')
    if value[0]>=value[2] or value[1]>=value[3]:
        raise photos.ServiceError('crop needs left < right and top < bottom')
    return [float(v) for v in value]


def automatic(record,settings,entry,directory):
    """Exactly the crop tool's auto level and Workflow's scan-border crop, on their own source."""
    from .engine import load_image,geometry,resize_float,normalized
    from . import file_access,preview_store
    from .panel_keys import switch_on,changed_keys
    if file_access.present(record['path']):
        source,_=load_image(record['path'],1400,settings['working_space'],raw_options=settings)
    else:source,_=preview_store.load_offline(directory,record['id'],settings)
    result=deepcopy(settings)
    if entry.get('auto_straighten'):
        from .upright import estimate
        from .optics import lens_shading
        analysis={**result,'upright':None,'straighten':0,'crop':None}
        image=geometry(lens_shading(resize_float(source,1400),analysis),analysis,False)
        result.update(straighten=0,upright=estimate(image,'level'))
    if entry.get('auto_crop'):
        from .scan_border import crop_for
        crop=crop_for(source,result)
        if crop is not None:result['crop']=list(crop)
    return normalized(switch_on(result,changed_keys(settings,result)))


def compose(entries,records,sources,directory,deadline):
    from .mcp_io import check_deadline
    from .engine import normalized
    originals={r['id']:r for r in records}
    states={ident:deepcopy(r['settings']) for ident,r in {**sources,**originals}.items()}
    failures={};presets={}
    for entry in entries:
        check_deadline(deadline)
        # All members of this entry copy the same source at the start of the entry.
        source=deepcopy(states[entry['copy_from']]) if entry.get('copy_from') is not None else None
        for ident in entry['ids']:
            if ident not in originals or ident in failures:continue
            check_deadline(deadline)
            record=originals[ident];value=states[ident]
            try:
                if entry.get('preset') is not None:
                    value=photos.preset_settings(entry['base'],value,record)
                    presets[ident]=entry['preset_name']
                elif source is not None:
                    if entry['copy_from'] in failures:raise photos.ServiceError('copy_from source could not be edited')
                    value=photos.copied_settings(source,value,record['info'],entry['keys'])
                if entry['changes'] is not None:value=photos.edited(value,*entry['changes'],record['info'],ident)
                if entry.get('auto_straighten') or entry.get('auto_crop'):
                    value=automatic(record,value,entry,directory)
                if 'crop' in entry:value={**value,'crop':entry['crop']}
                states[ident]=normalized(value)
            except Exception as error:
                failures[ident]=str(error) if isinstance(error,photos.ServiceError) else photos.reason(error,record['path'])
                states[ident]=record['settings'];presets.pop(ident,None)
    check_deadline(deadline)
    used=[r for r in records if r['id'] not in failures]
    return used,[states[r['id']] for r in used],[{'id':i,'error':e} for i,e in failures.items()],presets
