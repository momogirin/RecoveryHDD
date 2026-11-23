@echo off
setlocal
rem Double-click: Windows admin prompt (UAC) -> the read-only recovery tool opens in the browser.
rem This file is ASCII only on purpose: cmd.exe mis-parses multibyte (Korean) lines in .bat files.
rem All Korean messages are printed by the Python program itself.
rem For development/tests: set RT_NO_ELEVATE=1 to skip the admin prompt (disks cannot be read; --image demos only).

set "ROOT=%~dp0..\.."

if not "%RT_NO_ELEVATE%"=="1" (
  net session >nul 2>&1
  if errorlevel 1 (
    echo Administrator permission is required. Click Yes in the Windows prompt.
    if "%~1"=="" (
      powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
      powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
  )
)

where py >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3 is required. Install it from python.org and run this again.
    pause
    exit /b 1
  )
  set "PY=python"
)

cd /d "%ROOT%"
%PY% -B -m apfsrecover gui %*
if errorlevel 1 pause
