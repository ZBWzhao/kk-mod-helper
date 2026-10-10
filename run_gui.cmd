@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 正在创建独立 Python 环境……
    python -m venv .venv || goto :error
)

".venv\Scripts\python.exe" -c "import msgpack" >nul 2>nul
if errorlevel 1 (
    echo 正在安装所需组件……
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

".venv\Scripts\python.exe" -m kk_mod_helper.gui 2>>launcher-error.log
if errorlevel 1 goto :error
exit /b 0

:error
echo 启动失败，请查看上方错误信息。
echo 错误日志：%~dp0launcher-error.log
pause
exit /b 1
