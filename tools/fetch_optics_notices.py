"""One-time fetch of upstream notices; normal release builds use local copies."""
from pathlib import Path
from urllib.request import urlopen
from concurrent.futures import ThreadPoolExecutor
import hashlib,json

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'assets'/'licenses'/'Lensfun'
REV='101c745e847a5de4a1e569a94368ce2027198598'
BASE=f'https://raw.githubusercontent.com/lensfun/lensfun/{REV}/'
FILES={
    'Lensfun-README.md':BASE+'README.md',
    'Lensfun-LGPL-3.txt':'https://www.gnu.org/licenses/lgpl-3.0.txt',
    'Lensfun-GPL-3.txt':'https://www.gnu.org/licenses/gpl-3.0.txt',
    'Lensfun-CC-BY-SA-3.0.txt':'https://raw.githubusercontent.com/creativecommons/legalcode/main/by-sa_3.0.txt',
    'lensfunpy-LICENSE.txt':'https://raw.githubusercontent.com/letmaik/lensfunpy/v1.18.0/LICENSE',
    'lensfunpy-build.py.txt':'https://raw.githubusercontent.com/letmaik/lensfunpy/v1.18.0/setup.py',
    'Lensfun-source.tar.gz':f'https://codeload.github.com/lensfun/lensfun/tar.gz/{REV}',
    'GLib-LGPL-2.1.txt':'https://raw.githubusercontent.com/GNOME/glib/2.86.3/LICENSES/LGPL-2.1-or-later.txt',
    'PCRE2-LICENCE.txt':'https://raw.githubusercontent.com/PCRE2Project/pcre2/pcre2-10.47/LICENCE.md',
    'libiconv-LGPL-2.1.txt':'https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt',
    'gettext-runtime-COPYING.LIB.txt':'https://raw.githubusercontent.com/autotools-mirror/gettext/master/gettext-runtime/intl/COPYING.LIB',
}

def fetch(item):
    name,url=item
    data=urlopen(url,timeout=60).read()
    if len(data)<100:raise ValueError(f'Incomplete notice: {name}')
    (DEST/name).write_bytes(data)
    return {'file':name,'source':url,'sha256':hashlib.sha256(data).hexdigest()}

if __name__=='__main__':
    DEST.mkdir(parents=True,exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:records=list(pool.map(fetch,FILES.items()))
    (DEST/'sources.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
    print(f'Saved {len(records)} upstream source/notices files.')
