# 新介面的主視窗：開始 → 掃描 → 結果、停止與繼續、錯誤與重試、移除、規則、設定、
# 上次的結果、錯誤視窗與當機提示
import threading

import pytest
from PyQt6.QtCore import QSettings

from helpers import blob, wait_until
from src import file_ops, scan_job
from src.scan_options import ScanOptions
from src.selection import AutoRules

KB = 1024


@pytest.fixture(autouse=True)
def clean_state():
    """每個測試從乾淨的續掃紀錄與上次結果開始（資料夾在整個測試階段共用）。"""
    def clean():
        scan_job.clear_job()
        for path in (scan_job.last_results_path(), scan_job.last_results_path().with_suffix(".json")):
            path.unlink(missing_ok=True)
    clean()
    yield
    clean()


@pytest.fixture
def settings_path(tmp_path):
    return tmp_path / "settings.ini"


def make_window(settings_path, crashed=None):
    from src.ui.app_settings import AppSettings
    from src.ui.app_window import AppWindow
    settings = AppSettings(QSettings(str(settings_path), QSettings.Format.IniFormat))
    win = AppWindow(settings, crashed)
    win.resize(1100, 760)
    win.show()
    return win


@pytest.fixture
def win(qapp, settings_path):
    w = make_window(settings_path)
    yield w
    w.close()
    wait_until(qapp, lambda: not w.controller.busy, 5)


@pytest.fixture
def dupes(tree):
    data = blob(200 * KB, 1)
    tree("Pictures/photo.jpg", data, mtime=1_000_000)
    tree("Downloads/photo (1).jpg", data, mtime=2_000_000)
    tree("Backup/photo.jpg", data, mtime=3_000_000)
    tree("Pictures/other.jpg", blob(200 * KB, 2))
    return tree.root


@pytest.fixture
def qroot(tmp_path, monkeypatch):
    root = tmp_path / "Q"
    monkeypatch.setattr(file_ops, "quarantine_root_for", lambda p: root)
    return root


def start_scan(qapp, win, root, **options):
    win.home.set_options(ScanOptions(roots=[str(root)], **options))
    win.home.start_button.click()


def scan(qapp, win, root, **options):
    start_scan(qapp, win, root, **options)
    assert wait_until(qapp, lambda: win.stack.currentWidget() is win.results, 15)


def test_scan_shows_results_and_remembers_them(qapp, win, dupes):
    scan(qapp, win, dupes)
    groups = win.results.groups()
    assert len(groups) == 1 and len(groups[0].files) == 3
    kept = [fi.path.name for fi in groups[0].files if not fi.selected]
    assert kept == ["photo.jpg"]
    assert win.results.summary.text().startswith("將移除 2 個檔案")
    assert scan_job.load_job() is None
    summary = scan_job.load_last_summary()
    assert summary["groups"] == 1 and summary["roots"] == [str(dupes)]
    win.results.home_requested.emit()
    assert win.stack.currentWidget() is win.home and not win.home.last_box.isHidden()


def test_auto_selection_keeps_original_oldest(qapp, win, dupes):
    scan(qapp, win, dupes)
    g = win.results.groups()[0]
    kept = [fi.path.relative_to(dupes).as_posix() for fi in g.files if not fi.selected]
    assert kept == ["Pictures/photo.jpg"]          # 原始檔名中最舊的
    assert g.keep_reason == "最舊的"


def test_stop_returns_home_and_resume_finishes(qapp, win, dupes):
    start_scan(qapp, win, dupes)
    win.controller.pause()
    assert win.stack.currentWidget() is win.scan
    win.scan.stop_button.click()
    assert wait_until(qapp, lambda: win.stack.currentWidget() is win.home, 10)
    assert not win.home.resume_banner.isHidden()
    assert scan_job.load_job().state == "stopped"
    win.home.resume_button.click()
    assert wait_until(qapp, lambda: win.stack.currentWidget() is win.results, 15)
    assert win.scan.title.text() == "繼續掃描"
    assert len(win.results.groups()) == 1 and scan_job.load_job() is None


def test_failed_scan_offers_retry(qapp, win, dupes, monkeypatch):
    from src import scanner
    real = scanner.Scanner._run_passes
    calls = []

    def flaky(self):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(21, "裝置未就緒")
        return real(self)
    monkeypatch.setattr(scanner.Scanner, "_run_passes", flaky)
    start_scan(qapp, win, dupes)
    assert wait_until(qapp, lambda: not win.scan.error_banner.isHidden(), 10)
    assert "裝置未就緒" in win.scan.error_banner.text.text()
    assert scan_job.load_job().state == "failed"
    win.scan.retry_button.click()
    assert wait_until(qapp, lambda: win.stack.currentWidget() is win.results, 15)
    assert len(calls) == 2


