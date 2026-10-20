# 掃描引擎的基本行為
import threading
import time

from helpers import blob, group_paths
from src.scanner import Scanner

KB = 1024


def scan(root, **kw):
    return Scanner([str(root)], **kw).scan()


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
    s = Scanner([str(tree.root)], min_size=1)
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
