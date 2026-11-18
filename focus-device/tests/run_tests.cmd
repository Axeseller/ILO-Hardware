@echo off
rem Runs every test that doesn't need hardware:
rem   - C++ unit tests of the firmware's logic (firmware/proximity.h, firmware/rpa.h), compiled for the PC
rem   - the PC app and protocol tests against the simulator (opens the app window for ~25 s)
rem   - a compile of the real firmware for all three chips
setlocal
cd /d "%~dp0.."
set "TOOLS=%USERPROFILE%\.focus-tools\venv"
set "PENV=%USERPROFILE%\.platformio\penv\Scripts"
set "OUT=%TEMP%\focus-tests"
if not exist "%OUT%" mkdir "%OUT%"

if not exist "%TOOLS%\Scripts\python.exe" (
  echo Installing a C++ compiler for the PC tests ^(zig, via pip^)...
  python -m venv "%TOOLS%" || goto :fail
  "%TOOLS%\Scripts\python.exe" -m pip install --quiet ziglang || goto :fail
)

echo == C++ logic tests
for %%t in (test_proximity test_rpa) do (
  "%TOOLS%\Scripts\python.exe" -m ziglang c++ -std=c++17 -Wall -Wextra -O1 tests\%%t.cpp -o "%OUT%\%%t.exe" || goto :fail
  "%OUT%\%%t.exe" || goto :fail
)

echo == PC app and protocol tests
"%PENV%\python.exe" -m unittest discover -s tests -p "test_*.py" || goto :fail

echo == Firmware build (esp32, esp32-s3, esp32-c3)
set PLATFORMIO_DISABLE_PROGRESSBAR=true
"%PENV%\pio.exe" run -e esp32 -e esp32-s3 -e esp32-c3 || goto :fail

echo.
echo ALL TESTS PASSED
exit /b 0

:fail
echo.
echo TESTS FAILED
exit /b 1
