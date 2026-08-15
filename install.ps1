<#
    Installs Claude Sleeper for the current user:
      - copies the scripts to %LOCALAPPDATA%\ClaudeSleeper
      - generates a clock icon
      - writes config.json (default project folder = where this installer ran)
      - creates a Start Menu shortcut

    No admin rights needed. Re-run any time to update. Pass -Uninstall to remove.
#>
[CmdletBinding()]
param(
    [string] $DefaultCwd = (Get-Location).Path,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$AppName    = 'Claude Sleeper'
$InstallDir = Join-Path $env:LOCALAPPDATA 'ClaudeSleeper'
$StartMenu  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$Shortcut   = Join-Path $StartMenu "$AppName.lnk"
$SrcDir     = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }

if ($Uninstall) {
    if (Test-Path $Shortcut)   { Remove-Item $Shortcut -Force }
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
    # Remove any pending scheduled wake tasks.
    Get-ScheduledTask -TaskPath '\ClaudeSleeper\' -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed $AppName (files, shortcut, and scheduled tasks)." -ForegroundColor Green
    return
}

# --- Draw a clock icon (single 256x256 PNG frame wrapped in an .ico) ------
function New-ClockIcon($path) {
    Add-Type -AssemblyName System.Drawing
    $sz = 256
    $bmp = New-Object System.Drawing.Bitmap($sz, $sz)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.Clear([System.Drawing.Color]::Transparent)

    # Rounded background
    $bg = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(26, 27, 38))
    $path2 = New-Object System.Drawing.Drawing2D.GraphicsPath
    $r = 48; $d = $r * 2
    $path2.AddArc(0, 0, $d, $d, 180, 90)
    $path2.AddArc($sz - $d, 0, $d, $d, 270, 90)
    $path2.AddArc($sz - $d, $sz - $d, $d, $d, 0, 90)
    $path2.AddArc(0, $sz - $d, $d, $d, 90, 90)
    $path2.CloseFigure()
    $g.FillPath($bg, $path2)

    # Clock face
    $accent = [System.Drawing.Color]::FromArgb(122, 162, 247)
    $facePen = New-Object System.Drawing.Pen($accent, 12)
    $cx = 128; $cy = 132; $rad = 74
    $g.DrawEllipse($facePen, $cx - $rad, $cy - $rad, $rad * 2, $rad * 2)

    # Hands (pointing ~10:10)
    $handPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(220, 223, 233), 10)
    $handPen.StartCap = 'Round'; $handPen.EndCap = 'Round'
    $g.DrawLine($handPen, $cx, $cy, ($cx - 34), ($cy - 18))   # hour
    $g.DrawLine($handPen, $cx, $cy, ($cx + 16), ($cy - 48))   # minute
    $dot = New-Object System.Drawing.SolidBrush($accent)
    $g.FillEllipse($dot, $cx - 8, $cy - 8, 16, 16)

    # "z" sleep marks
    $fontZ = New-Object System.Drawing.Font('Segoe UI', 30, [System.Drawing.FontStyle]::Bold)
    $zBrush = New-Object System.Drawing.SolidBrush($accent)
    $g.DrawString('z', $fontZ, $zBrush, 196, 30)
    $fontZ2 = New-Object System.Drawing.Font('Segoe UI', 20, [System.Drawing.FontStyle]::Bold)
    $g.DrawString('z', $fontZ2, $zBrush, 176, 20)

    $g.Dispose()

    # PNG -> ICO container
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $png = $ms.ToArray(); $ms.Dispose(); $bmp.Dispose()

    $fs = [System.IO.File]::Create($path)
    $bw = New-Object System.IO.BinaryWriter($fs)
    $bw.Write([uint16]0)              # reserved
    $bw.Write([uint16]1)              # type = icon
    $bw.Write([uint16]1)              # image count
    $bw.Write([byte]0)                # width  (0 = 256)
    $bw.Write([byte]0)                # height (0 = 256)
    $bw.Write([byte]0)                # palette
    $bw.Write([byte]0)                # reserved
    $bw.Write([uint16]1)             # planes
    $bw.Write([uint16]32)            # bpp
    $bw.Write([uint32]$png.Length)   # size
    $bw.Write([uint32]22)            # offset (6 + 16)
    $bw.Write($png)
    $bw.Flush(); $bw.Dispose(); $fs.Dispose()
}

# --- Copy files ----------------------------------------------------------
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
foreach ($f in @('sleeper.ps1', 'sleeper_gui.ps1')) {
    Copy-Item (Join-Path $SrcDir $f) (Join-Path $InstallDir $f) -Force
}

$icoPath = Join-Path $InstallDir 'clock.ico'
New-ClockIcon $icoPath

# config.json — default project folder the GUI opens with
@{ DefaultCwd = $DefaultCwd } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDir 'config.json') -Encoding UTF8

# --- Start Menu shortcut -------------------------------------------------
# Prefer pwsh (PS7) but launch the GUI hidden; fall back to Windows PowerShell.
$launcher = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $launcher) { $launcher = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe' }

$guiPath = Join-Path $InstallDir 'sleeper_gui.ps1'
$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($Shortcut)
$lnk.TargetPath       = $launcher
$lnk.Arguments        = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$guiPath`""
$lnk.WorkingDirectory = $InstallDir
$lnk.IconLocation     = $icoPath
$lnk.Description       = 'Schedule Claude Code to resume after a delay'
$lnk.Save()

Write-Host ""
Write-Host "Installed $AppName" -ForegroundColor Green
Write-Host "  Files:      $InstallDir"
Write-Host "  Start Menu: $Shortcut"
Write-Host "  Default folder: $DefaultCwd"
Write-Host ""
Write-Host "Find it by pressing Start and typing 'Claude Sleeper'." -ForegroundColor Cyan
