"""Compile verbatim SDK color algorithms with minimal validation-only adapters.

The full SDK renderer has unrelated image/XMP dependencies. Extract only the
exact source bodies needed for this reference; record hashes and keep it out
of the application bundle. No reference value is computed by Luma Python code.
"""
from pathlib import Path
import os,subprocess,hashlib,json

ROOT=Path(__file__).resolve().parents[1]
sdk=ROOT/'validation/adobe-dng-sdk/dng_sdk_1_7_1/dng_sdk/source'
build=ROOT/'validation/dng-reference';build.mkdir(exist_ok=True)
wrapper=build/'reference.cpp'
def extract(file,marker):
    text=(sdk/file).read_text(encoding='utf-8-sig')
    start=text.index(marker);opening=text.index('{',start);depth=1;end=opening+1
    while depth:
        if text[end]=='{':depth+=1
        elif text[end]=='}':depth-=1
        end+=1
    if text[end:end+1]==';':end+=1
    return text[start:end]
items=[('dng_temperature.cpp','struct ruvt'),('dng_temperature.cpp','static const ruvt kTempTable'),
       ('dng_temperature.cpp','dng_xy_coord LegacyGetXY'),
       ('dng_temperature.cpp','void LegacySetXY'),('dng_xy_coord.cpp','dng_xy_coord XYZtoXY'),
       ('dng_xy_coord.cpp','dng_vector_3 XYtoXYZ'),('dng_xy_coord.cpp','static const real64 kCIEStdObserver2Degree'),
       ('dng_1d_function.cpp','static void RequireValidPiecewiseLinear'),
       ('dng_1d_function.cpp','real64 dng_piecewise_linear::Evaluate ('),
       ('dng_xy_coord.cpp','void dng_illuminant_data::CalculateSpectrumXY'),
       ('dng_utils.h','static inline real64 SmoothStep'),
       ('dng_point.h','inline real64 DistanceSquared (const dng_point_real64 &a,'),
       ('dng_xy_coord.cpp','class dng_map_temp_func'),
       ('dng_xy_coord.cpp','void CalculateTripleIlluminantWeights')]
fragments=[extract(file,marker) for file,marker in items]
prefix='#include "dng_reference_support.hpp"\nstatic const double kTintScale=-3000.;\n'
wrapper.write_text(prefix+'\n'.join(fragments)+r'''
#include <iostream>
#include <iomanip>
#include <string>
int main() {
  std::cout << std::setprecision(17);
  std::string op;
  while(std::cin >> op) {
    if(op=="weights") {
      double x,y,a,b,c,d,e,f,w1,w2,w3;
      std::cin >> x >> y >> a >> b >> c >> d >> e >> f;
      dng_illuminant_data l1,l2,l3;
      l1.SetWhiteXY(dng_xy_coord(a,b)); l2.SetWhiteXY(dng_xy_coord(c,d));
      l3.SetWhiteXY(dng_xy_coord(e,f));
      CalculateTripleIlluminantWeights(dng_xy_coord(x,y),l1,l2,l3,w1,w2,w3);
      std::cout << w1 << ' ' << w2 << ' ' << w3 << '\n';
    } else if(op=="xy") {
      double t,g;std::cin >> t >> g;auto xy=LegacyGetXY(t,g);
      std::cout << xy.x << ' ' << xy.y << '\n';
    } else if(op=="temperature") {
      double x,y;std::cin >> x >> y;dng_temperature t(dng_xy_coord(x,y));
      std::cout << t.Temperature() << ' ' << t.Tint() << '\n';
    } else if(op=="spectrum") {
      unsigned int sn,sd,dn,dd,n;std::cin >> sn >> sd >> dn >> dd >> n;
      std::vector<dng_urational> samples;
      for(unsigned int i=0;i<n;i++){unsigned int a,b;std::cin >> a >> b;samples.push_back(dng_urational(a,b));}
      dng_illuminant_data l;l.SetSpectrum(dng_urational(sn,sd),dng_urational(dn,dd),samples);
      std::cout << l.WhiteXY().x << ' ' << l.WhiteXY().y << '\n';
    } else return 2;
  }
  return 0;
}
''',encoding='utf-8')
locator=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
vs=subprocess.check_output([str(locator),'-latest','-products','*','-requires',
    'Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath'],text=True).strip()
if not vs:raise RuntimeError('MSVC x64 build tools required for the independent SDK reference.')
sources=[]
args=['/nologo','/O2','/MD','/EHsc','/std:c++17','/Gy','/Gw','/GL','/fp:precise',
      '/DqWinOS=1','/DqDNGUseXMP=0','/DqDNGUseJXL=0','/DqDNGUseJPEG=0','/DqDNGValidate=0',
      f'/I"{ROOT/"tools"}"',f'/Fe"{build/"reference.exe"}"',f'"{wrapper}"']
args += [f'"{p}"' for p in sources]+['/link','/LTCG','/OPT:REF','/OPT:ICF','user32.lib']
(build/'compile.rsp').write_text(' '.join(args),encoding='utf-8-sig')
script=build/'compile.cmd'
script.write_text(f'@echo off\ncall "{Path(vs)/"Common7/Tools/VsDevCmd.bat"}" -no_logo -arch=x64 -host_arch=x64\n'
                 'if errorlevel 1 exit /b 1\ncl.exe @compile.rsp\nexit /b %errorlevel%\n',encoding='utf-8')
result=subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c',str(script)],cwd=build,
    capture_output=True,text=True,errors='replace',env=dict(os.environ,VSLANG='1033'),creationflags=0x08000000)
(build/'compiler.log').write_text(result.stdout+result.stderr,encoding='utf-8')
if result.returncode:print((result.stdout+result.stderr)[-7000:])
result.check_returncode()
(build/'source-hashes.json').write_text(json.dumps({
    'wrapper':hashlib.sha256(wrapper.read_bytes()).hexdigest(),
    'adapters':hashlib.sha256((ROOT/'tools/dng_reference_support.hpp').read_bytes()).hexdigest(),
    'verbatim_functions':{file+':'+marker:hashlib.sha256(code.encode()).hexdigest()
        for (file,marker),code in zip(items,fragments)}},indent=2),encoding='utf-8')
print('Independent Adobe SDK reference built; validation use only.')
