@echo off
rem ---------------------------------------------------------------------------
rem  UI Report Tool - double-click launcher (no console window stays open).
rem  Starts the tray app with the virtual environment created by setup.bat.
rem  Extra arguments are passed on, e.g.  UIReportTool.bat --no-hotkeys
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" goto :novenv
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launch.pyw" %*
exit /b 0

:novenv
rem No .venv yet: use a system Python only if it already has every dependency installed.
where pyw >nul 2>nul
if errorlevel 1 goto :missing
py -3 -c "import PySide6, win32api, psutil, uiautomation" >nul 2>nul
if errorlevel 1 goto :missing
start "" pyw -3 "%~dp0launch.pyw" %*
exit /b 0

:missing
echo.
echo  UI Report Tool is not set up yet: the ".venv" folder was not found.
echo.
echo  Please double-click  setup.bat  in this folder first. It needs Python 3.11 or newer
echo  and an internet connection once, to download the libraries. Then start this file again.
echo.
pause
exit /b 1
