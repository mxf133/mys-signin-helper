@echo off
chcp 65001 >nul
rem 米游社签到助手 - 创建 .venv 并安装运行依赖
rem 说明：本脚本刻意不用多行 if ( ... ) 括号块，一律用 goto 跳转。
rem       cmd 在括号块上会预读，和中文加 chcp 65001 组合时会解析错位。
cd /d "%~dp0"
echo.
echo  === 安装 / 重建运行环境 (.venv) ===
echo.

rem ---- pip 镜像源（国内用户默认走清华源；海外网络可把下一行改成官方源）----
set "PIP_INDEX=-i https://pypi.tuna.tsinghua.edu.cn/simple"
rem set "PIP_INDEX="

rem ---- 1) 找一个可用的 Python ----
rem 顺序：官方启动器 py -3（自动挑最新 3.x）-> 常见安装路径（新到旧）->
rem       PATH 里的 python（逐个实测，跑不起来的直接淘汰）。
set "PY="

for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%i"
if defined PY goto :PY_FOUND

if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"
if exist "C:\Python310\python.exe" set "PY=C:\Python310\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if defined PY goto :PY_FOUND

rem PATH 里的 python 一律实测后再采用 —— 微软商店那套是 WindowsApps\python.exe
rem 占位程序，where 能找到但执行只会弹商店，直接采用会打出误导性的版本过低提示。
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY call :TRY_PY "%%i"
if defined PY goto :PY_FOUND

echo  [错误] 没有找到可用的 Python。
echo         请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
echo         安装时务必勾选 "Add Python to PATH"。
echo.
pause
exit /b 1

:PY_FOUND
rem ---- 实测：能启动 + 版本 >= 3.10，两条都过才采用 ----
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto :PY_OK
echo  [错误] 探测到的 Python 无法运行，或版本低于 3.10：
"%PY%" --version
echo         如果这是微软商店版的占位程序，请改装 python.org 的正式版本。
echo         下载：https://www.python.org/downloads/
echo.
pause
exit /b 1

:PY_OK
rem ---- 2) 创建虚拟环境 ----
if exist ".venv\Scripts\python.exe" goto :VENV_EXISTS
echo  [1/3] 创建虚拟环境 ...
"%PY%" -m venv ".venv"
if errorlevel 1 goto :VENV_FAIL
if not exist ".venv\Scripts\python.exe" goto :VENV_FAIL
goto :INSTALL

:VENV_EXISTS
echo  [1/3] 虚拟环境已存在，跳过创建
goto :INSTALL

:VENV_FAIL
echo  [错误] 创建虚拟环境失败。
pause
exit /b 1

:INSTALL
rem ---- 3) 安装依赖 ----
echo  [2/3] 升级 pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip %PIP_INDEX%

echo  [3/3] 安装依赖（genshin / playwright）...
".venv\Scripts\python.exe" -m pip install -r requirements.txt %PIP_INDEX%
if not errorlevel 1 goto :DONE

echo.
echo  [错误] 依赖安装失败。请检查网络后重试，或手动执行：
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:DONE
rem ---- 收尾 ----
echo.
echo  完成。现在可以双击 "启动米游社签到助手.bat" 打开界面，
echo  或直接双击桌面上的 "米游社签到助手" 快捷方式。
echo.
pause
exit /b 0

rem ---- 子过程：验证一个候选解释器是否真的可用 ----
:TRY_PY
if defined PY goto :eof
if "%~1"=="" goto :eof
"%~1" -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof
