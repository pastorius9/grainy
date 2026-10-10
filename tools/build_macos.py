"""Build release/Grainy.app and a disk image on macOS (the counterpart of Build Grainy.ps1).

    .venv/bin/python tools/build_macos.py            # app, self-test, update archive, disk image
    .venv/bin/python tools/build_macos.py --no-dmg   # app and self-test only

The app is for the architecture of the Python that runs this script. It is signed ad hoc (no
Apple developer identity), so a downloaded copy has to be allowed once in System Settings.
"""
from pathlib import Path
import argparse,json,os,platform,plistlib,re,shutil,subprocess,sys,tempfile

ROOT=Path(__file__).resolve().parents[1]
RELEASE=ROOT/'release'
APP=RELEASE/'Grainy.app'
IDENTIFIER='io.github.pastorius9.grainy'
# OpenCV's macOS wheel links FFmpeg (a GPL build, with x264/x265) for video I/O, which Grainy does not
# use. As on Windows no FFmpeg is shipped: the files are removed, and empty stand-ins take the place of
# the ones OpenCV cannot load without.
FFMPEG=('libavformat','libavcodec','libavutil','libavdevice','libavfilter','libswscale','libswresample','libpostproc')


def run(*command,**options):
    print('+',' '.join(str(c) for c in command),flush=True)
    return subprocess.run([str(c) for c in command],check=True,**options)


def tool(name):run(sys.executable,ROOT/'tools'/name)


def icon():
    """assets/brand/grainy.icns from the 1024 px logo: the tile at 824 px on a clear canvas (Apple's icon grid)."""
    from PIL import Image
    target=ROOT/'assets/brand/grainy.icns';source=ROOT/'assets/brand/grainy-logo-1024.png'
    if target.exists() and target.stat().st_mtime>=source.stat().st_mtime:return target
    tile=Image.open(source).convert('RGBA').resize((824,824),Image.Resampling.LANCZOS)
    canvas=Image.new('RGBA',(1024,1024),(0,0,0,0));canvas.paste(tile,(100,100))
    with tempfile.TemporaryDirectory() as folder:
        iconset=Path(folder)/'grainy.iconset';iconset.mkdir()
        for size in (16,32,128,256,512):
            canvas.resize((size,size),Image.Resampling.LANCZOS).save(iconset/f'icon_{size}x{size}.png')
            canvas.resize((size*2,size*2),Image.Resampling.LANCZOS).save(iconset/f'icon_{size}x{size}@2x.png')
        run('iconutil','-c','icns',iconset,'-o',target)
    return target


def dependencies(path):
    """Names of the libraries a Mach-O file loads from beside itself."""
    lines=subprocess.run(['otool','-L',str(path)],capture_output=True,text=True,check=True).stdout.splitlines()[1:]
    return {Path(line.split(' (')[0].strip()).name for line in lines if line.strip().startswith(('@loader_path','@rpath'))}


def stand_in(module,library,folder):
    """Replace an FFmpeg library OpenCV links with an empty one of the same name: every function OpenCV
    imports from it exists and stops the program if it were ever called (video I/O is not used)."""
    listing=subprocess.run(['nm','-m','-u',str(module)],capture_output=True,text=True,check=True).stdout
    stem=library.name.removesuffix('.dylib')
    symbols=sorted({line.split()[-3][1:] for line in listing.splitlines() if line.rstrip().endswith(f'(from {stem})')})
    if not symbols:raise SystemExit(f'OpenCV imports nothing from {library.name}; review stand_in.')
    described=subprocess.run(['otool','-L',str(library)],capture_output=True,text=True,check=True).stdout.splitlines()[1]
    compatibility,current=re.search(r'compatibility version ([\d.]+), current version ([\d.]+)',described).groups()
    architectures=subprocess.run(['lipo','-archs',str(module)],capture_output=True,text=True,check=True).stdout.split()
    source=folder/f'{stem}.c'
    source.write_text('/* Stand-in for a library Grainy does not ship. */\n'+''.join(
        f'__attribute__((visibility("default"))) void {name}(void){{__builtin_trap();}}\n' for name in symbols),encoding='utf-8')
    library.unlink()
    run('clang','-dynamiclib','-O1','-w',*[x for a in architectures for x in ('-arch',a)],'-mmacosx-version-min=11.0',
        '-install_name',f'@rpath/{library.name}','-compatibility_version',compatibility,'-current_version',current,
        '-o',library,source,capture_output=True)
    return len(symbols)


