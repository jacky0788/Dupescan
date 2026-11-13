# 整個資料夾都是重複的：判定、只列最上層、互為副本不能一起移除、移除前重新檢查、整個移到隔離資料夾
import os
from pathlib import Path

import pytest

from helpers import blob, make_scanner
from src import file_ops
from src.folder_dupes import FolderFinder, conflict, verify_folders

KB = 1024


def scan(tree):
    return make_scanner([str(tree.root)], min_size=1, skip_system=False).scan()


def finder(tree, groups, **kw):
    return FolderFinder(groups, [str(tree.root)], **kw)


def names(folders, root):
    return [Path(f.path).relative_to(root).as_posix() for f in folders]


@pytest.fixture
def photos(tree):
    """Photos 是正本；Backup 整個是 Photos 的副本（含子資料夾）；Mixed 有一個檔案別處沒有。"""
    a, b, c = blob(4 * KB, 1), blob(4 * KB, 2), blob(4 * KB, 3)
    tree("Photos/a.jpg", a)
    tree("Photos/2020/b.jpg", b)
    tree("Photos/c.jpg", c)
    tree("Backup/a.jpg", a)
    tree("Backup/2020/b.jpg", b)
    tree("Backup/2020/Thumbs.db", blob(100, 9))      # 系統小檔：不影響判定，跟著移除
    tree("Backup/empty.txt", b"")                    # 空檔案：同上
    (tree.root / "Backup/空資料夾").mkdir()
    tree("Mixed/c.jpg", c)
    tree("Mixed/only-here.txt", blob(2 * KB, 4))
    return tree


def test_find_all_lists_only_topmost_fully_duplicated_folders(photos):
    groups = scan(photos)
    found = finder(photos, groups).find_all()
    # Photos 的每個檔案在 Backup 都有……但 c.jpg 只有 Mixed 有 → Photos 也算（c.jpg 在 Mixed 有副本）
    assert sorted(names(found, photos.root)) == ["Backup", "Photos"]
    backup = next(f for f in found if f.name == "Backup")
    assert len(backup.files) == 2 and backup.size == 8 * KB and backup.subfolders == 2
    assert sorted(os.path.basename(p) for p in backup.extra) == ["Thumbs.db", "empty.txt"]
    assert "等 2 個資料夾" in backup.elsewhere_text()
    photos_dir = next(f for f in found if f.name == "Photos")
    assert "不能同時移除" in conflict([backup], photos_dir)      # 互為副本


def test_folder_with_unique_file_is_not_listed_and_reason_is_given(photos):
    groups = scan(photos)
    found, reason = finder(photos, groups).find_for(str(photos.root / "Mixed"))
    assert found == [] and "only-here.txt" in reason and "別處沒有" in reason


def test_files_filtered_out_by_scan_still_count(tree):
    """掃描時被篩掉的檔案（這裡是小於下限的）也是資料夾的內容，資料夾就不算整個重複。"""
    data = blob(4 * KB, 1)
    tree("A/x.bin", data)
    tree("B/x.bin", data)
    tree("B/small.bin", b"12")                      # 小於 min_size，掃描結果裡沒有
    groups = make_scanner([str(tree.root)], min_size=100, skip_system=False).scan()
    found = finder(tree, groups).find_all()
    assert names(found, tree.root) == ["A"]


def test_copies_only_inside_folder_do_not_count(tree):
    data = blob(4 * KB, 1)
    tree("Box/one/x.bin", data)
    tree("Box/two/x.bin", data)
    groups = scan(tree)
    f = finder(tree, groups)
    found = f.find_all()
    # Box 裡兩份都在 Box 裡 → Box 不行；one、two 各自可以，但不能同時移除
    assert sorted(names(found, tree.root)) == ["Box/one", "Box/two"]
    one, two = sorted(found, key=lambda x: x.path)
    assert conflict([one], two) and "不能同時移除" in conflict([one], two)
    assert conflict([], two) == ""
    df, reason = f.check(str(tree.root / "Box"))
    assert df is None and "只在這個資料夾裡" in reason


def test_nested_folders_cannot_both_be_selected(photos):
    groups = scan(photos)
    found, _ = finder(photos, groups).find_for(str(photos.root / "Backup" / "2020"))
    assert names(found, photos.root) == ["Backup", "Backup/2020"]
    assert "包含" in conflict([found[0]], found[1])
    assert "在這個資料夾裡" in conflict([found[1]], found[0])


