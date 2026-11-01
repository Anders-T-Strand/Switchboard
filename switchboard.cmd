@echo off
rem Starts Switchboard and opens the dashboard. Double-click it, or run .\switchboard in this folder.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" --open %*
if errorlevel 1 pause
