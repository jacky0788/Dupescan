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
    os.environ["DUPESCAN_SETTINGS"] = str(path / "settings.ini")   # 也不碰使用者真正的設定檔
    return path


@pytest.fixture(autouse=True)
def no_appdata_protection(monkeypatch):
    """pytest 的暫存資料夾在 AppData/Local/Temp 底下，測試期間不套用「AppData 受保護」規則。"""
    from src import delete_plan
    monkeypatch.setattr(delete_plan, "DEFAULT_PROTECTED",
                        [p for p in delete_plan.DEFAULT_PROTECTED if p != "*/appdata/*"])


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


@pytest.fixture(autouse=True)
def destroy_qt_widgets_after_test():
    """每個測試結束時確定地關閉並刪除所有頂層視窗，再做一次垃圾回收。

    否則測試留下的視窗（被 lambda 連線形成的循環參照留住）會在之後某次處理 Qt 事件時才被
    垃圾回收刪掉，可能正好刪到正在處理事件的物件而讓整個測試程序崩潰。
    """
    yield
    import gc
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
