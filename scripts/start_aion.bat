@echo off
rem Starts VMagicMirror (if VMAGICMIRROR_EXE is set in .env and it is not already running),
rem waits for it to load the avatar, then starts Aion.
setlocal
cd /d "%~dp0.."

if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="VMAGICMIRROR_EXE" set "VMM=%%B"
)

if defined VMM (
    tasklist /fi "imagename eq VMagicMirror.exe" | find /i "VMagicMirror.exe" >nul
    if errorlevel 1 (
        start "" "%VMM%"
        timeout /t 8 /nobreak >nul
    )
)

if exist "dist\Aurora.exe" (
    start "" "dist\Aurora.exe"
) else if exist "myenv\Scripts\pythonw.exe" (
    start "" "myenv\Scripts\pythonw.exe" "scriptspy\Aurora.py"
) else (
    echo Could not find dist\Aurora.exe or myenv\Scripts\pythonw.exe. See README, Installation.
    pause
)
endlocal
