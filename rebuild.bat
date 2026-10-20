@echo off
rem rebuild.bat -- double-click to sync latest changes into the packaged build (dist\vixiq\) and launch it.
rem With args from a terminal, e.g.:  rebuild.bat -Fast    rebuild.bat -Release
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0rebuild.ps1" %*
if "%~1"=="" pause
