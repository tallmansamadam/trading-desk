<#
    Claude Sleeper — a small WinForms front end for sleeper.ps1.
    Collects a delay + prompts, counts down live, then hands off to the worker
    script which resumes Claude Code in the chosen working directory.
#>
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# Hide the host console window (belt-and-suspenders alongside -WindowStyle Hidden).
try {
    $sig = '[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();' +
           '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
    $win = Add-Type -MemberDefinition $sig -Name 'W' -Namespace 'Con' -PassThru
    $h = $win::GetConsoleWindow()
    if ($h -ne [IntPtr]::Zero) { [void]$win::ShowWindow($h, 0) }  # 0 = SW_HIDE
} catch { }

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Worker    = Join-Path $ScriptDir 'sleeper.ps1'

# Default working directory: from config.json if the installer wrote one, else script dir.
$defaultCwd = $ScriptDir
$cfgPath = Join-Path $ScriptDir 'config.json'
if (Test-Path -LiteralPath $cfgPath) {
    try {
        $cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
        if ($cfg.DefaultCwd -and (Test-Path -LiteralPath $cfg.DefaultCwd)) { $defaultCwd = $cfg.DefaultCwd }
    } catch { }
}

# ---- Form ---------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Claude Sleeper'
$form.Size = New-Object System.Drawing.Size(500, 560)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(26, 27, 38)
$form.ForeColor = [System.Drawing.Color]::FromArgb(220, 223, 233)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$icoPath = Join-Path $ScriptDir 'clock.ico'
if (Test-Path -LiteralPath $icoPath) { try { $form.Icon = New-Object System.Drawing.Icon($icoPath) } catch { } }

$accent = [System.Drawing.Color]::FromArgb(122, 162, 247)
$panelBg = [System.Drawing.Color]::FromArgb(36, 40, 59)

function New-Label($text, $x, $y, $w) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.Location = New-Object System.Drawing.Point($x, $y)
    $l.Size = New-Object System.Drawing.Size($w, 20); $l.ForeColor = $form.ForeColor
    $form.Controls.Add($l); return $l
}

$title = New-Label 'Resume Claude after a delay' 20 15 460
$title.Font = New-Object System.Drawing.Font('Segoe UI', 13, [System.Drawing.FontStyle]::Bold)
$title.ForeColor = $accent
$title.Height = 30

# Delay: hours + minutes
New-Label 'Wait for' 20 60 60 | Out-Null
$numH = New-Object System.Windows.Forms.NumericUpDown
$numH.Location = New-Object System.Drawing.Point(90, 58)
$numH.Size = New-Object System.Drawing.Size(60, 24)
$numH.Minimum = 0; $numH.Maximum = 720; $numH.Value = 3
$numH.BackColor = $panelBg; $numH.ForeColor = $form.ForeColor
$form.Controls.Add($numH)
New-Label 'hours' 155 60 40 | Out-Null

$numM = New-Object System.Windows.Forms.NumericUpDown
$numM.Location = New-Object System.Drawing.Point(210, 58)
$numM.Size = New-Object System.Drawing.Size(60, 24)
$numM.Minimum = 0; $numM.Maximum = 59; $numM.Value = 0
$numM.BackColor = $panelBg; $numM.ForeColor = $form.ForeColor
$form.Controls.Add($numM)
New-Label 'minutes' 275 60 55 | Out-Null

# Prompts
New-Label 'Prompt(s) to send when it wakes (one per line):' 20 95 440 | Out-Null
$txtMsg = New-Object System.Windows.Forms.TextBox
$txtMsg.Location = New-Object System.Drawing.Point(20, 118)
$txtMsg.Size = New-Object System.Drawing.Size(445, 140)
$txtMsg.Multiline = $true; $txtMsg.ScrollBars = 'Vertical'
$txtMsg.BackColor = $panelBg; $txtMsg.ForeColor = $form.ForeColor
$txtMsg.BorderStyle = 'FixedSingle'
$txtMsg.Text = 'Continue where we left off.'
$form.Controls.Add($txtMsg)

# Mode
New-Label 'Session:' 20 272 60 | Out-Null
$cmbMode = New-Object System.Windows.Forms.ComboBox
$cmbMode.Location = New-Object System.Drawing.Point(90, 270)
$cmbMode.Size = New-Object System.Drawing.Size(180, 24)
$cmbMode.DropDownStyle = 'DropDownList'
[void]$cmbMode.Items.AddRange(@('Continue last session', 'New session', 'Resume by ID'))
$cmbMode.SelectedIndex = 0
$cmbMode.BackColor = $panelBg; $cmbMode.ForeColor = $form.ForeColor
$form.Controls.Add($cmbMode)