def test_protected_and_reference_dirs_are_never_listed(photos):
    groups = scan(photos)
    found = finder(photos, groups, reference_dirs=[str(photos.root / "Backup")]).find_all()
    assert names(found, photos.root) == ["Photos"]
    found = finder(photos, groups, protected=["*/backup/*"]).find_all()
    assert names(found, photos.root) == ["Photos"]


def test_scan_root_and_volume_root(photos):
    groups = scan(photos)
    f = finder(photos, groups)
    df, reason = f.check(photos.root.anchor)
    assert df is None and "根目錄" in reason
    df, reason = f.check(str(photos.root.parent))
    assert df is None and "範圍" in reason


def test_verify_detects_new_or_changed_files(photos):
    groups = scan(photos)
    found = finder(photos, groups).find_all()
    backup = next(f for f in found if f.name == "Backup")
    ok, skipped = verify_folders([backup], finder(photos, groups))
    assert [f.name for f in ok] == ["Backup"] and not skipped

    photos("Backup/2020/new.txt", blob(KB, 7))        # 檢查之後又放進新檔案
    ok, skipped = verify_folders([backup], finder(photos, groups))
    assert not ok and "new.txt" in skipped[0][1]
    os.remove(photos.root / "Backup/2020/new.txt")

    os.remove(photos.root / "Photos/2020/b.jpg")       # 外面那份不見了
    ok, skipped = verify_folders([backup], finder(photos, groups))
    assert not ok and "b.jpg" in skipped[0][1]


def test_verify_full_compare_catches_same_head_different_tail(tree):
    head = blob(8 * KB, 1)
    tree("A/x.bin", head + b"A" * KB)
    tree("B/x.bin", head + b"A" * KB)
    groups = scan(tree)
    df = finder(tree, groups).check(str(tree.root / "B"))[0]
    a = tree.root / "A/x.bin"
    mtime_ns = a.stat().st_mtime_ns
    a.write_bytes(head + b"B" * KB)                    # 掃描後被改掉（大小沒變）
    os.utime(a, ns=(mtime_ns, mtime_ns))               # 修改時間也偽裝成沒變
    ok, _ = verify_folders([df], finder(tree, groups), full_verify=False)
    assert ok                                          # 只看開頭分不出來
    ok, skipped = verify_folders([df], finder(tree, groups), full_verify=True)
    assert not ok and "內容" in skipped[0][1]


def test_remove_whole_folder_to_quarantine_and_restore(photos, tmp_path, monkeypatch):
    from src.ui.removal import remove_folders
    qroot = tmp_path / "Q"
    monkeypatch.setattr(file_ops, "quarantine_root_for", lambda p: qroot)
    groups = scan(photos)
    backup = next(f for f in finder(photos, groups).find_all() if f.name == "Backup")
    removed, results = remove_folders([backup], lambda: finder(photos, groups), "quarantine", None, True,
                                      lambda *a, **k: None)
    assert [f.name for f in removed] == ["Backup"] and all(r.ok for r in results)
    assert not (photos.root / "Backup").exists()
    assert (photos.root / "Photos/2020/b.jpg").exists()
    entry = file_ops.read_journal()[0]
    assert entry["quarantined"] == 1 and entry["bytes"] == 8 * KB
    restored = file_ops.restore_batch(Path(entry["manifests"][0]))
    assert all(r.ok for r in restored)
    assert (photos.root / "Backup/2020/Thumbs.db").exists() and (photos.root / "Backup/空資料夾").is_dir()


def test_results_model_drops_files_under_removed_folder(qapp, photos):
    from src.selection import AutoRules
    from src.ui.results_page import ResultsInfo, ResultsPage
    groups = scan(photos)
    page = ResultsPage()
    page.set_results(groups, ResultsInfo(roots=[str(photos.root)]), AutoRules())
    assert len(page.groups()) == 3
    page.folders_removed([str(photos.root / "Backup")])
    left = {fi.path.relative_to(photos.root).as_posix() for g in page.groups() for fi in g.files}
    assert left == {"Photos/c.jpg", "Mixed/c.jpg"}       # a.jpg、b.jpg 只剩一份，不再是重複
