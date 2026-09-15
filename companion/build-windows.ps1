param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
# Some managed shells omit USERPROFILE. Restore the actual Windows profile
# for this process only; PyInstaller uses it to resolve its dependency cache.
if (-not $env:USERPROFILE) {
    $taskProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if (-not $taskProfile -or -not (Test-Path -LiteralPath $taskProfile -PathType Container)) {
        throw 'The Windows user profile could not be resolved for the build.'
    }
    $env:USERPROFILE = $taskProfile
}
$taskRepo = Split-Path $PSScriptRoot -Parent
$taskOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $taskOutput) {
    throw 'Choose a new output directory. Existing builds are preserved.'
}
New-Item -ItemType Directory -Path $taskOutput | Out-Null
& $Python -m PyInstaller --onedir --windowed --noupx --name BridgeLibraryCompanion `
    --paths (Join-Path $taskRepo 'plugins/bridge_library') `
    --paths $PSScriptRoot --collect-submodules uvicorn `
    --recursive-copy-metadata fastapi --recursive-copy-metadata uvicorn `
    --recursive-copy-metadata python-multipart `
    --distpath (Join-Path $taskOutput 'dist') --workpath (Join-Path $taskOutput 'work') `
    --specpath $taskOutput (Join-Path $PSScriptRoot 'companion.py')
if ($LASTEXITCODE -ne 0) { throw 'Companion build failed' }
$taskBundle = Join-Path $taskOutput 'dist/BridgeLibraryCompanion'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $taskBundle
$taskReadme = Join-Path $taskBundle 'README.md'
[System.IO.File]::WriteAllText($taskReadme, ([System.IO.File]::ReadAllText($taskReadme).Replace('../docs/COMPANION.md', 'docs/COMPANION.md')))
$taskDocs = Join-Path $taskBundle 'docs'
New-Item -ItemType Directory -Path $taskDocs | Out-Null
Copy-Item -LiteralPath (Join-Path $taskRepo 'docs/COMPANION.md') -Destination $taskDocs
Copy-Item -LiteralPath (Join-Path $taskRepo 'LICENSE') -Destination $taskBundle
Copy-Item -LiteralPath (Join-Path $taskRepo 'NOTICE') -Destination $taskBundle
Copy-Item -LiteralPath (Join-Path $taskRepo 'LICENSES') -Destination $taskBundle -Recurse
$taskSource = Join-Path $taskBundle 'source'
New-Item -ItemType Directory -Path $taskSource | Out-Null
Copy-Item -LiteralPath (Join-Path $taskRepo 'plugins/bridge_library/library_plugin.py') -Destination $taskSource
Get-ChildItem -LiteralPath $PSScriptRoot -File | Where-Object {
    $_.Extension -in '.py', '.txt', '.ps1', '.md'
} | Copy-Item -Destination $taskSource
& $Python -m pip freeze | Set-Content -LiteralPath (Join-Path $taskSource 'requirements-build-lock.txt') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'Could not record the build dependencies' }
$taskFiles = Get-ChildItem -LiteralPath $taskBundle -File -Recurse
$taskFiles | ForEach-Object {
    $taskRelative = [System.IO.Path]::GetRelativePath($taskBundle, $_.FullName).Replace('\', '/')
    '{0}  {1}' -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $taskRelative
} | Set-Content -LiteralPath (Join-Path $taskBundle 'SHA256SUMS.txt') -Encoding utf8
Compress-Archive -LiteralPath $taskBundle -DestinationPath (Join-Path $taskOutput 'BridgeLibraryCompanion-windows-preview.zip')
Get-FileHash -LiteralPath (Join-Path $taskOutput 'BridgeLibraryCompanion-windows-preview.zip') -Algorithm SHA256
