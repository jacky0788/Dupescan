# 自動選取規則：每一組保留哪一份
from pathlib import Path

import pytest

from src.models import DuplicateGroup, FileInfo
from src.selection import (
    REASONS, AutoRules, FolderList, apply_selection, auto_select, condition, copy_info, copy_scores,
    folders_condition, is_protected_file, pick_keeper, remove_scores, split_dirs,
)


def group_of(*paths, mtimes=None):
    files = [FileInfo(path=Path(p), size=10, mtime=(mtimes or {}).get(p, 100.0)) for p in paths]
    return DuplicateGroup(hash_value="h", size=10, files=files)


def removed(g):
    return [fi.path.as_posix() for fi in g.files if fi.selected]


def names_removed(g):
    return [fi.path.name for fi in g.files if fi.selected]


# ── 條件 ─────────────────────────────────────────────────────────────────
def make_group():
    return group_of("D:/a/bb/photo.jpg", "D:/photo.jpg", "D:/z/very/long/path/photo.jpg",
                    mtimes={"D:/a/bb/photo.jpg": 200.0, "D:/photo.jpg": 100.0,
                            "D:/z/very/long/path/photo.jpg": 300.0})


@pytest.mark.parametrize("cond_id, winner", [("older", 1), ("newer", 2), ("sh_path", 1)])
def test_single_condition_keeper(cond_id, winner):
    assert pick_keeper(make_group().files, [condition(cond_id)]) == (winner, cond_id)


def test_no_condition_keeps_first():
    assert pick_keeper(make_group().files, []) == (0, None)


def test_conditions_apply_in_order_and_ties_fall_through():
    g = make_group()
    g.files[0].mtime = g.files[2].mtime = 300.0           # 0 和 2 一樣新
    assert pick_keeper(g.files, [condition("newer"), condition("sh_path")]) == (0, "sh_path")
    g.files[1].mtime = 300.0
    assert apply_selection(g, [condition("newer")]) is None          # 全部平手 → 保留第一份
    assert [fi.selected for fi in g.files] == [False, True, True]


# ── 像副本的檔名 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("name, canonical, markers", [
    ("photo.png",               "photo.png", 0),
    ("photo (1).png",           "photo.png", 1),
    ("photo (99).png",          "photo.png", 1),
    ("photo(2).png",            "photo.png", 1),
    ("photo（3）.png",           "photo.png", 1),
    ("photo - 複製.png",         "photo.png", 1),
    ("photo - 複製 (2).png",     "photo.png", 1),
    ("photo - Copy.png",        "photo.png", 1),
    ("photo - Copy (3).png",    "photo.png", 1),
    ("Copy of photo.png",       "photo.png", 1),
    ("Copy (2) of photo.png",   "photo.png", 1),
    ("photo copy 2.png",        "photo.png", 1),
    ("model.safetensors.dup1",  "model.safetensors", 1),
    ("model.dup2.safetensors",  "model.safetensors", 1),
    ("my+photo.png",            "my photo.png", 1),
    ("my%20photo.png",          "my photo.png", 1),
    ("my+photo (1).png",        "my photo.png", 2),
    ("photo (1) (1).png",       "photo.png", 2),
    ("photo (1) - 複製.png",     "photo.png", 2),
    ("archive (1).tar.gz",      "archive.tar.gz", 1),
    ("Photo.PNG",               "photo.png", 0),
    # 不是複本標記
    ("photo (100).png",         "photo (100).png", 0),
    ("Episode (2019).mp4",      "episode (2019).mp4", 0),
    ("photo (0).png",           "photo (0).png", 0),
    ("model.dupe.png",          "model.dupe.png", 0),
    ("100% done.png",           "100% done.png", 0),
])
def test_copy_info(name, canonical, markers):
    assert copy_info(name) == (canonical, markers)


def test_copy_scores_only_count_copies_of_files_in_the_group():
    g = group_of("D:/a/photo (1).png", "D:/a/photo (2).png", "D:/a/photo.png", "D:/a/IMG_1 (1).png")
    # IMG_1 (1).png 在群組裡沒有原名那份，看不出是副本
    assert copy_scores(g.files) == [1, 1, 0, 0]
    g = group_of("D:/a/photo (1) (1).png", "D:/a/photo (1).png")
    assert copy_scores(g.files) == [1, 0]


