"""Build Luma's exact DCP table evaluator using installed MSVC x64 tools."""
from pathlib import Path
import hashlib,json,os,subprocess

ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'native/dcp_tables.cpp'
output=ROOT/'assets/native/luma_dcp.dll';output.parent.mkdir(parents=True,exist_ok=True)
signature=hashlib.sha256(source.read_bytes()+Path(__file__).read_bytes()).hexdigest()
stamp=output.with_suffix('.json')
if output.exists() and stamp.exists():
    previous=json.loads(stamp.read_text(encoding='utf-8'))
    if previous.get('source_signature')==signature and previous.get('sha256')==hashlib.sha256(output.read_bytes()).hexdigest():
        print('Native DCP interpolation library is current.');raise SystemExit(0)
locator=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
vs=subprocess.check_output([str(locator),'-latest','-products','*','-requires',
    'Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath'],text=True).strip()
if not vs:raise RuntimeError('MSVC x64 build tools are required for native DCP interpolation.')
build=ROOT/'build/native-dcp';build.mkdir(parents=True,exist_ok=True)
arguments=['/nologo','/LD','/O2','/MD','/EHsc','/std:c++17','/fp:strict','/W4',
    f'/Fe"{output}"',f'"{source}"','/link',f'/IMPLIB:"{build/"luma_dcp.lib"}"',f'/PDB:"{build/"luma_dcp.pdb"}"']
(build/'compile.rsp').write_text(' '.join(arguments),encoding='utf-8-sig')
script=build/'compile.cmd'
script.write_text(f'@echo off\ncall "{Path(vs)/"Common7/Tools/VsDevCmd.bat"}" -no_logo -arch=x64 -host_arch=x64\n'
    'if errorlevel 1 exit /b 1\ncl.exe @compile.rsp\nexit /b %errorlevel%\n',encoding='utf-8')
process=subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c',str(script)],cwd=build,
    capture_output=True,text=True,errors='replace',env=dict(os.environ,VSLANG='1033'),creationflags=0x08000000)
(build/'compiler.log').write_text(process.stdout+process.stderr,encoding='utf-8')
if process.returncode:print(process.stdout+process.stderr)
process.check_returncode()
stamp.write_text(json.dumps({'abi':1,'source':'native/dcp_tables.cpp','floating_point':'MSVC /fp:strict',
    'source_signature':signature,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
    'sha256':hashlib.sha256(output.read_bytes()).hexdigest()},indent=2),encoding='utf-8')
print('Built exact DCP interpolation library.')
