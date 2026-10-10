"""Build the macOS native libraries (DCP tables, LittleCMS) with the Xcode command line tools.

The sources are the ones the Windows build uses, unchanged. Each library is built for Apple Silicon
and Intel in one file, and is signed ad hoc by the linker (required to load on Apple Silicon)."""
from pathlib import Path
import hashlib,json,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
NATIVE=ROOT/'assets/native'
ARCHITECTURES=['-arch','arm64','-arch','x86_64','-mmacosx-version-min=11.0']


def named(output):
    # Without this the linker records the output's full path (the builder's home folder) inside the library.
    return ['-install_name',f'@rpath/{output.name}','-o',str(output)]


def current(output,signature):
    stamp=output.with_suffix('.macos.json')
    if not output.exists() or not stamp.exists():return False
    previous=json.loads(stamp.read_text(encoding='utf-8'))
    return previous.get('source_signature')==signature and previous.get('sha256')==hashlib.sha256(output.read_bytes()).hexdigest()


def finish(output,signature,**details):
    assert str(Path.home()).encode() not in output.read_bytes(),'The library names the builder\'s home folder'
    output.with_suffix('.macos.json').write_text(json.dumps({**details,'architectures':['arm64','x86_64'],
        'source_signature':signature,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()},indent=2),encoding='utf-8')


def run(command,log):
    process=subprocess.run(command,capture_output=True,text=True,errors='replace')
    log.parent.mkdir(parents=True,exist_ok=True);log.write_text(process.stdout+process.stderr,encoding='utf-8')
    if process.returncode:print(process.stdout+process.stderr)
    process.check_returncode()


def dcp():
    source=ROOT/'native/dcp_tables.cpp';output=NATIVE/'luma_dcp.dylib'
    signature=hashlib.sha256(source.read_bytes()+Path(__file__).read_bytes()).hexdigest()
    if current(output,signature):print('Native DCP interpolation library is current.');return
    # The NumPy reference has no fused multiply-add; Clang fuses by default on Apple Silicon.
    # The source marks its exports for MSVC; here every function is exported already.
    run(['clang++','-shared','-O2','-std=c++17','-ffp-contract=off','-Wall','-Wextra','-D__declspec(x)=',
         *ARCHITECTURES,*named(output),str(source)],ROOT/'build/native-dcp/compiler-macos.log')
    finish(output,signature,abi=1,source='native/dcp_tables.cpp',floating_point='clang -ffp-contract=off',
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    print('Built exact DCP interpolation library.')


def color():
    source=ROOT/'vendor/lcms2-2.19'
    manifest=json.loads((source/'SOURCE.json').read_text(encoding='utf-8'))
    for name,digest in manifest['files'].items():
        assert hashlib.sha256((source/name).read_bytes()).hexdigest()==digest,name
    output=NATIVE/'luma_lcms2.dylib'
    signature=hashlib.sha256((source/'SOURCE.json').read_bytes()+Path(__file__).read_bytes()).hexdigest()
    if current(output,signature):print('Pinned LittleCMS native library is current.');return
    run(['clang','-shared','-O2',*COLOR_FLOATING_POINT,'-w',f'-I{source/"include"}',*ARCHITECTURES,*named(output),
         *[str(p) for p in sorted((source/'src').glob('cms*.c'))]],ROOT/'build/native-color/compiler-macos.log')
    finish(output,signature,version=manifest['version'],source=manifest['source'],
        floating_point=' '.join(['clang',*COLOR_FLOATING_POINT]))
    print(f'Built LittleCMS {manifest["version"]} from pinned source.')


def gpu():
    source=ROOT/'native/gpu_compute_metal.mm';output=NATIVE/'grainy_gpu.dylib'
    signature=hashlib.sha256(source.read_bytes()+Path(__file__).read_bytes()).hexdigest()
    if current(output,signature):print('Native GPU module is current.');return
    # The shaders are compiled by Metal when the device is first used (the Metal compiler is part of
    # macOS; an offline one would need the full Xcode).
    run(['clang++','-shared','-O2','-std=c++17','-fobjc-arc','-Wall','-Wextra',*ARCHITECTURES,*named(output),
         '-framework','Metal','-framework','Foundation',str(source)],ROOT/'build/native-gpu/compiler-macos.log')
    finish(output,signature,abi=16,source='native/gpu_compute_metal.mm')
    print('Built the Metal GPU module.')


# Clang's defaults: float transforms equal imagecodecs' LittleCMS bit for bit on Apple Silicon (tests/test_native_color.py).
COLOR_FLOATING_POINT=[]

if __name__=='__main__':
    if sys.platform!='darwin':raise SystemExit('This script builds the macOS libraries; run it on macOS.')
    NATIVE.mkdir(parents=True,exist_ok=True)
    dcp();color();gpu()
