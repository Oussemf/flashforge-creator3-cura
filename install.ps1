[CmdletBinding()]
param(
    [string]$CuraVersion = '5.12',
    [string]$ConfigPath,
    [string]$CachePath,
    [string]$BackupRoot = (Join-Path $PSScriptRoot 'backups')
)

$ErrorActionPreference = 'Stop'
# Use this host's module, not a PowerShell 7 module inherited through PSModulePath.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
if ($CuraVersion -notmatch '^\d+\.\d+$') { throw 'CuraVersion must use major.minor format.' }
$defaultConfig = Join-Path $env:APPDATA "cura\$CuraVersion"
$defaultCache = Join-Path $env:LOCALAPPDATA "cura\$CuraVersion"
if (-not $ConfigPath) { $ConfigPath = $defaultConfig }
if (-not $CachePath) { $CachePath = $defaultCache }
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$CachePath = [IO.Path]::GetFullPath($CachePath)
$BackupRoot = [IO.Path]::GetFullPath($BackupRoot)
if ($ConfigPath -eq $CachePath) { throw 'Configuration and cache paths must be different.' }
foreach ($path in @($ConfigPath, $CachePath)) {
    if ($BackupRoot -eq $path -or $BackupRoot.StartsWith($path.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Backups must be outside the Cura configuration and cache folders.'
    }
}
if ($ConfigPath -eq $defaultConfig -or $CachePath -eq $defaultCache) {
    if (Get-Process -Name 'UltiMaker-Cura', 'Cura' -ErrorAction SilentlyContinue) {
        throw 'Save your project and close Cura before installation.'
    }
} else {
    Write-Warning 'Custom paths selected: ensure no running Cura instance is using these folders.'
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Container)) {
    throw 'Configuration folder not found. Start Cura once, then close it, or set ConfigPath explicitly.'
}
$files = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'cura') -Recurse -File |
    Where-Object { $_.Extension -in '.py', '.qml', '.json' })
if ($files.Count -ne 9) { throw 'Incomplete distribution: expected nine Cura integration files.' }
$sourceRoot = (Join-Path $PSScriptRoot 'cura').TrimEnd('\') + '\'
$backup = Join-Path $BackupRoot ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $backup | Out-Null
$hashes = @()
foreach ($pair in @(@($ConfigPath, 'config'), @($CachePath, 'cache'))) {
    $source, $label = $pair
    $destination = Join-Path $backup $label
    New-Item -ItemType Directory -Path $destination | Out-Null
    if (Test-Path -LiteralPath $source -PathType Container) {
        $prefix = $source.TrimEnd('\') + '\'
        foreach ($item in Get-ChildItem -LiteralPath $source -Recurse -File -Force) {
            $relative = $item.FullName.Substring($prefix.Length)
            $copy = Join-Path $destination $relative
            New-Item -ItemType Directory -Path (Split-Path $copy) -Force | Out-Null
            $before = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
            Copy-Item -LiteralPath $item.FullName -Destination $copy
            $after = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
            if ($before -ne $after -or $before -ne (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash) {
                throw "Backup verification failed: $relative. No integration files have been installed."
            }
            $hashes += @{ path = "$label\$relative"; sha256 = $before }
        }
    }
}
ConvertTo-Json -InputObject @($hashes) -Depth 4 | Set-Content -LiteralPath (Join-Path $backup 'sha256.json') -Encoding UTF8
$journal = @()
foreach ($file in $files) {
    $relative = $file.FullName.Substring($sourceRoot.Length)
    $target = Join-Path $ConfigPath $relative
    $journal += @{
        path = $relative
        existed = (Test-Path -LiteralPath $target -PathType Leaf)
        installed_sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
}
@{
    config_path = $ConfigPath
    cache_path = $CachePath
    files = $journal
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $backup 'installation.json') -Encoding UTF8
Write-Host "Verified complete configuration/cache backup: $backup"
foreach ($entry in $journal) {
    $target = Join-Path $ConfigPath $entry.path
    New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceRoot $entry.path) -Destination $target -Force
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $entry.installed_sha256) {
        throw "Installation verification failed: $($entry.path). Restore using backup: $backup"
    }
}
Write-Host 'Installed nine English integration files. Existing printers and quality settings were not changed.'
Write-Host 'Restart Cura; add the Creator 3 - Cura Native Integration printer and its post-processing script.'
Write-Host "Rollback: .\restore.ps1 -BackupPath `"$backup`""
