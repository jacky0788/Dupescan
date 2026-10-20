# 主視窗冒煙測試（offscreen）：實際跑一次掃描、檢查結果樹與勾選統計
import pytest
from PyQt6.QtCore import Qt

from helpers import blob, wait_until

KB = 1024


@pytest.fixture
def window(qapp, tmp_path):
    from PyQt6.QtCore import QSettings
    from src.ui.main_window import MainWindow
    win = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
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


# ── 移除前預覽視窗與背景移除 ──────────────────────────────────────────────
def scan_and_select(qapp, window, tree, names_to_select, extra=()):
    data = blob(4 * KB, 9)
    for n in ["a/keep.bin", "b/dup1.bin", "c/dup2.bin", *extra]:
        tree(n, data)
    run_scan(qapp, window, tree.root)
    for fi in window._all_groups[0].files:
        fi.selected = fi.path.name in names_to_select
    window.tree.refresh_checks()


def test_dialog_blocks_whole_group_until_confirmed(qapp, window, tree):
    from src.delete_plan import build_plan
    from src.ui.delete_dialog import DeletePlanDialog
    scan_and_select(qapp, window, tree, {"keep.bin", "dup1.bin", "dup2.bin"})
    dlg = DeletePlanDialog(build_plan([(1, window._all_groups[0])]), window)
    assert dlg.selected() == []
    assert not dlg.btn_ok.isEnabled()
    dlg.cb_whole.setChecked(True)
    assert len(dlg.selected()) == 3
    assert dlg.btn_ok.isEnabled() and "3" in dlg.btn_ok.text()
    dlg.close()


