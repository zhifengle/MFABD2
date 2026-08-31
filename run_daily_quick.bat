@echo off
REM Unity Bridge 批量任务快速启动脚本
REM 使用 config.daily-quick.toml 配置执行日常任务

setlocal
pushd "%~dp0"

set "VENV_PYTHON=.venv\Scripts\python.exe"
set "SCRIPT=agent\run_batch_tasks.py"
set "CONFIG=config.daily-quick.toml"

REM 检查虚拟环境
if not exist "%VENV_PYTHON%" (
    echo [错误] 虚拟环境不存在: %VENV_PYTHON%
    echo 请先运行 agent\main.py 初始化环境
    pause
    exit /b 1
)

REM 检查配置文件
if not exist "%CONFIG%" (
    echo [错误] 配置文件不存在: %CONFIG%
    pause
    exit /b 1
)

echo ========================================
echo Unity Bridge 批量任务执行器
echo ========================================
echo 配置文件: %CONFIG%
echo.

REM 执行批量任务
"%VENV_PYTHON%" "%SCRIPT%" --config "%CONFIG%"

set "EXIT_CODE=%ERRORLEVEL%"

echo.
if %EXIT_CODE% equ 0 (
    echo [完成] 所有任务执行成功
) else (
    echo [失败] 部分任务执行失败，退出码: %EXIT_CODE%
)

pause
popd
exit /b %EXIT_CODE%
