<#
  One-time ESP32 toolchain setup for Windows. Safe to re-run.

    .\scripts\setup.ps1              install PlatformIO + esptool, pre-download the ESP32 toolchain, check USB
    .\scripts\setup.ps1 -UsbOnly     just check that Windows sees the board and has a driver for it
    .\scripts\setup.ps1 -AddToPath   also put `pio` on your user PATH (for terminals outside Cursor)
#>
param([switch]$UsbOnly, [switch]$AddToPath)
$ErrorActionPreference = 'Stop'
$penv = Join-Path $env:USERPROFILE '.platformio\penv'
$scripts = Join-Path $penv 'Scripts'
$project = Split-Path -Parent $PSScriptRoot

function Test-Usb {
  Write-Host "`n== USB check ==" -ForegroundColor Cyan
  $devs = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -match 'VID_(10C4|1A86|0403|303A)' })
  if (-not $devs) {
    Write-Host "No ESP32 or USB-serial chip plugged in."
    Write-Host "  Plug the board in with a DATA cable (charge-only cables are common), then run: .\scripts\setup.ps1 -UsbOnly"
    return
  }
  $links = @{
    '10C4' = 'https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers  (CP210x)'
    '1A86:7523' = 'https://www.wch-ic.com/downloads/CH341SER_EXE.html  (CH340)'
    '1A86:55D4' = 'https://www.wch-ic.com/downloads/CH343SER_EXE.html  (CH9102)'
    '0403' = 'https://ftdichip.com/drivers/vcp-drivers/  (FTDI)'
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

Write-Host "== PlatformIO ==" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $scripts 'python.exe'))) {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) { throw "Python 3 not found. Install it from python.org (tick 'Add python.exe to PATH') and re-run." }
  & $py.Source -m venv $penv
}
& (Join-Path $scripts 'python.exe') -m pip install --quiet --upgrade pip platformio esptool
& (Join-Path $scripts 'pio.exe') --version

Write-Host "`n== Downloading the ESP32 toolchain (first time takes a few minutes) ==" -ForegroundColor Cyan
Push-Location $project
try { & (Join-Path $scripts 'pio.exe') pkg install } finally { Pop-Location }

if ($AddToPath) {
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (($userPath -split ';') -notcontains $scripts) {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$scripts", 'User')
    Write-Host "`nAdded $scripts to your user PATH. Open a new terminal to use 'pio'."
  }
}

Test-Usb
Write-Host "`nDone. Open the project in Cursor:  cursor `"$project`"" -ForegroundColor Cyan
