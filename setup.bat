@echo off
rem ---------------------------------------------------------------------------
rem  UI Report Tool - one-time setup.
rem  Creates a private virtual environment in the ".venv" folder next to this file and installs
rem  the libraries from requirements.txt into it. Nothing is installed system-wide and the
rem  global Python is not modified.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
echo.
echo  === UI Report Tool - setup ===
echo.

rem --- 1. find Python 3.11+ ----------------------------------------------------
set "PYCMD="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto :havepython
python --version >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD goto :nopython

:havepython
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto :oldpython
echo  Using:
%PYCMD% --version
echo.

rem --- 2. virtual environment (only in this folder) ----------------------------
if exist "%~dp0.venv\Scripts\python.exe" goto :haveenv
echo  Creating the virtual environment in the .venv folder ...
%PYCMD% -m venv "%~dp0.venv"
if errorlevel 1 goto :failed
goto :installdeps

:haveenv
echo  The virtual environment already exists - updating it.

rem --- 3. libraries (into the venv only) ---------------------------------------
:installdeps
echo.
echo  Upgrading pip inside the virtual environment ...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
echo.
echo  Installing the required libraries (this can take a few minutes the first time) ...
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :failed

echo.
echo  =====================================================================
echo   Setup finished.
echo.
echo   Next steps:
echo     1. Double-click  UIReportTool.bat  to start the tool.
echo     2. Look for its icon in the system tray (bottom right, maybe under
echo        the ^^ arrow). Press Ctrl+Alt+S to capture a screenshot.
echo     3. Optional: turn on "Start with Windows" in the tray menu - Settings.
echo   =====================================================================
echo.
pause
exit /b 0

:nopython
echo  Python 3 was not found on this computer.
echo.
echo  Install Python 3.11 or newer, then run this file again:
echo    - download it from https://www.python.org/downloads/  and tick
echo      "Add python.exe to PATH" on the first installer page, or
echo    - open a terminal and run:  winget install Python.Python.3.12
echo.
pause
exit /b 1

:oldpython
echo  Your Python is too old. UI Report Tool needs Python 3.11 or newer.
echo  Install a newer one (https://www.python.org/downloads/ or
echo  "winget install Python.Python.3.12") and run this file again.
echo.
pause
exit /b 1

:failed
echo.
echo  Setup did not finish - see the messages above. If pip could not download the
echo  libraries, check your internet connection and run setup.bat again.
echo.
pause
exit /b 1
