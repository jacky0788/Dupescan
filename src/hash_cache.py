# 雜湊快取：記住每個候選檔案上次算出的前段／完整 hash，檔案沒變就直接沿用，
# 重掃同一顆磁碟時不必再讀一次內容；取消或中斷時已算好的部分也會保留。
#
# 「沒變」的判斷：大小與修改時間（奈秒）都相同，且 hash 演算法相同。
# 鍵：NTFS／ReFS 本機磁碟用「磁碟區序號＋檔案 ID」（同一顆磁碟內改名、搬移也認得）；
#     其他檔案系統（FAT／exFAT、網路磁碟）的檔案 ID 可能被別的檔案重複使用，改用路徑。
# 存放：%LOCALAPPDATA%\DupeScan\hashcache.sqlite3。只在掃描執行緒內使用（sqlite3 連線不跨執行緒）。

import ctypes
import functools
import os
import sqlite3
import sys
import time
from pathlib import Path

from .app_paths import data_dir
from .models import FileInfo

PRUNE_AFTER_DAYS = 365      # 超過一年沒再看到的紀錄在掃描結束時清掉
FLUSH_EVERY      = 2000     # 累積多少筆變更就寫入一次
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


class HashCache:
    """前段／完整 hash 的持久快取。open() 與其他方法都必須在同一個（掃描）執行緒呼叫。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._conn: sqlite3.Connection | None = None
        self._rows: dict[str, list] = {}   # 鍵 → [size, mtime_ns, algo, partial, partial_len, full]
        self._dirty: set[str] = set()
        self._now = int(time.time())

    # ── 連線 ──────────────────────────────────────────────────────────
    def open(self):
        path = self.path or data_dir() / "hashcache.sqlite3"
        self._conn = sqlite3.connect(str(path), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            " key TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,"
            " algo TEXT NOT NULL, partial TEXT, partial_len INTEGER, full TEXT,"
            " seen INTEGER NOT NULL)")
        self._conn.commit()

    def close(self):
        """寫入尚未存檔的變更、清掉過舊紀錄並關閉連線（取消掃描時也會呼叫）。"""
        if self._conn is None:
            return
        try:
            self.flush()
            self._conn.execute("DELETE FROM files WHERE seen < ?",
                               (self._now - PRUNE_AFTER_DAYS * 86400,))
            self._conn.commit()
        finally:
            self._conn.close()
            self._conn = None

    # ── 查詢與更新 ────────────────────────────────────────────────────
    def lookup(self, files: list[FileInfo], algo: str, partial_len: int
               ) -> dict[int, tuple[str | None, str | None]]:
        """查詢 files 的快取，回傳 {id(fi): (前段 hash, 完整 hash)}，只含仍然有效的部分。"""
        keys = {cache_key(fi): fi for fi in files}
        found: dict[str, tuple] = {}
        key_list = list(keys)
        for i in range(0, len(key_list), _LOOKUP_CHUNK):
            chunk = key_list[i:i + _LOOKUP_CHUNK]
            sql = ("SELECT key, size, mtime_ns, algo, partial, partial_len, full FROM files "
                   f"WHERE key IN ({','.join('?' * len(chunk))})")
            for row in self._conn.execute(sql, chunk):
                found[row[0]] = row[1:]

        hits: dict[int, tuple[str | None, str | None]] = {}
        for key, fi in keys.items():
            row = found.get(key)
            if not row:
                continue
            size, mtime_ns, row_algo, partial, plen, full = row
            if size != fi.size or mtime_ns != fi.mtime_ns or row_algo != algo:
                continue                                  # 檔案變了或演算法不同：作廢
            partial = partial if plen == partial_len else None
            if partial or full:
                hits[id(fi)] = (partial, full)
                self._rows[key] = [size, mtime_ns, algo, partial, plen if partial else None, full]
                self._dirty.add(key)                      # 更新 seen，避免被當成過舊紀錄清掉
        return hits

    def store(self, fi: FileInfo, algo: str, partial: str | None = None,
              partial_len: int | None = None, full: str | None = None):
        key = cache_key(fi)
        row = self._rows.get(key)
        if row is None or row[0] != fi.size or row[1] != fi.mtime_ns or row[2] != algo:
            row = [fi.size, fi.mtime_ns, algo, None, None, None]
            self._rows[key] = row
        if partial is not None:
            row[3], row[4] = partial, partial_len
        if full is not None:
            row[5] = full
        self._dirty.add(key)
        if len(self._dirty) >= FLUSH_EVERY:
            self.flush()

    def flush(self):
        if not self._dirty or self._conn is None:
            return
        data = [(k, *self._rows[k], self._now) for k in self._dirty]
        self._conn.executemany(
            "INSERT OR REPLACE INTO files (key, size, mtime_ns, algo, partial, partial_len, full, seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", data)
        self._conn.commit()
        self._dirty.clear()


def clear_cache(path: Path | None = None):
    """清除快取檔（使用者在設定中要求時）。這是 DupeScan 自己的快取，不是使用者的檔案。"""
    path = Path(path) if path else data_dir() / "hashcache.sqlite3"
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
