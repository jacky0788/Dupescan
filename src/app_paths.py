# 應用程式資料位置：操作紀錄、雜湊快取等跨次執行要保留的資料。
import os
from pathlib import Path


def data_dir() -> Path:
    """%LOCALAPPDATA%\\DupeScan；測試可用環境變數 DUPESCAN_DATA_DIR 改到暫存資料夾。"""
    override = os.environ.get("DUPESCAN_DATA_DIR")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "DupeScan"
    path.mkdir(parents=True, exist_ok=True)
    return path
