# 主視窗：一個視窗三個畫面（開始 → 掃描中 → 結果），上方一條細細的標題列放 ⋯ 選單。
#
# 狀態都會存下來，當機或直接關閉也不會丟：
#   掃描設定、規則、視窗大小 → 設定檔（變更後一秒內寫入）
#   掃描進度 → 續掃紀錄（ScanController）
#   掃描結果與保留／移除的選擇 → last_results.dupescan（掃描完成、變更後幾秒、移除後、關閉時）
import os
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QMainWindow, QMenu, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import __version__, hash_cache, scan_job
from ..app_paths import data_dir
from ..delete_plan import build_plan
from ..formatting import human_size
from ..logger import log_dir, logger
from ..report import save_report
from ..results_io import RESULT_SUFFIX, export_csv, load_results, save_results
from ..scan_options import ScanOptions
from ..selection import auto_select
from ..session import pid_alive
from . import theme
from .app_settings import AppSettings
from .dialogs import RemoveDialog, RulesDialog, SettingsDialog, TextDialog, about, open_folder, unreadable_dialog
from .errors import CrashNoticeDialog
from .history_dialog import HistoryDialog
from .home_page import HomePage
from .removal import remove_items
from .results_page import ResultsInfo, ResultsPage, open_file, reveal_in_explorer
from .scan_controller import ScanController
from .scan_page import ScanPage
from .tasks import TaskError, run_task
from .widgets import Toast, button, label

SAVE_RESULTS_DELAY_MS = 3000


