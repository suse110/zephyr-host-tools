@echo off
chcp 65001 > nul 2>&1  :: 解决中文乱码问题
echo "zephyr 3.7.0 Tool Min. Version"
echo "  CMake  3.20.5"
echo "  Python 3.10"
echo "  DTC    1.4.6"

set CUR_DIR=%~dp0
:: 配置OpenOCD路径（注意替换为你的实际路径，反斜杠可保留或用正斜杠）
set OPENOCD_ROOT=%CUR_DIR%\openocd\OpenOCD-20210519-0.11.0
:: 配置QEMU路径
set QEMU_ROOT=%CUR_DIR%\host-tools\qemu\win\w64-202107062
set QEMU_BIN_PATH=%CUR_DIR%\host-tools\qemu\win\w64-202107062
set NINJA_PATH=%CUR_DIR%\host-tools\ninja
set DTC_PATH=%CUR_DIR%\host-tools\dtc-msys2\bin
set CMAKE_PATH=%CUR_DIR%\host-tools\CMake\bin
set PATH=%OPENOCD_ROOT%;%QEMU_ROOT%;%NINJA_PATH%;%DTC_PATH%;%CMAKE_PATH%;%CUR_DIR%\host-tools;%PATH%

if exist ".\.venv\" (
	@.\.venv\Scripts\activate.bat
) else (
	@python -m venv .venv
	@.\.venv\Scripts\activate.bat
)


