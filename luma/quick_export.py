"""Quick export presets and lossless original copies with Grainy XMP settings."""
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil

PREFERENCE = 'previous_photo_export'
FORMATS = ('JPEG', 'AVIF', 'JPEG XL', 'PNG', 'PNG 16-bit', 'TIFF 16-bit', 'TIFF 8-bit', 'TIFF HDR 32-bit', 'Original')
SUFFIXES = {'JPEG':'.jpg','AVIF':'.avif','JPEG XL':'.jxl','PNG':'.png','PNG 16-bit':'.png','TIFF 16-bit':'.tif','TIFF 8-bit':'.tif',
            'TIFF HDR 32-bit':'.tif'}


def preset(kind):
    if kind not in ('small', 'large', 'original'):
        raise ValueError(kind)
    return dict(format='Original' if kind == 'original' else 'JPEG',
                quality=80 if kind == 'small' else 100,
                longest=2048 if kind == 'small' else 0,
                color_space='sRGB', keep_metadata=False, naming='{stem}', grayscale=True, dither=True)


def previous(catalog):
    value = catalog.preference(PREFERENCE)
    if not isinstance(value, dict):
        return None
    try:
        result = {key: value[key] for key in ('folder', 'format', 'quality', 'longest', 'color_space', 'keep_metadata', 'naming')}
        assert isinstance(result['folder'], str) and result['folder']
        assert result['format'] in FORMATS
        from . import engine
        assert result['format'] != 'TIFF HDR 32-bit' or engine.HDR_FEATURE
        assert type(result['quality']) is int and 40 <= result['quality'] <= 100
        assert type(result['longest']) is int and 0 <= result['longest'] <= 30000
        assert result['color_space'] in ('sRGB', 'Adobe RGB', 'Display P3', 'ProPhoto RGB')
        assert type(result['keep_metadata']) is bool
        # Added in 0.5.53; earlier saved settings keep working with both enabled.
        for key in ('grayscale', 'dither'):
            result[key] = value.get(key, True)
            assert type(result[key]) is bool
        name = result['naming'].format(stem='photo', n=1)
        assert name.strip() and Path(name).name == name and not any(c in name for c in '<>:"/\\|?*')
        return result
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, AssertionError):
        return None


def copy_original(record, destination):
    """Create a new original/XMP pair exclusively; clean up only our own files."""
    from .library import write_sidecar
    destination = Path(destination)
    sidecar = destination.with_suffix('.xmp')
    created = []
    try:
        with TemporaryDirectory(prefix='grainy-export-') as temporary:
            settings = Path(temporary) / 'settings.xmp'
            write_sidecar(record, settings)
            for source, target in ((Path(record['path']), destination), (settings, sidecar)):
                with source.open('rb') as incoming, target.open('xb') as outgoing:
                    created.append(target)
                    shutil.copyfileobj(incoming, outgoing)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    info = record.get('info', {})
    return (info.get('width', 0), info.get('height', 0))


def export_concurrency(records):
    """Photos exported at once: RAW decoding is single-threaded, so a few in parallel raise throughput.

    Measured 0.5.46 (i9-12900K, 12 MP): 1 -> 3 at a time cut NEF 0.79 -> 0.36 s and TIFF
    0.43 -> 0.29 s per photo. Measured 0.5.83 (same PC, twelve 12-26 MP raws to JPEG): 3 at a time
    8.4 s, 4 at a time 7.5 s, 6 at a time 7.6 s. Bounded by CPU count and a quarter of RAM.
    """
    import os
    from .render_cache import physical_memory
    cpus = os.cpu_count() or 1
    if len(records) < 2 or cpus < 8:
        return 1
    pixels = max((int(r['info'].get('width') or 0)*int(r['info'].get('height') or 0) for r in records), default=0) or 24_000_000
    budget = (physical_memory() or 8*1024**3)//4
    per_photo = pixels*3*4*8   # float32 RGB source plus develop/export intermediates
    return int(max(1, min(4, cpus//6, budget//per_photo)))
