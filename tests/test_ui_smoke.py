# 主視窗冒煙測試（offscreen）：實際跑一次掃描、檢查結果樹與勾選統計
import pytest

from helpers import blob, wait_until

KB = 1024


@pytest.fixture
def window(qapp):
    from src.ui.main_window import MainWindow
    win = MainWindow()
    yield win
    win.close()             # closeEvent 會停止掃描並等背景執行緒結束


def run_scan(qapp, win, root):
    win.path_edit.setText(str(root))
    win._start_scan()
    assert wait_until(qapp, lambda: not win._scanning, timeout=20)


def test_scan_populates_tree(qapp, window, tree):
    a, b = blob(4 * KB, 1), blob(6 * KB, 2)
    tree("x/a1.jpg", a)
    tree("y/a2.jpg", a)
    tree("x/b1.png", b)
    tree("y/b2.png", b)
    tree("y/b3.png", b)
    run_scan(qapp, window, tree.root)

    assert len(window._all_groups) == 2
    assert window.tree.topLevelItemCount() == 2
    window.tree.select_all(True)
    assert window.tree.selected_stats() == (5, 2 * 4 * KB + 3 * 6 * KB)

    window.ext_filter_edit.setText("png")
    wait_until(qapp, lambda: window.tree.topLevelItemCount() == 1, timeout=3)
    assert window.tree.topLevelItemCount() == 1


def test_scan_without_duplicates(qapp, window, tree):
    tree("a.bin", blob(4 * KB, 1))
    tree("b.bin", blob(4 * KB, 2))
    run_scan(qapp, window, tree.root)
    assert window._all_groups == []
    assert window.tree.topLevelItemCount() == 0


def test_cancel_shows_cancelled_not_no_duplicates(qapp, window, tree, monkeypatch):
    import time
    import src.scanner as sc
    orig = sc._partial_hash

    def slow(path, checkpoint=None):
        time.sleep(0.05)
        return orig(path, checkpoint)

    monkeypatch.setattr(sc, "_partial_hash", slow)
    data = blob(4 * KB, 1)
    for i in range(200):
        tree(f"f{i:03d}.bin", data)
    window.path_edit.setText(str(tree.root))
    window._start_scan()
    assert wait_until(qapp, lambda: "快速比對" in window.progress_label.text(), timeout=10)
    window._cancel_scan()
    assert wait_until(qapp, lambda: not window._scanning, timeout=30)
    assert "取消" in window.status_bar.currentMessage()
    assert "未發現" not in window.tree._placeholder.text()


def test_worker_stop_before_run_emits_cancelled(qapp, tree):
    from src.ui.main_window import ScanWorker
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    tree("b.bin", data)
    w = ScanWorker(7, [str(tree.root)], 1)
    got = []
    w.cancelled.connect(lambda sid: got.append(("cancel", sid)))
    w.finished.connect(lambda sid, g, st: got.append(("done", sid)))
    w.ended.connect(lambda: got.append(("ended",)))
    w.stop()
    w.run()
    assert got == [("cancel", 7), ("ended",)]


def test_restart_ignores_signals_from_previous_scan(qapp, window, tree, monkeypatch):
    import time
    import src.scanner as sc
    orig = sc._partial_hash

    def slow(path, checkpoint=None):
        time.sleep(0.02)
        return orig(path, checkpoint)

    monkeypatch.setattr(sc, "_partial_hash", slow)
    data = blob(4 * KB, 1)
    for i in range(100):
        tree(f"A/f{i:03d}.bin", data)
    other = blob(5 * KB, 2)
    tree("B/x1.bin", other)
    tree("B/x2.bin", other)

    window.path_edit.setText(str(tree.root / "A"))
    window._start_scan()
    assert wait_until(qapp, lambda: "快速比對" in window.progress_label.text(), timeout=10)
    window.path_edit.setText(str(tree.root / "B"))
    window._start_scan()                 # 掃描中直接換路徑重新開始
    assert wait_until(qapp, lambda: not window._scanning, timeout=20)
    assert len(window._all_groups) == 1
    assert {fi.path.name for fi in window._all_groups[0].files} == {"x1.bin", "x2.bin"}
    assert "取消" not in window.status_bar.currentMessage()


RESET_SCRIPT = r'''
import os, sys, time
sys.dont_write_bytecode = True
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, sys.argv[1])
from PyQt6.QtCore import qInstallMessageHandler
from PyQt6.QtWidgets import QApplication

def handler(mode, ctx, msg):
    if mode.name == "QtFatalMsg":
        print("FATAL", msg, flush=True)

qInstallMessageHandler(handler)
app = QApplication([])
import src.scanner as sc
from src.ui.main_window import MainWindow

state = {"hashing": 0}

def stubborn_full_hash(path, checkpoint=None):
    # 模擬卡在一次很久的系統呼叫：不經過暫停點，停止也無法中斷
    state["hashing"] += 1
    time.sleep(4)
    return "x"

sc._full_hash = stubborn_full_hash

def wait_for(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()

win = MainWindow()
win.path_edit.setText(sys.argv[2])
win._start_scan()
assert wait_for(lambda: state["hashing"] > 0, 15)
win._reset_all()                 # 舊版：等 5 秒後丟掉執行中的 QThread → Qt 直接終止程式
print("reset returned, threads =", len(win._threads), flush=True)
assert wait_for(lambda: not win._threads, 15)
print("retired thread released", flush=True)

state["hashing"] = 0
win._start_scan()
assert wait_for(lambda: state["hashing"] > 0, 15)
win.close()                      # 掃描中直接關閉視窗
print("closed during scan", flush=True)
wait_for(lambda: False, 0.5)
print("OK", flush=True)
'''


def test_reset_and_close_during_long_hash_do_not_crash(tree, tmp_path):
    import subprocess
    import sys
    from conftest import ROOT
    data = blob(4 * KB, 1)
    tree("data/a.bin", data)
    tree("data/b.bin", data)
    script = tmp_path / "reset_check.py"
    script.write_text(RESET_SCRIPT, encoding="utf-8")
    proc = subprocess.run([sys.executable, "-B", str(script), str(ROOT), str(tree.root / "data")],
                          capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert "FATAL" not in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
