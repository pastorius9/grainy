"""Bundle the installed runtime distributions' license files with the release."""
from importlib import metadata
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
# The Windows release folder by default; tools/build_macos.py passes the app bundle's folder.
destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'release' / 'Grainy' / 'THIRD_PARTY_LICENSES'
MAC = sys.platform == 'darwin'
destination.mkdir(parents=True, exist_ok=True)
names = ['PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6',
         'numpy', 'Pillow', 'rawpy', 'tifffile', 'opencv-python-headless',
         'piexif', 'Send2Trash', 'imagecodecs', 'ExifRead', 'lensfunpy', 'setuptools']
index = ['Grainy third-party runtime notices', '',
         'Qt/PySide6: https://www.qt.io/licensing/open-source-lgpl-obligations',
         'Qt source: https://download.qt.io/official_releases/qt/',
         'PySide source: https://code.qt.io/cgit/pyside/pyside-setup.git/',
         'Natural Earth map data (public domain): https://www.naturalearthdata.com/about/terms-of-use/',
         'Map source: https://github.com/nvkelso/natural-earth-vector', '',
         f'Dynamic Qt libraries are in {"Contents/Frameworks/PySide6" if MAC else "_internal/PySide6"} and may be replaced by compatible versions.',
         'Lensfun (LGPL-3.0), lens database (CC BY-SA 3.0), lensfunpy (MIT): see Lensfun/.',
         f'Lensfun {"libraries in Contents/Frameworks/lensfunpy" if MAC else "DLLs in _internal/lensfunpy"} may be replaced with ABI-compatible builds.',
         'LittleCMS 2.19.0 native transform library (MIT): see LittleCMS/.',
         'Pinned source: https://github.com/mm2/Little-CMS/tree/lcms2.19',
         'See each distribution directory for its bundled dependency notices.', '']
for name in names:
    dist = metadata.distribution(name)
    index.append(f'{dist.metadata["Name"]} {dist.version}')
    for entry in dist.files or []:
        if any(part.lower() in ('license', 'licenses', 'license.txt', 'license.md',
                                'copying', 'copying.txt', 'copying.lesser',
                                'license-3rd-party.txt') or part.lower().startswith('license.')
               for part in entry.parts):
            source = Path(dist.locate_file(entry))
            # A package named "licenses" is code, not a notice; its compiled files also name the build folder.
            if source.is_file() and source.suffix not in ('.py', '.pyc') and '__pycache__' not in entry.parts:
                target = destination / name / Path(*[p for p in entry.parts if p not in ('..', '.')])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
python_license = next((p for p in (Path(sys.base_prefix) / 'LICENSE.txt', *Path(sys.base_prefix).glob('lib/python3*/LICENSE.txt'))
                       if p.is_file()), None)
if python_license:
    shutil.copy2(python_license, destination / 'Python-LICENSE.txt')
shutil.copytree(ROOT/'assets'/'licenses'/'Lensfun',destination/'Lensfun',dirs_exist_ok=True)
shutil.copytree(ROOT/'assets'/'licenses'/'Color',destination/'Color',dirs_exist_ok=True)
native_notice=destination/'LittleCMS';native_notice.mkdir(exist_ok=True)
for name in ('LICENSE','SOURCE.json'):
    shutil.copy2(ROOT/'vendor/lcms2-2.19'/name,native_notice/name)
(destination / 'NOTICE.txt').write_text('\n'.join(index), encoding='utf-8')
print(f'Bundled notices for {len(names)} distributions.')
