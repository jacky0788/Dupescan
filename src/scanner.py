# 掃描引擎：找出內容完全相同的檔案，支援暫停／繼續／停止與 ETA 估算。
#
# 第 1 步  列出檔案、依大小分組   — 只用目錄列舉帶回的資訊，不讀內容
#          └ 大小相同的候選再查檔案 ID，把「同一個實體檔」的多條路徑（硬連結）合併成一筆
# 第 2 步  快速比對               — 比對開頭 4 KB；8 MB 以上的再比對結尾 4 KB
#          └ 4 KB 以下的小檔案，開頭就是全部內容，不必再讀第二次
# 第 3 步  比對完整內容           — 只對前兩步都相同的檔案整檔計算 xxh3_128
#
# 遍歷時不跟隨符號連結與 junction（目錄連接點／磁碟區掛載點），並一律排除資源回收筒、
# System Volume Information；其他略過規則由 ScanOptions 決定（見 scan_options.py）。
#
# 讀檔一律交給「每顆磁碟一個」的執行緒池：SSD／NVMe 平行讀取，HDD 單執行緒並依路徑順序讀
# （減少磁頭來回）；不同磁碟同時進行。讀檔迴圈每讀一個區塊就經過一次暫停點（_checkpoint），
# 所以暫停會真的停住磁碟讀取，停止也不必等目前這個大檔讀完。

import hashlib
import os
import threading
import time
import traceback
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

try:
    import xxhash
    _FAST_HASH = hasattr(xxhash, "xxh3_128")
except ImportError:
    # 未安裝 xxhash 時退回 hashlib.blake2b（Python 內建，較慢但不需額外安裝）
    _FAST_HASH = False

from .formatting import human_size
from .hash_cache import HashCache
from .models import DuplicateGroup, FileInfo
from .scan_options import ScanOptions

