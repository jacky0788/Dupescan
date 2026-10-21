@echo off
chcp 65001 >nul
:: 切換到此腳本所在目錄，確保雙擊啟動時路徑正確
cd /d "%~dp0"
title DupeScan

echo ========================================
echo   DupeScan - 重複檔案掃描工具
echo ========================================
echo.

:: 用法：run.bat         正常啟動（不留命令列視窗）
::       run.bat debug   保留命令列視窗，方便看錯誤訊息
set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\requirements.installed"

:: [1/3] 第一次執行時建立虛擬環境（套件裝在專案的 .venv，不影響全域 Python）
if exist "%PY%" goto deps
python --version >nul 2>&1
if errorlevel 1 goto no_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto old_python
echo [1/3] 第一次執行，建立虛擬環境 .venv ...
python -m venv "%VENV%"
if errorlevel 1 goto venv_failed

:deps
:: [2/3] requirements.txt 有變動（或第一次）才安裝相依套件
fc /b requirements.txt "%STAMP%" >nul 2>&1
if not errorlevel 1 goto check
echo [2/3] 安裝相依套件...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_failed
copy /y requirements.txt "%STAMP%" >nul

:check
"%PY%" -c "import PyQt6.QtWidgets, xxhash" >nul 2>&1
if not errorlevel 1 goto launch
:: 套件壞掉或被刪除：重新安裝一次
echo [2/3] 相依套件不完整，重新安裝...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_failed
copy /y requirements.txt "%STAMP%" >nul

:launch
echo [3/3] 啟動 DupeScan...
if /i "%~1"=="debug" goto launch_debug
start "" "%VENV%\Scripts\pythonw.exe" main.py
exit /b 0

:launch_debug
"%PY%" main.py
echo.
echo 程式已結束（結束代碼 %errorlevel%）。
pause
exit /b 0

:no_python
echo [錯誤] 找不到 Python！
echo.
echo 請先安裝 Python 3.11 以上版本：
echo   https://www.python.org/downloads/
echo.
echo 安裝時請勾選 "Add Python to PATH"
echo.
pause
exit /b 1

:old_python
echo [錯誤] Python 版本太舊，需要 3.11 以上：
python --version
echo.
pause
exit /b 1

:venv_failed
echo [錯誤] 建立虛擬環境失敗，請確認 Python 安裝完整（含 venv 模組）。
echo.
pause
exit /b 1

:pip_failed
echo.
echo [錯誤] 相依套件安裝失敗！
echo 請確認網路連線後再執行一次；若仍失敗，可刪除 .venv 資料夾後重試。
echo.
pause
exit /b 1
