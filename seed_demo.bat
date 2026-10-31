@echo off
setlocal
pushd "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run start.bat once to install dependencies, then stop it and try again.
  pause
  popd
  exit /b 1
)
.venv\Scripts\python.exe -m scripts.seed_demo
if errorlevel 1 echo Import failed. Keep the error above when asking for help.
pause
popd
