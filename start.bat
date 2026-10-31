@echo off
setlocal
pushd "%~dp0"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  python start_local.py
) else (
  py -3.12 start_local.py
)
if errorlevel 1 (
  echo.
  echo Startup failed. Install Python 3.12 and enable the Python launcher.
  echo Keep the error above when asking for help.
  pause
)
popd
