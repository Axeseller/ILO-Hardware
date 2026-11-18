@echo off
rem Double-click to open the Focus Device app. The first run installs what it needs.
rem   "Focus Device.cmd" --demo     opens straight into demo mode (no board needed)
setlocal
set "PENV=%USERPROFILE%\.platformio\penv\Scripts"
if not exist "%PENV%\pythonw.exe" (
  echo First run: installing the tools. This takes a few minutes...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
  if errorlevel 1 (
    echo.
    echo Setup failed. See the messages above.
    pause
    exit /b 1
  )
)
start "" "%PENV%\pythonw.exe" "%~dp0app\focus_app.py" %*
