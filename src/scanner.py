# 掃描引擎：3-Pass 重複檔案偵測，支援暫停/繼續/停止與 ETA 估算。
#
# Pass 1  遍歷目錄、依檔案大小分組   — 不讀檔案內容
#         └ 大小相同的候選再查檔案 ID，把「同一個實體檔」的多條路徑（硬連結）合併成一筆
# Pass 2  前 4 KB 快速 Hash         — 過濾約 90% 的偽候選
# Pass 3  完整 xxHash 比對          — 只對 Pass 2 剩餘的候選執行
#
# 遍歷時不跟隨符號連結與 junction（目錄連接點／磁碟區掛載點），並一律排除資源回收筒、
# System Volume Information；預設也略過隱藏／系統檔與雲端佔位檔（OneDrive 未下載的檔案，
# 讀取會觸發下載）。否則同一個檔案可能經由兩條路徑被列成「重複」，刪任一份就等於刪掉唯一的一份。
#
# Pass 2 / Pass 3 支援多執行緒（由 disk_detect.DiskProfile 提供執行緒數）：
#   SSD/NVMe → ThreadPoolExecutor 並行 I/O，大幅縮短比對時間
#   HDD      → 單執行緒循序讀取，避免磁頭隨機尋軌造成更大損耗

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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

# Windows 檔案屬性（stat_result.st_file_attributes）
_ATTR_HIDDEN                = 0x2
_ATTR_SYSTEM                = 0x4
_ATTR_OFFLINE               = 0x1000
_ATTR_RECALL_ON_OPEN        = 0x40000
_ATTR_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ATTRS = _ATTR_OFFLINE | _ATTR_RECALL_ON_OPEN | _ATTR_RECALL_ON_DATA_ACCESS
_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # junction 與磁碟區掛載點共用的 reparse tag

# 不論設定都不掃描的資料夾名稱（比對時不分大小寫）
EXCLUDED_DIR_NAMES = frozenset({
    "$recycle.bin", "recycler", "system volume information", ".dupescan_quarantine",
})

MAX_ERROR_SAMPLES = 200   # 無法讀取的檔案最多保留幾筆明細供 UI 顯示

# on_progress 回調簽名：(訊息, 目前進度, 總數, 步驟號, 總步驟數, 預估剩餘秒數)
# eta_secs = -1 表示尚無法估算
ProgressCallback = Callable[[str, int, int, int, int, int], None]


@dataclass
class ScanStats:
    """掃描過程的統計，掃描完成時隨結果一起交給 on_done。"""
    files_seen:       int = 0   # 符合大小條件、納入比對的檔案數
    candidates:       int = 0   # 大小有重複的候選數（已合併同一實體檔）
    same_file_merged: int = 0   # 因指向同一個實體檔（硬連結）而合併的路徑數
    skipped_links:    int = 0   # 略過的符號連結與 junction
    skipped_hidden:   int = 0   # 略過的隱藏／系統檔案與資料夾
    skipped_cloud:    int = 0   # 略過的雲端佔位檔
    skipped_excluded: int = 0   # 略過的排除資料夾（資源回收筒等）
    unreadable:       int = 0   # 無法讀取的檔案與資料夾
    unreadable_samples: list[str] = field(default_factory=list)

    def add_unreadable(self, path, exc: OSError):
        self.unreadable += 1
        if len(self.unreadable_samples) < MAX_ERROR_SAMPLES:
            reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
            self.unreadable_samples.append(f"{path}（{reason}）")


class _Stopped(Exception):
    """使用者停止掃描時，用來從深層迴圈直接跳出。"""


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


def _partial_hash(path: Path) -> str:
    """讀取檔案前 PARTIAL_READ bytes 並計算 hash；讀取失敗時拋出 OSError。
    此函式在 ThreadPoolExecutor worker thread 中執行，設計為純函式（無共用狀態）。
    """
    with open(path, "rb") as f:
        data = f.read(PARTIAL_READ)
    if _FAST_HASH:
        return xxhash.xxh64(data).hexdigest()
    return __import__("hashlib").blake2b(data, digest_size=8).hexdigest()


def _full_hash(path: Path) -> str:
    """讀取整個檔案計算完整 hash；讀取失敗時拋出 OSError。
    此函式在 ThreadPoolExecutor worker thread 中執行，設計為純函式（無共用狀態）。
    """
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


def _is_junction(entry: os.DirEntry) -> bool:
    """junction／磁碟區掛載點（Python 不把它們當成符號連結，os.walk 也會走進去）。"""
    is_junction = getattr(entry, "is_junction", None)   # Python 3.12+
    if is_junction is not None:
        return is_junction()
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) == _REPARSE_TAG_MOUNT_POINT


