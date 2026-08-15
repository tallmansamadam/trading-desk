<#
.SYNOPSIS
    Waits a given number of hours, then wakes Claude Code up with one or more prompts.

.EXAMPLE
    .\sleeper.ps1 -Hours 3 -Message "Check if the build passed and fix anything broken."

.EXAMPLE
    # Fractional hours, several prompts in sequence, detached so the terminal is free.
    .\sleeper.ps1 -Hours 0.5 -Message "Summarize what changed.","Now open a PR." -Detach

.EXAMPLE
    # Start a brand new session instead of continuing the last one in -Cwd.
    .\sleeper.ps1 -Hours 8 -Message "Morning triage." -Mode New
#>
[CmdletBinding()]
param(
    # Optional. If omitted, the script asks interactively (unless -Detach is used).
    [ValidateRange(0.0, 720.0)]
    [Nullable[double]] $Hours,

    [Parameter(Mandatory)]
    [string[]] $Message,

    # Continue = resume the most recent session in -Cwd; New = fresh session;
    # Resume = resume the specific session named by -SessionId.
    [ValidateSet('Continue', 'New', 'Resume')]
    [string] $Mode = 'Continue',

    [string] $SessionId,

    [string] $Cwd = (Get-Location).Path,

    [string] $LogFile = (Join-Path $PSScriptRoot 'sleeper.log'),

    # Re-launch self in a background process and return immediately.
    [switch] $Detach
)

$ErrorActionPreference = 'Stop'

if ($Mode -eq 'Resume' -and -not $SessionId) {
    throw "-Mode Resume requires -SessionId."
}

# Parse a human duration ("3h", "30m", "1.5h", "90" = minutes... no, plain = hours) into hours.
function ConvertTo-Hours {
    param([string] $Text)
    $t = $Text.Trim().ToLower()
    if ($t -match '^([0-9]*\.?[0-9]+)\s*h(ours?)?$') { return [double]$Matches[1] }
    if ($t -match '^([0-9]*\.?[0-9]+)\s*m(in(utes?)?)?$') { return [double]$Matches[1] / 60.0 }
    if ($t -match '^([0-9]*\.?[0-9]+)$') { return [double]$Matches[1] }  # bare number = hours
    return $null
}

# Prompt interactively for the delay when -Hours wasn't supplied on the command line.
if ($null -eq $Hours) {
    if ($Detach) { throw "-Hours is required when using -Detach (no console to prompt on)." }
    while ($true) {
        $answer = Read-Host 'How long before resuming Claude? (e.g. 3h, 30m, 1.5h)'
        $parsed = ConvertTo-Hours $answer
        if ($null -ne $parsed -and $parsed -ge 0 -and $parsed -le 720) {
            $Hours = $parsed
            break
        }
        Write-Host "  Didn't understand '$answer'. Use forms like 3h, 45m, or 1.5h (max 720h)." -ForegroundColor Yellow
    }
}

function Write-Log {
    param([string] $Text)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Text
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

if ($Detach) {
    $argList = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $PSCommandPath,
        '-Hours', $Hours,
        '-Mode', $Mode,
        '-Cwd', $Cwd,
        '-LogFile', $LogFile,
        '-Message'
    ) + $Message
    if ($SessionId) { $argList += @('-SessionId', $SessionId) }

    $proc = Start-Process -FilePath 'pwsh' -ArgumentList $argList -WindowStyle Hidden -PassThru
    Write-Log "Detached as PID $($proc.Id); waking at $((Get-Date).AddHours($Hours))."
    return
}

$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { throw "The 'claude' CLI is not on PATH." }
if (-not (Test-Path -LiteralPath $Cwd)) { throw "Working directory not found: $Cwd" }

$wakeAt = (Get-Date).AddHours($Hours)
Write-Log "Sleeping $Hours h. Wake at $wakeAt. Mode=$Mode Cwd=$Cwd Prompts=$($Message.Count)"

# Sleep in short slices so the wake time stays accurate if the machine suspends,
# and so Ctrl+C lands promptly.
while ((Get-Date) -lt $wakeAt) {
    $remaining = ($wakeAt - (Get-Date)).TotalSeconds
    Start-Sleep -Seconds ([Math]::Min(60, [Math]::Max(1, $remaining)))
}

Write-Log 'Waking up.'
Push-Location $Cwd
try {
    for ($i = 0; $i -lt $Message.Count; $i++) {
        # Only the first prompt honors -Mode; the rest continue the session it just touched.
        if ($i -eq 0) {
            switch ($Mode) {
                'Continue' { $sessionArgs = @('--continue') }
                'New'      { $sessionArgs = @() }
                'Resume'   { $sessionArgs = @('--resume', $SessionId) }
            }
        }
        else {
            $sessionArgs = @('--continue')
        }

        Write-Log "Prompt $($i + 1)/$($Message.Count): $($Message[$i])"
        $output = & $claude @sessionArgs -p $Message[$i] 2>&1 | Out-String
        Write-Log "Exit $LASTEXITCODE. Output:`n$output"

        if ($LASTEXITCODE -ne 0) {
            Write-Log 'Non-zero exit; stopping remaining prompts.'
            break
        }
    }
}
finally {
    Pop-Location
}

Write-Log 'Done.'
