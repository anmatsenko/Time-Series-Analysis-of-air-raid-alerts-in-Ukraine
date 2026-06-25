@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo    Ukraine Air Raid Alerts - Dashboard
echo ============================================
echo.

REM --- OPTIONAL: paste your alerts.in.ua token between the quotes to go live ---
REM --- Leave it empty to run in demo mode.                                  ---
set "ALERTS_IN_UA_TOKEN="

REM --- Find a Python launcher (py preferred, then python) ---
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo ERROR: Python was not found on this computer.
  echo.
  echo Install it from https://www.python.org/downloads/
  echo IMPORTANT: during setup, tick "Add Python to PATH".
  echo Then double-click this file again.
  echo.
  pause
  exit /b 1
)
echo Using Python launcher: %PY%
echo.

REM --- Make sure the app file is here ---
set "APP=air_alerts_live_dashboard.py"
if not exist "%APP%" (
  echo ERROR: Could not find %APP% in this folder:
  echo    %cd%
  echo.
  echo Put this .bat file in the SAME folder as %APP%, then try again.
  echo.
  pause
  exit /b 1
)

REM --- Install / update the required libraries (safe to run every time) ---
echo Checking and installing required libraries. This may take a minute the first time...
%PY% -m pip install --quiet --upgrade streamlit pandas plotly requests
if errorlevel 1 (
  echo.
  echo ERROR: Could not install the libraries.
  echo Check your internet connection and read the message above.
  echo.
  pause
  exit /b 1
)
echo Done.
echo.

echo ------------------------------------------------------------
echo Starting the dashboard...
echo A browser tab should open at http://localhost:8501
echo.
echo KEEP THIS WINDOW OPEN while you use the dashboard.
echo To stop it: click this window and press Ctrl+C, or close it.
echo ------------------------------------------------------------
echo.

%PY% -m streamlit run "%APP%"

echo.
echo The dashboard has stopped.
pause
