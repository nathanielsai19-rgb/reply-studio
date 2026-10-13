@echo off
cd /d "%~dp0"
start "" /b pythonw app.py
timeout /t 3 >nul
