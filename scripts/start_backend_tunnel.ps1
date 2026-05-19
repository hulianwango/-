[CmdletBinding()]
param(
    [string]$ProjectDir = "",
    [string]$PythonPath = "",
    [string]$CloudflaredPath = "",
    [string]$ConfigPath = "",
    [int]$Port = 8000,
    [int]$TunnelTimeoutSeconds = 90,
    [int]$BackendTimeoutSeconds = 30,
    [switch]$KeepToken,
    [switch]$NoStopPrevious,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Resolve-ToolPath {
    param(
        [string]$Name,
        [string]$Override
    )

    if ($Override) {
        if (Test-Path -LiteralPath $Override -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Override).Path
        }
        return (Get-Command $Override -ErrorAction Stop).Source
    }

    return (Get-Command $Name -ErrorAction Stop).Source
}

function Resolve-PythonPath {
    param([string]$Root, [string]$Override)

    if ($Override) {
        return Resolve-ToolPath -Name "python" -Override $Override
    }

    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $venvPython).Path
    }

    return Resolve-ToolPath -Name "python" -Override ""
}

function Stop-RecordedProcess {
    param(
        [string]$PidFile,
        [string]$Label,
        [string[]]$Markers
    )

    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        return
    }

    $rawPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $processId = 0
    if (-not [int]::TryParse($rawPid, [ref]$processId)) {
        return
    }

    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (-not $processInfo) {
        return
    }

    $commandLine = [string]$processInfo.CommandLine
    foreach ($marker in $Markers) {
        if ($commandLine -like "*$marker*") {
            Write-Host "Stopping previous $Label process (PID $processId)."
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
            return
        }
    }

    Write-Host "PID file for $Label points to another process; leaving it running."
}

function Stop-ManualBackendProcesses {
    param(
        [string]$ProjectRoot,
        [string]$RunServerPath
    )

    $escapedRoot = $ProjectRoot.Replace("\", "\\")
    $escapedRunServer = $RunServerPath.Replace("\", "\\")
    $foundProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $cmd = [string]$_.CommandLine
            ($cmd -like "*run_server.py*") -and
            (($cmd -like "*$ProjectRoot*") -or ($cmd -like "*$RunServerPath*") -or ($cmd -like "*$escapedRoot*") -or ($cmd -like "*$escapedRunServer*"))
        }

    foreach ($item in $foundProcesses) {
        Write-Host "Stopping existing backend process (PID $($item.ProcessId))."
        Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
    }

    if ($foundProcesses) {
        Start-Sleep -Milliseconds 700
    }
}

function Stop-ManualTunnelProcesses {
    param([string]$LocalUrl)

    $foundProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $cmd = [string]$_.CommandLine
            ($cmd -like "*cloudflared*") -and
            ($cmd -like "*tunnel*") -and
            ($cmd -like "*--url*") -and
            ($cmd -like "*$LocalUrl*")
        }

    foreach ($item in $foundProcesses) {
        Write-Host "Stopping existing cloudflared tunnel process (PID $($item.ProcessId))."
        Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
    }

    if ($foundProcesses) {
        Start-Sleep -Milliseconds 700
    }
}

function Get-RecentLogLines {
    param([string[]]$Paths)

    $lines = @()
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $lines += Get-Content -LiteralPath $path -Tail 20 -ErrorAction SilentlyContinue
        }
    }
    return ($lines -join [Environment]::NewLine)
}

function Wait-ForTunnelUrl {
    param(
        [System.Diagnostics.Process]$Process,
        [string[]]$LogPaths,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $pattern = "https://[a-zA-Z0-9-]+\.trycloudflare\.com"

    while ((Get-Date) -lt $deadline) {
        foreach ($path in $LogPaths) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                continue
            }

            $content = Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
            if ($content -match $pattern) {
                return $Matches[0]
            }
        }

        $Process.Refresh()
        if ($Process.HasExited) {
            $recent = Get-RecentLogLines -Paths $LogPaths
            throw "cloudflared exited before a tunnel URL was printed.`n$recent"
        }

        Start-Sleep -Milliseconds 500
    }

    $tail = Get-RecentLogLines -Paths $LogPaths
    throw "Timed out waiting for a trycloudflare URL after $TimeoutSeconds seconds.`n$tail"
}

function Wait-ForBackendHealth {
    param(
        [string]$HealthUrl,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2 -ErrorAction Stop
            if ($response.status -eq "ok") {
                return $true
            }
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }
    return $false
}

if (-not $ProjectDir) {
    $ProjectDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
} else {
    $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
}

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ProjectDir "config.local.yaml"
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path

