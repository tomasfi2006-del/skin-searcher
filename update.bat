@echo off
cd /d "%~dp0"
title Skin Searcher - update

rem Everything below runs inside one block so Windows has read it all before update.bat
rem can overwrite itself with a newer copy.
set "PS=$ErrorActionPreference='Stop'; $tmp=Join-Path $env:TEMP 'skin-searcher-update'; if(Test-Path $tmp){Remove-Item $tmp -Recurse -Force}; New-Item -ItemType Directory $tmp | Out-Null; $zip=Join-Path $tmp 'main.zip'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest 'https://codeload.github.com/tomasfi2006-del/skin-searcher/zip/refs/heads/main' -OutFile $zip -UseBasicParsing; Expand-Archive $zip -DestinationPath $tmp -Force; Copy-Item (Join-Path $tmp 'skin-searcher-main\*') -Destination '.' -Recurse -Force; Remove-Item $tmp -Recurse -Force"

(
  echo Updating Skin Searcher...
  echo Your Gemini key, settings and saved avatars are kept.
  echo.
  set "DONE="

  if exist ".git" (
    where git >nul 2>nul && (
      git pull --ff-only && set "DONE=1"
    )
  )

  if not defined DONE (
    where powershell >nul 2>nul || (
      echo PowerShell was not found on this computer, so the update cannot download.
      echo Download the ZIP from github.com/tomasfi2006-del/skin-searcher instead.
      pause
      exit /b 1
    )
    powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS%" && set "DONE=1"
  )

  if not defined DONE (
    echo.
    echo Update failed. Check your internet connection and try again.
    pause
    exit /b 1
  )

  where python >nul 2>nul && python -m pip install -q -r requirements.txt
  echo.
  echo Done - you now have the latest version.
  echo Double-click start.bat to open it.
  pause
  exit /b 0
)
