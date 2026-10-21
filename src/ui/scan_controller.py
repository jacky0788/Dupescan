# 掃描控制：在背景執行緒跑 Scanner，處理暫停／繼續／停止，並維護續掃紀錄（scan_job.json）。
#
#   開始   → 寫入續掃紀錄（進行中），之後每 2 秒更新停在第幾步
#   完成   → 清掉續掃紀錄
#   停止   → 紀錄標成「已停止」，下次可從「開始」畫面繼續
#   出錯   → 紀錄標成「出錯」，掃描畫面提供「重試（從中斷處繼續）」
#   當機或被強制結束 → 紀錄停在「進行中」，下次啟動一樣可以繼續
# 已經算好的 hash 都在雜湊快取裡（每 2 秒寫入），所以繼續掃描只需要重新列出檔案。
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal

from .. import scan_job
from ..hash_cache import HashCache
from ..logger import logger
from ..scan_options import ScanOptions
from ..scanner import STEP_NAMES, Scanner, ScanStats

SAVE_JOB_INTERVAL_MS = 2000

# 關閉視窗時還沒結束的掃描執行緒：保留參照，避免 QThread 在執行中被回收而閃退
_UNFINISHED: list = []


@dataclass
class Progress:
    message: str
    current: int
    total: int
    step: int
    eta: int            # 預估剩餘秒數；-1＝還無法估算

    @property
    def fraction(self) -> float | None:
        """這一步完成的比例；第 1 步（還不知道總數）為 None。"""
        return min(self.current / self.total, 1.0) if self.total > 0 else None


@dataclass
class ScanSummary:
    """一次完成的掃描：設定、時間與統計（存進結果檔，結果畫面顯示用）。"""
    options: ScanOptions
    scanned_at: str
    elapsed: float
    stats: ScanStats

    def to_dict(self) -> dict:
        st = self.stats
        return {"options": self.options.to_dict(), "elapsed": round(self.elapsed, 1),
                "files_seen": st.files_seen, "candidates": st.candidates,
                "unreadable": st.unreadable, "unreadable_samples": list(st.unreadable_samples),
                "cache_note": st.cache_note}


class _Worker(QObject):
    """在背景執行緒裡偵測磁碟類型、執行 Scanner。所有 signal 都帶 scan_id。"""
    progress = pyqtSignal(int, object)
    detected = pyqtSignal(int, str)
    finished = pyqtSignal(int, list, object)
    cancelled = pyqtSignal(int)
    failed = pyqtSignal(int, str, str)
    ended = pyqtSignal()

    def __init__(self, scan_id: int, options: ScanOptions):
        super().__init__()
        self.scan_id = scan_id
        # Scanner 在建構時就建立：run() 開始前呼叫 stop() 也會生效
        self.scanner = Scanner(
            options, cache=HashCache(),
            on_progress=lambda msg, cur, total, step, _steps, eta:
                self.progress.emit(scan_id, Progress(msg, cur, total, step, eta)),
            on_done=lambda groups, stats: self.finished.emit(scan_id, groups, stats),
            on_error=lambda msg: self.failed.emit(scan_id, msg, self.scanner.error_details),
            on_cancel=lambda: self.cancelled.emit(scan_id))

    def run(self):
        try:
            self._detect_disks()
            self.scanner.scan()
        except Exception as exc:          # Scanner 自己會處理例外；這裡是保險，畫面不會卡住
            self.failed.emit(self.scan_id, str(exc) or type(exc).__name__, traceback.format_exc())
        finally:
            self.ended.emit()

    def _detect_disks(self):
        """依每個資料夾所在磁碟的類型決定讀檔執行緒數（偵測失敗就用保守的預設）。"""
        from ..disk_detect import detect_disk_profile
        workers: dict[int, tuple[int, int]] = {}
        labels: list[tuple[str, str]] = []
        for root in self.scanner.roots:
            try:
                dev = os.stat(root).st_dev
                if dev in workers:
                    continue
                profile = detect_disk_profile(root)
            except Exception as exc:
                logger.warning(f"無法偵測磁碟類型：{root}（{exc}）")
                continue
            workers[dev] = (profile.pass2_workers, profile.pass3_workers)
            labels.append((root.anchor.rstrip("\\") or str(root), profile.summary()))
        if not workers:
            return
        self.scanner.workers_by_dev = workers
        self.scanner.pass2_workers, self.scanner.pass3_workers = next(iter(workers.values()))
        summary = (labels[0][1] if len(labels) == 1
                   else " · ".join(f"{drive} {text}" for drive, text in labels))
        self.detected.emit(self.scan_id, summary)


