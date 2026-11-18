@echo off
rem PlatformIO without touching PATH:  pio run -t upload  |  pio device monitor
"%USERPROFILE%\.platformio\penv\Scripts\pio.exe" %*
