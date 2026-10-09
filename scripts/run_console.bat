@echo off
rem Runs Aion from source in a console window so you can read the device list and transcript.
cd /d "%~dp0.."
"myenv\Scripts\python.exe" "scriptspy\Aurora.py"
pause
