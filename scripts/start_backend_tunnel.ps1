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
    [switch]$VisibleWindows,
    [switch]$Monitor,
    [int]$MonitorIntervalSeconds = 30,
    [string]$OAuthUsername = "admin",
    [string]$OAuthPassword = "248655ab",
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

function ConvertTo-SingleQuotedLiteral {
    param([string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertTo-CmdQuotedArgument {
    param([string]$Value)

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Start-VisiblePowerShellWorker {
    param(
        [string]$Command,
        [string]$WorkingDirectory
    )

    $encodedCommand = [Convert]::ToBase64String(
        [System.Text.Encoding]::Unicode.GetBytes($Command)
    )

    return Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoExit",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            $encodedCommand
        ) `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Normal `
        -PassThru
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

function Stop-BackendProcessOnPort {
    param(
        [int]$Port
    )

    $listeners = Get-NetTCPConnection `
        -LocalAddress 127.0.0.1 `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue

    foreach ($listener in $listeners) {
        $processId = [int]$listener.OwningProcess
        if (-not $processId) {
            continue
        }

        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if (-not $processInfo) {
            continue
        }

        $commandLine = [string]$processInfo.CommandLine
        if ($commandLine -like "*run_server.py*") {
            Write-Host "Stopping backend process on port $Port (PID $processId)."
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }

    if ($listeners) {
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

function Test-ProcessAlive {
    param([int]$ProcessId)

    if (-not $ProcessId) {
        return $false
    }

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    return [bool]$process
}

function Get-ListeningProcessId {
    param([int]$Port)

    $listener = Get-NetTCPConnection `
        -LocalAddress 127.0.0.1 `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1

    if (-not $listener) {
        return ""
    }
    return [string]$listener.OwningProcess
}

function Start-HealthMonitor {
    param(
        [string]$HealthUrl,
        [string]$PublicBaseUrl,
        [string]$ConnectorUrl,
        [int]$Port,
        [int]$BackendProcessId,
        [int]$TunnelProcessId,
        [int]$IntervalSeconds
    )

    Write-Host ""
    Write-Host "Monitoring every $IntervalSeconds seconds. Press Ctrl+C to stop this monitor."
    Write-Host "Backend and Cloudflare worker windows remain visible for logs."

    while ($true) {
        $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        $backendHealth = "DOWN"
        $oauthDiscovery = "DOWN"

        try {
            $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 -ErrorAction Stop
            if ($health.status -eq "ok") {
                $backendHealth = "OK"
            }
        } catch {
            $backendHealth = "DOWN"
        }

        try {
            $metadata = Invoke-RestMethod `
                -Uri "$PublicBaseUrl/.well-known/oauth-protected-resource" `
                -TimeoutSec 8 `
                -ErrorAction Stop
            if ([string]$metadata.resource -eq $ConnectorUrl) {
                $oauthDiscovery = "OK"
            } else {
                $oauthDiscovery = "MISMATCH"
            }
        } catch {
            $oauthDiscovery = "DOWN"
        }

        $portOwner = Get-ListeningProcessId -Port $Port
        if (-not $portOwner) {
            $portOwner = "none"
        }

        $backendProcess = if (Test-ProcessAlive -ProcessId $BackendProcessId) { "alive" } else { "exited" }
        $tunnelProcess = if (Test-ProcessAlive -ProcessId $TunnelProcessId) { "alive" } else { "exited" }

        Write-Host "[$time] backend=$backendHealth port=$Port owner=$portOwner backendProcess=$backendProcess tunnelProcess=$tunnelProcess oauth=$oauthDiscovery"
        Start-Sleep -Seconds $IntervalSeconds
    }
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
$TunnelInfoPath = Join-Path $LogsDir "latest_tunnel_info.txt"
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
if ($VisibleWindows) {
    Write-Host "Visible worker windows: enabled"
}

if ($DryRun) {
    Write-Host "Dry run only. No process was started and config was not changed."
    exit 0
}

if (-not $NoStopPrevious) {
    Stop-RecordedProcess -PidFile $BackendPidFile -Label "backend" -Markers @("run_server.py")
    Stop-RecordedProcess -PidFile $TunnelPidFile -Label "cloudflared" -Markers @("cloudflared", "trycloudflare")
    Stop-ManualBackendProcesses -ProjectRoot $ProjectDir -RunServerPath $RunServer
    Stop-BackendProcessOnPort -Port $Port
    Stop-ManualTunnelProcesses -LocalUrl $LocalBaseUrl
}

$remainingPortOwner = Get-ListeningProcessId -Port $Port
if ($remainingPortOwner) {
    throw "Port $Port is still occupied by PID $remainingPortOwner. Stop that process or start this script with another port."
}

Write-Host "Starting cloudflared tunnel..."
if ($VisibleWindows) {
    Set-Content -LiteralPath $TunnelErrLog -Value "" -Encoding UTF8
    $CloudCmd = "$(ConvertTo-CmdQuotedArgument $CloudflaredPath) tunnel --url $(ConvertTo-CmdQuotedArgument $LocalBaseUrl) 2>&1"
    $CloudCommand = @"
`$host.UI.RawUI.WindowTitle = "Private Literature MCP - Cloudflare"
Set-Location -LiteralPath $(ConvertTo-SingleQuotedLiteral $ProjectDir)
Write-Host "Cloudflare tunnel terminal"
Write-Host "Forwarding to: $LocalBaseUrl"
Write-Host "If this window shows a temporary 502 before backend is ready, wait for the Backend window."
Write-Host ""
cmd.exe /d /c $(ConvertTo-SingleQuotedLiteral $CloudCmd) | Tee-Object -FilePath $(ConvertTo-SingleQuotedLiteral $TunnelErrLog)
Write-Host ""
Write-Host "cloudflared stopped. You can close this window when finished."
"@
    $TunnelProcess = Start-VisiblePowerShellWorker `
        -Command $CloudCommand `
        -WorkingDirectory $ProjectDir
} else {
    $TunnelProcess = Start-Process `
        -FilePath $CloudflaredPath `
        -ArgumentList @("tunnel", "--url", $LocalBaseUrl) `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $TunnelOutLog `
        -RedirectStandardError $TunnelErrLog `
        -WindowStyle Hidden `
        -PassThru
}
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
    "--app-port", $Port,
    "--oauth-username", $OAuthUsername,
    "--oauth-password", $OAuthPassword,
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

$ConnectorUrl = "$($UpdateInfo.public_base_url)/mcp"
$TunnelInfo = @"
Public base URL:
$($UpdateInfo.public_base_url)

Connector URL for GPT:
$ConnectorUrl

OAuth username:
$OAuthUsername

OAuth password:
$OAuthPassword

Bearer token:
$($UpdateInfo.bearer_token)
"@

Set-Content -LiteralPath $TunnelInfoPath -Value $TunnelInfo -Encoding UTF8
try {
    Set-Clipboard -Value $ConnectorUrl -ErrorAction Stop
    $ClipboardMessage = "Connector URL was copied to clipboard."
} catch {
    $ClipboardMessage = "Clipboard copy was not available; copy the Connector URL below."
}

Write-Host "Updated config.local.yaml"
Write-Host ""
Write-Host "============================================================"
Write-Host "COPY THIS INTO GPT CONFIG"
Write-Host "Public base URL:"
Write-Host "$($UpdateInfo.public_base_url)"
Write-Host ""
Write-Host "Connector URL for GPT:"
Write-Host "$ConnectorUrl"
Write-Host ""
Write-Host "OAuth username:"
Write-Host "$OAuthUsername"
Write-Host ""
Write-Host "OAuth password:"
Write-Host "$OAuthPassword"
Write-Host ""
Write-Host "Bearer token:"
Write-Host "$($UpdateInfo.bearer_token)"
Write-Host "============================================================"
Write-Host $ClipboardMessage
Write-Host "Saved the same info to: $TunnelInfoPath"
Write-Host ""

Write-Host "Starting backend..."
if ($VisibleWindows) {
    Set-Content -LiteralPath $BackendErrLog -Value "" -Encoding UTF8
    $BackendCmd = "$(ConvertTo-CmdQuotedArgument $PythonPath) $(ConvertTo-CmdQuotedArgument $RunServer) 2>&1"
    $BackendCommand = @"
`$host.UI.RawUI.WindowTitle = "Private Literature MCP - Backend"
Set-Location -LiteralPath $(ConvertTo-SingleQuotedLiteral $ProjectDir)
Write-Host "Backend terminal"
Write-Host "Local site: $LocalBaseUrl"
Write-Host ""
cmd.exe /d /c $(ConvertTo-SingleQuotedLiteral $BackendCmd) | Tee-Object -FilePath $(ConvertTo-SingleQuotedLiteral $BackendErrLog)
Write-Host ""
Write-Host "Backend stopped. You can close this window when finished."
"@
    $BackendProcess = Start-VisiblePowerShellWorker `
        -Command $BackendCommand `
        -WorkingDirectory $ProjectDir
} else {
    $BackendProcess = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @($RunServer) `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $BackendOutLog `
        -RedirectStandardError $BackendErrLog `
        -WindowStyle Hidden `
        -PassThru
}
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
Write-Host "Connector URL: $ConnectorUrl"
Write-Host "Saved GPT config info: $TunnelInfoPath"
Write-Host "Logs:"
Write-Host "  $BackendErrLog"
Write-Host "  $TunnelErrLog"

if ($Monitor) {
    Start-HealthMonitor `
        -HealthUrl $HealthUrl `
        -PublicBaseUrl $PublicBaseUrl `
        -ConnectorUrl $ConnectorUrl `
        -Port $Port `
        -BackendProcessId $BackendProcess.Id `
        -TunnelProcessId $TunnelProcess.Id `
        -IntervalSeconds $MonitorIntervalSeconds
}
