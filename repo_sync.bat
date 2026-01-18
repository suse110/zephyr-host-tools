@echo off
setlocal enabledelayedexpansion

echo Git裸仓库批量更新工具
echo ========================================
echo.

set count=0
set success_count=0
set fail_count=0

REM 检查是否有命令行参数
if "%~1"=="" (
    echo 未指定仓库，将遍历当前目录下的所有.git裸仓库
    goto :search_current
)

echo 正在更新指定的裸仓库...
echo.

:process_args
REM 处理所有命令行参数作为裸仓库路径
for %%a in (%*) do (
    set /a count+=1
    echo [仓库!count!]: %%~a
    call :update_repository "%%~a"
)

goto :summary

:search_current
echo 正在搜索当前目录下的.git裸仓库...
echo.

for /d %%d in (*.git) do (
    set /a count+=1
    echo [仓库!count!]: %%d
    call :update_repository "%%d"
)

goto :summary

:update_repository
set "repo_path=%~1"

REM 验证是否为.git结尾的目录
if /i not "!repo_path:~-4!"==".git" (
    echo   警告: 路径不是以.git结尾 [!repo_path!]
    echo   跳过此仓库...
    echo.
    goto :eof
)

REM 检查目录是否存在
if not exist "!repo_path!\" (
    echo   错误: 目录不存在 [!repo_path!]
    echo.
    set /a fail_count+=1
    goto :eof
)

REM 检查是否是目录（不是文件）
if exist "!repo_path!\*" (
    rem 是目录，继续执行
) else (
    echo   错误: 不是有效的目录 [!repo_path!]
    echo.
    set /a fail_count+=1
    goto :eof
)

REM 进入仓库目录并执行更新
pushd "!repo_path!" 2>nul
if errorlevel 1 (
    echo   错误: 无法进入目录 [!repo_path!]
    echo.
    set /a fail_count+=1
    goto :eof
)

echo   执行: git remote update
git remote update 2>nul

if !errorlevel! equ 0 (
    echo   状态: ^✓ 更新成功
    set /a success_count+=1
) else (
    echo   状态: ^✗ 更新失败
    set /a fail_count+=1
)

popd
echo.
goto :eof

:summary
echo ========================================
echo 执行完成
echo 总共发现仓库: %count%
echo 成功更新仓库: %success_count%
echo 失败更新仓库: %fail_count%

if %count% equ 0 (
    echo.
    echo 警告: 没有找到任何Git裸仓库！
    echo 裸仓库的目录名必须以.git结尾
)

echo.
pause