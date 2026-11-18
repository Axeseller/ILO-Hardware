@echo off
rem Control the board:  esp led on  |  esp  (interactive)  |  esp --detect
"%USERPROFILE%\.platformio\penv\Scripts\python.exe" "%~dp0tools\esp.py" %*
