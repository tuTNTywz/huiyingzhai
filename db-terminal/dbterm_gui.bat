@echo off
chcp 65001 >nul
title 汇英斋数据库管理
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 goto no_pythonw
start "" pythonw "%~dp0dbterm_gui.py" %*
exit /b 0

:no_pythonw
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python 3。
  echo 请安装 Python 3.8+，安装时勾选 "Add python.exe to PATH"。
  echo 下载：https://www.python.org/downloads/
  pause
  exit /b 1
)
python "%~dp0dbterm_gui.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
