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
#
# 暫停／停止：讀檔迴圈每讀一個區塊就經過一次暫停點（_checkpoint），worker 執行緒也一樣，
# 所以暫停會真的停住磁碟讀取，停止也不必等目前這個大檔讀完。

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

try:
    import xxhash
    _FAST_HASH = True
except ImportError:
    # 未安裝 xxhash 時退回 hashlib.blake2b（Python 內建，較慢但不需額外安裝）
    _FAST_HASH = False

from .hash_cache import HashCache
from .models import DuplicateGroup, FileInfo

HASH_ALGO     = "xxh64" if _FAST_HASH else "blake2b"   # 寫進快取，換演算法時舊紀錄自動作廢
PARTIAL_READ  = 4096    # Pass 2 每個檔案讀取的前綴長度（bytes）
CHUNK_SIZE    = 131072  # Pass 3 每次 read() 的 chunk 大小（128 KB，減少系統呼叫次數）
EMIT_INTERVAL = 0.15    # 進度 signal 最小發送間隔（秒），避免 Qt 事件佇列積壓
IN_FLIGHT_PER_WORKER = 4   # 平行模式下每個執行緒最多預先排入的工作數
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
# 暫停點：讀檔函式每讀一個區塊呼叫一次，參數是剛讀到的位元組數
Checkpoint = Callable[[int], None]


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
    skipped_changing: int = 0   # 掃描過程中大小改變（正在寫入）的檔案
    cache_hits_partial: int = 0  # 前段 hash 直接沿用快取的檔案數
    cache_hits_full:    int = 0  # 完整 hash 直接沿用快取的檔案數（省下整檔讀取）
    unreadable:       int = 0   # 無法讀取的檔案與資料夾
    unreadable_samples: list[str] = field(default_factory=list)

    def add_unreadable(self, path, exc: OSError):
        self.unreadable += 1
        if len(self.unreadable_samples) < MAX_ERROR_SAMPLES:
            reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
            self.unreadable_samples.append(f"{path}（{reason}）")


class _Stopped(Exception):
    """使用者停止掃描時，用來從深層迴圈（含 worker 執行緒）直接跳出。"""


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


