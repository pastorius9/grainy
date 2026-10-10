"""Build our pinned MIT LittleCMS source with the installed MSVC x64 toolchain."""
from pathlib import Path
import hashlib,json,os,subprocess

ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'vendor/lcms2-2.19'
manifest=json.loads((source/'SOURCE.json').read_text(encoding='utf-8'))
for name,digest in manifest['files'].items():
    assert hashlib.sha256((source/name).read_bytes()).hexdigest()==digest,name
output=ROOT/'assets/native/luma_lcms2.dll';output.parent.mkdir(parents=True,exist_ok=True)
signature=hashlib.sha256((source/'SOURCE.json').read_bytes()+Path(__file__).read_bytes()).hexdigest()
stamp=output.with_suffix('.json')
if output.exists() and stamp.exists():
    previous=json.loads(stamp.read_text(encoding='utf-8'))
    if previous.get('source_signature')==signature and previous.get('sha256')==hashlib.sha256(output.read_bytes()).hexdigest():
        print('Pinned LittleCMS native library is current.');raise SystemExit(0)
locator=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
vs=subprocess.check_output([str(locator),'-latest','-products','*','-requires',
    'Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath'],text=True).strip()
if not vs:raise RuntimeError('MSVC x64 build tools are required to build native color conversion.')
build=ROOT/'build/native-color';build.mkdir(parents=True,exist_ok=True)
# /d1trimfile keeps the build folder out of the library (assert messages carry the source file name).
arguments=['/nologo','/LD','/O2','/MD','/fp:precise','/W3','/wd4996','/DCMS_DLL_BUILD',f'/d1trimfile:"{ROOT}\\\\"',
    f'/I"{source / "include"}"',f'/Fe"{output}"']
arguments += [f'"{p}"' for p in sorted((source/'src').glob('cms*.c'))]
arguments += ['/link',f'/IMPLIB:"{build / "luma_lcms2.lib"}"',f'/PDB:"{build / "luma_lcms2.pdb"}"']
(build/'compile.rsp').write_text(' '.join(arguments),encoding='utf-8-sig')
script=build/'compile.cmd'
script.write_text(f'@echo off\ncall "{Path(vs)/"Common7/Tools/VsDevCmd.bat"}" -no_logo -arch=x64 -host_arch=x64\n'
    'if errorlevel 1 exit /b 1\ncl.exe @compile.rsp\nexit /b %errorlevel%\n',encoding='utf-8')
process=subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c',str(script)],cwd=build,
    capture_output=True,text=True,errors='replace',env=dict(os.environ,VSLANG='1033'),creationflags=0x08000000)
(build/'compiler.log').write_text(process.stdout+process.stderr,encoding='utf-8')
process.check_returncode()
stamp.write_text(json.dumps({'version':manifest['version'],'source':manifest['source'],
    'source_signature':signature,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()},indent=2),encoding='utf-8')
print('Built LittleCMS 2.19.0 from pinned source.')
