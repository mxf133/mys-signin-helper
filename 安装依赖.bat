@echo off
rem 米游社签到助手 - 创建 .venv 并安装运行依赖
cd /d "%~dp0"
chcp 65001 >nul
echo.
echo  === 安装 / 重建运行环境 (.venv) ===
echo.

rem ---- pip 镜像源（国内用户默认走清华源；海外网络可把下一行改成官方源）----
set "PIP_INDEX=-i https://pypi.tuna.tsinghua.edu.cn/simple"
rem set "PIP_INDEX="

rem ---- 1) 找一个可用的 Python ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"
if not defined PY if exist "C:\Python310\python.exe" set "PY=C:\Python310\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if not defined PY (
  echo  [错误] 没有找到 Python。
  echo         请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
  echo         安装时务必勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo  [错误] Python 版本过低，需要 3.10 或更高。当前：
  "%PY%" --version
  echo.
  pause
  exit /b 1
)

rem ---- 2) 创建虚拟环境 ----
if not exist ".venv\Scripts\python.exe" (
  echo  [1/3] 创建虚拟环境 ...
  "%PY%" -m venv ".venv"
  if errorlevel 1 (
    echo  [错误] 创建虚拟环境失败。
    pause
    exit /b 1
  )
) else (
  echo  [1/3] 虚拟环境已存在，跳过创建
)

rem ---- 3) 安装依赖 ----
echo  [2/3] 升级 pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip %PIP_INDEX%

echo  [3/3] 安装依赖（genshin / playwright）...
".venv\Scripts\python.exe" -m pip install -r requirements.txt %PIP_INDEX%
if errorlevel 1 (
  echo.
  echo  [错误] 依赖安装失败。请检查网络后重试，或手动执行：
  echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo.
echo  完成。现在可以双击 "启动米游社签到助手.bat" 打开界面，
echo  或直接双击桌面上的 "米游社签到助手" 快捷方式。
echo.
pause
