@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
if errorlevel 1 (
    echo.
    echo The Windows build failed. Read the error above.
    pause
    exit /b 1
)

echo.
echo Finished. The executable is in dist\RemoteControlDesk.exe
pause