def test_copy_name_keeps_original_name():
    g = group_of("D:/dl/photo (1).png", "D:/dl/photo (2).png", "D:/dl/photo - 複製.png",
                 "D:/dl/photo.png", "D:/dl/photo.png.dup1")
    assert apply_selection(g, [condition("copy_name")]) == "copy_name"
    assert names_removed(g) == ["photo (1).png", "photo (2).png", "photo - 複製.png", "photo.png.dup1"]


def test_copy_name_breaks_plus_vs_space_tie_that_path_length_cannot():
    g = group_of("D:/a/my+photo.png", "D:/b/my photo.png")
    assert len(str(g.files[0].path)) == len(str(g.files[1].path))
    assert apply_selection(g, [condition("sh_path")]) is None          # 一樣長：保留第一份（+ 那份）
    assert names_removed(g) == ["my photo.png"]
    assert apply_selection(g, [condition("copy_name"), condition("sh_path")]) == "copy_name"
    assert names_removed(g) == ["my+photo.png"]


def test_copy_name_ties_fall_through_to_next_condition():
    # 兩個原名彼此無關：「原始檔名」平手 → 交給「最新的」
    g = group_of("D:/a/IMG_1.png", "D:/b/vacation.png", "D:/b/vacation (1).png",
                 mtimes={"D:/b/vacation.png": 50.0})
    assert apply_selection(g, [condition("copy_name"), condition("newer")]) == "newer"
    assert names_removed(g) == ["vacation.png", "vacation (1).png"]


# ── 資料夾 ───────────────────────────────────────────────────────────────
def test_split_dirs():
    assert split_dirs(' _trash ; "D:\\My Models" ;; Downloads ') == ["_trash", "D:\\My Models", "Downloads"]
    assert split_dirs("  ") == []


@pytest.mark.parametrize("pattern, path, hit", [
    ("D:\\Models",       "D:/Models/a.bin",            True),
    ("d:/models/",       "D:/Models/sub/a.bin",        True),    # 含子資料夾、不分大小寫
    ("D:\\Models",       "D:/Models2/a.bin",           False),   # 不同資料夾
    ("D:\\Models",       "E:/Models/a.bin",            False),
    ("D:\\Models",       "D:/Models.bin",              False),   # 只比對資料夾
    ("D:",               "D:/x/a.bin",                 True),
    ("\\\\NAS\\share",   "//NAS/share/x/a.bin",        True),
    ("_trash",           "E:/a/_trash/b/c.bin",        True),
    ("_trash",           "E:/a/old_trash_2023/c.bin",  True),    # 名稱片段：含有就算
    ("_trash",           "E:/a/_trash.bin",            False),   # 檔名不算
    ("\\old\\",          "E:/old/a.bin",               True),    # 前後加分隔符號＝整個名稱相同
    ("\\old\\",          "E:/older/a.bin",             False),
    ("models\\blobs",    "D:/Ollama/models/blobs/x",   True),
    ("下載",             "D:/我的下載/a.png",          True),
    ("\\",               "D:/a/b.png",                 False),   # 只有分隔符號的樣式略過
])
def test_folder_list_matching(pattern, path, hit):
    assert FolderList([pattern]).contains(Path(path)) is hit


def test_remove_scores_follow_list_order():
    remove = FolderList(["_trash", "Downloads"])
    g = group_of("D:/Models/a.bin", "E:/Downloads/a.bin", "F:/_trash/a.bin", "D:/Downloads/_trash/a.bin")
    assert remove_scores(g.files, remove) == [0, 1, 2, 2]


def test_remove_folders_condition():
    g = group_of("E:/_trash/m.bin", "D:/Models/m.bin", "D:/Downloads/m.bin")
    assert apply_selection(g, [folders_condition(["_trash", "Downloads"])]) == "folders"
    assert removed(g) == ["E:/_trash/m.bin", "D:/Downloads/m.bin"]
    g = group_of("D:/a/x.bin", "D:/b/x.bin")
    assert apply_selection(g, [folders_condition(["_trash"])]) is None   # 都不在：平手


