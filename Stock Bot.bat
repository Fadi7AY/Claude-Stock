@echo off
rem Double-click to start the Halal Stock Bot app. Keep this window open while the bot runs.
title Halal Stock Bot
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app.py
  goto end
)
where wsl >nul 2>nul
if %errorlevel%==0 (
  wsl python3 app.py
  goto end
)
python app.py
:end
if errorlevel 1 pause
