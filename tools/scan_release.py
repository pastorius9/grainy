"""Fail when a release folder names the machine it was built on.

Searches every file (and the compressed entries of nested zip files and PyInstaller archives) for
the build folder, the user's home folder and the user name, as UTF-8 and UTF-16 text. Run after a
build: `python tools/scan_release.py [release folder] [extra word ...]`.
"""
from pathlib import Path
import getpass, io, os, re, sys, zipfile, zlib

ROOT = Path(__file__).resolve().parents[1]
folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT/'release'/'Grainy'
words = set(sys.argv[2:])
if not os.environ.get('GITHUB_ACTIONS'):
    # A hosted build runner has nothing private, and third-party wheels built on such runners carry
    # the same runner paths (C:\Users\runneradmin\...), so there only the extra words are searched.
    words |= {str(ROOT), ROOT.as_posix(), str(Path.home()), Path.home().as_posix()}
    user = getpass.getuser()
    if len(user) >= 4:words.add(user)      # shorter names match by chance in binary data
needles = [n for word in words if word for n in (word.lower().encode('utf-8'), word.lower().encode('utf-16-le'))]


def blobs(name, data, depth=0):
    yield name, data
    if depth < 2 and data[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                for entry in inner.namelist():
                    yield from blobs(f'{name}!{entry}', inner.read(entry), depth+1)
        except zipfile.BadZipFile:pass
    elif depth == 0 and name.lower().endswith(('.exe', '.pkg', '.pyz')):
        for match in re.finditer(rb'\x78[\x01\x5e\x9c\xda]', data):     # zlib streams of a PyInstaller archive
            try:yield f'{name}@{match.start()}', zlib.decompressobj().decompress(data[match.start():match.start()+16_000_000])
            except zlib.error:pass


hits = []
for path in sorted(p for p in folder.rglob('*') if p.is_file()):
    for name, data in blobs(str(path.relative_to(folder)), path.read_bytes()):
        low = data.lower()
        for needle in needles:
            index = low.find(needle)
            if index >= 0:
                hits.append((name, data[max(0, index-30):index+len(needle)+50])); break
for name, context in hits[:40]:print(name, '|', repr(context)[:200])
print(f'{folder}: {len(hits)} file(s) name the build machine' if hits else f'{folder}: no build-machine paths or user name found' if needles else f'{folder}: hosted runner, nothing to search for')
raise SystemExit(1 if hits else 0)