$txtSid = New-Object System.Windows.Forms.TextBox
$txtSid.Location = New-Object System.Drawing.Point(280, 270)
$txtSid.Size = New-Object System.Drawing.Size(185, 24)
$txtSid.BackColor = $panelBg; $txtSid.ForeColor = $form.ForeColor
$txtSid.BorderStyle = 'FixedSingle'; $txtSid.Enabled = $false
$txtSid.Text = ''
$form.Controls.Add($txtSid)
$phSid = 'session id'
$cmbMode.Add_SelectedIndexChanged({ $txtSid.Enabled = ($cmbMode.SelectedIndex -eq 2) })

# Working directory
New-Label 'Project folder (where Claude resumes):' 20 305 440 | Out-Null
$txtCwd = New-Object System.Windows.Forms.TextBox
$txtCwd.Location = New-Object System.Drawing.Point(20, 328)
$txtCwd.Size = New-Object System.Drawing.Size(360, 24)
$txtCwd.BackColor = $panelBg; $txtCwd.ForeColor = $form.ForeColor
$txtCwd.BorderStyle = 'FixedSingle'; $txtCwd.Text = $defaultCwd
$form.Controls.Add($txtCwd)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Location = New-Object System.Drawing.Point(388, 327)
$btnBrowse.Size = New-Object System.Drawing.Size(77, 26)
$btnBrowse.Text = 'Browse'
$btnBrowse.FlatStyle = 'Flat'; $btnBrowse.BackColor = $panelBg; $btnBrowse.ForeColor = $form.ForeColor
$form.Controls.Add($btnBrowse)
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    if (Test-Path -LiteralPath $txtCwd.Text) { $dlg.SelectedPath = $txtCwd.Text }
    if ($dlg.ShowDialog() -eq 'OK') { $txtCwd.Text = $dlg.SelectedPath }
})

# Scheduling mode
$chkTask = New-Object System.Windows.Forms.CheckBox
$chkTask.Location = New-Object System.Drawing.Point(20, 360)
$chkTask.Size = New-Object System.Drawing.Size(445, 22)
$chkTask.Text = 'Use Windows Task Scheduler (survives reboot / sleep)'
$chkTask.ForeColor = $form.ForeColor
$form.Controls.Add($chkTask)

# Status / countdown
$lblStatus = New-Label '' 20 386 445
$lblStatus.Height = 40
$lblStatus.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
$lblStatus.TextAlign = 'MiddleCenter'

# Buttons
$btnStart = New-Object System.Windows.Forms.Button
$btnStart.Location = New-Object System.Drawing.Point(20, 470)
$btnStart.Size = New-Object System.Drawing.Size(220, 40)
$btnStart.Text = 'Start countdown'
$btnStart.FlatStyle = 'Flat'; $btnStart.BackColor = $accent
$btnStart.ForeColor = [System.Drawing.Color]::Black
$btnStart.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($btnStart)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Location = New-Object System.Drawing.Point(250, 470)
$btnCancel.Size = New-Object System.Drawing.Size(215, 40)
$btnCancel.Text = 'Close'
$btnCancel.FlatStyle = 'Flat'; $btnCancel.BackColor = $panelBg; $btnCancel.ForeColor = $form.ForeColor
$form.Controls.Add($btnCancel)

# ---- Countdown state ----------------------------------------------------
$script:wakeAt = $null
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500

function Set-InputsEnabled($on) {
    foreach ($c in @($numH, $numM, $txtMsg, $cmbMode, $btnBrowse, $txtCwd, $chkTask)) { $c.Enabled = $on }
    $txtSid.Enabled = ($on -and $cmbMode.SelectedIndex -eq 2)
}

function Get-SelectedMode {
    switch ($cmbMode.SelectedIndex) { 0 { 'Continue' } 1 { 'New' } 2 { 'Resume' } }
}

# Write the prompts to a file and return its path. $dir controls persistence:
# temp for the live countdown, the install jobs folder for scheduled tasks.
function Write-MessageFile($dir) {
    $lines = $txtMsg.Text -split "`r?`n" | Where-Object { $_.Trim() -ne '' }
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $file = Join-Path $dir ("claude_sleeper_{0}.txt" -f ([guid]::NewGuid().ToString('N')))
    Set-Content -LiteralPath $file -Value $lines -Encoding UTF8
    return $file
}

# The pwsh/powershell argument string that runs the worker immediately.
function Get-WorkerArgString($msgFile, $mode) {
    $a = @(
        '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $Worker),
        '-Hours', '0',
        '-Mode', $mode,
        '-Cwd', ('"{0}"' -f $txtCwd.Text),
        '-MessageFile', ('"{0}"' -f $msgFile)
    )
    if ($mode -eq 'Resume') { $a += @('-SessionId', ('"{0}"' -f $txtSid.Text)) }
    return ($a -join ' ')
}

