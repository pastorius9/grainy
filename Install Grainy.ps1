param([string]$CatalogPath = '')
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'Grainy'
if (-not (Test-Path -LiteralPath (Join-Path $source 'Grainy.exe'))) { throw 'Keep this installer next to the Grainy folder.' }
$destination = Join-Path $env:LOCALAPPDATA 'Programs\Grainy'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'Grainy.exe') -Destination $destination -Force
Copy-Item -LiteralPath (Join-Path $source '_internal') -Destination $destination -Recurse -Force
Copy-Item -LiteralPath (Join-Path $source 'THIRD_PARTY_LICENSES') -Destination $destination -Recurse -Force
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Grainy.lnk'
$legacyPath = Join-Path $desktop 'Luma Photo Studio.lnk'
$legacyTarget = Join-Path $env:LOCALAPPDATA 'Programs\Luma\Luma.exe'
$legacyShortcut = $null
if (Test-Path -LiteralPath $legacyPath) {
    $candidate = $shell.CreateShortcut($legacyPath)
    if ($candidate.TargetPath -eq $legacyTarget) { $legacyShortcut = $candidate }
}
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $destination 'Grainy.exe'
$shortcut.WorkingDirectory = $destination
$shortcut.Description = 'Grainy Photo Studio'
if ($CatalogPath) {
    $shortcut.Arguments = '--data-dir "' + [IO.Path]::GetFullPath($CatalogPath) + '"'
} elseif ($legacyShortcut -and -not $shortcut.Arguments) {
    $shortcut.Arguments = $legacyShortcut.Arguments
}
$shortcut.Save()
# Only replace this app's known legacy shortcut; leave its library and old binary intact.
if ($legacyShortcut) { Remove-Item -LiteralPath $legacyPath }
Write-Output ('Installed: ' + $shortcut.TargetPath)
