"""Run every regression, keeping interaction timing tests free of CPU contention."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
# These assert latency / continuous frame delivery, not only final pixels.
# Run them after all parallel workers have exited, with unchanged thresholds.
TIMING_FILES=('tests/test_interaction.py','tests/test_studio_ui.py')


def results(path):
    cases=[]
    for case in ET.parse(path).iter('testcase'):
        status=next((tag for tag in ('failure','error','skipped') if case.find(tag) is not None),'passed')
        cases.append(dict(classname=case.get('classname'),name=case.get('name'),status=status,seconds=float(case.get('time',0))))
    return cases


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,default=4,choices=range(1,9))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    folder=args.output or ROOT/'validation'/f'check-{uuid4().hex[:8]}'
    folder=folder.resolve();folder.mkdir(parents=True,exist_ok=False)
    phases=[('serial',[])] if args.workers==1 else [
        ('parallel',['-n',str(args.workers),'--dist=loadfile',*[f'--ignore={p}' for p in TIMING_FILES]]),
        ('interaction',list(TIMING_FILES))]
    started=time.perf_counter();report=dict(workers=args.workers,phases=[],status='running')
    for name,extra in phases:
        xml=folder/f'{name}.xml';log=folder/f'{name}.log'
        command=[sys.executable,'-m','pytest','-q','-W','error::UserWarning','--durations=25',f'--junitxml={xml}',*extra]
        print(f'{name}: running',flush=True);phase_start=time.perf_counter()
        with log.open('w',encoding='utf-8') as output:
            run=subprocess.run(command,cwd=ROOT,stdout=output,stderr=subprocess.STDOUT)
        phase=dict(name=name,exit_code=run.returncode,seconds=time.perf_counter()-phase_start,
            command=command,log=str(log),cases=results(xml) if xml.is_file() else [])
        report['phases'].append(phase)
        print(f'{name}: exit {run.returncode}, {len(phase["cases"])} cases, {phase["seconds"]:.2f}s',flush=True)
        if run.returncode:break
    report['seconds']=time.perf_counter()-started
    report['status']='passed' if len(report['phases'])==len(phases) and all(p['exit_code']==0 for p in report['phases']) else 'failed'
    (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('status','workers','seconds')}),flush=True)
    print(f'Report: {folder / "report.json"}',flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
