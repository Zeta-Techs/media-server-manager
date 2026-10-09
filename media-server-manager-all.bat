@echo off
setlocal
set "DIR=%~dp0"
cd /d "%DIR%"
start "Media Server Manager Web" /min python -m media_server_manager_web
start "Media Server Manager Worker" /min python -m media_server_manager_worker
echo Media Server Manager 已启动： http://127.0.0.1:8088
echo 关闭窗口不会停止后台进程，请使用任务管理器或 Ctrl+C 停止对应进程。
endlocal
