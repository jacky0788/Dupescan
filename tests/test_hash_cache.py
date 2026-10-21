# 雜湊快取：重掃不讀檔、檔案變了會重算、改名仍認得、取消後保留進度、舊紀錄清理
import os
import sqlite3
import threading
import time

import pytest

from helpers import blob, group_paths, make_scanner
import src.scanner as sc
from src.hash_cache import HashCache, _stable_ids
from src.scanner import Scanner

KB = 1024


@pytest.fixture
def calls(monkeypatch):
    """計算實際讀檔算 hash 的次數。"""
    counter = {"partial": 0, "full": 0}
    orig_partial, orig_full = sc._partial_hash, sc._full_hash

    def partial(path, checkpoint=None):
        counter["partial"] += 1
        return orig_partial(path, checkpoint)

    def full(path, checkpoint=None):
        counter["full"] += 1
        return orig_full(path, checkpoint)

    monkeypatch.setattr(sc, "_partial_hash", partial)
    monkeypatch.setattr(sc, "_full_hash", full)
    return counter


def make_data(tree):
    data = blob(8 * KB, 1)
    for n in ("a.bin", "sub/b.bin", "sub/c.bin"):
        tree(n, data)
    tree("other.bin", blob(8 * KB, 2))           # 同大小、內容不同
    tree("unique.bin", blob(9 * KB, 3))


def scan(root, db, **kw):
    s = make_scanner([str(root)], min_size=1, cache=HashCache(db), **kw)
    return s, s.scan()


def test_second_scan_reads_nothing(tree, tmp_path, calls):
    make_data(tree)
    db = tmp_path / "cache.sqlite3"
    _, first = scan(tree.root, db)
    assert calls == {"partial": 4, "full": 3}
    calls.update(partial=0, full=0)
    s, second = scan(tree.root, db)
    assert calls == {"partial": 0, "full": 0}
    assert group_paths(first, tree.root) == group_paths(second, tree.root)
    assert s.stats.cache_hits_full == 3 and s.stats.cache_hits_partial == 4


def test_changed_file_is_rehashed(tree, tmp_path, calls):
    make_data(tree)
    db = tmp_path / "cache.sqlite3"
    scan(tree.root, db)
    b = tree.root / "sub" / "b.bin"
    data = bytearray(b.read_bytes())
    data[-1] ^= 0xFF                              # 改尾端：前 4 KB 相同、大小相同
    b.write_bytes(bytes(data))
    st = os.stat(b)
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    calls.update(partial=0, full=0)
    _, groups = scan(tree.root, db)
    assert calls == {"partial": 1, "full": 1}     # 只有改過的那個檔重讀
    assert group_paths(groups, tree.root) == {frozenset({"a.bin", "sub/c.bin"})}


def test_renamed_file_keeps_cache_on_ntfs(tree, tmp_path, calls):
    if not _stable_ids(tree.root.anchor):
        pytest.skip("暫存資料夾不在本機 NTFS／ReFS 上")
    make_data(tree)
    db = tmp_path / "cache.sqlite3"
    scan(tree.root, db)
    os.rename(tree.root / "a.bin", tree.root / "a_renamed.bin")
    calls.update(partial=0, full=0)
    _, groups = scan(tree.root, db)
    assert calls == {"partial": 0, "full": 0}
    assert group_paths(groups, tree.root) == {frozenset({"a_renamed.bin", "sub/b.bin", "sub/c.bin"})}


def test_cancelled_scan_keeps_progress(tree, tmp_path, monkeypatch):
    for g in range(20):
        data = blob(5 * KB + g, g)
        tree(f"x/g{g:02d}.bin", data)
        tree(f"y/g{g:02d}.bin", data)
    db = tmp_path / "cache.sqlite3"
    done = [0]
    orig_full = sc._full_hash

    def slow_full(path, checkpoint=None):
        time.sleep(0.05)
        result = orig_full(path, checkpoint)
        done[0] += 1
        return result

    monkeypatch.setattr(sc, "_full_hash", slow_full)
    s = make_scanner([str(tree.root)], min_size=1, cache=HashCache(db))
    th = threading.Thread(target=s.scan)
    th.start()
    end = time.monotonic() + 20
    while done[0] < 10 and time.monotonic() < end:
        time.sleep(0.01)
    s.stop()
    th.join(10)
    finished_before_cancel = done[0]
    assert 10 <= finished_before_cancel < 40

    monkeypatch.setattr(sc, "_full_hash", orig_full)
    s2, groups = scan(tree.root, db)
    assert len(groups) == 20
    assert s2.stats.cache_hits_full == finished_before_cancel   # 取消前算好的不用再算


def test_algorithm_change_invalidates_cache(tree, tmp_path, calls, monkeypatch):
    make_data(tree)
    db = tmp_path / "cache.sqlite3"
    scan(tree.root, db)
    monkeypatch.setattr(sc, "HASH_ALGO", "another-algo")
    calls.update(partial=0, full=0)
    scan(tree.root, db)
    assert calls == {"partial": 4, "full": 3}


def test_old_entries_are_pruned(tree, tmp_path):
    make_data(tree)
    db = tmp_path / "cache.sqlite3"
    s = make_scanner([str(tree.root)], min_size=1, cache=HashCache(db))
    s.cache._now -= 400 * 86400                   # 這次寫入的紀錄假裝是一年多前看到的
    s.scan()

    def count():
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            conn.close()

    assert count() == 4
    later = HashCache(db)                         # 之後任何一次掃描結束時都會清掉過舊紀錄
    later.open()
    later.close()
    assert count() == 0