def _is_hidden(name: str, attrs: int) -> bool:
    """「.」開頭的名稱，或帶有 Windows 隱藏／系統屬性。"""
    return name.startswith(".") or bool(attrs & (_ATTR_HIDDEN | _ATTR_SYSTEM))


def normalize_roots(roots) -> list[Path]:
    """轉成絕對路徑，並去掉重複或位於其他根目錄底下的路徑，避免同一批檔案被掃兩次。"""
    items = sorted({os.path.abspath(str(r)) for r in roots},
                   key=lambda p: len(os.path.normcase(p)))
    kept: list[str] = []
    for p in items:
        norm = os.path.normcase(p)
        if any(norm == os.path.normcase(k) or
               norm.startswith(os.path.normcase(k).rstrip("\\/") + os.sep) for k in kept):
            continue
        kept.append(p)
    return [Path(p) for p in kept]


def _path_key(fi: FileInfo) -> str:
    return str(fi.path).casefold()


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
        on_done: Callable[[list[DuplicateGroup], ScanStats], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ):
        self.roots          = normalize_roots(roots)
        self.min_size       = min_size
        self.include_hidden = include_hidden
        self.pass2_workers  = pass2_workers
        self.pass3_workers  = pass3_workers
        self.on_progress    = on_progress
        self.on_done        = on_done
        self.on_error       = on_error
        self.on_cancel      = on_cancel
        self.stats          = ScanStats()

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
    def scan(self) -> list[DuplicateGroup] | None:
        """執行掃描。

        完成 → 呼叫 on_done(groups, stats) 並回傳 groups；
        被停止 → 只呼叫 on_cancel() 並回傳 None（不會被當成「沒有重複」）；
        發生例外 → 呼叫 on_error(msg) 並回傳 None。
        """
        try:
            groups = self._run()
        except Exception as exc:
            if self.on_error:
                self.on_error(str(exc))
            return None
        if groups is None:
            if self.on_cancel:
                self.on_cancel()
            return None
        if self.on_done:
            self.on_done(groups, self.stats)
        return groups

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

    # ── Pass 1：遍歷 ───────────────────────────────────────────────────
    def _walk(self):
        """依序產生每個符合條件的檔案（FileInfo）。停止時拋出 _Stopped。"""
        stats = self.stats
        last_emit = time.monotonic()
        for root in self.roots:
            stack = [str(root)]
            while stack:
                if self._check():
                    raise _Stopped
                current = stack.pop()
                try:
                    it = os.scandir(current)
                except OSError as exc:
                    stats.add_unreadable(current, exc)
                    continue
                with it:
                    for entry in it:
                        name = entry.name
                        try:
                            if entry.is_symlink():
                                stats.skipped_links += 1
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if _is_junction(entry):
                                    stats.skipped_links += 1
                                elif name.casefold() in EXCLUDED_DIR_NAMES:
                                    stats.skipped_excluded += 1
                                elif not self.include_hidden and _is_hidden(
                                        name, getattr(entry.stat(follow_symlinks=False),
                                                      "st_file_attributes", 0)):
                                    stats.skipped_hidden += 1
                                else:
                                    stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            st = os.stat(entry.path)
                        except OSError as exc:
                            stats.add_unreadable(entry.path, exc)
                            continue

                        attrs = getattr(st, "st_file_attributes", 0)
                        if not self.include_hidden and _is_hidden(name, attrs):
                            stats.skipped_hidden += 1
                            continue
                        if attrs & _CLOUD_ATTRS:
                            stats.skipped_cloud += 1
                            continue
                        if st.st_size < self.min_size:
                            continue
                        stats.files_seen += 1
                        yield FileInfo(path=Path(entry.path), size=st.st_size,
                                       mtime=st.st_mtime, mtime_ns=st.st_mtime_ns)

                        if stats.files_seen % 512 == 0:
                            if self._check():
                                raise _Stopped
                            now = time.monotonic()
                            if now - last_emit >= EMIT_INTERVAL:
                                self._emit(f"掃描中... 已找到 {stats.files_seen:,} 個檔案",
                                           stats.files_seen, 0, 1, -1)
                                last_emit = now

    def _merge_same_files(self, candidates: dict[int, list[FileInfo]]) -> dict[int, list[FileInfo]]:
        """以檔案 ID（st_dev + st_ino）合併指向同一個實體檔的路徑（硬連結）。

        合併後的路徑記在代表檔的 aliases，不會被列為「重複」——刪掉其中一條路徑既不省空間，
        若是經由 junction 等方式重複看到，更可能刪掉唯一的一份。
        """
        merged: dict[int, list[FileInfo]] = {}
        checked = 0
        for size, files in candidates.items():
            files.sort(key=_path_key)          # 代表路徑固定取字母序最前者
            seen: dict[tuple[int, int], FileInfo] = {}
            unique: list[FileInfo] = []
            for fi in files:
                checked += 1
                if checked % 256 == 0 and self._check():
                    raise _Stopped
                try:
                    st = os.stat(fi.path)
                except OSError as exc:
                    self.stats.add_unreadable(fi.path, exc)
                    continue
                fi.dev, fi.ino = st.st_dev, st.st_ino
                key = (st.st_dev, st.st_ino)
                if st.st_ino and key in seen:      # ino 為 0 代表檔案系統不提供，不合併
                    seen[key].aliases.append(str(fi.path))
                    self.stats.same_file_merged += 1
                    continue
                seen[key] = fi
                unique.append(fi)
            if len(unique) > 1:
                merged[size] = unique
        return merged

    # ── Pass 2 路徑選擇 ────────────────────────────────────────────────
    def _pass2_sequential(self, candidates, total_c, t0) -> dict | None:
        """HDD 安全的循序 Pass 2：細粒度暫停/停止，維持順序讀取。
        回傳 by_partial dict；若使用者停止則回傳 None。
        """
        by_partial: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
        done = 0
        last_emit = t0

        for files in candidates.values():
            for fi in files:
                if self._check():
                    return None     # 使用者已停止，中斷掃描
                try:
                    by_partial[(fi.size, _partial_hash(fi.path))].append(fi)
                except OSError as exc:
                    self.stats.add_unreadable(fi.path, exc)
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
        by_partial: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
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
                try:
                    by_partial[(fi.size, fut.result())].append(fi)
                except OSError as exc:
                    self.stats.add_unreadable(fi.path, exc)
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
        by_full: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
        done = 0
        last_emit = t0

        for files in candidates2.values():
            for fi in files:
                if self._check():
                    return None
                try:
                    fh = _full_hash(fi.path)
                    fi.hash_full = fh
                    by_full[(fi.size, fh)].append(fi)
                except OSError as exc:
                    self.stats.add_unreadable(fi.path, exc)
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
        by_full: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
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
                try:
                    fh = fut.result()
                    fi.hash_full = fh
                    by_full[(fi.size, fh)].append(fi)
                except OSError as exc:
                    self.stats.add_unreadable(fi.path, exc)
                done += 1
                now = time.monotonic()
                if now - last_emit >= EMIT_INTERVAL or done == total2:
                    eta = self._eta(t0, done, total2) if total2 > 0 else 0
                    self._emit(f"完整比對中... ({done:,}/{total2:,})",
                               done, total2, 3, eta)
                    last_emit = now

        return by_full

    # ── 3-Pass 主流程 ──────────────────────────────────────────────────
    def _run(self) -> list[DuplicateGroup] | None:
        """執行 3-pass 演算法；使用者停止時回傳 None。"""

        # ── Pass 1：遍歷目錄，依檔案大小分組 ──────────────────────────
        self._emit("掃描檔案中...", 0, 0, 1)
        by_size: dict[int, list[FileInfo]] = defaultdict(list)
        try:
            for fi in self._walk():
                by_size[fi.size].append(fi)
            # 只保留大小有重複的群組，其他不可能是重複檔案；再合併同一實體檔的路徑
            candidates = self._merge_same_files(
                {s: f for s, f in by_size.items() if len(f) > 1})
        except _Stopped:
            return None
        by_size.clear()
        total_c = sum(len(v) for v in candidates.values())
        self.stats.candidates = total_c
        scanned = self.stats.files_seen
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
            return None   # 使用者中止

        # 大小相同但前 4 KB 不同的一定不是重複，直接刪除
        # （以「大小＋前段 hash」分組，避免不同大小但開頭相同的檔案被送去完整比對）
        candidates2 = {k: f for k, f in by_partial.items() if len(f) > 1}
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
            return None   # 使用者中止

        # 建立結果群組：群組內依路徑排序（平手規則的結果才會固定），
        # 群組依可釋放空間由大到小排序（最值得刪除的排最前面）
        groups: list[DuplicateGroup] = []
        for (size, h), files in by_full.items():
            if len(files) > 1:
                files.sort(key=_path_key)
                groups.append(DuplicateGroup(hash_value=h, size=size, files=files))
        groups.sort(key=lambda g: (-g.wasted_bytes, _path_key(g.files[0])))
        return groups
