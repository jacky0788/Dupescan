"""
DupeScan - Scanner Engine
Optimized 3-pass duplicate detection:
  Pass 1: Group by file size  (no I/O, instant)
  Pass 2: Compare first 4 KB  (eliminates most false positives cheaply)
  Pass 3: Full xxHash / blake2b for confirmed candidates
"""

import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable

try:
    import xxhash
    _FAST_HASH = True
except ImportError:
    import hashlib
    _FAST_HASH = False

from .models import DuplicateGroup, FileInfo

PARTIAL_READ = 4096   # bytes read in pass-2
CHUNK_SIZE   = 65536  # bytes per chunk in full-hash pass


def _partial_hash(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            data = f.read(PARTIAL_READ)
        if _FAST_HASH:
            return xxhash.xxh64(data).hexdigest()
        return __import__("hashlib").blake2b(data, digest_size=8).hexdigest()
    except (OSError, PermissionError):
        return None


def _full_hash(path: Path, progress_cb: Callable[[int], None] | None = None) -> str | None:
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
                if progress_cb:
                    progress_cb(len(chunk))
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


class Scanner:
    """
    Thread-safe scanner. Call scan() from a worker thread.
    Provides callbacks for live progress updates to the UI.
    """

    def __init__(
        self,
        roots: list[str],
        min_size: int = 1,
        include_hidden: bool = False,
        on_progress: Callable[[str, int, int], None] | None = None,
        on_done: Callable[[list[DuplicateGroup]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.roots = [Path(r) for r in roots]
        self.min_size = min_size
        self.include_hidden = include_hidden
        self.on_progress = on_progress   # (message, current, total)
        self.on_done = on_done
        self.on_error = on_error
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def scan(self) -> list[DuplicateGroup]:
        try:
            groups = self._run()
            if self.on_done:
                self.on_done(groups)
            return groups
        except Exception as exc:
            if self.on_error:
                self.on_error(str(exc))
            return []

    # ------------------------------------------------------------------
    def _run(self) -> list[DuplicateGroup]:
        # ── Pass 1: collect all files, group by size ──────────────────
        self._emit("正在掃描檔案...", 0, 0)
        by_size: dict[int, list[FileInfo]] = defaultdict(list)
        scanned = 0

        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                if self._stop_event.is_set():
                    return []
                if not self.include_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]

                for fname in filenames:
                    if not self.include_hidden and fname.startswith("."):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        stat = fpath.stat()
                        size = stat.st_size
                        if size < self.min_size:
                            continue
                        fi = FileInfo(path=fpath, size=size, mtime=stat.st_mtime)
                        by_size[size].append(fi)
                        scanned += 1
                        if scanned % 500 == 0:
                            self._emit(f"掃描中... 已找到 {scanned} 個檔案", 0, 0)
                    except (OSError, PermissionError):
                        continue

        # Keep only sizes that appear more than once
        candidates = {s: files for s, files in by_size.items() if len(files) > 1}
        total_candidate = sum(len(v) for v in candidates.values())
        self._emit(f"第一輪完成，{total_candidate} 個候選檔案待比對 hash", 0, total_candidate)

        # ── Pass 2: partial hash (first 4 KB) ─────────────────────────
        by_partial: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        for files in candidates.values():
            for fi in files:
                if self._stop_event.is_set():
                    return []
                ph = _partial_hash(fi.path)
                if ph:
                    by_partial[ph].append(fi)
                done += 1
                if done % 100 == 0:
                    self._emit(f"快速比對中... ({done}/{total_candidate})", done, total_candidate)

        candidates2 = {ph: files for ph, files in by_partial.items() if len(files) > 1}
        total2 = sum(len(v) for v in candidates2.values())
        self._emit(f"第二輪完成，{total2} 個檔案需完整 hash", 0, total2)

        # ── Pass 3: full hash ──────────────────────────────────────────
        by_full: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        for files in candidates2.values():
            for fi in files:
                if self._stop_event.is_set():
                    return []
                fh = _full_hash(fi.path)
                if fh:
                    fi.hash_full = fh
                    by_full[fh].append(fi)
                done += 1
                self._emit(f"完整比對中... ({done}/{total2})", done, total2)

        # Build result groups
        groups: list[DuplicateGroup] = []
        for h, files in by_full.items():
            if len(files) > 1:
                groups.append(DuplicateGroup(
                    hash_value=h,
                    size=files[0].size,
                    files=files,
                ))

        # Sort by wasted space (largest first)
        groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
        return groups

    def _emit(self, msg: str, cur: int, total: int):
        if self.on_progress:
            self.on_progress(msg, cur, total)
