@echo off
rem Double-click to install: runs install.ps1 without the PowerShell execution-policy prompt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
