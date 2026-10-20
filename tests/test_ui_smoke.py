# 主視窗冒煙測試（offscreen）：實際跑一次掃描、檢查結果樹與勾選統計
import pytest

from helpers import blob, wait_until

KB = 1024


@pytest.fixture
def window(qapp):
    from src.ui.main_window import MainWindow
    win = MainWindow()
    yield win
    win._kill_thread()      # 收掉背景執行緒，避免 QThread 在執行中被回收
    win.close()


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

    def slow(path):
        time.sleep(0.05)
        return orig(path)

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
