# Start the cloudlogs viewer on Windows (PowerShell), outside WSL.
#
#   .\run.ps1
#   .\run.ps1 -Port 9000
#   .\run.ps1 -BindHost 127.0.0.1        # loopback only
#   $env:CLOUDLOGS_INPUT = 'logs\**\*.log'; .\run.ps1
#
# There is no authentication; on an untrusted network use -BindHost 127.0.0.1.
param(
    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 8000 }),
    [string]$BindHost = $(if ($env:HOST) { $env:HOST } else { '0.0.0.0' })
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

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
