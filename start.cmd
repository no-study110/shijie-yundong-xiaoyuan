@echo off
setlocal
rem ==== Environment for com.zjwh protocol suite (7.3.92) ====
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "NODE_PATH=C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules"
set "NODE_EXECUTABLE=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
cd /d "%~dp0"

echo [zjwh] Python : %PY%
echo [zjwh] Node   : %NODE_EXECUTABLE%
echo.

%PY% cli.py %*
endlocal
