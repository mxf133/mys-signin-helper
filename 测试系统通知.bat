@echo off
chcp 65001 >nul
rem  Miyoushe Sign-in Assistant - test the Windows notification
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\python.exe" goto NOPY

echo.
echo  Sending a test notification, please look at the bottom-right corner...
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0cli.py" doctor --notify
echo.
pause
exit /b 0

:NOPY
echo.
echo  [ERROR] Python environment (.venv) not found.
echo  Please run "°²×°ÒÀÀµ.bat" in this folder first.
echo.
pause
exit /b 1
