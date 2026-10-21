# 掃描引擎的行為
import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from helpers import blob, group_paths, make_scanner
from src.scanner import Scanner

KB = 1024
needs_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows 專用")


def scan(root, **kw):
    return make_scanner([str(root)], **kw).scan()


def test_finds_identical_files(tree):
    data = blob(8 * KB, 1)
    tree("a/x.bin", data)
    tree("b/y.bin", data)
    tree("c/z.bin", blob(8 * KB, 2))      # 同大小、不同內容
    tree("d/w.bin", blob(9 * KB, 3))      # 大小唯一
    groups = scan(tree.root, min_size=1)
    assert group_paths(groups, tree.root) == {frozenset({"a/x.bin", "b/y.bin"})}
    assert groups[0].size == 8 * KB
    assert groups[0].wasted_bytes == 8 * KB


def test_groups_sorted_by_wasted_bytes(tree):
    small, big = blob(4 * KB, 1), blob(20 * KB, 2)
    for i in range(3):
        tree(f"s{i}.bin", small)            # 可省 8 KB
    tree("b1.bin", big)
    tree("b2.bin", big)                     # 可省 20 KB
    groups = scan(tree.root, min_size=1)
    assert [g.wasted_bytes for g in groups] == [20 * KB, 8 * KB]
    assert len(groups[1].files) == 3


def test_same_head_different_tail_not_grouped(tree):
    head = blob(8 * KB, 1)
    tree("a.bin", head + b"A" * 100)
    tree("b.bin", head + b"B" * 100)
    assert scan(tree.root, min_size=1) == []


def test_min_size_filters_small_files(tree):
    data = blob(2 * KB, 1)
    tree("a.bin", data)
    tree("b.bin", data)
    assert scan(tree.root, min_size=4 * KB) == []
    assert len(scan(tree.root, min_size=1 * KB)) == 1


def test_dot_files_skipped_unless_include_hidden(tree):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    tree(".hidden/b.bin", data)
    tree(".c.bin", data)
    assert scan(tree.root, min_size=1) == []
    groups = scan(tree.root, min_size=1, include_hidden=True)
    assert group_paths(groups, tree.root) == {frozenset({"a.bin", ".hidden/b.bin", ".c.bin"})}


def test_parallel_matches_sequential(tree):
    for g in range(20):
        data = blob(3 * KB + g, g)
        for c in range(3):
            tree(f"d{c}/g{g}.bin", data)
        tree(f"noise/n{g}.bin", blob(3 * KB + g, 1000 + g))   # 同大小的雜訊
    seq = scan(tree.root, min_size=1, pass2_workers=1, pass3_workers=1)
    par = scan(tree.root, min_size=1, pass2_workers=8, pass3_workers=4)
    assert len(seq) == 20
    assert group_paths(seq, tree.root) == group_paths(par, tree.root)


def test_pause_blocks_until_resume(tree):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    tree("b.bin", data)
    s = make_scanner([str(tree.root)], min_size=1)
    s.pause()                                # 開始前先暫停 → 會卡在第一個暫停點
    result = []
    t = threading.Thread(target=lambda: result.append(s.scan()))
    t.start()
    time.sleep(0.3)
    assert t.is_alive()
    s.resume()
    t.join(5)
    assert not t.is_alive()
    assert len(result[0]) == 1


# ── 掃描結果正確性：連結、排除、屬性、取消、無法讀取 ──────────────────────

def make_junction(link: Path, target: Path):
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   check=True, capture_output=True)


def add_attributes(path: Path, attrs: int):
    k32 = ctypes.windll.kernel32
    assert k32.SetFileAttributesW(str(path), k32.GetFileAttributesW(str(path)) | attrs)


@needs_windows
def test_junction_is_not_followed(tree):
    real = tree("real/photo.bin", blob(4 * KB, 1)).parent
    make_junction(tree.root / "link_to_real", real)
    s = make_scanner([str(tree.root)], min_size=1)
    assert s.scan() == []                       # 同一個檔案，不是兩份重複
    assert s.stats.skipped_links == 1


