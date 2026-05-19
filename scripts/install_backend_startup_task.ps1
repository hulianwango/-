[CmdletBinding()]
param(
    [string]$TaskName = "PrivateLiteratureMcpBackend",
    [string]$ProjectDir = "",
    [string]$PythonPath = "",
    [switch]$AtStartup
)

$ErrorActionPreference = "Stop"

if (-not $ProjectDir) {
    $ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
    $ProjectDir = (Resolve-Path $ProjectDir).Path
}

if (-not $PythonPath) {
    $PythonPath = (Get-Command python -ErrorAction Stop).Source
}

$RunServer = Join-Path $ProjectDir "run_server.py"
if (-not (Test-Path -LiteralPath $RunServer -PathType Leaf)) {
    throw "run_server.py was not found under $ProjectDir"
}

$Trigger = if ($AtStartup) {
    New-ScheduledTaskTrigger -AtStartup
} else {
    New-ScheduledTaskTrigger -AtLogOn
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$RunServer`"" `
    -WorkingDirectory $ProjectDir

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Start the Private Literature MCP FastAPI backend." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Python: $PythonPath"
Write-Host "Project: $ProjectDir"