# ── 保護的資料夾 ─────────────────────────────────────────────────────────
def test_protected_file_is_kept_and_other_copies_removed():
    g = group_of("D:/Master/a.png", "E:/copy/a.png", "F:/b/a.png")
    # 只看「路徑最短」本來會留 F:/b/a.png；有保護的資料夾時保留它裡面那份
    assert apply_selection(g, [condition("sh_path")], FolderList(["D:\\Master"])) == "protected"
    assert removed(g) == ["E:/copy/a.png", "F:/b/a.png"]


def test_several_protected_files_are_all_kept():
    g = group_of("D:/Master/a.png", "D:/Master/x/a.png", "E:/copy/a.png")
    assert apply_selection(g, [condition("sh_path")], FolderList(["Master"])) == "sh_path"
    assert removed(g) == ["E:/copy/a.png"]


def test_empty_protected_list_changes_nothing():
    g = group_of("D:/a/x.png", "D:/b/x.png")
    assert apply_selection(g, [condition("sh_path")], FolderList([])) is None
    assert removed(g) == ["D:/b/x.png"]


# ── 自動選取規則 ─────────────────────────────────────────────────────────
def kept(g):
    return [fi.path.as_posix() for fi in g.files if not fi.selected]


def grp(*paths, mtimes):
    return group_of(*paths, mtimes=dict(zip(paths, mtimes)))


def test_auto_select_keeps_original_name_then_oldest():
    g1 = grp("D:/a/photo (1).jpg", "D:/b/photo.jpg", mtimes=[100, 200])
    g2 = grp("D:/a/x.jpg", "D:/b/y.jpg", mtimes=[300, 100])
    counts = auto_select([g1, g2], AutoRules())
    assert kept(g1) == ["D:/b/photo.jpg"] and g1.keep_reason == REASONS["copy_name"]
    assert kept(g2) == ["D:/b/y.jpg"] and g2.keep_reason == REASONS["older"]
    assert counts == {REASONS["copy_name"]: 1, REASONS["older"]: 1}


@pytest.mark.parametrize("fallback, winner", [
    ("oldest", "D:/long/folder/b.jpg"), ("newest", "D:/c.jpg"), ("shortest", "D:/c.jpg")])
def test_auto_select_fallbacks(fallback, winner):
    g = grp("D:/long/folder/b.jpg", "D:/c.jpg", mtimes=[100, 200])
    auto_select([g], AutoRules(copy_names=False, fallback=fallback))
    assert kept(g) == [winner]


def test_auto_select_remove_folders_come_first():
    g = grp("D:/Downloads/photo.jpg", "D:/Pictures/photo (1).jpg", mtimes=[100, 200])
    auto_select([g], AutoRules(remove_dirs=["Downloads"]))
    assert kept(g) == ["D:/Pictures/photo (1).jpg"]
    assert g.keep_reason == REASONS["folders"]


def test_auto_select_protected_files_are_always_kept():
    g = grp("D:/Archive/a.jpg", "D:/Archive/b.jpg", "D:/c.jpg", mtimes=[300, 200, 100])
    rules = AutoRules(protected_dirs=["D:/Archive"])
    auto_select([g], rules)
    assert kept(g) == ["D:/Archive/a.jpg", "D:/Archive/b.jpg"]
    assert is_protected_file(g.files[0], rules) and not is_protected_file(g.files[2], rules)


def test_auto_select_tie_keeps_first():
    g = grp("D:/a/x.jpg", "D:/b/x.jpg", mtimes=[100, 100])
    auto_select([g], AutoRules(fallback="oldest"))
    assert kept(g) == ["D:/a/x.jpg"] and g.keep_reason == REASONS[None]


def test_auto_rules_round_trip_and_tolerates_junk():
    rules = AutoRules(copy_names=False, protected_dirs=["D:/A"], remove_dirs=["_trash"], fallback="newest")
    assert AutoRules.from_dict(rules.to_dict()) == rules
    assert AutoRules.from_dict({"copy_names": "x", "protected_dirs": "D:/A", "remove_dirs": [1, "", "ok"],
                                "fallback": "weird"}) == AutoRules(remove_dirs=["ok"])
    assert AutoRules.from_dict(None) == AutoRules()
