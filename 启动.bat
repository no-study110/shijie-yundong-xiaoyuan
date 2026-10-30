@echo off
chcp 65001 >nul
title 运动世界校园 7.3.92 - 快速启动

echo.
echo ========================================
echo 运动世界校园 7.3.92 协议套件
echo ========================================
echo.
echo 请选择运行方式:
echo.
echo 1. 命令行版本 (CLI)
echo 2. 网页控制台 (Web UI)
echo 3. 退出
echo.

set /p choice="请输入选项 (1/2/3): "

if "%choice%"=="1" (
    echo.
    echo 启动命令行版本...
    echo.
    sport_cli.exe --help
    echo.
    echo 示例命令:
    echo   登录: sport_cli.exe -u 手机号 -p 密码
    echo   跑步: sport_cli.exe --run --distance 2150 --time-sec 760
    echo.
    pause
) else if "%choice%"=="2" (
    echo.
    echo 启动网页控制台...
    echo 浏览器将自动打开 http://127.0.0.1:9999（端口见 config.ini 的 [webui] port）
    echo.
    sport_webui.exe
) else if "%choice%"=="3" (
    exit
) else (
    echo.
    echo 无效选项，请重新运行
    pause
)
