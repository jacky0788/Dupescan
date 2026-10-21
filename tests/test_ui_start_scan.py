# 新介面：設定檔、開始畫面、掃描中畫面與掃描控制（續掃紀錄、暫停、停止、錯誤）
import os

import pytest
from PyQt6.QtCore import QSettings

from helpers import blob, wait_until
from src import scan_job
from src.scan_options import ScanOptions
from src.selection import AutoRules


# ── 設定檔 ───────────────────────────────────────────────────────────────
@pytest.fixture
def settings(tmp_path, qapp):
    from src.ui.app_settings import AppSettings
    path = tmp_path / "settings.ini"
    return lambda: AppSettings(QSettings(str(path), QSettings.Format.IniFormat)), path


def test_settings_defaults_and_round_trip(settings):
    make, _ = settings
    s = make()
    assert s.scan_options == ScanOptions() and s.auto_rules == AutoRules()
    assert (s.text_size, s.sort, s.remove_method, s.full_verify) == ("normal", "wasted", "recycle", False)
    s.scan_options = ScanOptions(roots=["D:/a"], types=["image"], min_size=0)
    s.auto_rules = AutoRules(protected_dirs=["D:/keep"], fallback="newest")
    s.text_size, s.sort, s.remove_method, s.full_verify = "large", "name", "quarantine", True
    assert s.sync()
    s2 = make()
    assert s2.scan_options.roots == ["D:/a"] and s2.scan_options.min_size == 0
    assert s2.auto_rules.fallback == "newest"
    assert (s2.text_size, s2.sort, s2.remove_method, s2.full_verify) == ("large", "name", "quarantine", True)
    assert s2.notice == ""


def test_settings_with_broken_values_fall_back_to_defaults(settings):
    make, path = settings
    path.write_text('[scan]\noptions={roots: [D:/a]}\n[view]\ntext_size=huge\n'
                    '[remove]\nfull_verify=maybe\n', encoding="utf-8")
    s = make()
    assert s.scan_options == ScanOptions()
    assert s.text_size == "normal" and s.full_verify is False
    assert "預設值" in s.notice


def test_settings_migrate_folders_from_old_version(settings):
    make, path = settings
    path.write_text('[scan]\npaths="D:\\\\Photos; E:\\\\Backup; relative"\ninclude_exts="psd, ai"\n'
                    'include_hidden=true\n[select]\nreference_dirs=D:\\\\Photos\nremove_dirs=_trash\n',
                    encoding="utf-8")
    s = make()
    options = s.scan_options
    assert options.roots == ["D:\\Photos", "E:\\Backup"]
    assert options.custom_exts == ["psd", "ai"] and options.include_hidden
    assert s.auto_rules.protected_dirs == ["D:\\Photos"] and s.auto_rules.remove_dirs == ["_trash"]


# ── 開始畫面 ─────────────────────────────────────────────────────────────
@pytest.fixture
def home(qapp):
    from src.ui.home_page import HomePage
    page = HomePage()
    page.resize(900, 800)
    page.show()
    return page


def test_home_options_round_trip(home, tmp_path):
    (tmp_path / "a").mkdir()
    options = ScanOptions(roots=[str(tmp_path / "a")], types=["image", "video"], custom_exts=["psd"],
                          min_size=1024 * 1024, exclude_dirs=[str(tmp_path / "a" / "skip")],
                          skip_system=False, include_hidden=True)
    home.set_options(options)
    assert home.options() == options
    assert not home.advanced.isHidden()                 # 有進階設定時自動展開
    assert not home.custom_row.isHidden()


def test_type_chips_all_is_exclusive(home):
    chips = home.types.chips
    chips["image"].click()
    assert home.types.selected() == ["image"] and not chips["all"].isChecked()
    chips["video"].click()
    assert home.types.selected() == ["image", "video"]
    chips["all"].click()
    assert home.types.selected() == [] and chips["all"].isChecked()
    chips["audio"].click()
    chips["audio"].click()                                # 全部取消 → 回到「全部」
    assert chips["all"].isChecked() and home.options().types == []


def test_custom_type_needs_extensions(home, tmp_path):
    home.folders.add_paths([str(tmp_path)])
    started = []
    home.start_requested.connect(started.append)
    home.types.chips["custom"].click()
    home.start_button.click()
    assert not started and not home.custom_error.isHidden()
    home.custom_exts.setText(".PSD, blend")
    home.start_button.click()
    assert started and started[0].custom_exts == ["psd", "blend"] and started[0].types == []


