# 雜湊快取：記住每個候選檔案上次算出的開頭／結尾／完整 hash，檔案沒變就直接沿用，
# 重掃同一顆磁碟時不必再讀一次內容；停止、出錯或當機時已算好的部分也會保留（最多損失幾秒）。
#
# 「沒變」的判斷：大小與修改時間（奈秒）都相同，且 hash 演算法（含取樣長度）相同。
# 鍵：NTFS／ReFS 本機磁碟用「磁碟區序號＋檔案 ID」（同一顆磁碟內改名、搬移也認得）；
#     其他檔案系統（FAT／exFAT、網路磁碟）的檔案 ID 可能被別的檔案重複使用，改用路徑。
# 存放：%LOCALAPPDATA%\DupeScan\hashcache.sqlite3。只在掃描執行緒內使用（sqlite3 連線不跨執行緒）。
#
# 出狀況時不讓掃描失敗：快取檔損壞 → 改名備份後重建；被另一個 DupeScan 佔用或無法寫入 →
# 這次不使用快取。狀況寫在 note，掃描完成時由 UI 顯示。

import ctypes
import functools
import os
import sqlite3
import sys
import time
from pathlib import Path

from .app_paths import data_dir
from .models import FileInfo

SCHEMA_VERSION   = 2        # 資料表結構版本（不同時整張重建）
PRUNE_AFTER_DAYS = 365      # 超過一年沒再看到的紀錄在掃描結束時清掉
FLUSH_EVERY      = 2000     # 累積多少筆變更就寫入一次……
FLUSH_SECONDS    = 2.0      # ……或距離上次寫入超過幾秒（當機時最多損失這麼久的進度）
_LOOKUP_CHUNK    = 500      # 每次 SQL 查詢的鍵數（SQLite 參數上限 999）
_DRIVE_REMOVABLE, _DRIVE_FIXED = 2, 3


@functools.lru_cache(maxsize=64)
def _stable_ids(anchor: str) -> bool:
    """這顆磁碟的檔案 ID 是否穩定可當鍵（本機 NTFS／ReFS）。"""
    if sys.platform != "win32" or not anchor or anchor.startswith("\\\\"):
        return False
    k32 = ctypes.windll.kernel32
    if k32.GetDriveTypeW(anchor) not in (_DRIVE_FIXED, _DRIVE_REMOVABLE):
        return False
    fs = ctypes.create_unicode_buffer(32)
    ok = k32.GetVolumeInformationW(anchor, None, 0, None, None, None, fs, 32)
    return bool(ok) and fs.value.upper() in ("NTFS", "REFS")


def cache_key(fi: FileInfo) -> str:
    if fi.ino and _stable_ids(fi.path.anchor):
        return f"i:{fi.dev}:{fi.ino}"
    return "p:" + os.path.normcase(str(fi.path))


def default_cache_path() -> Path:
    return data_dir() / "hashcache.sqlite3"


