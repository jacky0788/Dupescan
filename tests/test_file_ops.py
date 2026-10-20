# 檔案處理：資源回收筒／隔離資料夾／還原（不會永久刪除）
import json
import os
from pathlib import Path

import pytest

from helpers import blob
from src import file_ops

KB = 1024


@pytest.fixture
def qroot(tmp_path, monkeypatch):
    """隔離資料夾改到暫存資料夾，避免測試寫到磁碟根目錄。"""
    root = tmp_path / "Q"
    monkeypatch.setattr(file_ops, "quarantine_root_for", lambda p: root)
    return root


def make_files(tree, n=3):
    return [(tree(f"d{i}/f{i}.bin", blob(4 * KB, i)), 4 * KB) for i in range(n)]


def test_quarantine_moves_files_and_restores(tree, qroot):
    items = make_files(tree)
    originals = {p: p.read_bytes() for p, _ in items}
    results = file_ops.remove_files(items, "quarantine")

    assert all(r.ok and r.method == "quarantine" for r in results)
    assert not any(p.exists() for p, _ in items)
    for r in results:
        assert Path(r.dest).read_bytes() == originals[Path(r.path)]

    entry = file_ops.read_journal()[0]
    assert entry["quarantined"] == 3 and entry["bytes"] == 3 * 4 * KB
    manifest = Path(entry["manifests"][0])
    assert file_ops.manifest_status(manifest) == (0, 3)

    restored = file_ops.restore_batch(manifest)
    assert all(r.ok for r in restored)
    assert {p: p.read_bytes() for p in originals} == originals
    assert file_ops.manifest_status(manifest) == (3, 3)
    assert file_ops.restore_batch(manifest) == []          # 再還原一次不會重複動作


def test_restore_never_overwrites_existing_file(tree, qroot):
    items = make_files(tree, 1)
    path = items[0][0]
    file_ops.remove_files(items, "quarantine")
    path.write_bytes(b"new file at the same place")
    manifest = Path(file_ops.read_journal()[0]["manifests"][0])
    (res,) = file_ops.restore_batch(manifest)
    assert not res.ok and "同名" in res.note
    assert path.read_bytes() == b"new file at the same place"
    stored = json.loads(manifest.read_text(encoding="utf-8"))["items"][0]["stored"]
    assert Path(stored).exists()                           # 隔離的那份還在


@pytest.mark.parametrize("drive_type, nuke, cap_mb, size, expect", [
    (4, None, None, 10, "網路磁碟"),
    (2, None, None, 10, "卸除式"),
    (3, 1, None, 10, "停用"),
    (3, 0, 1, 2 * 1024 * 1024, "容量上限"),
    (3, 0, 1, 1024, ""),
    (3, None, None, 10, ""),
])
def test_recycle_obstacle(monkeypatch, drive_type, nuke, cap_mb, size, expect):
    monkeypatch.setattr(file_ops, "_drive_type", lambda anchor: drive_type)
    monkeypatch.setattr(file_ops, "_bitbucket_settings", lambda anchor: (nuke, cap_mb))
    reason = file_ops.recycle_obstacle(Path("D:/x/y.bin"), size)
    assert (expect in reason) if expect else reason == ""


def test_long_path_cannot_go_to_recycle_bin():
    assert "路徑太長" in file_ops.recycle_obstacle(Path("D:/" + "a" * 300), 1)


def test_files_that_cannot_be_recycled_go_to_quarantine(tree, qroot, monkeypatch):
    monkeypatch.setattr(file_ops, "recycle_obstacle", lambda p, s, c=None: "網路磁碟沒有資源回收筒")
    shell = []
    monkeypatch.setattr(file_ops, "_shell_recycle", lambda paths, hwnd: shell.append(paths))
    results = file_ops.remove_files(make_files(tree), "recycle")
    assert shell == []
    assert all(r.ok and r.method == "quarantine" and "網路磁碟" in r.note for r in results)


def test_shell_failure_falls_back_to_quarantine(tree, qroot, monkeypatch):
    monkeypatch.setattr(file_ops, "recycle_obstacle", lambda p, s, c=None: "")
    monkeypatch.setattr(file_ops, "_shell_recycle", lambda paths, hwnd: (0x7C, False))
    results = file_ops.remove_files(make_files(tree), "recycle")
    assert all(r.ok and r.method == "quarantine" and "0x7c" in r.note for r in results)


def test_recycled_and_cancelled_are_reported(tree, qroot, monkeypatch):
    items = make_files(tree, 2)
    monkeypatch.setattr(file_ops, "recycle_obstacle", lambda p, s, c=None: "")

    def fake_shell(paths, hwnd):
        os.remove(paths[0])          # 第一個「進了回收筒」，第二個在 Windows 提示中被取消
        return 0x4C7, True

    monkeypatch.setattr(file_ops, "_shell_recycle", fake_shell)
    results = {Path(r.path).name: r for r in file_ops.remove_files(items, "recycle")}
    assert results["f0.bin"].ok and results["f0.bin"].method == "recycle"
    assert not results["f1.bin"].ok and "取消" in results["f1.bin"].note
    assert items[1][0].exists()      # 取消的檔案原封不動，也不會被搬去隔離區


@pytest.mark.skipif(not os.environ.get("DUPESCAN_TEST_RECYCLE"),
                    reason="會在真的資源回收筒留下測試檔，設定 DUPESCAN_TEST_RECYCLE=1 才執行")
def test_real_recycle_bin(tree):
    p = tree("dupescan-recycle-selftest.txt", b"DupeScan recycle bin self-test, safe to delete.")
    (res,) = file_ops.remove_files([(p, p.stat().st_size)], "recycle")
    assert res.ok and res.method == "recycle", res
    assert not p.exists()
