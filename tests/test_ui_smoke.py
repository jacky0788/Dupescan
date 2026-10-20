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
