# 移除計畫：整組全刪、掃描後變動、保留的那份不見、受保護資料夾
import os
from pathlib import Path

from helpers import blob
from src.delete_plan import (
    KEEP, OK, PROTECTED, PROTECTED_NOTE, REFERENCE_NOTE, SKIP, build_plan, is_protected,
)
from src.scanner import Scanner

KB = 1024


def scan_one_group(tree, names, data=None):
    data = data or blob(8 * KB, 1)
    for n in names:
        tree(n, data)
    groups = Scanner([str(tree.root)], min_size=1).scan()
    assert len(groups) == 1
    return groups[0]


def by_name(plan):
    return {e.file.path.name: e for g in plan.groups for e in g.entries}


def select(group, *names):
    for fi in group.files:
        fi.selected = fi.path.name in names


def test_normal_selection(tree):
    g = scan_one_group(tree, ["a.bin", "b.bin", "c.bin"])
    select(g, "b.bin", "c.bin")
    plan = build_plan([(1, g)])
    st = {n: e.status for n, e in by_name(plan).items()}
    assert st == {"a.bin": KEEP, "b.bin": OK, "c.bin": OK}
    assert [e.file.path.name for _, e in plan.removable()] == ["b.bin", "c.bin"]


def test_unselected_groups_are_ignored(tree):
    g = scan_one_group(tree, ["a.bin", "b.bin"])
    assert build_plan([(1, g)]).groups == []


def test_whole_group_is_blocked_unless_allowed(tree):
    g = scan_one_group(tree, ["a.bin", "b.bin"])
    select(g, "a.bin", "b.bin")
    plan = build_plan([(1, g)])
    assert plan.groups[0].whole_group
    assert plan.removable() == []
    assert len(plan.removable(allow_whole_group=True)) == 2


def test_changed_or_missing_files_are_skipped(tree):
    g = scan_one_group(tree, ["a.bin", "b.bin", "c.bin", "d.bin"])
    select(g, "b.bin", "c.bin", "d.bin")
    b, c, d = (tree.root / n for n in ("b.bin", "c.bin", "d.bin"))
    st = os.stat(b)
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # 修改時間變了
    c.unlink()                                                          # 檔案不見了
    data = bytearray(d.read_bytes())
    data[0] ^= 0xFF                                                     # 開頭內容變了，大小與時間不變
    st = os.stat(d)
    d.write_bytes(bytes(data))
    os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns))
    entries = by_name(build_plan([(1, g)]))
    assert entries["b.bin"].status == SKIP and "修改時間" in entries["b.bin"].note
    assert entries["c.bin"].status == SKIP and "不存在" in entries["c.bin"].note
    assert entries["d.bin"].status == SKIP and "開頭內容" in entries["d.bin"].note


def test_group_is_skipped_when_kept_copy_is_gone(tree):
    g = scan_one_group(tree, ["a.bin", "b.bin"])
    select(g, "b.bin")
    (tree.root / "a.bin").unlink()                  # 唯一保留的那份不見了
    plan = build_plan([(1, g)])
    e = by_name(plan)["b.bin"]
    assert e.status == SKIP and "保留的那份" in e.note
    assert plan.removable(allow_whole_group=True, allow_protected=True) == []


def test_protected_folders(tree):
    g = scan_one_group(tree, ["keep.bin", "web/node_modules/x.bin", "lib/site-packages/y.bin"])
    select(g, "x.bin", "y.bin")
    plan = build_plan([(1, g)])
    entries = by_name(plan)
    assert entries["x.bin"].status == PROTECTED
    assert entries["y.bin"].status == PROTECTED
    assert plan.removable() == []
    assert len(plan.removable(allow_protected=True)) == 2


def test_is_protected_patterns():
    assert is_protected(Path("C:/Windows/System32/a.dll"))
    assert is_protected(Path("D:/Ollama/models/blobs/sha256-abc"))
    assert is_protected(Path("C:/Users/me/.ollama/models/blobs/sha256-abc"))
    assert is_protected(Path("D:/Games/SteamLibrary/steamapps/common/x.pak"))
    assert not is_protected(Path("D:/Photos/windows/a.jpg"))     # 不是磁碟根目錄的 Windows
    assert not is_protected(Path("E:/Downloads/model.safetensors"))


def test_reference_folders_are_treated_as_protected(tree):
    g = scan_one_group(tree, ["keep.bin", "正本/m.bin", "other/o.bin", "web/node_modules/n.bin"])
    select(g, "m.bin", "o.bin", "n.bin")
    for dirs in ([str(tree.root / "正本")], ["正本"]):          # 完整路徑或名稱片段都可以
        entries = by_name(build_plan([(1, g)], reference_dirs=dirs))
        assert (entries["m.bin"].status, entries["m.bin"].note) == (PROTECTED, REFERENCE_NOTE)
        assert (entries["n.bin"].status, entries["n.bin"].note) == (PROTECTED, PROTECTED_NOTE)
        assert entries["o.bin"].status == OK
    plan = build_plan([(1, g)], reference_dirs=["正本"])
    assert [e.file.path.name for _, e in plan.removable()] == ["o.bin"]
    assert len(plan.removable(allow_protected=True)) == 3
    assert by_name(build_plan([(1, g)]))["m.bin"].status == OK     # 沒設定參考資料夾