@needs_windows
def test_hardlinks_count_as_one_physical_file(tree):
    data = blob(4 * KB, 1)
    a = tree("a.bin", data)
    os.link(a, tree.root / "a_link.bin")
    s = make_scanner([str(tree.root)], min_size=1)
    assert s.scan() == []
    assert s.stats.same_file_merged == 1

    tree("copy/a_copy.bin", data)               # 真正的第二份
    groups = make_scanner([str(tree.root)], min_size=1).scan()
    assert len(groups) == 1 and len(groups[0].files) == 2
    assert groups[0].wasted_bytes == 4 * KB     # 不把硬連結算進可省空間
    rep = next(fi for fi in groups[0].files if fi.aliases)
    assert [Path(p).name for p in rep.aliases] == ["a_link.bin"]


def test_overlapping_roots_are_scanned_once(tree):
    data = blob(4 * KB, 1)
    tree("sub/a.bin", data)
    tree("sub/b.bin", data)
    groups = make_scanner([str(tree.root), str(tree.root / "sub")], min_size=1).scan()
    assert len(groups) == 1 and len(groups[0].files) == 2


def test_recycle_bin_and_system_volume_info_excluded(tree):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    tree("$RECYCLE.BIN/S-1-5-21/$Rabc.bin", data)
    tree("System Volume Information/x.bin", data)
    s = make_scanner([str(tree.root)], min_size=1, include_hidden=True)
    assert s.scan() == []
    assert s.stats.skipped_excluded == 2


@needs_windows
def test_hidden_and_system_attributes_skipped_by_default(tree):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    add_attributes(tree("h.bin", data), 0x2)    # FILE_ATTRIBUTE_HIDDEN
    add_attributes(tree("s.bin", data), 0x4)    # FILE_ATTRIBUTE_SYSTEM
    assert make_scanner([str(tree.root)], min_size=1).scan() == []
    groups = make_scanner([str(tree.root)], min_size=1, include_hidden=True).scan()
    assert len(groups[0].files) == 3


@needs_windows
def test_offline_placeholder_skipped(tree):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    add_attributes(tree("cloud.bin", data), 0x1000)   # FILE_ATTRIBUTE_OFFLINE
    s = make_scanner([str(tree.root)], min_size=1)
    assert s.scan() == []
    assert s.stats.skipped_cloud == 1


def test_stop_reports_cancel_instead_of_done(tree):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    tree("b.bin", data)
    done, cancelled = [], []
    s = make_scanner([str(tree.root)], min_size=1,
                on_done=lambda g, st: done.append(g),
                on_cancel=lambda: cancelled.append(True))
    s.stop()
    assert s.scan() is None
    assert done == [] and cancelled == [True]


@needs_windows
def test_unreadable_file_is_counted(tree):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    locked = tree("locked.bin", data)
    tree("c.bin", data)
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    # 以不分享模式開著檔案，掃描時讀取會失敗（sharing violation）
    h = k32.CreateFileW(str(locked), 0x80000000, 0, None, 3, 0, None)
    assert h not in (None, ctypes.c_void_p(-1).value)
    try:
        s = make_scanner([str(tree.root)], min_size=1)
        groups = s.scan()
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))
    assert len(groups) == 1 and len(groups[0].files) == 2
    assert s.stats.unreadable == 1
    assert "locked.bin" in s.stats.unreadable_samples[0]


def test_files_in_group_sorted_by_path(tree):
    data = blob(4 * KB, 1)
    for name in ["c.bin", "a.bin", "B.bin"]:
        tree(name, data)
    for workers in (1, 8):
        groups = make_scanner([str(tree.root)], min_size=1,
                         pass2_workers=workers, pass3_workers=workers).scan()
        assert [fi.path.name for fi in groups[0].files] == ["a.bin", "B.bin", "c.bin"]


