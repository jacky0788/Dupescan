@echo off
chcp 65001 >nul
title DupeScan 啟動程式

echo ========================================
echo   DupeScan — 重複檔案掃描工具
echo ========================================
echo.

:: 確認 Python 是否安裝
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python！
    echo.
    echo 請先安裝 Python 3.11 以上版本：
    echo   https://www.python.org/downloads/
    echo.
    echo 安裝時請勾選 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo [1/3] Python 版本：
python --version
echo.

echo [2/3] 正在安裝 / 更新依賴套件...
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo [錯誤] 依賴套件安裝失敗！
    echo 請確認網路連線，或手動執行：pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo [3/3] 啟動 DupeScan...
echo.
python main.py

if errorlevel 1 (
    echo.
    echo [錯誤] 程式執行時發生問題，請查看上方錯誤訊息。
    pause
)
