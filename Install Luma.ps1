param([string]$CatalogPath = '')
# Compatibility entry point for the renamed app.
& (Join-Path $PSScriptRoot 'Install Grainy.ps1') -CatalogPath $CatalogPath
