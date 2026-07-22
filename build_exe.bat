@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_portable.ps1"
exit /b %ERRORLEVEL%
