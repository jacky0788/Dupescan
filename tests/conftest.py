# 測試共用設定：把專案根目錄加進 sys.path、Qt 一律用 offscreen（不開視窗）
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True, scope="session")
def isolated_data_dir(tmp_path_factory):
    """操作紀錄、快取等一律寫到暫存資料夾，不碰使用者的 %LOCALAPPDATA%\\DupeScan。"""
    path = tmp_path_factory.mktemp("dupescan-data")
    os.environ["DUPESCAN_DATA_DIR"] = str(path)
    return path


@pytest.fixture
def tree(tmp_path):
    """建檔小幫手：tree("a/b.bin", data, mtime=...) 在 tmp_path 底下建立檔案並回傳路徑。"""
    def make(rel: str, data: bytes, mtime: float | None = None) -> Path:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p
    make.root = tmp_path
    return make


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