def _format_bytes(n: float) -> str:
    """進度訊息用的簡短容量字串（不依賴 humanize）。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _new_hasher(partial: bool):
    if _FAST_HASH:
        return xxhash.xxh64()
    import hashlib
    return hashlib.blake2b(digest_size=8 if partial else 32)


def _partial_hash(path: Path, checkpoint: Checkpoint | None = None) -> str:
    """讀取檔案前 PARTIAL_READ bytes 並計算 hash；讀取失敗時拋出 OSError。
    可在 worker 執行緒執行（無共用狀態）；checkpoint 在讀檔前呼叫，暫停時會在此阻塞。
    """
    if checkpoint:
        checkpoint(0)
    with open(path, "rb") as f:
        data = f.read(PARTIAL_READ)
    h = _new_hasher(partial=True)
    h.update(data)
    return h.hexdigest()


def _full_hash(path: Path, checkpoint: Checkpoint | None = None) -> str:
    """讀取整個檔案計算完整 hash；讀取失敗時拋出 OSError。
    每讀一個區塊就呼叫 checkpoint：暫停時阻塞、停止時拋出 _Stopped，大檔也能立刻中斷。
    """
    h = _new_hasher(partial=False)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            if checkpoint:
                checkpoint(len(chunk))
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
    stop() / pause() / resume() 可從任意執行緒安全呼叫，也可以在 scan() 開始前呼叫。

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
        cache: HashCache | None = None,   # 雜湊快取；None 表示每次都重新讀檔
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
        self.cache          = cache
        self.stats          = ScanStats()

        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()   # 初始為「執行中」狀態（Event set = 不阻塞）

        # 目前這個比對階段的進度（worker 執行緒會累加 _bytes_done，以 lock 保護）
        self._lock        = threading.Lock()
        self._bytes_done  = 0
        self._stage: tuple | None = None   # (step, label, total_files, total_bytes, t0)
        self._files_done  = 0
        self._last_emit   = 0.0
        self._scan_thread = 0

    # ── 控制方法（可從任意執行緒安全呼叫）────────────────────────────
    def stop(self):
        self._stop_event.set()
        # 若目前處於暫停狀態，必須同時 set pause_event，
        # 否則阻塞在 _checkpoint() 的 wait() 永遠收不到 stop 訊號
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()   # clear → _checkpoint() 的 wait() 開始阻塞

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
        self._scan_thread = threading.get_ident()
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

    # ── 暫停點與進度 ───────────────────────────────────────────────────
    def _checkpoint(self, nbytes: int = 0):
        """暫停點（任何執行緒都可呼叫）：暫停時阻塞，停止時拋出 _Stopped。

        讀檔函式每讀一個區塊呼叫一次並帶入位元組數，用來估算大檔的進度；
        在 scan thread 上呼叫時順便發送進度（循序模式讀大檔時進度列也會動）。
        """
        if nbytes:
            with self._lock:
                self._bytes_done += nbytes
        if not self._pause_event.is_set():
            self._pause_event.wait()
        if self._stop_event.is_set():
            raise _Stopped
        if nbytes and threading.get_ident() == self._scan_thread:
            self._report()

    def _emit(self, msg: str, current: int, total: int,
              step: int, eta_secs: int = -1):
        """發送進度更新，EMIT_INTERVAL 節流由呼叫方負責，此處不做限制。"""
        if self.on_progress:
            self.on_progress(msg, current, total, step, TOTAL_STEPS, eta_secs)

    @staticmethod
    def _eta(start_time: float, done: float, total: float) -> int:
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

    def _report(self, force: bool = False):
        """依目前階段的進度發送訊息（時間節流）。Pass 3 以位元組估算 ETA，大檔才準。"""
        if self._stage is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < EMIT_INTERVAL:
            return
        self._last_emit = now
        step, label, total_files, total_bytes, t0 = self._stage
        done = self._files_done
        if step == 3 and total_bytes > 0:
            with self._lock:
                done_bytes = min(self._bytes_done, total_bytes)
            msg = (f"{label}... ({done:,}/{total_files:,} 個檔案・"
                   f"{_format_bytes(done_bytes)} / {_format_bytes(total_bytes)})")
            self._emit(msg, int(1000 * done_bytes / total_bytes), 1000, step,
                       self._eta(t0, done_bytes, total_bytes))
        else:
            self._emit(f"{label}... ({done:,}/{total_files:,})", done, total_files, step,
                       self._eta(t0, done, total_files))

    # ── Pass 1：遍歷 ───────────────────────────────────────────────────
    def _walk(self) -> Iterator[FileInfo]:
        """依序產生每個符合條件的檔案（FileInfo）。停止時拋出 _Stopped。"""
        stats = self.stats
        last_emit = time.monotonic()
        for root in self.roots:
            stack = [str(root)]
            while stack:
                self._checkpoint()
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
                            # Windows 上直接用目錄列舉時帶回的資料，不必每個檔案再開一次（實測快約 16 倍）；
                            # 少了的 st_ino / st_dev 只在合併硬連結時對候選檔另外查
                            st = entry.stat(follow_symlinks=False)
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
                            self._checkpoint()
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
                if checked % 256 == 0:
                    self._checkpoint()
                try:
                    st = os.stat(fi.path)
                except OSError as exc:
                    self.stats.add_unreadable(fi.path, exc)
                    continue
                if st.st_size != fi.size:          # 目錄列舉後大小又變了：正在寫入，不納入比對
                    self.stats.skipped_changing += 1
                    continue
                fi.mtime, fi.mtime_ns = st.st_mtime, st.st_mtime_ns
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

    # ── Pass 2 / Pass 3：雜湊比對 ──────────────────────────────────────
    def _hash_stage(self, files: list[FileInfo], fn, workers: int,
                    step: int, label: str) -> Iterator[tuple[FileInfo, str]]:
        """對每個檔案呼叫 fn(path, checkpoint)，依完成順序產生 (FileInfo, digest)。

        讀取失敗的檔案記入統計後略過；使用者停止時拋出 _Stopped。
        workers > 1 時以執行緒池並行，但同時排入的工作不超過 workers × IN_FLIGHT_PER_WORKER 個
        （不會一次建立數十萬個 future），暫停／停止由 worker 在讀檔區塊之間自行檢查。
        """
        self._stage = (step, label, len(files), sum(fi.size for fi in files), time.monotonic())
        self._files_done = 0
        with self._lock:
            self._bytes_done = 0
        self._report(force=True)

        if workers <= 1:
            for fi in files:
                self._checkpoint()
                try:
                    digest = fn(fi.path, self._checkpoint)
                except OSError as exc:
                    self.stats.add_unreadable(fi.path, exc)
                else:
                    yield fi, digest
                self._files_done += 1
                self._report()
            self._report(force=True)
            return

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dupescan-hash")
        pending: dict = {}
        queue = iter(files)
        completed = False

        def submit_next():
            fi = next(queue, None)
            if fi is not None:
                pending[pool.submit(fn, fi.path, self._checkpoint)] = fi

        try:
            for _ in range(workers * IN_FLIGHT_PER_WORKER):
                submit_next()
            while pending:
                # 逾時醒來發送進度：單一大檔要讀很久時進度列也會動
                finished, _ = wait(pending, timeout=EMIT_INTERVAL, return_when=FIRST_COMPLETED)
                for fut in finished:
                    fi = pending.pop(fut)
                    try:
                        digest = fut.result()
                    except OSError as exc:
                        self.stats.add_unreadable(fi.path, exc)
                    else:
                        yield fi, digest
                    self._files_done += 1
                    submit_next()
                self._checkpoint()
                self._report()
            completed = True
            self._report(force=True)
        finally:
            if not completed:
                # 停止或發生例外：讓執行中的 worker 在下一個讀檔區塊就結束
                self._stop_event.set()
                self._pause_event.set()
                for fut in pending:
                    fut.cancel()
            pool.shutdown(wait=True)

    # ── 3-Pass 主流程 ──────────────────────────────────────────────────
    def _run(self) -> list[DuplicateGroup] | None:
        """執行 3-pass 演算法；使用者停止時回傳 None。"""
        if self.cache is not None:
            self.cache.open()
        try:
            return self._run_passes()
        except _Stopped:
            return None
        finally:
            self._stage = None
            if self.cache is not None:
                self.cache.close()          # 取消時也會把已算好的 hash 存起來，下次接著用

    def _run_passes(self) -> list[DuplicateGroup]:
        # ── Pass 1：遍歷目錄，依檔案大小分組 ──────────────────────────
        self._emit("掃描檔案中...", 0, 0, 1)
        by_size: dict[int, list[FileInfo]] = defaultdict(list)
        for fi in self._walk():
            by_size[fi.size].append(fi)
        # 只保留大小有重複的群組，其他不可能是重複檔案；再合併同一實體檔的路徑
        candidates = self._merge_same_files({s: f for s, f in by_size.items() if len(f) > 1})
        by_size.clear()
        flat = [fi for files in candidates.values() for fi in files]
        self.stats.candidates = len(flat)
        scanned = self.stats.files_seen
        self._emit(
            f"大小分組完成 — 共掃描 {scanned:,} 個檔案，{len(flat):,} 個候選",
            scanned, scanned, 1, 0,
        )
        cached = self.cache.lookup(flat, HASH_ALGO, PARTIAL_READ) if self.cache else {}

        # ── Pass 2：前 4 KB 快速 Hash ────────────────────────────────
        # 以「大小＋前段 hash」分組：大小相同但前 4 KB 不同的一定不是重複，
        # 大小不同但開頭相同的檔案也不會被送去完整比對
        by_partial: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
        todo = []
        for fi in flat:
            hit = cached.get(id(fi), (None, None))[0]
            if hit:
                by_partial[(fi.size, hit)].append(fi)
                self.stats.cache_hits_partial += 1
            else:
                todo.append(fi)
        for fi, digest in self._hash_stage(todo, _partial_hash, self.pass2_workers,
                                           2, "快速比對中"):
            by_partial[(fi.size, digest)].append(fi)
            if self.cache:
                self.cache.store(fi, HASH_ALGO, partial=digest, partial_len=PARTIAL_READ)
        flat2 = [fi for files in by_partial.values() if len(files) > 1 for fi in files]
        self._emit(f"快速比對完成 — {len(flat2):,} 個檔案需完整比對",
                   len(flat), len(flat), 2, 0)

        # ── Pass 3：完整 Hash ────────────────────────────────────────
        by_full: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
        todo = []
        for fi in flat2:
            hit = cached.get(id(fi), (None, None))[1]
            if hit:
                fi.hash_full = hit
                by_full[(fi.size, hit)].append(fi)
                self.stats.cache_hits_full += 1
            else:
                todo.append(fi)
        for fi, digest in self._hash_stage(todo, _full_hash, self.pass3_workers,
                                           3, "完整比對中"):
            fi.hash_full = digest
            by_full[(fi.size, digest)].append(fi)
            if self.cache:
                self.cache.store(fi, HASH_ALGO, full=digest)

        # 建立結果群組：群組內依路徑排序（平手規則的結果才會固定），
        # 群組依可釋放空間由大到小排序（最值得刪除的排最前面）
        groups: list[DuplicateGroup] = []
        for (size, h), files in by_full.items():
            if len(files) > 1:
                files.sort(key=_path_key)
                groups.append(DuplicateGroup(hash_value=h, size=size, files=files))
        groups.sort(key=lambda g: (-g.wasted_bytes, _path_key(g.files[0])))
        return groups