class ScanController(QObject):
    started = pyqtSignal(object, bool)        # ScanOptions, 是否為繼續掃描
    progress = pyqtSignal(object)             # Progress
    disk_detected = pyqtSignal(str)
    paused_changed = pyqtSignal(bool)
    finished = pyqtSignal(list, object)       # 群組, ScanSummary
    stopped = pyqtSignal()
    failed = pyqtSignal(str, str)             # 訊息, 詳細內容（呼叫堆疊）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scan_id = 0
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._pending: tuple[ScanOptions, bool] | None = None
        self._job: scan_job.ScanJob | None = None
        self._job_dirty = False
        self._options: ScanOptions | None = None
        self._t0 = 0.0
        self._paused_at = 0.0
        self._paused_total = 0.0
        self._state = "idle"                  # idle／running／stopping
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(SAVE_JOB_INTERVAL_MS)
        self._save_timer.timeout.connect(self._save_job)

    # ── 狀態 ──────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        """有掃描正在進行（含暫停中、等待上一次結束中）。"""
        return self._state == "running" or self._pending is not None

    @property
    def busy(self) -> bool:
        """背景執行緒還沒結束（停止中也算）。"""
        return self._thread is not None or self._pending is not None

    @property
    def paused(self) -> bool:
        return self._worker is not None and self._worker.scanner.is_paused

    @property
    def options(self) -> ScanOptions | None:
        return self._options

    # ── 控制 ──────────────────────────────────────────────────────────
    def start(self, options: ScanOptions, resumed: bool = False):
        """開始掃描；上一次的掃描還在收尾時，等它結束再開始。"""
        self._options = options
        if self._thread is not None:
            self._pending = (options, resumed)
            if self._worker is not None:
                self._worker.scanner.stop()
            self.started.emit(options, resumed)
            return
        self._launch(options, resumed)

    def _launch(self, options: ScanOptions, resumed: bool):
        self._scan_id += 1
        self._state = "running"
        self._t0 = time.monotonic()
        self._paused_total = 0.0
        self._paused_at = 0.0
        self._job = scan_job.ScanJob(options)
        self._job_dirty = True
        self._save_job()
        self._save_timer.start()
        logger.info(("繼續掃描" if resumed else "開始掃描") + f" — {options.describe()}")

        thread = QThread()
        worker = _Worker(self._scan_id, options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.detected.connect(self._on_detected)
        worker.finished.connect(self._on_finished)
        worker.cancelled.connect(self._on_cancelled)
        worker.failed.connect(self._on_failed)
        # 直接在背景執行緒呼叫 quit（執行緒安全）：主執行緒在 shutdown() 等待時也能結束
        worker.ended.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._on_thread_finished)
        self._thread, self._worker = thread, worker
        self.started.emit(options, resumed)
        thread.start()

    def pause(self):
        if self._state == "running" and self._worker and not self._worker.scanner.is_paused:
            self._worker.scanner.pause()
            self._paused_at = time.monotonic()
            logger.info("暫停掃描")
            self.paused_changed.emit(True)

    def resume(self):
        if self._worker and self._worker.scanner.is_paused:
            self._worker.scanner.resume()
            if self._paused_at:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = 0.0
            logger.info("繼續掃描（解除暫停）")
            self.paused_changed.emit(False)

    def stop(self):
        """停止掃描並保留續掃紀錄（stopped 會在背景執行緒真的停下後發出）。"""
        if self._pending is not None:
            self._pending = None
            self._mark_job("stopped")
            self.stopped.emit()
            return
        if self._state != "running" or self._worker is None:
            return
        self._state = "stopping"
        self._mark_job("stopped")
        self._worker.scanner.stop()

    def shutdown(self, wait_ms: int = 3000) -> bool:
        """關閉視窗時呼叫：停止掃描、保留續掃紀錄，最多等 wait_ms；回傳執行緒是否已結束。"""
        self._pending = None
        if self._thread is None:
            return True
        if self._state == "running":
            logger.info("關閉視窗時停止掃描（下次可以繼續）")
        self._state = "stopping"
        self._mark_job("stopped")
        if self._worker is not None:
            self._worker.scanner.stop()
        thread = self._thread
        if thread.wait(wait_ms):
            return True
        logger.warning("掃描執行緒沒有及時結束（可能正在等待很慢的磁碟）")
        _UNFINISHED.append((thread, self._worker))
        return False

    # ── 續掃紀錄 ──────────────────────────────────────────────────────
    def _save_job(self):
        if self._job is None or not self._job_dirty:
            return
        self._job_dirty = False
        try:
            scan_job.save_job(self._job)
        except OSError as exc:
            logger.warning(f"無法寫入續掃紀錄：{exc}")

    def _mark_job(self, state: str, error: str = ""):
        self._save_timer.stop()
        if self._job is None:
            return
        self._job.state = state
        self._job.error = error
        self._job_dirty = True
        self._save_job()

    # ── 背景執行緒的回報 ──────────────────────────────────────────────
    def _current(self, scan_id: int) -> bool:
        return scan_id == self._scan_id and self._state != "idle"

    def _on_progress(self, scan_id: int, progress: Progress):
        if not self._current(scan_id) or self._state == "stopping":
            return
        if self._job is not None:
            fraction = progress.fraction
            self._job.step = progress.step
            self._job.step_progress = fraction if fraction is not None else 0.0
            self._job_dirty = True
        self.progress.emit(progress)

    def _on_detected(self, scan_id: int, summary: str):
        if self._current(scan_id):
            logger.info(f"磁碟：{summary}")
            self.disk_detected.emit(summary)

    def _elapsed(self) -> float:
        paused = self._paused_total + (time.monotonic() - self._paused_at if self._paused_at else 0)
        return max(time.monotonic() - self._t0 - paused, 0.0)

    def _on_finished(self, scan_id: int, groups: list, stats: ScanStats):
        if not self._current(scan_id):
            return
        self._save_timer.stop()
        self._state = "idle"
        self._job = None
        try:
            scan_job.clear_job()
        except OSError as exc:
            logger.warning(f"無法清除續掃紀錄：{exc}")
        summary = ScanSummary(self._options, datetime.now().isoformat(timespec="seconds"),
                              self._elapsed(), stats)
        wasted = sum(g.wasted_bytes for g in groups)
        logger.info(f"掃描完成 — {len(groups):,} 組重複，最多可釋放 {wasted:,} bytes，"
                    f"費時 {summary.elapsed:.1f} 秒")
        logger.info(f"  檢查 {stats.files_seen:,} 個檔案、候選 {stats.candidates:,}、"
                    f"快取沿用 開頭 {stats.cache_hits_partial:,}／完整 {stats.cache_hits_full:,}、"
                    f"略過 連結 {stats.skipped_links:,}／隱藏 {stats.skipped_hidden:,}／"
                    f"雲端 {stats.skipped_cloud:,}／排除 {stats.skipped_excluded:,}／"
                    f"寫入中 {stats.skipped_changing:,}")
        if stats.cache_note:
            logger.warning(f"  快取：{stats.cache_note}")
        if stats.unreadable:
            logger.warning(f"  無法讀取 {stats.unreadable:,} 個：")
            for sample in stats.unreadable_samples[:50]:
                logger.warning(f"    {sample}")
        self.finished.emit(groups, summary)

    def _on_cancelled(self, scan_id: int):
        if scan_id != self._scan_id:
            return
        self._save_timer.stop()
        if self._pending is None and self._state != "idle":
            self._state = "idle"
            logger.info(f"已停止掃描（{STEP_NAMES.get(self._job.step, '') if self._job else ''}）")
            self.stopped.emit()

    def _on_failed(self, scan_id: int, message: str, details: str):
        if not self._current(scan_id):
            return
        self._state = "idle"
        self._mark_job("failed", message)
        logger.error(f"掃描時發生錯誤：{message}\n{details}")
        self.failed.emit(message, details)

    def _on_thread_finished(self):
        thread, worker = self._thread, self._worker
        self._thread = self._worker = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        if self._state == "stopping":          # 停止後沒有收到 cancelled（例如剛好完成）
            self._state = "idle"
            self.stopped.emit()
        if self._pending is not None:
            options, resumed = self._pending
            self._pending = None
            self._launch(options, resumed)


def wait_unfinished(timeout_ms: int = 5000) -> bool:
    """程式結束前等還沒結束的掃描執行緒；回傳是否都結束了。"""
    deadline = time.monotonic() + timeout_ms / 1000
    for thread, _ in list(_UNFINISHED):
        left = int(max(deadline - time.monotonic(), 0) * 1000)
        if not thread.wait(left):
            return False
    _UNFINISHED.clear()
    return True
