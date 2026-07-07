@echo off
rem PrivaVox installer wrapper - double-click me.
rem Runs Install-PrivaVox.ps1 with ExecutionPolicy Bypass (this process only);
rem a bare .ps1 double-click would open Notepad instead of running.
title PrivaVox
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-PrivaVox.ps1"
echo.
pause
