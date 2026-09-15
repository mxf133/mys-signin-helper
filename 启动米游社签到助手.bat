@echo off
rem 米游社签到助手 - 启动图形界面（不弹命令行窗口）
cd /d "%~dp0"
chcp 65001 >nul

if not exist "%~dp0.venv\Scripts\pythonw.exe" goto NOPY

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.pyw"
exit /b 0

:NOPY
echo.
echo  [错误] 没有找到 Python 运行环境（.venv）。
echo  请先双击本目录下的 "安装依赖.bat" 完成环境安装，再运行本程序。
echo.
pause
exit /b 1