function Get-Runner {
    $p7 = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
    if ($p7) { return $p7 }
    return (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe')
}

# Live-countdown path: launch the worker right now in a visible window.
function Launch-Worker {
    $tmp = Write-MessageFile ([System.IO.Path]::GetTempPath())
    Start-Process -FilePath (Get-Runner) -ArgumentList (Get-WorkerArgString $tmp (Get-SelectedMode)) | Out-Null
}

# Task Scheduler path: register a one-time task at $wakeAt that survives
# reboot/sleep (StartWhenAvailable catches up a missed start). Returns the
# task name on success; throws on failure.
function Register-WakeTask($wakeAt) {
    $jobsDir = Join-Path $ScriptDir 'jobs'
    $msgFile = Write-MessageFile $jobsDir
    $mode = Get-SelectedMode
    $argStr = Get-WorkerArgString $msgFile $mode

    $action  = New-ScheduledTaskAction -Execute (Get-Runner) -Argument $argStr
    $trigger = New-ScheduledTaskTrigger -Once -At $wakeAt
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $taskName = 'Wake_' + $wakeAt.ToString('yyyyMMdd_HHmmss')

    Register-ScheduledTask -TaskName $taskName -TaskPath '\ClaudeSleeper\' `
        -Action $action -Trigger $trigger -Settings $settings `
        -Description ('Resume Claude Code at {0}' -f $wakeAt) -Force | Out-Null
    return $taskName
}

$timer.Add_Tick({
    $remaining = $script:wakeAt - (Get-Date)
    if ($remaining.TotalSeconds -le 0) {
        $timer.Stop()
        $lblStatus.Text = 'Waking Claude now...'
        Launch-Worker
        [System.Windows.Forms.MessageBox]::Show(
            'Claude is resuming in a new window.', 'Claude Sleeper',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
        $form.Close()
        return
    }
    $lblStatus.ForeColor = $accent
    $lblStatus.Text = "Waking in {0:00}:{1:00}:{2:00}`n(at {3:t})" -f `
        [int]$remaining.TotalHours, $remaining.Minutes, $remaining.Seconds, $script:wakeAt
})

$btnStart.Add_Click({
    if ($timer.Enabled) { return }
    $totalMin = ([int]$numH.Value) * 60 + [int]$numM.Value
    if ($totalMin -le 0) {
        [System.Windows.Forms.MessageBox]::Show('Set a delay of at least 1 minute.', 'Claude Sleeper',
            'OK', 'Warning') | Out-Null
        return
    }
    if (($txtMsg.Text -split "`r?`n" | Where-Object { $_.Trim() -ne '' }).Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show('Enter at least one prompt.', 'Claude Sleeper',
            'OK', 'Warning') | Out-Null
        return
    }
    if (-not (Test-Path -LiteralPath $txtCwd.Text)) {
        [System.Windows.Forms.MessageBox]::Show('Project folder does not exist.', 'Claude Sleeper',
            'OK', 'Warning') | Out-Null
        return
    }
    if ($cmbMode.SelectedIndex -eq 2 -and [string]::IsNullOrWhiteSpace($txtSid.Text)) {
        [System.Windows.Forms.MessageBox]::Show('Resume by ID needs a session id.', 'Claude Sleeper',
            'OK', 'Warning') | Out-Null
        return
    }

    $script:wakeAt = (Get-Date).AddMinutes($totalMin)

    if ($chkTask.Checked) {
        # Durable path: hand off to Task Scheduler and close.
        try {
            $name = Register-WakeTask $script:wakeAt
        } catch {
            [System.Windows.Forms.MessageBox]::Show(
                "Could not create the scheduled task:`n`n$($_.Exception.Message)",
                'Claude Sleeper', 'OK', 'Error') | Out-Null
            return
        }
        [System.Windows.Forms.MessageBox]::Show(
            ("Scheduled.`n`nClaude will resume at {0:g}.`nTask: \ClaudeSleeper\{1}`n`n" +
             "It survives reboot and sleep, and runs when you're next logged in. " +
             "You can close this window.") -f $script:wakeAt, $name,
            'Claude Sleeper', 'OK', 'Information') | Out-Null
        $form.Close()
        return
    }

    # Live-countdown path.
    Set-InputsEnabled $false
    $btnStart.Text = 'Counting down...'; $btnStart.Enabled = $false
    $btnCancel.Text = 'Cancel'
    $timer.Start()
})

$btnCancel.Add_Click({
    if ($timer.Enabled) {
        $timer.Stop()
        Set-InputsEnabled $true
        $btnStart.Text = 'Start countdown'; $btnStart.Enabled = $true
        $btnCancel.Text = 'Close'
        $lblStatus.ForeColor = [System.Drawing.Color]::FromArgb(200, 120, 120)
        $lblStatus.Text = 'Cancelled.'
    } else {
        $form.Close()
    }
})

[void]$form.ShowDialog()
