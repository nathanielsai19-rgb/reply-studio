@echo off
cd /d "%~dp0"
start "" python app.py
timeout /t 3 >nul
