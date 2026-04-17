# 掃描引擎：3-Pass 重複檔案偵測，支援暫停/繼續/停止與 ETA 估算。
#
# Pass 1  依檔案大小分組       — 純 stat()，無磁碟讀取
# Pass 2  前 4 KB 快速 Hash   — 過濾約 90% 的偽候選
# Pass 3  完整 xxHash 比對    — 只對 Pass 2 剩餘的候選執行
#
# Pass 2 / Pass 3 支援多執行緒（由 disk_detect.DiskProfile 提供執行緒數）：
#   SSD/NVMe → ThreadPoolExecutor 並行 I/O，大幅縮短比對時間
#   HDD      → 單執行緒循序讀取，避免磁頭隨機尋軌造成更大損耗

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

try:
    import xxhash
    _FAST_HASH = True
except ImportError:
    # 未安裝 xxhash 時退回 hashlib.blake2b（Python 內建，較慢但不需額外安裝）
    _FAST_HASH = False

from .models import DuplicateGroup, FileInfo

PARTIAL_READ  = 4096    # Pass 2 每個檔案讀取的前綴長度（bytes）
CHUNK_SIZE    = 131072  # Pass 3 每次 read() 的 chunk 大小（128 KB，減少系統呼叫次數）
EMIT_INTERVAL = 0.15    # 進度 signal 最小發送間隔（秒），避免 Qt 事件佇列積壓
TOTAL_STEPS   = 3
STEP_NAMES    = {
    1: "依檔案大小分組",
    2: "快速 Hash 比對（前 4 KB）",
    3: "完整 Hash 比對",
}

# on_progress 回調簽名：(訊息, 目前進度, 總數, 步驟號, 總步驟數, 預估剩餘秒數)
# eta_secs = -1 表示尚無法估算
ProgressCallback = Callable[[str, int, int, int, int, int], None]


def _format_eta(seconds: int) -> str:
    """將秒數格式化為中文可讀時間字串，供進度列顯示。"""
    if seconds < 0:
        return "計算中..."
    if seconds < 5:
        return "即將完成"
    if seconds < 60:
        return f"約 {seconds} 秒"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"約 {m} 分 {s} 秒" if s else f"約 {m} 分鐘"
    h, m2 = divmod(m, 60)
    return f"約 {h} 小時 {m2} 分"


def _partial_hash(path: Path) -> str | None:
    """讀取檔案前 PARTIAL_READ bytes 並計算 hash，IO 失敗回傳 None。
    此函式在 ThreadPoolExecutor worker thread 中執行，設計為純函式（無共用狀態）。
    """
    try:
        with open(path, "rb") as f:
            data = f.read(PARTIAL_READ)
        if _FAST_HASH:
            return xxhash.xxh64(data).hexdigest()
        return __import__("hashlib").blake2b(data, digest_size=8).hexdigest()
    except (OSError, PermissionError):
        return None


def _full_hash(path: Path) -> str | None:
    """讀取整個檔案計算完整 hash，IO 失敗回傳 None。
    此函式在 ThreadPoolExecutor worker thread 中執行，設計為純函式（無共用狀態）。
    """
    try:
        if _FAST_HASH:
            h = xxhash.xxh64()
        else:
            h = __import__("hashlib").blake2b(digest_size=32)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


