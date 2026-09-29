@echo off
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python is not installed. Get it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during install.
  pause
  exit /b
)
python -c "import requests, PIL, numpy" 2>nul || python -m pip install -r requirements.txt
python catalog.py
pause
