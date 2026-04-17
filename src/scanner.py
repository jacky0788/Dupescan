"""
DupeScan - Scanner Engine
3-pass duplicate detection with pause/resume and ETA estimation:
  Pass 1: Group by file size        (no I/O, instant)
  Pass 2: Partial hash (first 4 KB) (cheap, filters ~90% of candidates)
  Pass 3: Full xxHash               (only for confirmed candidates)
"""

import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

try:
    import xxhash
    _FAST_HASH = True
except ImportError:
    _FAST_HASH = False

from .models import DuplicateGroup, FileInfo

PARTIAL_READ = 4096    # bytes read in pass-2
CHUNK_SIZE   = 131072  # bytes per chunk in full-hash pass (128 KB, better throughput)
EMIT_INTERVAL = 0.15   # minimum seconds between progress signals
TOTAL_STEPS  = 3
STEP_NAMES   = {
    1: "依檔案大小分組",
    2: "快速 Hash 比對（前 4 KB）",
    3: "完整 Hash 比對",
}

# on_progress signature: (message, current, total, step, total_steps, eta_secs)
#   eta_secs = -1 means unknown
ProgressCallback = Callable[[str, int, int, int, int, int], None]


def _format_eta(seconds: int) -> str:
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
    try:
        with open(path, "rb") as f:
            data = f.read(PARTIAL_READ)
        if _FAST_HASH:
            return xxhash.xxh64(data).hexdigest()
        return __import__("hashlib").blake2b(data, digest_size=8).hexdigest()
    except (OSError, PermissionError):
        return None


def _full_hash(path: Path) -> str | None:
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
    """
    Thread-safe scanner with pause/resume support.
    Call scan() from a worker thread; use stop()/pause()/resume() from any thread.

    on_progress(msg, current, total, step, total_steps, eta_secs)
      - step: 1/2/3 (current pass)
      - eta_secs: estimated remaining seconds, -1 if unknown
    """

    def __init__(
        self,
        roots: list[str],
        min_size: int = 1,
        include_hidden: bool = False,
        on_progress: ProgressCallback | None = None,
        on_done: Callable[[list[DuplicateGroup]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.roots = [Path(r) for r in roots]
        self.min_size = min_size
        self.include_hidden = include_hidden
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error

        self._stop_event  = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()   # starts in running state (not paused)

    # ── Control methods (safe to call from any thread) ─────────────────
    def stop(self):
        self._stop_event.set()
        self._pause_event.set()   # unblock if currently paused

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    # ── Entry point ────────────────────────────────────────────────────
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

    # ── Internal helpers ───────────────────────────────────────────────
    def _check(self) -> bool:
        """Pause-point: blocks while paused. Returns True if scan should stop."""
        self._pause_event.wait()
        return self._stop_event.is_set()

    def _emit(self, msg: str, current: int, total: int,
              step: int, eta_secs: int = -1):
        if self.on_progress:
            self.on_progress(msg, current, total, step, TOTAL_STEPS, eta_secs)

    @staticmethod
    def _eta(start_time: float, done: int, total: int) -> int:
        if done <= 0 or total <= 0:
            return -1
        elapsed = time.monotonic() - start_time
        if elapsed < 0.5:
            return -1
        rate = done / elapsed
        return int((total - done) / rate)

    # ── 3-pass algorithm ───────────────────────────────────────────────
    def _run(self) -> list[DuplicateGroup]:

        # ── Pass 1: collect files, group by size ───────────────────────
        self._emit("掃描檔案中...", 0, 0, 1)
        by_size: dict[int, list[FileInfo]] = defaultdict(list)
        scanned = 0
        t0 = time.monotonic()

        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                if self._check():
                    return []
                if not self.include_hidden:
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
                        continue

        candidates = {s: f for s, f in by_size.items() if len(f) > 1}
        total_c = sum(len(v) for v in candidates.values())
        self._emit(
            f"大小分組完成 — 共掃描 {scanned:,} 個檔案，{total_c:,} 個候選",
            scanned, scanned, 1, 0,
        )

        # ── Pass 2: partial hash (first 4 KB) ──────────────────────────
        by_partial: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        t0 = time.monotonic()
        last_emit2 = t0

        for files in candidates.values():
            for fi in files:
                if self._check():
                    return []
                ph = _partial_hash(fi.path)
                if ph:
                    by_partial[ph].append(fi)
                done += 1
                now = time.monotonic()
                if now - last_emit2 >= EMIT_INTERVAL or done == total_c:
                    eta = self._eta(t0, done, total_c)
                    self._emit(
                        f"快速比對中... ({done:,}/{total_c:,})",
                        done, total_c, 2, eta,
                    )
                    last_emit2 = now

        candidates2 = {ph: f for ph, f in by_partial.items() if len(f) > 1}
        total2 = sum(len(v) for v in candidates2.values())
        self._emit(
            f"快速比對完成 — {total2:,} 個檔案需完整比對",
            total_c, total_c, 2, 0,
        )

        # ── Pass 3: full hash ──────────────────────────────────────────
        by_full: dict[str, list[FileInfo]] = defaultdict(list)
        done = 0
        t0 = time.monotonic()
        last_emit3 = t0

        for files in candidates2.values():
            for fi in files:
                if self._check():
                    return []
                fh = _full_hash(fi.path)
                if fh:
                    fi.hash_full = fh
                    by_full[fh].append(fi)
                done += 1
                now = time.monotonic()
                if now - last_emit3 >= EMIT_INTERVAL or done == total2:
                    eta = self._eta(t0, done, total2) if total2 > 0 else 0
                    self._emit(
                        f"完整比對中... ({done:,}/{total2:,})",
                        done, total2, 3, eta,
                    )
                    last_emit3 = now

        # Build and sort result groups
        groups: list[DuplicateGroup] = [
            DuplicateGroup(hash_value=h, size=files[0].size, files=files)
            for h, files in by_full.items()
            if len(files) > 1
        ]
        groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
        return groups
