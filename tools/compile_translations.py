"""Compile reviewed, UTF-8 translation pairs; fail on duplicate or invalid templates."""
import json
from pathlib import Path
from string import Formatter

ROOT=Path(__file__).resolve().parents[1]
directory=ROOT/'assets'/'i18n'
result={}
for path in sorted(directory.glob('en*.txt')):
    for number,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
        if not line.strip():continue
        source,target=line.split(' => ',1)
        source=source.replace('\\n','\n');target=target.replace('\\n','\n')
        if source in result and result[source]!=target:raise ValueError(f'Duplicate translation: {path}:{number} {source}')
        fields=lambda s:sorted(field for _,field,_,_ in Formatter().parse(s) if field is not None)
        if fields(source)!=fields(target):raise ValueError(f'Changed placeholders: {path}:{number}')
        result[source]=target
# Windows line endings on every system, so compiling on macOS leaves the file byte for byte the same.
(directory/'en.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\r\n')
print(f'{len(result)} reviewed English translations compiled')
