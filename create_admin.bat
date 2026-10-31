@echo off
setlocal
pushd "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run start.bat first.
  pause
  popd
  exit /b 1
)
.venv\Scripts\python.exe -m app.manage_admin
pause
popd
