@echo off
setlocal
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "NODE_PATH=C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules"
set "NODE_EXECUTABLE=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
cd /d "%~dp0"
echo.
echo   运动世界校园 网页控制台启动中...
echo   浏览器访问: http://127.0.0.1:9999（端口见 config.ini 的 [webui] port）
echo   关闭本窗口即停止服务
echo.
%PY% webui.py
endlocal