def test_remove_flow_moves_files_and_reports(qapp, window, tree, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QDialog
    from src import file_ops
    from src.ui import delete_dialog, main_window

    qroot = tmp_path / "Q"
    monkeypatch.setattr(file_ops, "quarantine_root_for", lambda p: qroot)

    def accept_with_quarantine(dlg):
        dlg.set_method("quarantine")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(delete_dialog.DeletePlanDialog, "exec", accept_with_quarantine)
    reports = []
    monkeypatch.setattr(main_window.QMessageBox, "information",
                        lambda *a, **k: reports.append(a[2]))

    scan_and_select(qapp, window, tree, {"dup1.bin", "dup2.bin"})
    scan_id = window._scan_id
    window._delete_selected()
    assert wait_until(qapp, lambda: reports and window._removal is None, timeout=20)
    assert "隔離資料夾：2 個" in reports[0]
    assert not (tree.root / "b/dup1.bin").exists() and not (tree.root / "c/dup2.bin").exists()
    assert (tree.root / "a/keep.bin").exists()
    assert len(list(qroot.rglob("*.bin"))) == 2
    # 不重新掃描，直接更新結果：只剩一份的群組消失
    assert window._scan_id == scan_id and not window._scanning
    assert window._all_groups == []
    assert "處理完了" in window.tree._placeholder.text()


def test_partial_removal_keeps_group_and_view(qapp, window, tree, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QDialog
    from src import file_ops
    from src.ui import delete_dialog, main_window

    monkeypatch.setattr(file_ops, "quarantine_root_for", lambda p: tmp_path / "Q")
    monkeypatch.setattr(delete_dialog.DeletePlanDialog, "exec",
                        lambda dlg: (dlg.set_method("quarantine"), QDialog.DialogCode.Accepted)[1])
    monkeypatch.setattr(main_window.QMessageBox, "information", lambda *a, **k: None)

    scan_and_select(qapp, window, tree, {"dup1.bin"}, extra=["d/dup3.bin"])
    other = blob(6 * KB, 10)
    tree("x/o1.bin", other)
    tree("y/o2.bin", other)
    run_scan(qapp, window, tree.root)                 # 兩個群組：4 份與 2 份
    big = next(g for g in window._all_groups if len(g.files) == 4)
    for fi in big.files:
        fi.selected = fi.path.name == "dup1.bin"
    window.tree.refresh_checks()
    window.tree.topLevelItem(0).setExpanded(True)
    expanded_before = window.tree.expanded_group_ids()

    window._delete_selected()
    assert wait_until(qapp, lambda: window._removal is None and len(big.files) == 3, timeout=20)
    assert len(window._all_groups) == 2
    assert window.tree.topLevelItemCount() == 2
    assert window.tree.expanded_group_ids() == expanded_before
    assert window.lbl_groups.text() == "2 個群組"


# ── 操作小改善 ─────────────────────────────────────────────────────────
def three_groups(tree):
    """可省空間：big 2×8 KB → 8 KB；many 4×3 KB → 9 KB；small 2×2 KB → 2 KB。"""
    for i in range(2):
        tree(f"big/b{i}.bin", blob(8 * KB, 1))
    for i in range(4):
        tree(f"many/m{i}.bin", blob(3 * KB, 2))
    for i in range(2):
        tree(f"small/s{i}.bin", blob(2 * KB, 3))


def test_default_sort_is_by_wasted_space(qapp, window, tree):
    three_groups(tree)
    run_scan(qapp, window, tree.root)
    order = [window.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1).wasted_bytes
             for i in range(window.tree.topLevelItemCount())]
    assert order == [9 * KB, 8 * KB, 2 * KB]


def test_font_size_change_does_not_rebuild(qapp, window, tree):
    three_groups(tree)
    run_scan(qapp, window, tree.root)
    first = window.tree.topLevelItem(0)
    window.font_size_spin.setValue(9)
    qapp.processEvents()
    small = window.tree.visualItemRect(first).height()
    window.font_size_spin.setValue(18)
    qapp.processEvents()
    assert window.tree.topLevelItem(0) is first          # 沒有重建
    assert window.tree.visualItemRect(first).height() > small


def test_space_toggles_and_selected_label_shows_size(qapp, window, tree):
    from PyQt6.QtTest import QTest
    three_groups(tree)
    run_scan(qapp, window, tree.root)
    group_item = window.tree.topLevelItem(0)              # many：4 × 3 KB
    group_item.setExpanded(True)
    file_item = group_item.child(1)
    window.tree.setCurrentItem(file_item)
    QTest.keyClick(window.tree, Qt.Key.Key_Space)
    assert file_item.data(0, Qt.ItemDataRole.UserRole + 1).selected
    assert window.lbl_selected.text() == "已選 1・3.0 KiB"
    window.tree.setCurrentItem(group_item)
    QTest.keyClick(window.tree, Qt.Key.Key_Space)         # 群組列：整組切換
    assert all(fi.selected for fi in group_item.data(0, Qt.ItemDataRole.UserRole + 1).files)


def test_path_column_needs_ctrl_to_open_explorer(qapp, window, tree, monkeypatch):
    from PyQt6.QtTest import QTest
    three_groups(tree)
    run_scan(qapp, window, tree.root)
    located = []
    monkeypatch.setattr(type(window.tree), "_locate", lambda self, fi: located.append(fi))
    group_item = window.tree.topLevelItem(0)
    group_item.setExpanded(True)
    qapp.processEvents()
    rect = window.tree.visualItemRect(group_item.child(0))
    x = window.tree.header().sectionViewportPosition(1) + 20
    pos = rect.center()
    pos.setX(x)
    QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert located == []                                  # 單擊不再開檔案總管
    QTest.mouseClick(window.tree.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier, pos)
    assert len(located) == 1


# ── 掃描設定：多路徑、拖放、側邊列選項 ─────────────────────────────────
def test_multiple_paths_find_cross_folder_duplicates(qapp, window, tree):
    data = blob(4 * KB, 5)
    tree("one/a.bin", data)
    tree("two/b.bin", data)
    tree("three/c.bin", blob(4 * KB, 6))
    window.path_edit.setText(f"{tree.root / 'one'}; {tree.root / 'two'}")
    assert window._scan_paths() == [str(tree.root / "one"), str(tree.root / "two")]
    window._start_scan()
    assert wait_until(qapp, lambda: not window._scanning, timeout=20)
    assert len(window._all_groups) == 1
    assert {fi.path.name for fi in window._all_groups[0].files} == {"a.bin", "b.bin"}


def test_sidebar_options_are_applied(qapp, window, tree):
    data = blob(4 * KB, 5)
    for n in ("a.jpg", "b.jpg", "a.log", "b.log", "node_modules/c.jpg"):
        tree(n, data)
    window.exclude_ext_edit.setText("log")
    window.exclude_dir_edit.setText("node_modules")
    window.min_size_spin.setValue(2)
    window.min_unit_combo.setCurrentText("KB")
    run_scan(qapp, window, tree.root)
    assert len(window._all_groups) == 1
    assert {fi.path.name for fi in window._all_groups[0].files} == {"a.jpg", "b.jpg"}
    window.min_unit_combo.setCurrentText("MB")            # 最小 2 MB → 4 KB 的檔案都不比對
    run_scan(qapp, window, tree.root)
    assert window._all_groups == []


def test_drop_folders_adds_scan_paths(qapp, window, tree):
    from PyQt6.QtCore import QMimeData, QPointF, QUrl
    from PyQt6.QtGui import QDropEvent
    a = tree("x/a.bin", b"1").parent
    b = tree("y/b.bin", b"2").parent
    window.path_edit.setText(str(a))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(b)), QUrl.fromLocalFile(str(tree.root / "y" / "b.bin"))])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(event)
    assert window._scan_paths() == [str(a), str(b)]       # 只加入資料夾，檔案略過


