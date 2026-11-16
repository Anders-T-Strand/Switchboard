@echo off
rem Starts Switchboard in its own window. Double-click it, or run .\switchboard in this folder.
rem With arguments it runs that command in this terminal instead, and opens the dashboard in your
rem browser if the command is the server: .\switchboard serve, .\switchboard ask "a question".
if "%~1"=="" goto app
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" --open %*
goto done
:app
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" app
:done
if errorlevel 1 pause