class Scanner:
    """3-Pass 重複檔案掃描引擎，執行緒安全。

    應在 worker thread 呼叫 scan()；
    stop() / pause() / resume() 可從任意執行緒安全呼叫。

    pass2_workers / pass3_workers > 1 啟用並行 I/O（適合 SSD/NVMe）。
    HDD 請保持 workers=1 以維持循序讀取。
    """

    def __init__(
        self,
        roots: list[str],
        min_size: int = 1,
        include_hidden: bool = False,
        pass2_workers: int = 1,   # Pass 2 執行緒數（由 DiskProfile 提供）
        pass3_workers: int = 1,   # Pass 3 執行緒數（由 DiskProfile 提供）
        on_progress: ProgressCallback | None = None,
        on_done: Callable[[list[DuplicateGroup]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.roots          = [Path(r) for r in roots]
        self.min_size       = min_size
        self.include_hidden = include_hidden
        self.pass2_workers  = pass2_workers
        self.pass3_workers  = pass3_workers
        self.on_progress    = on_progress
        self.on_done        = on_done
        self.on_error       = on_error

        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()   # 初始為「執行中」狀態（Event set = 不阻塞）

    # ── 控制方法（可從任意執行緒安全呼叫）────────────────────────────
    def stop(self):
        self._stop_event.set()
        # 若目前處於暫停狀態，必須同時 set pause_event，
        # 否則 scan thread 永遠阻塞在 _check() 的 wait() 而無法收到 stop 訊號
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()   # clear → _check() 的 wait() 開始阻塞

    def resume(self):
        self._pause_event.set()     # set → 解除阻塞，繼續掃描

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    # ── 入口 ──────────────────────────────────────────────────────────
    def scan(self) -> list[DuplicateGroup]:
        """執行掃描，完成後呼叫 on_done；例外時呼叫 on_error。"""
        try:
            groups = self._run()
            if self.on_done:
                self.on_done(groups)
            return groups
        except Exception as exc:
            if self.on_error:
                self.on_error(str(exc))
            return []

    # ── 內部工具 ───────────────────────────────────────────────────────
    def _check(self) -> bool:
        """循序模式的暫停點：若已暫停則在此阻塞，回傳 True 表示應停止掃描。
        只能在 scan thread 呼叫（不適合在 worker thread 內使用）。
        """
        self._pause_event.wait()
        return self._stop_event.is_set()

    def _stopped(self) -> bool:
        """非阻塞的停止查詢，供並行消費迴圈在 as_completed 之後快速檢查。"""
        return self._stop_event.is_set()

    def _emit(self, msg: str, current: int, total: int,
              step: int, eta_secs: int = -1):
        """發送進度更新，EMIT_INTERVAL 節流由呼叫方負責，此處不做限制。"""
        if self.on_progress:
            self.on_progress(msg, current, total, step, TOTAL_STEPS, eta_secs)

    @staticmethod
    def _eta(start_time: float, done: int, total: int) -> int:
        """依已耗時間與完成比例線性估算剩餘秒數。
        開始不足 0.5 秒時樣本太少，回傳 -1（顯示「計算中」）。
        """
        if done <= 0 or total <= 0:
            return -1
        elapsed = time.monotonic() - start_time
        if elapsed < 0.5:
            return -1
        rate = done / elapsed
        return int((total - done) / rate)

    # ── Pass 2 路徑選擇 ────────────────────────────────────────────────
    def _pass2_sequential(self, candidates, total_c, t0) -> dict | None:
        """HDD 安全的循序 Pass 2：細粒度暫停/停止，維持順序讀取。
        回傳 by_partial dict；若使用者停止則回傳 None。
        """
        by_partial: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        last_emit = t0

        for files in candidates.values():
            for fi in files:
                if self._check():
                    return None     # 使用者已停止，中斷掃描
                ph = _partial_hash(fi.path)
                if ph:
                    by_partial[ph].append(fi)
                done += 1
                now = time.monotonic()
                if now - last_emit >= EMIT_INTERVAL or done == total_c:
                    self._emit(f"快速比對中... ({done:,}/{total_c:,})",
                               done, total_c, 2, self._eta(t0, done, total_c))
                    last_emit = now

        return by_partial

    def _pass2_parallel(self, candidates, total_c, t0, workers) -> dict | None:
        """SSD/NVMe 並行 Pass 2：ThreadPoolExecutor 同時發出多個讀取請求。

        執行緒安全說明：
        - worker thread 只執行 _partial_hash（純函式，只讀磁碟回傳字串）
        - 所有 dict/list 操作在本方法的 as_completed 迴圈中循序執行（scan thread）
        - 無共用可變狀態，不需要 lock

        暫停：在消費迴圈呼叫 _pause_event.wait()，已提交的 future 繼續跑完
        停止：呼叫 f.cancel() 嘗試取消尚未開始的 future，已在跑的等完即捨棄
        """
        by_partial: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        last_emit = t0
        # 展平成一維 list，方便一次性提交給 executor
        flat = [fi for files in candidates.values() for fi in files]

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_partial_hash, fi.path): fi for fi in flat}
            for fut in as_completed(futs):
                self._pause_event.wait()    # 暫停時在此阻塞，等待 resume()
                if self._stopped():
                    for f in futs:
                        f.cancel()          # 嘗試取消尚未開始的 future
                    return None
                fi = futs[fut]
                ph = fut.result()
                if ph:
                    by_partial[ph].append(fi)
                done += 1
                now = time.monotonic()
                if now - last_emit >= EMIT_INTERVAL or done == total_c:
                    self._emit(f"快速比對中... ({done:,}/{total_c:,})",
                               done, total_c, 2, self._eta(t0, done, total_c))
                    last_emit = now

        return by_partial

    # ── Pass 3 路徑選擇 ────────────────────────────────────────────────
    def _pass3_sequential(self, candidates2, total2, t0) -> dict | None:
        """HDD 安全的循序 Pass 3。"""
        by_full: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        last_emit = t0

        for files in candidates2.values():
            for fi in files:
                if self._check():
                    return None
                fh = _full_hash(fi.path)
                if fh:
                    fi.hash_full = fh
                    by_full[fh].append(fi)
                done += 1
                now = time.monotonic()
                if now - last_emit >= EMIT_INTERVAL or done == total2:
                    eta = self._eta(t0, done, total2) if total2 > 0 else 0
                    self._emit(f"完整比對中... ({done:,}/{total2:,})",
                               done, total2, 3, eta)
                    last_emit = now

        return by_full

    def _pass3_parallel(self, candidates2, total2, t0, workers) -> dict | None:
        """SSD/NVMe 並行 Pass 3，執行緒安全說明同 _pass2_parallel。"""
        by_full: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        last_emit = t0
        flat = [fi for files in candidates2.values() for fi in files]

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_full_hash, fi.path): fi for fi in flat}
            for fut in as_completed(futs):
                self._pause_event.wait()
                if self._stopped():
                    for f in futs:
                        f.cancel()
                    return None
                fi = futs[fut]
                fh = fut.result()
                if fh:
                    fi.hash_full = fh
                    by_full[fh].append(fi)
                done += 1
                now = time.monotonic()
                if now - last_emit >= EMIT_INTERVAL or done == total2:
                    eta = self._eta(t0, done, total2) if total2 > 0 else 0
                    self._emit(f"完整比對中... ({done:,}/{total2:,})",
                               done, total2, 3, eta)
                    last_emit = now

        return by_full

    # ── 3-Pass 主流程 ──────────────────────────────────────────────────
    def _run(self) -> list[DuplicateGroup]:

        # ── Pass 1：遍歷目錄，依檔案大小分組 ──────────────────────────
        # 只呼叫 stat()，無 read()，速度極快
        self._emit("掃描檔案中...", 0, 0, 1)
        by_size: dict[int, list[FileInfo]] = defaultdict(list)
        scanned = 0
        t0 = time.monotonic()

        for root in self.roots:
            # followlinks=False：避免 symlink 循環導致無限遍歷
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                if self._check():
                    return []
                if not self.include_hidden:
                    # 原地修改 dirnames 讓 os.walk 跳過隱藏子目錄
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]

                for fname in filenames:
                    if self._check():
                        return []
                    if not self.include_hidden and fname.startswith("."):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        stat = fpath.stat()
                        if stat.st_size < self.min_size:
                            continue
                        by_size[stat.st_size].append(
                            FileInfo(path=fpath, size=stat.st_size, mtime=stat.st_mtime)
                        )
                        scanned += 1
                        if scanned % 300 == 0:
                            self._emit(
                                f"掃描中... 已找到 {scanned:,} 個檔案",
                                scanned, 0, 1, -1,
                            )
                    except (OSError, PermissionError):
                        continue    # 無權限或檔案已不存在，跳過

        # 只保留大小有重複的群組，其他不可能是重複檔案
        candidates = {s: f for s, f in by_size.items() if len(f) > 1}
        total_c = sum(len(v) for v in candidates.values())
        self._emit(
            f"大小分組完成 — 共掃描 {scanned:,} 個檔案，{total_c:,} 個候選",
            scanned, scanned, 1, 0,
        )

        # ── Pass 2：前 4 KB 快速 Hash（循序 or 並行）─────────────────
        t0 = time.monotonic()
        if self.pass2_workers > 1:
            by_partial = self._pass2_parallel(candidates, total_c, t0, self.pass2_workers)
        else:
            by_partial = self._pass2_sequential(candidates, total_c, t0)

        if by_partial is None:
            return []   # 使用者中止

        # 前 4 KB hash 不同的一定不是重複，直接刪除
        candidates2 = {ph: f for ph, f in by_partial.items() if len(f) > 1}
        total2 = sum(len(v) for v in candidates2.values())
        self._emit(
            f"快速比對完成 — {total2:,} 個檔案需完整比對",
            total_c, total_c, 2, 0,
        )

        # ── Pass 3：完整 Hash（循序 or 並行）─────────────────────────
        t0 = time.monotonic()
        if self.pass3_workers > 1:
            by_full = self._pass3_parallel(candidates2, total2, t0, self.pass3_workers)
        else:
            by_full = self._pass3_sequential(candidates2, total2, t0)

        if by_full is None:
            return []   # 使用者中止

        # 建立結果群組，依可釋放空間由大到小排序（最值得刪除的排最前面）
        groups: list[DuplicateGroup] = [
            DuplicateGroup(hash_value=h, size=files[0].size, files=files)
            for h, files in by_full.items()
            if len(files) > 1
        ]
        groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
        return groups