class HashCache:
    """開頭／結尾／完整 hash 的持久快取。open() 與其他方法都必須在同一個（掃描）執行緒呼叫。"""

    def __init__(self, path: Path | None = None, timeout: float = 10.0):
        self.path = Path(path) if path else None
        self.timeout = timeout             # 等待另一個連線釋放鎖定的秒數
        self.note = ""                     # 發生過的狀況（給 UI 顯示），沒事時是空字串
        self._conn: sqlite3.Connection | None = None
        self._rows: dict[str, list] = {}   # 鍵 → [size, mtime_ns, algo, head, tail, full]
        self._dirty: set[str] = set()
        self._now = int(time.time())
        self._last_flush = time.monotonic()

    # ── 連線 ──────────────────────────────────────────────────────────
    def open(self):
        path = self.path or default_cache_path()
        try:
            self._connect(path)
        except sqlite3.DatabaseError as exc:
            if self._is_locked(exc):
                self._disable("快取正被另一個 DupeScan 使用，這次掃描不使用快取")
                return
            # 檔案損壞（不是資料庫、資料表毀損…）：改名備份後重建
            self._close_quietly()
            backup = path.with_name(f"{path.name}.bad-{time.strftime('%Y%m%d_%H%M%S')}")
            try:
                os.replace(path, backup)
                for suffix in ("-wal", "-shm"):
                    extra = path.with_name(path.name + suffix)
                    if extra.exists():
                        os.replace(extra, backup.with_name(backup.name + suffix))
                self._connect(path)
                self.note = f"快取檔已損壞，已改名備份並重建（{backup.name}）"
            except (OSError, sqlite3.DatabaseError):
                self._disable("快取檔損壞且無法重建，這次掃描不使用快取")
        except OSError:
            self._disable("無法建立快取檔，這次掃描不使用快取")

    def _connect(self, path: Path):
        self._conn = sqlite3.connect(str(path), timeout=self.timeout)
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self._conn.execute("DROP TABLE IF EXISTS files")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            " key TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,"
            " algo TEXT NOT NULL, head TEXT, tail TEXT, full TEXT, seen INTEGER NOT NULL)")
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    @staticmethod
    def _is_locked(exc: Exception) -> bool:
        return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()

    def _disable(self, note: str):
        self._close_quietly()
        self.note = note
        self._rows.clear()
        self._dirty.clear()

    def _close_quietly(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def close(self):
        """寫入尚未存檔的變更、清掉過舊紀錄並關閉連線（停止或出錯時也會呼叫）。"""
        if self._conn is None:
            return
        try:
            self.flush()
            if self._conn is not None:
                self._conn.execute("DELETE FROM files WHERE seen < ?",
                                   (self._now - PRUNE_AFTER_DAYS * 86400,))
                self._conn.commit()
        except sqlite3.Error:
            pass
        finally:
            self._close_quietly()

    # ── 查詢與更新 ────────────────────────────────────────────────────
    def lookup(self, files: list[FileInfo], algo: str
               ) -> dict[int, tuple[str | None, str | None, str | None]]:
        """查詢 files 的快取，回傳 {id(fi): (開頭, 結尾, 完整)}，只含仍然有效的紀錄。"""
        if self._conn is None:
            return {}
        keys = {cache_key(fi): fi for fi in files}
        found: dict[str, tuple] = {}
        key_list = list(keys)
        try:
            for i in range(0, len(key_list), _LOOKUP_CHUNK):
                chunk = key_list[i:i + _LOOKUP_CHUNK]
                sql = ("SELECT key, size, mtime_ns, algo, head, tail, full FROM files "
                       f"WHERE key IN ({','.join('?' * len(chunk))})")
                for row in self._conn.execute(sql, chunk):
                    found[row[0]] = row[1:]
        except sqlite3.Error as exc:
            self._disable(f"讀取快取時發生錯誤（{exc}），這次掃描不使用快取")
            return {}

        hits: dict[int, tuple] = {}
        for key, fi in keys.items():
            row = found.get(key)
            if not row:
                continue
            size, mtime_ns, row_algo, head, tail, full = row
            if size != fi.size or mtime_ns != fi.mtime_ns or row_algo != algo:
                continue                                  # 檔案變了或演算法不同：作廢
            if head or tail or full:
                hits[id(fi)] = (head, tail, full)
                self._rows[key] = [size, mtime_ns, algo, head, tail, full]
                self._dirty.add(key)                      # 更新 seen，避免被當成過舊紀錄清掉
        return hits

    def store(self, fi: FileInfo, algo: str, head: str | None = None,
              tail: str | None = None, full: str | None = None):
        if self._conn is None:
            return
        key = cache_key(fi)
        row = self._rows.get(key)
        if row is None or row[0] != fi.size or row[1] != fi.mtime_ns or row[2] != algo:
            row = [fi.size, fi.mtime_ns, algo, None, None, None]
            self._rows[key] = row
        if head is not None:
            row[3] = head
        if tail is not None:
            row[4] = tail
        if full is not None:
            row[5] = full
        self._dirty.add(key)
        if (len(self._dirty) >= FLUSH_EVERY
                or time.monotonic() - self._last_flush >= FLUSH_SECONDS):
            self.flush()

    def flush(self):
        self._last_flush = time.monotonic()
        if not self._dirty or self._conn is None:
            return
        data = [(k, *self._rows[k], self._now) for k in self._dirty]
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO files (key, size, mtime_ns, algo, head, tail, full, seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", data)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._disable(f"寫入快取時發生錯誤（{exc}），這次掃描之後的部分不使用快取")
            return
        self._dirty.clear()


def cache_size(path: Path | None = None) -> int:
    """快取檔（含 -wal）目前占用的位元組數。"""
    path = Path(path) if path else default_cache_path()
    total = 0
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def clear_cache(path: Path | None = None):
    """清除快取檔（使用者在設定中要求時）。這是 DupeScan 自己的快取，不是使用者的檔案。"""
    path = Path(path) if path else default_cache_path()
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
