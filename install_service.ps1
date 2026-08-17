<#
.SYNOPSIS
    Install (or remove) the trading desk as a background program that starts
    with Windows and serves its GUI at http://127.0.0.1:6400.

.DESCRIPTION
    Registers a Scheduled Task that runs at logon in your own user context.
    No administrator rights are needed and nothing is written outside your
    profile and this folder.

    Installing does NOT arm trading. The service starts disarmed: it watches,
    reports, and serves the dashboard, but places no orders until you press ARM
    in the GUI. Arming is a decision a person makes while looking at the state,
    not a side effect of an install script.

.EXAMPLE
    .\install_service.ps1                 # install and start
.EXAMPLE
    .\install_service.ps1 -Uninstall      # remove the task and stop the service
.EXAMPLE
    .\install_service.ps1 -Status         # is it registered? is it running?
#>
[CmdletBinding()]
param(
    [switch] $Uninstall,
    [switch] $Status,
    [int]    $Port = 6400
)

$ErrorActionPreference = 'Stop'

$TaskName = 'ClaudeTradingDesk64'
$Root     = $PSScriptRoot
$Script   = Join-Path $Root 'desk_service.py'
$LogDir   = Join-Path $Root 'logs'
$OutLog   = Join-Path $LogDir 'service.out.log'
$Url      = "http://127.0.0.1:$Port"

function Get-PythonW {
    # pythonw runs without a console window, which is what you want for
    # something that lives in the background. Fall back to python if absent.
    $py = (Get-Command pythonw -ErrorAction SilentlyContinue)
    if ($py) { return $py.Source }
    $py = (Get-Command python -ErrorAction SilentlyContinue)
    if ($py) {
        $candidate = Join-Path (Split-Path $py.Source) 'pythonw.exe'
        if (Test-Path $candidate) { return $candidate }
        return $py.Source
    }
    throw "Neither pythonw nor python is on PATH."
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "  task        : REGISTERED ($($task.State))"
        Write-Host "  last run    : $($info.LastRunTime)  result $($info.LastTaskResult)"
    } else {
        Write-Host "  task        : not registered"
    }
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Host "  service     : RUNNING on port $Port (pid $($listening.OwningProcess -join ','))"
        Write-Host "  dashboard   : $Url"
    } else {
        Write-Host "  service     : not listening on $Port"
    }
    $state = Join-Path $LogDir 'service_state.json'
    if (Test-Path $state) {
        $armed = (Get-Content $state -Raw | ConvertFrom-Json).armed
        Write-Host "  auto-trade  : $(if ($armed) { 'ARMED' } else { 'disarmed' })"
    }
    $halt = Join-Path $Root 'HALT'
    if (Test-Path $halt) { Write-Host "  kill switch : HALTED" }
}

function Stop-Service-Processes {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'desk_service\.py' } |
        ForEach-Object {
            Write-Host "  stopping pid $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

# ---------------------------------------------------------------- status ----
if ($Status) {
    Write-Host "CLAUDE TRADING DESK 64"
    Show-Status
    return
}

# ------------------------------------------------------------- uninstall ----
if ($Uninstall) {
    Write-Host "Removing the trading desk service..."
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  scheduled task removed"
    } else {
        Write-Host "  no scheduled task registered"
    }
    Stop-Service-Processes
    foreach ($dir in @([Environment]::GetFolderPath('Programs'),
                       [Environment]::GetFolderPath('Desktop'))) {
        $lnk = Join-Path $dir 'Trading Desk 64.lnk'
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "  removed $lnk" }
    }
    Write-Host ""
    Write-Host "Uninstalled. Your data, logs and .env are untouched."
    Write-Host "NOTE: any open positions and resting orders are still at the broker."
    Write-Host "      This removed the automation, not the book."
    return
}

# --------------------------------------------------------------- install ----
if (-not (Test-Path $Script)) { throw "desk_service.py not found in $Root" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$PythonW = Get-PythonW
Write-Host "CLAUDE TRADING DESK 64 - install"
Write-Host "  python      : $PythonW"
Write-Host "  script      : $Script"
Write-Host "  port        : $Port"

Stop-Service-Processes

$action = New-ScheduledTaskAction -Execute $PythonW `
    -Argument "`"$Script`" --no-browser --port $Port" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Claude Trading Desk 64 - paper trading service and dashboard" | Out-Null
Write-Host "  scheduled task registered (runs at logon, restarts on failure)"

# The service runs under pythonw, so it has no window and no taskbar entry.
# Without a shortcut there is literally nothing to click, and a Start Menu
# entry alone is easy to miss - so put one on the desktop too.
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Programs'),
                   [Environment]::GetFolderPath('Desktop'))) {
    $lnkPath = Join-Path $dir 'Trading Desk 64.lnk'
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $Url
    $lnk.Description = 'Open the Trading Desk 64 dashboard (service runs in the background)'
    $lnk.Save()
}
Write-Host "  shortcuts created (Start Menu and Desktop)"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5

Write-Host ""
Show-Status
Write-Host ""
Write-Host "Installed. The service starts automatically at logon."
Write-Host ""
Write-Host "It runs in the BACKGROUND with no window - there is no app to find in"
Write-Host "the taskbar. The interface is a web page. Open it with the"
Write-Host "'Trading Desk 64' shortcut on your desktop, or go to $Url"
Write-Host ""
Write-Host "It is DISARMED: it watches and reports but will not place orders."
Write-Host "Press ARM in the dashboard when you want it to trade."
Write-Host ""
Write-Host "  .\install_service.ps1 -Status      check on it"
Write-Host "  .\install_service.ps1 -Uninstall   remove it"