HEAD_READ     = 4096            # 快速比對：開頭讀多少（bytes）
TAIL_READ     = 4096            # 快速比對：結尾讀多少
TAIL_MIN_SIZE = 8 * 1024 * 1024 # 這麼大以上的檔案才另外比對結尾（整檔讀取的代價遠大於一次尋軌）
CHUNK_SIZE    = 1024 * 1024     # 整檔比對每次讀取的區塊大小
PARTIAL_READ  = HEAD_READ       # 舊名稱（相容）
# 寫進快取的演算法標記：換演算法或取樣長度時，舊紀錄自動作廢
HASH_ALGO     = (("xxh3_128" if _FAST_HASH else "blake2b128") + f"/h{HEAD_READ}/t{TAIL_READ}")
EMIT_INTERVAL = 0.15            # 進度 signal 最小發送間隔（秒），避免 Qt 事件佇列積壓
IN_FLIGHT_PER_WORKER = 4        # 每個執行緒最多預先排入的工作數
TOTAL_STEPS   = 3
STEP_NAMES    = {
    1: "列出檔案",
    2: "快速比對",
    3: "比對檔案內容",
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
    files_seen:       int = 0   # 符合條件、納入比對的檔案數
    candidates:       int = 0   # 大小有重複的候選數（已合併同一實體檔）
    same_file_merged: int = 0   # 因指向同一個實體檔（硬連結）而合併的路徑數
    skipped_links:    int = 0   # 略過的符號連結與 junction
    skipped_hidden:   int = 0   # 略過的隱藏／系統檔案與資料夾
    skipped_cloud:    int = 0   # 略過的雲端佔位檔
    skipped_excluded: int = 0   # 略過的排除資料夾（資源回收筒、系統資料夾、使用者排除的）
    skipped_changing: int = 0   # 掃描過程中大小改變（正在寫入）的檔案
    small_files:      int = 0   # 4 KB 以下、不必整檔讀第二次的檔案
    tail_checked:     int = 0   # 另外比對結尾的大檔
    cache_hits_partial: int = 0  # 開頭 hash 直接沿用快取的檔案數
    cache_hits_full:    int = 0  # 完整 hash 直接沿用快取的檔案數（省下整檔讀取）
    cache_note:       str = ""  # 快取發生的狀況（損壞已重建、被佔用暫不使用…），給 UI 顯示
    unreadable:       int = 0   # 無法讀取的檔案與資料夾
    unreadable_samples: list[str] = field(default_factory=list)

    def add_unreadable(self, path, exc: OSError):
        self.unreadable += 1
        if len(self.unreadable_samples) < MAX_ERROR_SAMPLES:
            reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
            self.unreadable_samples.append(f"{path}（{reason}）")


class _Stopped(Exception):
    """使用者停止掃描時，用來從深層迴圈（含 worker 執行緒）直接跳出。"""


def _new_hasher():
    """開頭、結尾與完整內容用同一種 hash，所以小檔案的開頭 hash 就等於完整 hash。"""
    if _FAST_HASH:
        return xxhash.xxh3_128()
    return hashlib.blake2b(digest_size=16)


_local = threading.local()


def _buffer() -> bytearray:
    """每個執行緒重複使用的讀檔緩衝區（大小跟著 CHUNK_SIZE）。"""
    buf = getattr(_local, "buf", None)
    if buf is None or len(buf) != CHUNK_SIZE:
        buf = _local.buf = bytearray(CHUNK_SIZE)
    return buf


def _partial_hash(path: Path, checkpoint: Checkpoint | None = None) -> str:
    """開頭 HEAD_READ bytes 的 hash；讀取失敗時拋出 OSError。checkpoint 在讀檔前呼叫。"""
    if checkpoint:
        checkpoint(0)
    with open(path, "rb", buffering=0) as f:
        data = f.read(HEAD_READ)
    h = _new_hasher()
    h.update(data)
    return h.hexdigest()


def _tail_hash(path: Path, checkpoint: Checkpoint | None = None) -> str:
    """結尾 TAIL_READ bytes 的 hash（只用在大於 TAIL_MIN_SIZE 的檔案）。"""
    if checkpoint:
        checkpoint(0)
    with open(path, "rb", buffering=0) as f:
        f.seek(-TAIL_READ, os.SEEK_END)
        data = f.read(TAIL_READ)
    h = _new_hasher()
    h.update(data)
    return h.hexdigest()


def _full_hash(path: Path, checkpoint: Checkpoint | None = None) -> str:
    """整個檔案的 hash；讀取失敗時拋出 OSError。
    每讀一個區塊就呼叫 checkpoint：暫停時阻塞、停止時拋出 _Stopped，大檔也能立刻中斷。
    """
    h = _new_hasher()
    buf = _buffer()
    view = memoryview(buf)
    with open(path, "rb", buffering=0) as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            h.update(view[:n])
            if checkpoint:
                checkpoint(n)
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

    workers_by_dev 指定每顆磁碟（st_dev）的 (第 2 步, 第 3 步) 執行緒數；SSD／NVMe 用多個
    平行讀取，HDD 用 1 個並依路徑順序讀取。沒有指定的磁碟用 pass2_workers／pass3_workers。
    """

    def __init__(
        self,
        options: ScanOptions,
        *,
        cache: HashCache | None = None,   # 雜湊快取；None 表示每次都重新讀檔
        workers_by_dev: dict[int, tuple[int, int]] | None = None,
                                          # 各磁碟（st_dev）的 (Pass2, Pass3) 執行緒數
        pass2_workers: int = 1,           # 沒有個別設定的磁碟用的執行緒數
        pass3_workers: int = 1,
        on_progress: ProgressCallback | None = None,
        on_done: Callable[[list[DuplicateGroup], ScanStats], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ):
        self.options        = options
        self.roots          = normalize_roots(options.roots)
        self.min_size       = max(options.min_size, 1)   # 空檔案一律不算重複
        self.include_hidden = options.include_hidden
        self.extensions     = options.extensions()       # None＝所有檔案
        self.excluded_names = options.excluded_names()
        self.excluded_paths = options.excluded_paths()
        self.pass2_workers  = pass2_workers
        self.pass3_workers  = pass3_workers
        self.on_progress    = on_progress
        self.on_done        = on_done
        self.on_error       = on_error
        self.on_cancel      = on_cancel
        self.cache          = cache
        self.workers_by_dev = workers_by_dev or {}
        self.stats          = ScanStats()
        self.error_details  = ""     # 發生例外時的完整呼叫堆疊

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
            self.error_details = traceback.format_exc()     # 完整的呼叫堆疊（寫進 log 用）
            if self.on_error:
                self.on_error(str(exc) or type(exc).__name__)
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
            msg = (f"{label}... ({done:,}/{total_files:,} 個檔案 · "
                   f"{human_size(done_bytes)} / {human_size(total_bytes)})")
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
                                elif (name.casefold() in EXCLUDED_DIR_NAMES
                                      or name.casefold() in self.excluded_names
                                      or os.path.normcase(entry.path) in self.excluded_paths):
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
                        if (self.extensions is not None
                                and os.path.splitext(name)[1][1:].lower() not in self.extensions):
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
    def _workers_for(self, dev: int, step: int) -> int:
        """這顆磁碟在 Pass 2／Pass 3 要用幾個執行緒（沒有個別設定時用預設值）。"""
        pair = self.workers_by_dev.get(dev)
        if pair is not None:
            return pair[0] if step == 2 else pair[1]
        return self.pass2_workers if step == 2 else self.pass3_workers

    def _hash_stage(self, files: list[FileInfo], fn, step: int, label: str,
                    store: Callable[[FileInfo, str], None] | None = None
                    ) -> Iterator[tuple[FileInfo, str]]:
        """對每個檔案呼叫 fn(path, checkpoint)，依完成順序產生 (FileInfo, digest)。

        讀取失敗的檔案記入統計後略過；使用者停止時拋出 _Stopped。
        store：每個結果都會先交給它（寫進快取）；停止時已經算完、還沒交回的結果也會存起來，
        續掃時就不必重算。
        每顆磁碟各有自己的執行緒池與在途上限（執行緒數 × IN_FLIGHT_PER_WORKER），不同磁碟同時
        進行；單執行緒的磁碟（HDD）依路徑順序讀，同一個資料夾的檔案在磁碟上通常相鄰。
        """
        self._stage = (step, label, len(files), sum(fi.size for fi in files), time.monotonic())
        self._files_done = 0
        with self._lock:
            self._bytes_done = 0
        self._report(force=True)
        if not files:
            return

        batches: dict[int, list[FileInfo]] = defaultdict(list)
        for fi in files:
            batches[fi.dev].append(fi)
        queues: dict[int, deque] = {}
        pools: dict[int, ThreadPoolExecutor] = {}
        limits: dict[int, int] = {}
        for dev, batch in batches.items():
            workers = max(1, self._workers_for(dev, step))
            if workers == 1:
                batch.sort(key=_path_key)
            queues[dev] = deque(batch)
            pools[dev] = ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix=f"dupescan-pass{step}")
            limits[dev] = workers * IN_FLIGHT_PER_WORKER

        pending: dict = {}
        in_flight: Counter = Counter()

        def fill(dev: int):
            queue = queues[dev]
            while queue and in_flight[dev] < limits[dev]:
                fi = queue.popleft()
                pending[pools[dev].submit(fn, fi.path, self._checkpoint)] = (dev, fi)
                in_flight[dev] += 1

        completed = False
        try:
            for dev in queues:
                fill(dev)
            while pending:
                # 逾時醒來發送進度：單一大檔要讀很久時進度列也會動
                finished, _ = wait(pending, timeout=EMIT_INTERVAL, return_when=FIRST_COMPLETED)
                for fut in finished:
                    dev, fi = pending.pop(fut)
                    in_flight[dev] -= 1
                    try:
                        digest = fut.result()
                    except OSError as exc:
                        self.stats.add_unreadable(fi.path, exc)
                    else:
                        if store:
                            store(fi, digest)
                        yield fi, digest
                    self._files_done += 1
                    fill(dev)
                self._checkpoint()
                self._report()
            completed = True
        finally:
            if not completed:
                # 停止或發生例外：讓執行中的 worker 在下一個讀檔區塊就結束
                self._stop_event.set()
                self._pause_event.set()
                for fut in pending:
                    fut.cancel()
            for pool in pools.values():
                pool.shutdown(wait=True)
            if not completed and store:
                for fut, (_, fi) in pending.items():
                    if fut.done() and not fut.cancelled() and fut.exception() is None:
                        store(fi, fut.result())
        self._report(force=True)

    # ── 主流程 ─────────────────────────────────────────────────────────
    def _run(self) -> list[DuplicateGroup] | None:
        """執行三個步驟；使用者停止時回傳 None。"""
        if self.cache is not None:
            self.cache.open()
            self.stats.cache_note = self.cache.note
        try:
            return self._run_passes()
        except _Stopped:
            return None
        finally:
            self._stage = None
            if self.cache is not None:
                self.cache.close()          # 停止或出錯時也會把已算好的 hash 存起來，下次接著用
                self.stats.cache_note = self.cache.note

    def _run_passes(self) -> list[DuplicateGroup]:
        # ── 第 1 步：列出檔案，依大小分組 ─────────────────────────────
        self._emit("正在列出檔案...", 0, 0, 1)
        by_size: dict[int, list[FileInfo]] = defaultdict(list)
        for fi in self._walk():
            by_size[fi.size].append(fi)
        # 只保留大小有重複的群組，其他不可能是重複檔案；再合併同一實體檔的路徑
        candidates = self._merge_same_files({s: f for s, f in by_size.items() if len(f) > 1})
        by_size.clear()
        flat = [fi for files in candidates.values() for fi in files]
        self.stats.candidates = len(flat)
        scanned = self.stats.files_seen
        self._emit(f"列出完成 — 共 {scanned:,} 個檔案，{len(flat):,} 個大小相同的候選",
                   scanned, scanned, 1, 0)
        cached = self.cache.lookup(flat, HASH_ALGO) if self.cache else {}

        def cached_part(fi: FileInfo, index: int) -> str | None:
            hit = cached.get(id(fi))
            return hit[index] if hit else None

        # ── 第 2 步：快速比對（開頭；大檔再比結尾）──────────────────
        by_head: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
        todo = []
        for fi in flat:
            head = cached_part(fi, 0)
            if head:
                by_head[(fi.size, head)].append(fi)
                self.stats.cache_hits_partial += 1
            else:
                todo.append(fi)
        cache = self.cache
        store_head = (lambda fi, d: cache.store(fi, HASH_ALGO, head=d)) if cache else None
        store_tail = (lambda fi, d: cache.store(fi, HASH_ALGO, tail=d)) if cache else None
        store_full = (lambda fi, d: cache.store(fi, HASH_ALGO, full=d)) if cache else None
        for fi, digest in self._hash_stage(todo, _partial_hash, 2, "快速比對中", store_head):
            by_head[(fi.size, digest)].append(fi)

        by_full: dict[tuple[int, str], list[FileInfo]] = defaultdict(list)
        need_full: list[list[FileInfo]] = []
        need_tail: list[list[FileInfo]] = []
        for (size, head), files in by_head.items():
            if len(files) < 2:
                continue
            if size <= HEAD_READ:
                # 小檔案：開頭就是全部內容，開頭 hash 即完整 hash，不必再讀
                for fi in files:
                    fi.hash_full = head
                    by_full[(size, head)].append(fi)
                self.stats.small_files += len(files)
            elif size >= TAIL_MIN_SIZE and not all(cached_part(fi, 2) for fi in files):
                need_tail.append(files)
            else:
                need_full.append(files)

        if need_tail:
            by_tail: dict[tuple[int, int, str], list[FileInfo]] = defaultdict(list)
            group_of = {id(fi): gi for gi, files in enumerate(need_tail) for fi in files}
            todo = []
            for files in need_tail:
                for fi in files:
                    tail = cached_part(fi, 1)
                    if tail:
                        by_tail[(fi.size, group_of[id(fi)], tail)].append(fi)
                    else:
                        todo.append(fi)
            self.stats.tail_checked += len(todo)
            for fi, digest in self._hash_stage(todo, _tail_hash, 2, "快速比對中（檔尾）",
                                               store_tail):
                by_tail[(fi.size, group_of[id(fi)], digest)].append(fi)
            need_full += [files for files in by_tail.values() if len(files) > 1]

        flat2 = [fi for files in need_full for fi in files]
        self._emit(f"快速比對完成 — {len(flat2):,} 個檔案需要比對完整內容",
                   len(flat), len(flat), 2, 0)

        # ── 第 3 步：比對完整內容 ───────────────────────────────────
        todo = []
        for fi in flat2:
            full = cached_part(fi, 2)
            if full:
                fi.hash_full = full
                by_full[(fi.size, full)].append(fi)
                self.stats.cache_hits_full += 1
            else:
                todo.append(fi)
        for fi, digest in self._hash_stage(todo, _full_hash, 3, "比對檔案內容", store_full):
            fi.hash_full = digest
            by_full[(fi.size, digest)].append(fi)

        # 建立結果群組：群組內依路徑排序（平手規則的結果才會固定），
        # 群組依可釋放空間由大到小排序（最值得處理的排最前面）
        groups: list[DuplicateGroup] = []
        for (size, h), files in by_full.items():
            if len(files) > 1:
                files.sort(key=_path_key)
                groups.append(DuplicateGroup(hash_value=h, size=size, files=files))
        groups.sort(key=lambda g: (-g.wasted_bytes, _path_key(g.files[0])))
        return groups