def test_start_checks_folders(home, tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    missing = tmp_path / "missing"
    started, messages = [], []
    home.start_requested.connect(started.append)
    home.message.connect(lambda text, kind: messages.append(kind))
    assert not home.start_button.isEnabled()              # 還沒有資料夾
    home.folders.add_paths([str(good), str(missing)])
    home.start_button.click()
    assert not started and messages == ["warning"]
    row = home.folders._rows[os.path.normpath(str(missing))]
    assert not row.error.isHidden() and "找不到" in row.error.text()
    home.folders.remove_path(os.path.normpath(str(missing)))
    home.start_button.click()
    assert started and started[0].roots == [os.path.normpath(str(good))]


def test_folder_list_merges_nested_folders(home, tmp_path):
    parent, child = tmp_path / "p", tmp_path / "p" / "c"
    child.mkdir(parents=True)
    messages = []
    home.folders.message.connect(messages.append)
    home.folders.add_paths([str(child)])
    home.folders.add_paths([str(parent)])                 # 父資料夾取代子資料夾
    assert home.folders.paths() == [os.path.normpath(str(parent))]
    assert home.folders.add_paths([str(child)]) == 0      # 已包含在清單裡
    assert home.folders.add_paths([str(parent).upper()]) == 0
    assert len(messages) == 3


def test_home_resume_banner_and_last_results(home):
    assert home.resume_banner.isHidden() and home.last_box.isHidden()
    job = scan_job.ScanJob(ScanOptions(roots=["D:/a"]), state="failed", step=2, step_progress=0.5,
                           error="磁碟沒有回應")
    home.set_job(job)
    assert not home.resume_banner.isHidden() and "錯誤" in home.resume_banner.title.text()
    assert "磁碟沒有回應" in home.resume_banner.text.text()
    home.set_job(None)
    assert home.resume_banner.isHidden()
    home.set_last_summary({"roots": ["D:/a"], "scanned_at": "2026-10-21T10:00:00", "groups": 3,
                           "wasted": 2048})
    assert not home.last_box.isHidden() and "3 組重複" in home.last_text.text()


# ── 掃描中畫面 ───────────────────────────────────────────────────────────
def test_scan_page_states(qapp):
    from src.ui.scan_controller import Progress
    from src.ui.scan_page import ScanPage
    page = ScanPage()
    page.start(ScanOptions(roots=["D:/a"]), resumed=True)
    assert page.title.text() == "繼續掃描"
    page.update_progress(Progress("掃描中... 已找到 10 個檔案", 10, 0, 1, -1))
    assert page.bar.maximum() == 0 and page.percent.text() == ""
    page.update_progress(Progress("比對檔案內容...", 250, 1000, 3, 30))
    assert page.percent.text() == "25%" and "約 30 秒" in page.eta.text()
    page.set_paused(True)
    assert page.pause_button.text() == "繼續" and "暫停" in page.eta.text()
    page.show_error("磁碟沒有回應")
    assert not page.error_banner.isHidden() and page.progress_box.isHidden()
    page.start(ScanOptions(roots=["D:/a"]))
    assert page.error_banner.isHidden() and page.pause_button.text() == "暫停"


# ── 掃描控制 ─────────────────────────────────────────────────────────────
@pytest.fixture
def dupes(tree):
    data = blob(20_000, 1)
    tree("a/one.bin", data)
    tree("b/two.bin", data)
    tree("b/other.bin", blob(20_000, 2))
    return tree.root


class Recorder:
    def __init__(self, ctrl):
        self.events = []
        ctrl.started.connect(lambda options, resumed: self.events.append(("started", resumed)))
        ctrl.finished.connect(lambda groups, summary: self.events.append(("finished", groups, summary)))
        ctrl.stopped.connect(lambda: self.events.append(("stopped",)))
        ctrl.failed.connect(lambda msg, details: self.events.append(("failed", msg, details)))

    def kinds(self):
        return [e[0] for e in self.events]

    def last(self, kind):
        return next(e for e in reversed(self.events) if e[0] == kind)


@pytest.fixture
def controller(qapp):
    from src.ui.scan_controller import ScanController
    ctrl = ScanController()
    yield ctrl
    ctrl.shutdown(5000)
    wait_until(qapp, lambda: not ctrl.busy, 5)
    scan_job.clear_job()


def test_controller_completes_and_clears_job(qapp, controller, dupes):
    rec = Recorder(controller)
    controller.start(ScanOptions(roots=[str(dupes)], min_size=0))
    assert scan_job.load_job() is not None                # 一開始就寫入續掃紀錄
    assert wait_until(qapp, lambda: "finished" in rec.kinds())
    _, groups, summary = rec.last("finished")
    assert len(groups) == 1 and len(groups[0].files) == 2
    assert summary.stats.files_seen == 3 and summary.elapsed >= 0
    assert scan_job.load_job() is None
    assert wait_until(qapp, lambda: not controller.busy)


def test_controller_stop_keeps_resumable_job(qapp, controller, dupes):
    rec = Recorder(controller)
    controller.start(ScanOptions(roots=[str(dupes)], min_size=0))
    controller.pause()
    assert controller.paused
    controller.stop()
    assert wait_until(qapp, lambda: "stopped" in rec.kinds())
    assert "finished" not in rec.kinds()
    job = scan_job.load_job()
    assert job is not None and job.state == "stopped" and job.options.roots == [str(dupes)]
    assert wait_until(qapp, lambda: not controller.busy)


def test_controller_restart_while_stopping_runs_once(qapp, controller, dupes):
    rec = Recorder(controller)
    controller.start(ScanOptions(roots=[str(dupes)], min_size=0))
    controller.pause()
    controller.start(ScanOptions(roots=[str(dupes)], min_size=0), resumed=True)   # 上一次還沒結束
    assert wait_until(qapp, lambda: "finished" in rec.kinds())
    assert wait_until(qapp, lambda: not controller.busy)
    assert rec.kinds().count("finished") == 1 and "stopped" not in rec.kinds()


def test_controller_error_marks_job_failed(qapp, controller, dupes, monkeypatch):
    from src import scanner

    def boom(self):
        raise RuntimeError("讀取失敗")
    monkeypatch.setattr(scanner.Scanner, "_run_passes", boom)
    rec = Recorder(controller)
    controller.start(ScanOptions(roots=[str(dupes)], min_size=0))
    assert wait_until(qapp, lambda: "failed" in rec.kinds())
    _, message, details = rec.last("failed")
    assert message == "讀取失敗" and "RuntimeError" in details
    job = scan_job.load_job()
    assert job.state == "failed" and job.error == "讀取失敗"


def test_controller_shutdown_while_paused(qapp, controller, dupes):
    controller.start(ScanOptions(roots=[str(dupes)], min_size=0))
    controller.pause()
    assert controller.shutdown(5000)
    assert scan_job.load_job().state == "stopped"
