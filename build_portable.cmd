@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 请先运行 run_gui.cmd 创建 Python 环境。
    pause
    exit /b 1
)

echo 正在安装打包组件……
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt || goto :error

echo 正在生成便携版 EXE……
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean KK-Mod-Helper.spec || goto :error

echo.
echo 打包完成：%~dp0dist\KK-Mod-Helper.exe
pause
exit /b 0

:error
echo 打包失败。
pause
exit /b 1