$PythonPath = Resolve-PythonPath -Root $ProjectDir -Override $PythonPath
$CloudflaredPath = Resolve-ToolPath -Name "cloudflared" -Override $CloudflaredPath

$RunServer = Join-Path $ProjectDir "run_server.py"
$ConfigUpdater = Join-Path $ProjectDir "scripts\update_runtime_config.py"
$LogsDir = Join-Path $ProjectDir "logs"
$BackendPidFile = Join-Path $LogsDir "backend.pid"
$TunnelPidFile = Join-Path $LogsDir "cloudflared.pid"
$BackendOutLog = Join-Path $LogsDir "backend.current.out.log"
$BackendErrLog = Join-Path $LogsDir "backend.current.err.log"
$TunnelOutLog = Join-Path $LogsDir "cloudflared.current.out.log"
$TunnelErrLog = Join-Path $LogsDir "cloudflared.current.err.log"
$LocalBaseUrl = "http://127.0.0.1:$Port"

if (-not (Test-Path -LiteralPath $RunServer -PathType Leaf)) {
    throw "run_server.py was not found under $ProjectDir"
}
if (-not (Test-Path -LiteralPath $ConfigUpdater -PathType Leaf)) {
    throw "Config updater was not found: $ConfigUpdater"
}

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

Write-Host "Project: $ProjectDir"
Write-Host "Python: $PythonPath"
Write-Host "cloudflared: $CloudflaredPath"
Write-Host "Config: $ConfigPath"
Write-Host "Local backend URL: $LocalBaseUrl"

if ($DryRun) {
    Write-Host "Dry run only. No process was started and config was not changed."
    exit 0
}

if (-not $NoStopPrevious) {
    Stop-RecordedProcess -PidFile $BackendPidFile -Label "backend" -Markers @("run_server.py")
    Stop-RecordedProcess -PidFile $TunnelPidFile -Label "cloudflared" -Markers @("cloudflared", "trycloudflare")
    Stop-ManualBackendProcesses -ProjectRoot $ProjectDir -RunServerPath $RunServer
    Stop-ManualTunnelProcesses -LocalUrl $LocalBaseUrl
}

Write-Host "Starting cloudflared tunnel..."
$TunnelProcess = Start-Process `
    -FilePath $CloudflaredPath `
    -ArgumentList @("tunnel", "--url", $LocalBaseUrl) `
    -WorkingDirectory $ProjectDir `
    -RedirectStandardOutput $TunnelOutLog `
    -RedirectStandardError $TunnelErrLog `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $TunnelPidFile -Value $TunnelProcess.Id -Encoding ASCII

$PublicBaseUrl = Wait-ForTunnelUrl `
    -Process $TunnelProcess `
    -LogPaths @($TunnelOutLog, $TunnelErrLog) `
    -TimeoutSeconds $TunnelTimeoutSeconds

Write-Host "Tunnel URL: $PublicBaseUrl"

$UpdateArgs = @(
    $ConfigUpdater,
    "--config", $ConfigPath,
    "--public-base-url", $PublicBaseUrl,
    "--output-json"
)
if ($KeepToken) {
    $UpdateArgs += "--keep-token"
} else {
    $UpdateArgs += "--generate-token"
}

$UpdateOutput = & $PythonPath @UpdateArgs
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update config.local.yaml"
}
$UpdateInfo = $UpdateOutput | ConvertFrom-Json

Write-Host "Updated config.local.yaml"
Write-Host "MCP URL: $($UpdateInfo.public_base_url)/mcp"
Write-Host "Bearer token: $($UpdateInfo.bearer_token)"

Write-Host "Starting backend..."
$BackendProcess = Start-Process `
    -FilePath $PythonPath `
    -ArgumentList @($RunServer) `
    -WorkingDirectory $ProjectDir `
    -RedirectStandardOutput $BackendOutLog `
    -RedirectStandardError $BackendErrLog `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $BackendPidFile -Value $BackendProcess.Id -Encoding ASCII

$HealthUrl = "$LocalBaseUrl/health"
if (Wait-ForBackendHealth -HealthUrl $HealthUrl -TimeoutSeconds $BackendTimeoutSeconds) {
    Write-Host "Backend health OK: $HealthUrl"
} else {
    Write-Warning "Backend did not answer /health within $BackendTimeoutSeconds seconds."
    Write-Warning "Check log: $BackendErrLog"
}

Write-Host ""
Write-Host "Ready."
Write-Host "Local site: $LocalBaseUrl"
Write-Host "Public site: $PublicBaseUrl"
Write-Host "Connector URL: $PublicBaseUrl/mcp"
Write-Host "Logs:"
Write-Host "  $BackendErrLog"
Write-Host "  $TunnelErrLog"