def is_binary(path):
    if path.is_symlink() or not path.is_file():return False
    with path.open('rb') as handle:return handle.read(4) in (b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe')


def built_for(path):
    """The macOS version a Mach-O file was built for (its newest slice), or None."""
    lines=subprocess.run(['otool','-l',str(path)],capture_output=True,text=True).stdout.splitlines()
    found=[tuple(int(x) for x in field.split()[1].split('.')[:2]) for number,line in enumerate(lines) if 'LC_BUILD_VERSION' in line
           for field in lines[number:number+6] if field.strip().startswith('minos')]
    return max(found) if found else None


def minimum_system():
    """The oldest macOS the app is for, which Finder enforces: the newest system that a bundled package
    names in its wheel tag (macosx_14_0 ...), or that Python itself was built for.

    The files are not asked one by one. PySide6's binding libraries carry build version 15.0 inside a
    wheel that promises 13.0, and 0.5.84 took that at its word: built and tested on macOS 14, it then
    declared 15.0 and Finder would not open it there."""
    from importlib import metadata
    bundled=APP/'Contents/Frameworks';newest=(11,0)
    for distribution in metadata.distributions():
        tops={Path(str(file)).parts[0] for file in distribution.files or [] if Path(str(file)).parts}
        if not any((bundled/top).exists() for top in tops):continue            # a build tool, not part of the app
        for major,minor in re.findall(r'^Tag: \S*macosx_(\d+)_(\d+)_',distribution.read_text('WHEEL') or '',re.M):
            newest=max(newest,(int(major),int(minor)))
    for path in [APP/'Contents/MacOS/Grainy',*bundled.glob('Python.framework/Versions/*/Python'),*bundled.glob('libpython*.dylib')]:
        if is_binary(path):newest=max(newest,built_for(path) or newest)
    return '.'.join(str(x) for x in newest)


def remove_ffmpeg():
    cv=APP/'Contents/Frameworks/cv2'
    if not cv.is_dir():raise SystemExit('OpenCV is missing from the app.')
    module=next(cv.glob('cv2*.so'));libraries=(cv/'.dylibs').resolve()
    direct=[libraries/n for n in sorted(dependencies(module)) if n.startswith(FFMPEG)]
    if not direct:raise SystemExit('OpenCV no longer links FFmpeg directly; review remove_ffmpeg.')
    # PyInstaller keeps one copy of each library name for the whole app, and OpenCV's folder holds many of
    # them: keep what anything other than FFmpeg still loads.
    others=[p for p in (APP/'Contents').rglob('*') if is_binary(p) and p.parent.resolve()!=libraries]
    needed=set();queue=[n for path in others for n in dependencies(path)]
    while queue:
        name=queue.pop()
        if name in needed or name.startswith(FFMPEG) or not (libraries/name).exists():continue
        needed.add(name);queue.extend(dependencies(libraries/name))
    with tempfile.TemporaryDirectory() as folder:
        functions=sum(stand_in(module,library,Path(folder)) for library in direct)
    stand_ins={library.name for library in direct};removed=0
    for path in list(libraries.glob('*.dylib')):
        if path.name not in needed and path.name not in stand_ins:path.unlink();removed+=1
    # PyInstaller links every library from Contents/Frameworks and Contents/Resources as well.
    for path in list(APP.rglob('*')):
        if path.is_symlink() and not path.exists():path.unlink()
    left=[p.name for p in APP.rglob('*') if p.is_file() and not p.is_symlink()
          and p.name.lower().startswith(FFMPEG+('libx264','libx265','ffmpeg')) and not (p.name in stand_ins and p.stat().st_size<200_000)]
    if left:raise SystemExit(f'FFmpeg files remain in the app: {left}')
    print(f'Removed {removed} FFmpeg-related libraries; {len(stand_ins)} empty stand-ins ({functions} functions) keep OpenCV loading.')


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--no-dmg',action='store_true')
    args=parser.parse_args()
    if sys.platform!='darwin':raise SystemExit('Run this on macOS.')
    sys.path.insert(0,str(ROOT))
    from luma import __version__
    tool('compile_translations.py');tool('build_native_macos.py')
    for stale in (APP,RELEASE/'Grainy'):
        if stale.exists():shutil.rmtree(stale)
    run(sys.executable,'-m','PyInstaller','--clean','--noconfirm','--windowed','--name','Grainy','--icon',icon(),
        '--osx-bundle-identifier',IDENTIFIER,'--distpath',RELEASE,'--workpath',ROOT/'build','--specpath',ROOT/'build',
        '--add-data',f'{ROOT/"assets"}:assets','--add-data',f'{ROOT/"README.html"}:.','--add-data',f'{ROOT/"FEATURE_GAPS.md"}:.',
        '--add-data',f'{ROOT/"CHANGELOG.md"}:.','--collect-all','imagecodecs','--collect-all','lensfunpy',
        '--exclude-module','scipy','--exclude-module','tkinter',ROOT/'main.py',cwd=ROOT)
    shutil.rmtree(RELEASE/'Grainy',ignore_errors=True)          # PyInstaller's unbundled copy
    remove_ffmpeg()
    info=APP/'Contents/Info.plist'
    with info.open('rb') as handle:values=plistlib.load(handle)
    oldest=minimum_system();print(f'Oldest macOS the app declares: {oldest}')
    here=tuple(int(x) for x in platform.mac_ver()[0].split('.')[:2])
    if tuple(int(x) for x in oldest.split('.'))>here:
        # The self-test below runs the program file directly, so it would pass on a system Finder refuses.
        raise SystemExit(f'The app would declare macOS {oldest}, newer than this Mac ({platform.mac_ver()[0]}).')
    values.update(CFBundleName='Grainy',CFBundleDisplayName='Grainy',CFBundleShortVersionString=__version__,
        CFBundleVersion=__version__,NSHighResolutionCapable=True,LSMinimumSystemVersion=oldest,
        LSApplicationCategoryType='public.app-category.photography',
        NSHumanReadableCopyright='MIT License. Third-party notices: Contents/Resources/THIRD_PARTY_LICENSES')
    with info.open('wb') as handle:plistlib.dump(values,handle)
    run(sys.executable,ROOT/'tools/package_notices.py',APP/'Contents/Resources/THIRD_PARTY_LICENSES')
    shutil.copy2(ROOT/'LICENSE',APP/'Contents/Resources/LICENSE.txt')
    # The changes above invalidate PyInstaller's signature; Apple Silicon refuses unsigned code.
    run('codesign','--force','--deep','--sign','-',APP)
    run('codesign','--verify','--deep','--strict',APP)
    with tempfile.TemporaryDirectory() as folder:
        report=Path(folder)/'selftest.json'
        result=subprocess.run([str(APP/'Contents/MacOS/Grainy'),'--self-test'],env={**os.environ,'LUMA_SELF_TEST_REPORT':str(report)})
        outcome=json.loads(report.read_text(encoding='utf-8')) if report.exists() else {'status':'no report'}
        if result.returncode or outcome.get('status')!='passed':
            raise SystemExit(f'Self-test of the built app failed: {outcome.get("error",outcome)}')
        print(f'Self-test of the built app passed ({len(outcome)} entries).')
    # Nothing in the app may name where it was built. A hosted runner's home folder is no secret, and
    # third-party libraries built on the same service contain it; there only this checkout's path is checked.
    private=[str(ROOT).encode()]+([] if os.environ.get('CI') else [str(Path.home()).encode()])
    leaked=[p.relative_to(APP) for p in APP.rglob('*') if p.is_file() and not p.is_symlink() and any(text in p.read_bytes() for text in private)]
    if leaked:raise SystemExit(f'Files in the app name the folder it was built in: {leaked[:10]}')
    if not args.no_dmg:
        machine={'arm64':'arm64','x86_64':'x64'}.get(platform.machine(),platform.machine())
        # The zip is what the app's own updater downloads (luma/updater.py); the disk image is for people.
        archive=RELEASE/f'Grainy-{__version__}-macos-{machine}.zip'
        archive.unlink(missing_ok=True);run('ditto','-c','-k','--keepParent',APP,archive)
        print(f'Update archive: {archive.relative_to(ROOT)}')
        image=RELEASE/f'Grainy-{__version__}-macos-{machine}.dmg'
        with tempfile.TemporaryDirectory() as folder:
            stage=Path(folder)/'Grainy'
            stage.mkdir();run('ditto',APP,stage/'Grainy.app');os.symlink('/Applications',stage/'Applications')
            run('hdiutil','create','-volname','Grainy','-srcfolder',stage,'-ov','-format','UDZO',image)
        print(f'Disk image: {image.relative_to(ROOT)}')
    print(f'App: {APP.relative_to(ROOT)}')


if __name__=='__main__':main()
