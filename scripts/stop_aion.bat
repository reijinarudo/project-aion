@echo off
rem Stops Aion and leaves VMagicMirror running. You can also end a session by saying
rem "Thank you Aion, that is all for now."
taskkill /im Aurora.exe /f >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*scriptspy*Aurora.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Aion stopped.