def test_same_head_but_different_size_not_fully_hashed(tree, monkeypatch):
    import src.scanner as sc
    calls = []
    orig = sc._full_hash
    monkeypatch.setattr(sc, "_full_hash", lambda p, cp=None: calls.append(p) or orig(p, cp))
    head = blob(8 * KB, 1)
    tree("a1.bin", head + b"1" * 10)
    tree("a2.bin", blob(8 * KB + 10, 2))        # 與 a1 同大小、開頭不同
    tree("b1.bin", head + b"2" * 20)            # 與 a1 開頭相同、大小不同
    tree("b2.bin", blob(8 * KB + 20, 3))
    assert make_scanner([str(tree.root)], min_size=1).scan() == []
    assert calls == []


# ── 掃描控制：暫停真的停住讀取、停止不必等大檔讀完 ──────────────────────
def test_pause_really_stops_parallel_reads(tree, monkeypatch):
    import src.scanner as sc
    lock = threading.Lock()
    reads = [0]
    orig = sc._partial_hash

    def counting(path, checkpoint=None):
        if checkpoint:
            checkpoint(0)
        with lock:
            reads[0] += 1
        time.sleep(0.004)
        return orig(path, checkpoint)

    monkeypatch.setattr(sc, "_partial_hash", counting)
    payload = blob(2040, 1)
    n = 1200
    for i in range(n):
        tree(f"f{i:04d}.bin", payload + i.to_bytes(8, "little"))   # 同大小、內容不同
    in_pass2 = threading.Event()
    s = make_scanner([str(tree.root)], min_size=1, pass2_workers=8, pass3_workers=1,
                on_progress=lambda m, c, t, st, ts, eta:
                    in_pass2.set() if st == 2 and c > 0 else None)
    th = threading.Thread(target=s.scan)
    th.start()
    assert in_pass2.wait(10)
    s.pause()
    time.sleep(0.1)                     # 已經在讀的檔案讓它讀完
    with lock:
        before = reads[0]
    time.sleep(1.0)
    with lock:
        during = reads[0] - before
    s.resume()
    th.join(20)
    assert during == 0
    assert reads[0] == n


@pytest.mark.parametrize("workers", [1, 4])
def test_stop_interrupts_large_file(tree, monkeypatch, workers):
    import src.scanner as sc
    monkeypatch.setattr(sc, "CHUNK_SIZE", 4096)
    data = blob(4 * 1024 * 1024, 1)     # 4 MB → 1,024 個讀檔區塊
    tree("a.bin", data)
    tree("b.bin", data)
    orig = sc._full_hash

    def slow_full(path, checkpoint=None):
        def slow_checkpoint(n=0):
            time.sleep(0.002)           # 每個區塊 2 ms → 一個檔約 2 秒
            checkpoint(n)
        return orig(path, slow_checkpoint)

    monkeypatch.setattr(sc, "_full_hash", slow_full)
    in_pass3 = threading.Event()
    s = make_scanner([str(tree.root)], min_size=1, pass2_workers=workers, pass3_workers=workers,
                on_progress=lambda m, c, t, st, ts, eta: in_pass3.set() if st == 3 else None)
    result = []
    th = threading.Thread(target=lambda: result.append(s.scan()))
    th.start()
    assert in_pass3.wait(10)
    time.sleep(0.2)
    t0 = time.monotonic()
    s.stop()
    th.join(10)
    assert time.monotonic() - t0 < 0.5
    assert result == [None]


