@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo    Air Raid Alerts - Historical Importer
echo ============================================
echo.

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

REM --- Make sure the importer is in this folder ---
set "APP=import_archive.py"
if not exist "%APP%" (
  echo ERROR: Could not find %APP% in this folder:
  echo    %cd%
  echo.
  echo Put this .bat file in the SAME folder as %APP%, then try again.
  echo.
  pause
  exit /b 1
)

REM --- Make sure required libraries are present ---
echo Checking required libraries...
%PY% -m pip install --quiet --upgrade pandas requests
echo.

echo Importing historical data. This downloads the full 2022-today archive
echo and may take a minute. Please wait until it says it is done...
echo ------------------------------------------------------------
echo.

%PY% "%APP%"

echo.
echo ------------------------------------------------------------
echo Finished. If you see "Store now holds ... rows" above, it worked,
echo and alert_history_store.csv is now in this folder.
echo You can now run the dashboard.
echo.
pause
