@echo off
rem Double-click to sign the current worker release. See sign_worker.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign_worker.ps1" %*
echo.
pause