def test_settings_are_remembered(qapp, tmp_path):
    from PyQt6.QtCore import QSettings
    from src.ui.main_window import MainWindow
    ini = str(tmp_path / "remember.ini")
    first = MainWindow(QSettings(ini, QSettings.Format.IniFormat))
    first.path_edit.setText("D:/Photos; E:/Backup")
    first.min_size_spin.setValue(5)
    first.min_unit_combo.setCurrentText("MB")
    first.exclude_dir_edit.setText("node_modules")
    first.cb_cache.setChecked(False)
    first.font_size_spin.setValue(16)
    first.sort_combo.setCurrentIndex(4)
    first._global_cond._checkboxes["older"].setChecked(True)
    first._global_cond._checkboxes["sh_path"].setChecked(True)
    first._toggle_scan_panel()                       # 收起「掃描設定」
    first.close()

    second = MainWindow(QSettings(ini, QSettings.Format.IniFormat))
    assert second.path_edit.text() == "D:/Photos; E:/Backup"
    assert second.min_size_spin.value() == 5 and second.min_unit_combo.currentText() == "MB"
    assert second.exclude_dir_edit.text() == "node_modules"
    assert not second.cb_cache.isChecked()
    assert second.font_size_spin.value() == 16 and second.tree._font_size == 16
    assert second.sort_combo.currentIndex() == 4
    assert second._global_cond._checked_order == ["older", "sh_path"]
    assert not second._sb_scan_open and not second._sb_scan_body.isVisibleTo(second)
    second.close()


def test_save_and_reopen_results_keeps_selection(qapp, window, tree, tmp_path):
    from PyQt6.QtCore import QSettings
    from src.ui.main_window import MainWindow
    three_groups(tree)
    run_scan(qapp, window, tree.root)
    window._all_groups[0].files[0].selected = True
    path = str(tmp_path / "saved.dupescan")
    window._save_results(path)
    window._export_csv(str(tmp_path / "all.csv"))
    assert (tmp_path / "all.csv").exists()

    other = MainWindow(QSettings(str(tmp_path / "other.ini"), QSettings.Format.IniFormat))
    other._open_results(path)
    assert len(other._all_groups) == 3
    assert other.tree.topLevelItemCount() == 3
    assert other.tree.selected_stats()[0] == 1
    assert "掃描於" in other.status_bar.currentMessage()
    assert other.path_edit.text() == str(tree.root)
    other.close()


def test_export_report_is_saved_and_escaped(qapp, window, tree, monkeypatch, isolated_data_dir):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    data = blob(4 * KB, 1)
    tree("R&D/a.dat", data)
    tree("R&D/b.dat", data)
    run_scan(qapp, window, tree.root / "R&D")
    window._export_report()
    report = sorted((isolated_data_dir / "reports").glob("dupescan_report_*.html"))[-1]
    page = report.read_text(encoding="utf-8")
    assert "<strong>.dat</strong>" in page
    assert "R&amp;D" in page and "R&D<" not in page          # 路徑中的 & 有跳脫
    assert opened and opened[0].startswith("file:")


# ── 智慧選取規則 ───────────────────────────────────────────────────────
def selected_names(group):
    return {fi.path.name for fi in group.files if fi.selected}


def quick_action(window, group_row: int, text: str):
    """右鍵選單「快速選取」子選單裡含 text 的動作（不實際彈出選單）。"""
    menu = window.tree._context_menu(window.tree.topLevelItem(group_row))
    quick = next(a.menu() for a in menu.actions() if a.menu() and "快速選取" in a.text())
    return next(a for a in quick.actions() if text in a.text())


def test_copy_name_rule_from_sidebar_and_context_menu(qapp, window, tree):
    data = blob(4 * KB, 21)
    for n in ("dl/photo.png", "dl/photo (1).png", "dl/photo - 複製.png"):
        tree(n, data)
    run_scan(qapp, window, tree.root)
    g = window._all_groups[0]

    window._global_cond._checkboxes["copy_name"].setChecked(True)
    window._global_cond._checkboxes["sh_path"].setChecked(True)
    assert "1. 像副本的檔名" in window._global_cond._order_label.text()
    window._global_apply_conditions()
    assert selected_names(g) == {"photo (1).png", "photo - 複製.png"}
    assert "像副本的檔名優先移除 1" in window.status_bar.currentMessage()

    window.tree.select_all(False)
    quick_action(window, 0, "原始檔名").trigger()
    assert selected_names(g) == {"photo (1).png", "photo - 複製.png"}
    assert window.lbl_selected.text().startswith("已選 2")
