[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath
)

$ErrorActionPreference = 'Stop'
# Use this host's module, not a PowerShell 7 module inherited through PSModulePath.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
$BackupPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $BackupPath).Path)
$journal = Get-Content -LiteralPath (Join-Path $BackupPath 'installation.json') -Raw | ConvertFrom-Json
$isDefaultConfig = [IO.Path]::GetFullPath($journal.config_path).StartsWith(
    (Join-Path $env:APPDATA 'cura').TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
if ($isDefaultConfig -and (Get-Process -Name 'UltiMaker-Cura', 'Cura' -ErrorAction SilentlyContinue)) {
    throw 'Save your project and close Cura before restoring.'
}
$config = [IO.Path]::GetFullPath($journal.config_path).TrimEnd('\') + '\'
$manifest = Get-Content -LiteralPath (Join-Path $BackupPath 'sha256.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest) {
    $source = [IO.Path]::GetFullPath((Join-Path $BackupPath $entry.path))
    if (-not $source.StartsWith($BackupPath.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unsafe path in backup manifest.'
    }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $entry.sha256) {
        throw "Corrupted backup file: $($entry.path)"
    }
}
$targets = @()
foreach ($entry in $journal.files) {
    $target = [IO.Path]::GetFullPath((Join-Path $config $entry.path))
    if (-not $target.StartsWith($config, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unsafe path in installation journal.'
    }
    if ((Test-Path -LiteralPath $target) -and
        (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $entry.installed_sha256) {
        throw "Installed file changed after installation: $target. Preserve your edits before restoring."
    }
    $targets += @{ entry = $entry; target = $target }
}
foreach ($item in $targets) {
    if ($item.entry.existed) {
        New-Item -ItemType Directory -Path (Split-Path $item.target) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $BackupPath ('config\' + $item.entry.path)) -Destination $item.target -Force
        $expected = ($manifest | Where-Object { $_.path -eq ('config\' + $item.entry.path) }).sha256
        if ((Get-FileHash -LiteralPath $item.target -Algorithm SHA256).Hash -ne $expected) {
            throw "Restoration verification failed: $($item.target)"
        }
    } elseif (Test-Path -LiteralPath $item.target) {
        Remove-Item -LiteralPath $item.target
    }
}
Write-Host 'Integration files restored. Other Cura files, printers and later user settings were left untouched.'
Write-Host 'The full configuration/cache backup remains available in the backup folder.'
