"""Build the D3D11 compute module for GPU develop stages with the installed Windows SDK."""
from pathlib import Path
import hashlib,json,os,subprocess
ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'native/gpu_compute.cpp';output=ROOT/'assets/native/grainy_gpu.dll'
output.parent.mkdir(parents=True,exist_ok=True);stamp=output.with_suffix('.json')
shaders_tool=ROOT/'tools/gpu_shaders.py'
signature=hashlib.sha256(source.read_bytes()+Path(__file__).read_bytes()+shaders_tool.read_bytes()).hexdigest()
if output.exists() and stamp.exists():
    old=json.loads(stamp.read_text())
    if old.get('source_signature')==signature and old.get('sha256')==hashlib.sha256(output.read_bytes()).hexdigest():
        print('Native GPU compute module is current.');raise SystemExit(0)
locator=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
vs=subprocess.check_output([str(locator),'-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath'],text=True).strip()
if not vs:raise RuntimeError('MSVC x64 build tools are required.')
build=ROOT/'build/native-gpu';build.mkdir(parents=True,exist_ok=True)
# Precompile the HLSL so the app never runs the shader compiler (about 0.6 s per device creation).
import sys
sys.path.insert(0,str(ROOT/'tools'))
from gpu_shaders import compile_shader,header,sources
compiled={}
for name,text in sources().items():
    code,messages=compile_shader(name,text)
    if code is None:raise RuntimeError(f'{name} failed to compile: {messages}')
    compiled[name]=code
(build/'gpu_shaders.h').write_text(header(compiled),encoding='utf-8')
arguments=['/nologo','/LD','/O2','/MD','/EHsc','/std:c++17','/W4','/DNTDDI_VERSION=0x0A000010','/DGRAINY_PRECOMPILED_SHADERS',f'/I"{build}"',
    f'/Fe"{output}"',f'"{source}"','/link','d3d11.lib','dxgi.lib','d3dcompiler.lib',f'/IMPLIB:"{build/"grainy_gpu.lib"}"',f'/PDB:"{build/"grainy_gpu.pdb"}"']
(build/'compile.rsp').write_text(' '.join(arguments),encoding='utf-8-sig')
script=build/'compile.cmd';script.write_text(f'@echo off\ncall "{Path(vs)/"Common7/Tools/VsDevCmd.bat"}" -no_logo -arch=x64 -host_arch=x64\nif errorlevel 1 exit /b 1\ncl.exe @compile.rsp\nexit /b %errorlevel%\n')
run=subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c',str(script)],cwd=build,capture_output=True,text=True,errors='replace',env=dict(os.environ,VSLANG='1033'),creationflags=0x08000000)
(build/'compiler.log').write_text(run.stdout+run.stderr)
if run.returncode:print(run.stdout+run.stderr)
run.check_returncode()
stamp.write_text(json.dumps(dict(abi=1,source='native/gpu_compute.cpp',source_signature=signature,sha256=hashlib.sha256(output.read_bytes()).hexdigest()),indent=2))
print('Built D3D11 GPU compute module.')