class AppWindow(QMainWindow):
    def __init__(self, settings: AppSettings | None = None, crashed: list[dict] | None = None):
        super().__init__()
        self.settings = settings or AppSettings()
        self.crashed = crashed or []
        self.setWindowTitle("DupeScan")
        self.setMinimumSize(880, 600)
        self.resize(1180, 800)
        geometry = self.settings.geometry
        if geometry is not None:
            self.restoreGeometry(geometry)

        self.controller = ScanController(self)
        self.home = HomePage()
        self.scan = ScanPage()
        self.results = ResultsPage()
        self._error_details = ""
        self._results_dirty = False

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_top_bar())
        self.stack = QStackedWidget()
        for page in (self.home, self.scan, self.results):
            self.stack.addWidget(page)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.toast = Toast(central)
        self.stack.currentChanged.connect(self._page_changed)

        self._settings_timer = QTimer(self, singleShot=True, interval=1000)
        self._settings_timer.timeout.connect(self._save_settings)
        self._results_timer = QTimer(self, singleShot=True, interval=SAVE_RESULTS_DELAY_MS)
        self._results_timer.timeout.connect(self._save_last_results)
        self._connect()

        self.home.set_options(self.settings.scan_options)
        self.results.set_remove_method(self.settings.remove_method)
        self._refresh_home()
        QShortcut(QKeySequence.StandardKey.Find, self, self._focus_search)

    # ── 版面 ──────────────────────────────────────────────────────────
    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 8, 12, 8)
        lay.addWidget(label("DupeScan", "brand"))
        lay.addWidget(label(__version__, "faint"))
        lay.addStretch()
        self.menu_button = button("", "icon", tooltip="更多功能")
        self.menu_button.setIcon(theme.icon("more"))
        self.menu_button.setAccessibleName("更多功能")
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._update_menu)
        self.act_history = self.menu.addAction("操作紀錄與還原…", self._show_history)
        self.menu.addSeparator()
        self.act_open = self.menu.addAction("開啟儲存的結果…", self._open_results_file)
        self.act_save = self.menu.addAction("儲存目前的結果…", self._save_results_file)
        self.act_report = self.menu.addAction("匯出 HTML 報表", self._export_report)
        self.act_csv = self.menu.addAction("匯出 CSV…", self._export_csv)
        self.menu.addSeparator()
        self.menu.addAction("設定…", self._show_settings)
        self.menu.addAction("開啟 log 資料夾", lambda: self._open_folder(log_dir()))
        self.menu.addAction("關於 DupeScan", lambda: about(self))
        self.menu_button.setMenu(self.menu)
        lay.addWidget(self.menu_button)
        return bar

    def _connect(self):
        home, scan, results, ctrl = self.home, self.scan, self.results, self.controller
        home.start_requested.connect(self._start_scan)
        home.resume_requested.connect(self._resume_scan)
        home.discard_requested.connect(self._discard_job)
        home.open_last_requested.connect(self._open_last_results)
        home.options_changed.connect(self._settings_timer.start)
        home.message.connect(self.show_message)

        scan.pause_requested.connect(ctrl.pause)
        scan.resume_requested.connect(ctrl.resume)
        scan.stop_requested.connect(self._stop_scan)
        scan.retry_requested.connect(self._resume_scan)
        scan.home_requested.connect(self._go_home)
        scan.details_requested.connect(self._show_scan_error)

        ctrl.started.connect(self._on_started)
        ctrl.progress.connect(scan.update_progress)
        ctrl.disk_detected.connect(scan.set_disk)
        ctrl.paused_changed.connect(scan.set_paused)
        ctrl.finished.connect(self._on_finished)
        ctrl.stopped.connect(self._on_stopped)
        ctrl.failed.connect(self._on_failed)

        results.home_requested.connect(self._go_home)
        results.rescan_requested.connect(self._rescan)
        results.rules_requested.connect(self._edit_rules)
        results.remove_requested.connect(self._remove_selected)
        results.unreadable_requested.connect(self._show_unreadable)
        results.open_file_requested.connect(self._open_file)
        results.reveal_file_requested.connect(self._reveal_file)
        results.selection_edited.connect(self._results_edited)
        results.message.connect(self.show_message)

    # ── 共用 ──────────────────────────────────────────────────────────
    def show_message(self, text: str, kind: str = "info", action=None):
        self.toast.show_message(text, kind, action)

    def startup_notices(self):
        """視窗顯示後呼叫：上次沒有正常關閉、設定檔有問題時提示。"""
        if self.crashed:
            dialog = CrashNoticeDialog(self.crashed, has_job=scan_job.load_job() is not None,
                                       has_last=scan_job.load_last_summary() is not None, parent=self)
            dialog.exec()
            dialog.deleteLater()
        if self.settings.notice:
            self.show_message(self.settings.notice, "warning")
            self.settings.notice = ""

    def _page_changed(self):
        on_results = self.stack.currentWidget() is self.results and not self.results.bottom.isHidden()
        self.toast.bottom_offset = 96 if on_results else 24      # 不要蓋住結果畫面底部的按鈕列

    def _focus_search(self):
        if self.stack.currentWidget() is self.results:
            self.results.search.setFocus()
            self.results.search.selectAll()

    def _open_folder(self, path):
        error = open_folder(path)
        if error:
            self.show_message(f"無法開啟資料夾：{error}", "error")

    def _refresh_home(self):
        job = scan_job.load_job()
        if job is not None and job.state == "running" and job.pid != os.getpid() and pid_alive(job.pid):
            job = None                                  # 另一個 DupeScan 正在掃描
        self.home.set_job(job)
        self.home.set_last_summary(scan_job.load_last_summary())

    def _go_home(self):
        self._refresh_home()
        self.stack.setCurrentWidget(self.home)

    # ── 掃描 ──────────────────────────────────────────────────────────
    def _start_scan(self, options: ScanOptions, resumed: bool = False):
        self._flush_results()
        self._save_settings()
        self.controller.start(options, resumed)

    def _resume_scan(self):
        job = scan_job.load_job()
        if job is None:
            self.show_message("找不到未完成的掃描紀錄", "warning")
            self._go_home()
            return
        self._start_scan(job.options, resumed=True)

    def _rescan(self):
        info = self.results.info
        if info is None:
            return
        self._start_scan(info.rescan_options())

    def _discard_job(self):
        scan_job.clear_job()
        self.home.set_job(None)
        self.show_message("已捨棄未完成的掃描")

    def _stop_scan(self):
        self.scan.set_stopping()
        self.controller.stop()

    def _on_started(self, options: ScanOptions, resumed: bool):
        self._error_details = ""
        self.scan.start(options, resumed)
        self.stack.setCurrentWidget(self.scan)

    def _on_stopped(self):
        self._go_home()
        self.show_message("已停止掃描。已經比對過的部分都保留了，可以隨時繼續。")

    def _on_failed(self, message: str, details: str):
        self._error_details = details
        self.scan.show_error(message)
        self.stack.setCurrentWidget(self.scan)

    def _show_scan_error(self):
        dialog = TextDialog("掃描錯誤", "掃描時發生錯誤", "以下是錯誤的詳細內容（也寫在 log 裡）。",
                            self._error_details or "（沒有詳細內容）", self)
        dialog.exec()
        dialog.deleteLater()

    def _on_finished(self, groups: list, summary):
        rules = self.settings.auto_rules
        auto_select(groups, rules)
        info = ResultsInfo.from_summary(summary)
        self.results.set_results(groups, info, rules, self.settings.sort)
        self.stack.setCurrentWidget(self.results)
        self._results_dirty = True
        self._save_last_results()
        if summary.stats.cache_note:
            logger.warning(f"快取：{summary.stats.cache_note}")
        if groups:
            count, size, _ = self.results.model.totals()
            self.show_message(f"已依自動選取規則選好每組要保留的檔案：將移除 {count:,} 個，釋放 {human_size(size)}。"
                              "可以點檔案前面的膠囊調整。", "success")

    # ── 結果 ──────────────────────────────────────────────────────────
    def _show_results(self, groups: list, info: ResultsInfo):
        self.results.set_results(groups, info, self.settings.auto_rules, self.settings.sort)
        self.stack.setCurrentWidget(self.results)

    def _open_last_results(self):
        path = scan_job.last_results_path()
        try:
            groups, meta = load_results(path)
        except (ValueError, OSError) as exc:
            logger.warning(f"無法開啟上次的結果：{exc}")
            self.show_message(f"無法開啟上次的結果：{exc}", "error")
            self.home.set_last_summary(None)
            return
        self._show_results(groups, ResultsInfo.from_meta(meta))

    def _open_results_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "開啟儲存的結果", str(Path.home()),
                                              f"DupeScan 結果 (*{RESULT_SUFFIX})")
        if not path:
            return
        try:
            groups, meta = load_results(path)
        except (ValueError, OSError) as exc:
            self.show_message(f"無法開啟這個檔案：{exc}", "error")
            return
        logger.info(f"開啟結果檔：{path}（{len(groups)} 組）")
        self._flush_results()
        self._show_results(groups, ResultsInfo.from_meta(meta, path))
        self._results_dirty = True
        self._save_last_results()
        self.show_message("已開啟結果。檔案在存檔後可能有變動，移除前會再檢查一次。")

    def _save_results_file(self):
        info = self.results.info
        if info is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "儲存結果", str(Path.home() / f"dupescan{RESULT_SUFFIX}"),
                                              f"DupeScan 結果 (*{RESULT_SUFFIX})")
        if not path:
            return
        try:
            save_results(path, self.results.groups(), info.roots, info.scanned_at, scan=info.to_scan_dict())
        except OSError as exc:
            self.show_message(f"無法儲存：{exc.strerror or exc}", "error")
            return
        self.show_message("已儲存結果", "success", ("開啟資料夾", lambda: self._open_folder(Path(path).parent)))

    def _export_csv(self):
        if self.results.info is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "匯出 CSV", str(Path.home() / "dupescan_結果.csv"),
                                              "CSV (*.csv)")
        if not path:
            return
        try:
            export_csv(path, self.results.groups())
        except OSError as exc:
            self.show_message(f"無法匯出：{exc.strerror or exc}", "error")
            return
        self.show_message("已匯出 CSV", "success", ("開啟資料夾", lambda: self._open_folder(Path(path).parent)))

    def _export_report(self):
        info = self.results.info
        if info is None or not self.results.groups():
            self.show_message("沒有可以匯出的結果", "warning")
            return
        try:
            path = save_report(self.results.groups(), info.roots)
        except OSError as exc:
            self.show_message(f"無法產生報表：{exc.strerror or exc}", "error")
            return
        logger.info(f"匯出報表：{path}")
        webbrowser.open(path.as_uri())
        self.show_message("報表已在瀏覽器開啟", "success", ("開啟資料夾", lambda: self._open_folder(path.parent)))

    def _results_edited(self):
        self._results_dirty = True
        self._results_timer.start()

    def _flush_results(self):
        if self._results_timer.isActive():
            self._results_timer.stop()
        self._save_last_results()

    def _save_last_results(self):
        """把目前的結果（含保留／移除的選擇）存成「上次的結果」。"""
        info = self.results.info
        if info is None or not self._results_dirty:
            return
        groups = self.results.groups()
        try:
            save_results(scan_job.last_results_path(), groups, info.roots, info.scanned_at,
                         scan=info.to_scan_dict())
            scan_job.save_last_summary(info.roots, info.scanned_at, len(groups),
                                       sum(g.wasted_bytes for g in groups))
            self._results_dirty = False
        except OSError as exc:
            logger.warning(f"無法儲存上次的結果：{exc}")

    def _show_unreadable(self):
        info = self.results.info
        if info is None:
            return
        dialog = unreadable_dialog(info.unreadable_samples, info.unreadable, self)
        dialog.exec()
        dialog.deleteLater()

    def _open_file(self, fi):
        error = open_file(fi.path)
        if error:
            self.show_message(f"無法開啟檔案：{error}", "error")
        else:
            logger.info(f"開啟檔案：{fi.path}")

    def _reveal_file(self, fi):
        error = reveal_in_explorer(fi.path)
        if error:
            self.show_message(f"無法在檔案總管中顯示：{error}", "error")

    # ── 規則與移除 ────────────────────────────────────────────────────
    def _edit_rules(self):
        dialog = RulesDialog(self.settings.auto_rules, has_results=bool(self.results.groups()), parent=self)
        dialog.protected.message.connect(self.show_message)
        dialog.remove.message.connect(self.show_message)
        accepted = dialog.exec() == RulesDialog.DialogCode.Accepted
        rules = dialog.rules()
        dialog.deleteLater()
        if not accepted:
            return
        self.settings.auto_rules = rules
        self._save_settings()
        logger.info(f"自動選取規則：{rules.to_dict()}")
        if self.results.groups():
            counts = self.results.apply_rules(rules)
            self._results_edited()
            parts = "、".join(f"{reason} {n:,} 組" for reason, n in counts.most_common(4))
            self.show_message(f"已依新規則重新選取：{parts}", "success")

    def _remove_selected(self):
        groups = [(n, g) for n, g in enumerate(self.results.groups(), 1) if any(fi.selected for fi in g.files)]
        if not groups:
            return
        rules = self.settings.auto_rules
        try:
            plan = run_task(self, "檢查檔案", "正在確認要移除的檔案還在、而且沒有變動…",
                            lambda report: build_plan(groups, reference_dirs=rules.protected_dirs))
        except TaskError as exc:
            self._task_failed("檢查檔案時發生錯誤", exc)
            return
        method, full_verify = self.settings.remove_method, self.settings.full_verify
        dialog = RemoveDialog(plan, method, full_verify, self)
        accepted = dialog.exec() == RemoveDialog.DialogCode.Accepted
        items = dialog.selected() if accepted else []
        dialog.deleteLater()
        if not items:
            return
        logger.info(f"移除 {len(items)} 個檔案（{method}{'，完整比對' if full_verify else ''}）")
        hwnd = int(self.winId())
        try:
            results = run_task(self, "移除檔案", "正在移除…",
                               lambda report: remove_items(items, method, hwnd, full_verify, report), delay_ms=0)
        except TaskError as exc:
            self._task_failed("移除檔案時發生錯誤", exc)
            return
        self._removal_done(results, method, {str(fi.path): fi.size for fi, _ in items})

    def _removal_done(self, results: list, method: str, sizes: dict[str, int]):
        ok = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        for r in ok:
            logger.info(f"  {'回收筒' if r.method == 'recycle' else '隔離'}：{r.path}" + (f"（{r.note}）" if r.note else ""))
        for r in bad:
            logger.warning(f"  沒有移除：{r.path}（{r.note}）")
        logger.info(f"移除完成：成功 {len(ok)}、沒有移除 {len(bad)}")
        if ok:
            self.results.files_removed({r.path for r in ok})
            self._results_dirty = True
            self._save_last_results()
        freed = sum(sizes.get(r.path, 0) for r in ok)
        quarantined = sum(1 for r in ok if r.method == "quarantine")
        where = "資源回收筒" if method == "recycle" and not quarantined else (
            "隔離資料夾" if quarantined == len(ok) else "資源回收筒或隔離資料夾")
        text = f"已把 {len(ok):,} 個檔案移到{where}，釋放 {human_size(freed)}。" if ok else "沒有移除任何檔案。"
        if bad:
            text += f" {len(bad):,} 個沒有移除。"
            body = "\n".join(f"{r.path}（{r.note}）" for r in bad)
            self.show_message(text, "warning", ("查看", lambda: self._show_text(
                "沒有移除的檔案", f"{len(bad):,} 個檔案沒有移除",
                "這些檔案維持原狀（沒有被刪除或搬移）。", body)))
        elif quarantined:
            self.show_message(text, "success", ("操作紀錄", self._show_history))
        else:
            self.show_message(text, "success")

    def _show_text(self, title, heading, text, body):
        dialog = TextDialog(title, heading, text, body, self)
        dialog.exec()
        dialog.deleteLater()

    def _task_failed(self, title: str, exc: TaskError):
        logger.error(f"{title}：{exc}\n{exc.details}")
        self._show_text(title, title, f"{exc}\n\n檔案沒有被更動。詳細內容如下（也寫在 log 裡）：", exc.details)

    # ── ⋯ 選單 ────────────────────────────────────────────────────────
    def _update_menu(self):
        has_results = self.results.info is not None and self.stack.currentWidget() is self.results
        has_groups = has_results and bool(self.results.groups())
        self.act_save.setEnabled(has_results)
        self.act_csv.setEnabled(has_groups)
        self.act_report.setEnabled(has_groups)
        self.act_open.setEnabled(not self.controller.running)

    def _show_history(self):
        dialog = HistoryDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _show_settings(self):
        dialog = SettingsDialog(self.settings, hash_cache.cache_size(), self.controller.busy, self)
        dialog.data_button.clicked.connect(lambda: self._open_folder(data_dir()))
        dialog.log_button.clicked.connect(lambda: self._open_folder(log_dir()))
        accepted = dialog.exec() == SettingsDialog.DialogCode.Accepted
        text_size, method, full_verify = dialog.values()
        clear = dialog.cache_cleared
        dialog.deleteLater()
        if not accepted:
            return
        if clear and not self.controller.busy:
            try:
                hash_cache.clear_cache()
                logger.info("已清除雜湊快取")
            except OSError as exc:
                self.show_message(f"無法清除快取：{exc.strerror or exc}", "error")
        if text_size != self.settings.text_size:
            self.settings.text_size = text_size
            theme.apply(QApplication.instance(), theme.font_px(text_size))
            self.results.set_font_metrics_changed()
        self.settings.remove_method = method
        self.settings.full_verify = full_verify
        self.results.set_remove_method(method)
        self._save_settings()

    # ── 設定與關閉 ────────────────────────────────────────────────────
    def _save_settings(self):
        self._settings_timer.stop()
        self.settings.scan_options = self.home.options()
        if self.results.info is not None:
            self.settings.sort = self.results.sort_key()
        self.settings.geometry = self.saveGeometry()
        if not self.settings.sync() and self.settings.notice:
            self.show_message(self.settings.notice, "warning")
            self.settings.notice = ""

    def closeEvent(self, event):
        if self.controller.busy:
            self.controller.shutdown(3000)       # 續掃紀錄標成「已停止」，下次可以繼續
        self._flush_results()
        self._save_settings()
        self.toast.hide()
        logger.info("關閉 DupeScan")
        event.accept()