def test_remove_moves_copies_and_updates_results(qapp, win, dupes, qroot, monkeypatch):
    from src.ui.dialogs import RemoveDialog
    win.settings.remove_method = "quarantine"
    win.results.set_remove_method("quarantine")
    scan(qapp, win, dupes)
    shown = []
    monkeypatch.setattr(RemoveDialog, "exec", lambda self: (shown.append(self.summary.text()),
                                                            RemoveDialog.DialogCode.Accepted)[1])
    win.results.remove_button.click()
    assert shown and "2 個檔案" in shown[0]
    assert (dupes / "Pictures/photo.jpg").exists()
    assert not (dupes / "Downloads/photo (1).jpg").exists() and not (dupes / "Backup/photo.jpg").exists()
    assert win.results.groups() == [] and win.results.body.currentIndex() == 1
    assert "隔離資料夾" in win.toast.text.text()
    assert file_ops.read_journal()[0]["quarantined"] == 2
    assert scan_job.load_last_summary()["groups"] == 0


def test_full_verify_skips_copies_that_changed(qapp, win, dupes, qroot, monkeypatch):
    from src.ui.dialogs import RemoveDialog
    win.settings.remove_method = "quarantine"
    win.settings.full_verify = True
    scan(qapp, win, dupes)
    changed = dupes / "Backup/photo.jpg"
    data = bytearray(changed.read_bytes())
    data[-1] ^= 0xFF                                  # 只改最後一個位元組：大小、開頭都一樣
    stat = changed.stat()
    changed.write_bytes(bytes(data))
    import os
    os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns))     # 修改時間也一樣
    monkeypatch.setattr(RemoveDialog, "exec", lambda self: RemoveDialog.DialogCode.Accepted)
    win.results.remove_button.click()
    assert changed.exists() and not (dupes / "Downloads/photo (1).jpg").exists()
    assert "1 個沒有移除" in win.toast.text.text()


def test_edit_rules_reselects_and_is_saved(qapp, win, dupes, monkeypatch):
    from src.ui.dialogs import RulesDialog
    scan(qapp, win, dupes)

    def accept(self):
        self.fallback.set_selected(["newest"])
        self.copy_names.setChecked(False)
        return RulesDialog.DialogCode.Accepted
    monkeypatch.setattr(RulesDialog, "exec", accept)
    win.results.rules_requested.emit()
    g = win.results.groups()[0]
    assert [fi.path.relative_to(dupes).as_posix() for fi in g.files if not fi.selected] == ["Backup/photo.jpg"]
    assert win.settings.auto_rules.fallback == "newest" and not win.settings.auto_rules.copy_names


def test_settings_dialog_applies_choices(qapp, win, monkeypatch):
    from src.ui.dialogs import SettingsDialog

    def accept(self):
        self.quarantine.setChecked(True)
        self.full_verify.setChecked(True)
        self.text_size.set_selected(["large"])
        return SettingsDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", accept)
    win._show_settings()
    assert (win.settings.remove_method, win.settings.full_verify, win.settings.text_size) == \
        ("quarantine", True, "large")
    assert win.results.remove_label == "移到隔離資料夾"
    from src.ui import theme
    theme.apply(qapp, theme.font_px("normal"))


def test_last_results_reopen_after_restart(qapp, settings_path, dupes):
    first = make_window(settings_path)
    scan(qapp, first, dupes)
    g = first.results.groups()[0]
    first.results.model.keep_all(g)
    first.results.selection_edited.emit()
    first.close()
    assert wait_until(qapp, lambda: not first.controller.busy, 5)
    second = make_window(settings_path)
    try:
        assert not second.home.last_box.isHidden()
        assert second.home.folders.paths() == [str(dupes)]          # 掃描設定也記住了
        second.home.open_last_requested.emit()
        assert second.stack.currentWidget() is second.results
        reopened = second.results.groups()[0]
        assert not any(fi.selected for fi in reopened.files) and reopened.keep_reason == "全部保留"
    finally:
        second.close()


def test_close_during_scan_keeps_resumable_job(qapp, settings_path, dupes):
    first = make_window(settings_path)
    start_scan(qapp, first, dupes)
    first.controller.pause()
    first.close()
    job = scan_job.load_job()
    assert job is not None and job.state == "stopped"
    second = make_window(settings_path)
    try:
        assert not second.home.resume_banner.isHidden()
    finally:
        second.close()


def test_crash_notice_on_startup(qapp, settings_path, monkeypatch, tmp_path):
    from src.ui.errors import CrashNoticeDialog
    log = tmp_path / "crash.log"
    log.write_text("Fatal Python error: Segmentation fault\n  File \"x.py\", line 1", encoding="utf-8")
    shown = []
    monkeypatch.setattr(CrashNoticeDialog, "exec", lambda self: shown.append(self.log_text) or 0)
    w = make_window(settings_path, crashed=[{"pid": 1, "crash_log": str(log)}])
    try:
        w.startup_notices()
        assert shown and "Segmentation fault" in shown[0]
    finally:
        w.close()


