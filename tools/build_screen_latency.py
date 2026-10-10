"""Build build/screen-latency/screen_latency.exe (Desktop Duplication recorder used by
tools/interactive_comparison.py) with the installed MSVC tools."""
import os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
build = ROOT/'build/screen-latency'; build.mkdir(parents=True, exist_ok=True)
locator = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
vs = subprocess.check_output([str(locator), '-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                              '-property', 'installationPath'], text=True).strip()
source = ROOT/'tools/native/screen_latency.cpp'; exe = build/'screen_latency.exe'
script = build/'compile.cmd'
script.write_text(f'@echo off\ncall "{Path(vs)/"Common7/Tools/VsDevCmd.bat"}" -no_logo -arch=x64 -host_arch=x64\n'
                  f'cl.exe /nologo /O2 /EHsc /std:c++17 "{source}" /Fe"{exe}" /Fo"{build/"screen_latency.obj"}" user32.lib\nexit /b %errorlevel%\n')
subprocess.run(['cmd', '/c', str(script)], cwd=build, check=True)
print(exe)
