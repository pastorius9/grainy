"""Read and time effective profiles without changing Windows settings."""
import sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from luma.display_profiles import resolve
app=QApplication([])
report={'version':'0.5.8','platform':app.platformName(),'windows_settings_changed':False,'screens':[]}
for screen in app.screens():
    times=[]
    for _ in range(20):
        start=time.perf_counter();state=resolve('auto',screen.name());times.append((time.perf_counter()-start)*1000)
    report['screens'].append(dict(screen=screen.name(),profile=state.path,description=state.description,
        error=state.error,message=state.message,bytes=len(state.data or b''),sha256=state.digest,
        median_ms=sorted(times)[len(times)//2],max_ms=max(times)))
assert report['screens'] and not any(s['error'] for s in report['screens']),report
path=Path(__file__).resolve().parents[1]/'validation/display-windows-0.5.8.json'
path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