# ── 掃描設定：大小上限、副檔名、排除資料夾、各磁碟執行緒數 ──────────────
def test_file_types_and_custom_extensions(tree):
    img, doc, mdl, other = blob(4 * KB, 1), blob(4 * KB, 2), blob(4 * KB, 3), blob(4 * KB, 4)
    for n in ("a.JPG", "b.png"):
        tree(n, img)
    for n in ("a.pdf", "b.pdf"):
        tree(n, doc)
    for n in ("a.safetensors", "b.safetensors"):
        tree(n, mdl)
    for n in ("a.xyz", "b.xyz"):
        tree(n, other)
    paths = lambda **kw: group_paths(make_scanner([tree.root], **kw).scan(), tree.root)
    assert len(paths()) == 4                                            # 全部類型
    assert paths(types=["image"]) == {frozenset({"a.JPG", "b.png"})}    # 副檔名不分大小寫
    assert paths(types=["image", "model"]) == {frozenset({"a.JPG", "b.png"}),
                                               frozenset({"a.safetensors", "b.safetensors"})}
    assert paths(custom_exts=["xyz"]) == {frozenset({"a.xyz", "b.xyz"})}
    assert paths(types=["document"], custom_exts=["xyz"]) == {frozenset({"a.pdf", "b.pdf"}),
                                                              frozenset({"a.xyz", "b.xyz"})}


def test_min_size_and_empty_files(tree):
    tree("e1.txt", b"")
    tree("e2.txt", b"")                     # 空檔案彼此「相同」，但一律不算重複
    small = blob(2 * KB, 1)
    tree("s1.bin", small)
    tree("s2.bin", small)
    assert group_paths(make_scanner([tree.root], min_size=0).scan(), tree.root) ==         {frozenset({"s1.bin", "s2.bin"})}
    assert make_scanner([tree.root], min_size=4 * KB).scan() == []


def test_exclude_dirs_by_path_includes_subfolders(tree):
    data = blob(4 * KB, 1)
    for n in ("keep/a.bin", "other/c.bin", "skipme/d.bin", "skipme/deep/e.bin"):
        tree(n, data)
    s = make_scanner([tree.root], exclude_dirs=[str(tree.root / "SkipMe")])   # 不分大小寫
    assert group_paths(s.scan(), tree.root) == {frozenset({"keep/a.bin", "other/c.bin"})}
    assert s.stats.skipped_excluded == 1


def test_skip_system_folders(tree, monkeypatch):
    data = blob(4 * KB, 1)
    for n in ("a.bin", "web/node_modules/b.bin", "repo/__pycache__/c.bin", "fake_windows/d.bin"):
        tree(n, data)
    monkeypatch.setenv("SystemRoot", str(tree.root / "fake_windows"))   # 系統路徑由環境變數決定
    s = make_scanner([tree.root])
    assert s.scan() == []                                   # 只剩 a.bin 一份
    assert s.stats.skipped_excluded == 3
    groups = make_scanner([tree.root], skip_system=False).scan()
    assert len(groups[0].files) == 4


def test_scan_options_round_trip_and_bad_data():
    from src.scan_options import ScanOptions, parse_exts
    o = ScanOptions(roots=["D:/a"], types=["image", "video"], custom_exts=["psd"], min_size=1024,
                    exclude_dirs=["D:/a/tmp"], skip_system=False, include_hidden=True)
    assert ScanOptions.from_dict(o.to_dict()) == o
    bad = ScanOptions.from_dict({"roots": "D:/a", "types": ["image", "nope", 3], "min_size": -5,
                                 "custom_exts": [" .PSD", 1], "skip_system": "no"})
    assert bad.roots == [] and bad.types == ["image"] and bad.custom_exts == ["psd"]
    assert bad.min_size == ScanOptions().min_size and bad.skip_system is True
    assert ScanOptions.from_dict(None) == ScanOptions()
    assert parse_exts("psd, .AI  raw;psd，jxl") == ["psd", "ai", "raw", "jxl"]


def test_workers_per_device(tree, monkeypatch):
    data = blob(4 * KB, 1)
    tree("a.bin", data)
    tree("b.bin", data)
    dev = os.stat(tree.root).st_dev
    used = []
    orig = Scanner._hash_batch

    def spy(self, files, fn, workers):
        used.append(workers)
        return orig(self, files, fn, workers)

    monkeypatch.setattr(Scanner, "_hash_batch", spy)
    make_scanner([str(tree.root)], min_size=1, pass2_workers=1, pass3_workers=1,
            workers_by_dev={dev: (6, 3)}).scan()
    assert used == [6, 3]
