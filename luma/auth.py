"""Find the native Codex executable; account management belongs to the CLI."""
import os
import shutil
from pathlib import Path


# Where the npm package keeps the native program: vendor/<target>/bin/ since Codex 0.163, vendor/<target>/codex/ before;
# in a platform package (@openai/codex-win32-x64, codex-darwin-arm64) or in the main package itself.
NATIVE_WINDOWS=tuple(f'{place}vendor/*/{folder}/codex.exe' for place in ('node_modules/@openai/codex-win32-*/','') for folder in ('bin','codex'))
NATIVE_POSIX=tuple(f'{place}vendor/*/{folder}/codex' for place in ('node_modules/@openai/codex-darwin-*/','') for folder in ('bin','codex'))


def _find_codex_posix(configured=None):
    """macOS: the native program inside the npm package (its `codex` command is a Node script, which an app
    started from Finder cannot run: its PATH has no node), or a program installed as `codex` itself."""
    def usable(path):return path.is_file() and os.access(path,os.X_OK) and path.suffix not in ('.js','.mjs')
    if configured:
        path=Path(configured)
        return str(path.resolve()) if usable(path) else None
    # Finder gives apps a short PATH; npm's and Homebrew's usual places are checked as well.
    places=[shutil.which('codex'),'/opt/homebrew/bin/codex','/usr/local/bin/codex',str(Path.home()/'.npm-global'/'bin'/'codex')]
    for place in dict.fromkeys(p for p in places if p):
        command=Path(place)
        if not command.exists():continue
        real=command.resolve()
        for folder in real.parents:
            if folder.name=='codex' and folder.parent.name=='@openai':
                for native in [p for pattern in NATIVE_POSIX for p in sorted(folder.glob(pattern))]:
                    if usable(native):return str(native.resolve())
        with real.open('rb') as handle:script=handle.read(2)==b'#!'
        if usable(real) and not script:return str(real)
    return None


def find_codex(configured=None):
    if os.name!='nt':return _find_codex_posix(configured)
    if configured:
        path=Path(configured)
        return str(path.resolve()) if path.is_file() and path.suffix.lower()=='.exe' else None
    native=shutil.which('codex.exe')
    if native:
        return native
    roots=[Path(os.environ.get('APPDATA',Path.home()))/'npm']
    for name in ('codex','codex.cmd','codex.ps1'):
        found=shutil.which(name)
        if found:
            roots.append(Path(found).parent)
    for root in dict.fromkeys(roots):
        package=root/'node_modules'/'@openai'/'codex'
        for pattern in NATIVE_WINDOWS:
            for path in sorted(package.glob(pattern)):
                if path.is_file():
                    return str(path.resolve())
    return None
