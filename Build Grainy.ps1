$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $runtime "$PSScriptRoot\tools\compile_translations.py"
if ($LASTEXITCODE -ne 0) { throw 'Translation compilation failed.' }
& $runtime "$PSScriptRoot\tools\build_native_color.py"
if ($LASTEXITCODE -ne 0) { throw 'Native color build failed.' }
& $runtime "$PSScriptRoot\tools\build_native_dcp.py"
if ($LASTEXITCODE -ne 0) { throw 'Native DCP build failed.' }
& $runtime "$PSScriptRoot\tools\build_native_hdr.py"
if ($LASTEXITCODE -ne 0) { throw 'Native HDR build failed.' }
& $runtime "$PSScriptRoot\tools\build_native_gpu.py"
if ($LASTEXITCODE -ne 0) { throw 'Native GPU build failed.' }
$buildPath = $env:PATH
try {
    # Prevent unrelated programs' ICU libraries from replacing Windows' ICU.
    $env:PATH = "$PSScriptRoot\.venv\Scripts;$env:SystemRoot\System32;$env:SystemRoot"
    & $runtime -m PyInstaller --clean --noconfirm --windowed --name Grainy --icon "$PSScriptRoot\assets\brand\grainy.ico" --distpath release --workpath build --specpath build --add-data "$PSScriptRoot\assets;assets" --add-data "$PSScriptRoot\README.html;." --add-data "$PSScriptRoot\FEATURE_GAPS.md;." --add-data "$PSScriptRoot\CHANGELOG.md;." --collect-all imagecodecs --collect-all lensfunpy --exclude-module scipy --exclude-module tkinter "$PSScriptRoot\main.py"
} finally { $env:PATH = $buildPath }
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
# OpenCV's wheel carries an FFmpeg build for video I/O, which Grainy does not use: ship no FFmpeg at all.
Get-ChildItem -Path "$PSScriptRoot\release\Grainy\_internal\cv2" -Filter 'opencv_videoio_ffmpeg*.dll' -ErrorAction SilentlyContinue | Remove-Item -Force
if (Get-ChildItem -Path "$PSScriptRoot\release\Grainy" -Recurse -Include '*ffmpeg*','avcodec*','avformat*','avutil*','swscale*' -ErrorAction SilentlyContinue) { throw 'FFmpeg files remain in the release.' }
Copy-Item -Path "$PSScriptRoot\.venv\Lib\site-packages\PySide6\*140*.dll" -Destination "$PSScriptRoot\release\Grainy\_internal" -Force
& $runtime "$PSScriptRoot\tools\package_notices.py"
if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
# The macOS libraries and icon live beside the Windows ones in assets; they are not part of this release.
Get-ChildItem -Path "$PSScriptRoot\release\Grainy\_internal\assets" -Recurse -Include '*.dylib','*.macos.json','*.icns' -ErrorAction SilentlyContinue | Remove-Item -Force
Copy-Item -LiteralPath 'Install Grainy.ps1' -Destination 'release\Install Grainy.ps1' -Force
Copy-Item -LiteralPath 'LICENSE' -Destination 'release\Grainy\LICENSE.txt' -Force
# Nothing in the release may name this PC: its folders or its user name.
& $runtime "$PSScriptRoot\tools\scan_release.py"
if ($LASTEXITCODE -ne 0) { throw 'The release names the build machine.' }
Write-Output 'Release available in release\Grainy\Grainy.exe'
