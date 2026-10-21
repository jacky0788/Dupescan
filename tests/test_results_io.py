# 結果匯出 CSV、儲存與再開啟
import csv
import gzip

import pytest

from helpers import blob, make_scanner
from src.results_io import export_csv, load_results, save_results

KB = 1024


def scanned(tree):
    data = blob(4 * KB, 1)
    for n in ("a.bin", "x/b.bin", "y/c.bin"):
        tree(n, data)
    groups = make_scanner([str(tree.root)], min_size=1).scan()
    groups[0].files[1].selected = True
    groups[0].files[0].aliases = [str(tree.root / "link.bin")]
    return groups


def test_save_and_load_round_trip(tree, tmp_path):
    groups = scanned(tree)
    path = tmp_path / "r.dupescan"
    save_results(path, groups, [str(tree.root)], "2026-10-20T12:00:00")
    loaded, meta = load_results(path)
    assert meta["roots"] == [str(tree.root)] and meta["scanned_at"] == "2026-10-20T12:00:00"
    assert len(loaded) == 1
    a, b = groups[0], loaded[0]
    assert (b.hash_value, b.size) == (a.hash_value, a.size)
    assert [(f.path, f.selected, f.mtime_ns, f.aliases) for f in b.files] == \
           [(f.path, f.selected, f.mtime_ns, f.aliases) for f in a.files]


def test_load_rejects_other_files(tmp_path):
    bad = tmp_path / "bad.dupescan"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError):
        load_results(bad)
    other = tmp_path / "other.dupescan"
    with gzip.open(other, "wt", encoding="utf-8") as f:
        f.write('{"format": "something-else"}')
    with pytest.raises(ValueError, match="不是"):
        load_results(other)


def test_export_csv(tree, tmp_path):
    groups = scanned(tree)
    path = tmp_path / "r.csv"
    export_csv(path, groups)
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    assert rows[0][:2] == ["群組", "處理"] and len(rows) == 4
    assert [r[1] for r in rows[1:]].count("移除") == 1
