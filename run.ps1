# Start the cloudlogs viewer on Windows (PowerShell), outside WSL.
#
#   .\run.ps1
#   .\run.ps1 path\to\app.log            # ingest this file instead
#   .\run.ps1 a.log b.log logs\           # several files, or a directory
#   .\run.ps1 'logs\**\*.log'             # a glob -- quote it
#   .\run.ps1 -Port 9000 app.log
#   .\run.ps1 -BindHost 127.0.0.1        # loopback only
#   $env:CLOUDLOGS_INPUT = 'logs\**\*.log'; .\run.ps1
#
# Relative paths are taken from the directory you ran this in, not from the
# project root. Arguments win over CLOUDLOGS_INPUT.
#
# There is no authentication; on an untrusted network use -BindHost 127.0.0.1.
#
# If PowerShell refuses to run this ("is not digitally signed"), either launch
# it as  powershell -ExecutionPolicy Bypass -File .\run.ps1  or allow local
# scripts once with  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned.
# PositionalBinding=$false: without it -Port and -BindHost claim positions 0
# and 1, so `.\run.ps1 -Port 9000 app.log` would bind the path to -BindHost.
[CmdletBinding(PositionalBinding = $false)]
param(
    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 8000 }),
    [string]$BindHost = $(if ($env:HOST) { $env:HOST } else { '0.0.0.0' }),
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$LogPath
)

$ErrorActionPreference = 'Stop'

# Resolve inputs against the caller's directory before moving to the project
# root. A glob stays a pattern -- ingest expands it -- so Resolve-Path is out.
$launchPwd = (Get-Location).Path
Set-Location -Path $PSScriptRoot

if ($LogPath) {
    $resolved = $LogPath | ForEach-Object {
        if ([System.IO.Path]::IsPathRooted($_)) { $_ } else { Join-Path $launchPwd $_ }
    }
    $env:CLOUDLOGS_INPUT = ($resolved -join [System.IO.Path]::PathSeparator)
    Write-Host "cloudlogs: input $env:CLOUDLOGS_INPUT"
} elseif ($env:CLOUDLOGS_INPUT) {
    Write-Host "cloudlogs: input $env:CLOUDLOGS_INPUT  (from CLOUDLOGS_INPUT)"
}

$py = $env:PYTHON
if (-not $py) {
    foreach ($candidate in 'py', 'python', 'python3') {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { $py = $candidate; break }
    }
}
if (-not $py) { throw 'cloudlogs: no Python on PATH; install Python 3.10+ or set $env:PYTHON' }

Write-Host "cloudlogs: http://localhost:$Port"
if ($BindHost -ne '127.0.0.1' -and $BindHost -ne 'localhost') {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        ForEach-Object { Write-Host "cloudlogs: http://$($_.IPAddress):$Port" }
}

& $py -m uvicorn cloudlogs.main:app --reload --host $BindHost --port $Port
