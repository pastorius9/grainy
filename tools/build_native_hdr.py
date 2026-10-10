"""Build the app-owned D3D11 scRGB viewport with the installed Windows SDK."""
from pathlib import Path
import hashlib,json,os,subprocess
ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'native/hdr_display.cpp';output=ROOT/'assets/native/grainy_hdr.dll'
output.parent.mkdir(parents=True,exist_ok=True);stamp=output.with_suffix('.json')
signature=hashlib.sha256(source.read_bytes()+Path(__file__).read_bytes()).hexdigest()
if output.exists() and stamp.exists():
    old=json.loads(stamp.read_text())
    if old.get('source_signature')==signature and old.get('sha256')==hashlib.sha256(output.read_bytes()).hexdigest():
        print('Native HDR renderer is current.');raise SystemExit(0)
locator=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
vs=subprocess.check_output([str(locator),'-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath'],text=True).strip()
if not vs:raise RuntimeError('MSVC x64 build tools are required.')
build=ROOT/'build/native-hdr';build.mkdir(parents=True,exist_ok=True)
arguments=['/nologo','/LD','/O2','/MD','/EHsc','/std:c++17','/W4','/DNTDDI_VERSION=0x0A000010',
    f'/Fe"{output}"',f'"{source}"','/link','d3d11.lib','dxgi.lib','d3dcompiler.lib','user32.lib',f'/IMPLIB:"{build/"grainy_hdr.lib"}"',f'/PDB:"{build/"grainy_hdr.pdb"}"']
(build/'compile.rsp').write_text(' '.join(arguments),encoding='utf-8-sig')
script=build/'compile.cmd';script.write_text(f'@echo off\ncall "{Path(vs)/"Common7/Tools/VsDevCmd.bat"}" -no_logo -arch=x64 -host_arch=x64\nif errorlevel 1 exit /b 1\ncl.exe @compile.rsp\nexit /b %errorlevel%\n')
run=subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c',str(script)],cwd=build,capture_output=True,text=True,errors='replace',env=dict(os.environ,VSLANG='1033'),creationflags=0x08000000)
(build/'compiler.log').write_text(run.stdout+run.stderr)
if run.returncode:print(run.stdout+run.stderr)
run.check_returncode()
stamp.write_text(json.dumps(dict(abi=1,source='native/hdr_display.cpp',source_signature=signature,sha256=hashlib.sha256(output.read_bytes()).hexdigest()),indent=2))
print('Built D3D11 FP16 scRGB HDR renderer.')
