@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
set "launcher=python"
if exist ".venv\Scripts\python.exe" set "launcher=.venv\Scripts\python.exe"
if exist "runtime\python.exe" set "launcher=runtime\python.exe"
"%launcher%" -B -X utf8 "monitor.py" --version
if errorlevel 1 goto missing
"%launcher%" -B -X utf8 "app\export_package.py" %*
set "result=%errorlevel%"
popd
pause
exit /b %result%
:missing
echo 请解压完整的 Windows x64 便携包，或按 README 安装 Python 3.10+ 和依赖。
popd
pause
exit /b 1

