<#
  One-time setup for the Focus Device project on Windows. Safe to re-run.

    .\scripts\setup.ps1               install PlatformIO + esptool + pyserial, download the ESP32 toolchain
                                      and the Bluetooth library, add a desktop shortcut, check USB
    .\scripts\setup.ps1 -UsbOnly      only check that Windows sees the board and has a driver for it
    .\scripts\setup.ps1 -NoShortcut   skip the desktop shortcut
#>
param([switch]$UsbOnly, [switch]$NoShortcut)
$ErrorActionPreference = 'Stop'
$penv = Join-Path $env:USERPROFILE '.platformio\penv'
$scripts = Join-Path $penv 'Scripts'
$project = Split-Path -Parent $PSScriptRoot

function Test-Usb {
  Write-Host "`n== USB check ==" -ForegroundColor Cyan
  $devs = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -match 'VID_(10C4|1A86|0403|303A)' })
  if (-not $devs) {
    Write-Host "No ESP32 plugged in (that's fine for now)."
    Write-Host "  When you plug it in, use a USB cable that carries DATA. Many cables only charge."
    return
  }
  $links = @{
    '10C4'      = 'https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers  (CP210x)'
    '1A86:7523' = 'https://www.wch-ic.com/downloads/CH341SER_EXE.html  (CH340)'
    '1A86:55D4' = 'https://www.wch-ic.com/downloads/CH343SER_EXE.html  (CH9102)'
    '0403'      = 'https://ftdichip.com/drivers/vcp-drivers/  (FTDI)'
  }
  foreach ($d in $devs) {
    $vid = [regex]::Match($d.DeviceID, 'VID_([0-9A-F]{4})').Groups[1].Value
    $pid_ = [regex]::Match($d.DeviceID, 'PID_([0-9A-F]{4})').Groups[1].Value
    $com = [regex]::Match([string]$d.Name, '\((COM\d+)\)').Groups[1].Value
    if ($com -and $d.ConfigManagerErrorCode -eq 0) {
      Write-Host "OK   $($d.Name)" -ForegroundColor Green
    } elseif ($vid -ne '303A' -and $d.ConfigManagerErrorCode -ne 0) {
      Write-Host "DRIVER MISSING   $($d.Name)  [$vid`:$pid_]" -ForegroundColor Yellow
      $link = $links["$vid`:$pid_"]; if (-not $link) { $link = $links[$vid] }
      if ($link) { Write-Host "  Install the driver from: $link, then unplug and replug the board." }
    }
  }
}

if ($UsbOnly) { Test-Usb; exit 0 }

Write-Host "== Python tools (PlatformIO, esptool, pyserial) ==" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $scripts 'python.exe'))) {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py -or $py.Source -like '*WindowsApps*') { $py = Get-Command py -ErrorAction SilentlyContinue }
  if (-not $py) { throw "Python 3 not found. Install it from https://www.python.org (tick 'Add python.exe to PATH'), then run this again." }
  & $py.Source -m venv $penv
}
# Installs what's missing; doesn't upgrade a PlatformIO that already works.
& (Join-Path $scripts 'python.exe') -m pip install --quiet --disable-pip-version-check platformio esptool pyserial
& (Join-Path $scripts 'python.exe') -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) { throw "This Python has no tkinter (the app's window toolkit). Install Python from python.org instead of the Microsoft Store." }
& (Join-Path $scripts 'pio.exe') --version

Write-Host "`n== Downloading the ESP32 toolchain and Bluetooth library (first time: a few minutes) ==" -ForegroundColor Cyan
Push-Location $project
try {
  $env:PLATFORMIO_DISABLE_PROGRESSBAR = 'true'
  & (Join-Path $scripts 'pio.exe') pkg install
} finally { Pop-Location }

if (-not $NoShortcut) {
  $desktop = [Environment]::GetFolderPath('Desktop')
  $lnk = Join-Path $desktop 'Focus Device.lnk'
  $sh = New-Object -ComObject WScript.Shell
  $s = $sh.CreateShortcut($lnk)
  $s.TargetPath = Join-Path $scripts 'pythonw.exe'
  $s.Arguments = '"' + (Join-Path $project 'app\focus_app.py') + '"'
  $s.WorkingDirectory = $project
  $s.Description = 'Focus Device: how close is your phone?'
  $s.IconLocation = "$env:SystemRoot\System32\shell32.dll,15"
  $s.Save()
  Write-Host "`nAdded a 'Focus Device' shortcut to your desktop."
}

Test-Usb
Write-Host "`nDone. Open the app with the desktop shortcut or 'Focus Device.cmd'." -ForegroundColor Cyan