def test_error_reporter_shows_one_dialog_for_repeated_errors(qapp):
    import sys
    from src.ui.errors import ErrorReporter
    reporter = ErrorReporter()
    old_hook, old_thread_hook = sys.excepthook, threading.excepthook
    reporter.install()
    try:
        try:
            1 / 0
        except ZeroDivisionError:
            sys.excepthook(*sys.exc_info())
        assert wait_until(qapp, lambda: reporter._dialog is not None and reporter._dialog.isVisible(), 5)
        dialog = reporter._dialog
        assert "ZeroDivisionError" in dialog.summary.text()

        def boom():
            raise ValueError("背景執行緒出錯")
        t = threading.Thread(target=boom)
        t.start()
        t.join()
        assert wait_until(qapp, lambda: dialog.count == 2, 5)
        assert "背景執行緒出錯" in dialog.details.toPlainText()
        dialog.accept()
    finally:
        reporter.uninstall()
        sys.excepthook, threading.excepthook = old_hook, old_thread_hook


def test_run_task_returns_result_or_raises(qapp):
    from src.ui.tasks import TaskError, run_task
    progress = []
    assert run_task(None, "t", "x", lambda report: (report("一半", 1, 2), progress.append(1), 42)[2]) == 42

    def fail(report):
        raise RuntimeError("壞掉了")
    with pytest.raises(TaskError) as info:
        run_task(None, "t", "x", fail)
    assert "壞掉了" in str(info.value) and "RuntimeError" in info.value.details


def test_rules_dialog_round_trip(qapp):
    from src.ui.dialogs import RulesDialog
    rules = AutoRules(copy_names=False, protected_dirs=["D:\\Archive"], remove_dirs=["Downloads"],
                      fallback="shortest")
    dialog = RulesDialog(rules)
    assert dialog.rules() == rules
    dialog._reset()
    assert dialog.rules() == AutoRules()


def test_remove_dialog_protected_files_need_confirmation(qapp, tree, monkeypatch):
    from src import delete_plan
    from src.models import DuplicateGroup, FileInfo
    from src.ui.dialogs import RemoveDialog
    data = blob(10 * KB, 3)
    paths = [tree("keep/a.bin", data), tree("system/b.bin", data), tree("other/c.bin", data)]
    monkeypatch.setattr(delete_plan, "DEFAULT_PROTECTED", ["*/system/*"])
    files = [FileInfo(p, p.stat().st_size, mtime_ns=p.stat().st_mtime_ns) for p in paths]
    files[1].selected = files[2].selected = True
    plan = delete_plan.build_plan([(1, DuplicateGroup("h", len(data), files))])
    dialog = RemoveDialog(plan, "recycle", False)
    assert len(dialog.selected()) == 1 and dialog.ok_button.isEnabled()
    dialog.allow_protected.setChecked(True)
    assert len(dialog.selected()) == 2 and "2 個" in dialog.ok_button.text()
    dialog._toggle_list()
    assert dialog.tree.topLevelItemCount() == 1


def test_scan_without_duplicates_shows_empty_state(qapp, win, tree):
    tree("a.bin", blob(200 * KB, 1))
    tree("b.bin", blob(200 * KB, 2))
    scan(qapp, win, tree.root)
    assert win.results.body.currentIndex() == 1 and win.results.bottom.isHidden()
    assert "2 個檔案" in win.results.empty_text.text()


def test_drop_folders_onto_folder_list(qapp, win, tree):
    from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PyQt6.QtGui import QDropEvent
    (tree.root / "x").mkdir()
    file_path = tree("y.bin", b"1")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tree.root / "x")), QUrl.fromLocalFile(str(file_path))])
    messages = []
    win.home.folders.message.connect(messages.append)
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    win.home.folders.dropEvent(event)
    assert win.home.folders.paths() == [str(tree.root / "x")]
    assert messages and "只能加入資料夾" in messages[-1]


def test_menu_exports_and_reopens_results(qapp, win, dupes, tmp_path, monkeypatch):
    import webbrowser
    from PyQt6.QtWidgets import QFileDialog
    scan(qapp, win, dupes)
    win._update_menu()
    assert win.act_save.isEnabled() and win.act_csv.isEnabled() and win.act_report.isEnabled()
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    win._export_report()
    assert opened and opened[0].startswith("file:")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(tmp_path / "out.csv"), "")))
    win._export_csv()
    assert (tmp_path / "out.csv").exists()
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(tmp_path / "saved.dupescan"), "")))
    g = win.results.groups()[0]
    win.results.model.keep_all(g)
    win._save_results_file()
    assert (tmp_path / "saved.dupescan").exists()
    win.results.home_requested.emit()
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(tmp_path / "saved.dupescan"), "")))
    win._open_results_file()
    assert win.stack.currentWidget() is win.results
    assert not any(fi.selected for fi in win.results.groups()[0].files)
    assert win.results.info.source == str(tmp_path / "saved.dupescan")


def test_broken_last_results_shows_message(qapp, win):
    scan_job.last_results_path().write_bytes(b"not a results file")
    scan_job.save_last_summary(["D:/a"], "2026-10-21T10:00:00", 3, 1024)
    win._refresh_home()
    assert not win.home.last_box.isHidden()
    win.home.open_last_requested.emit()
    assert win.stack.currentWidget() is win.home and win.home.last_box.isHidden()
    assert "無法開啟上次的結果" in win.toast.text.text()
