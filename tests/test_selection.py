# 快速選取規則
from pathlib import Path

import pytest

from src.models import DuplicateGroup, FileInfo
from src.selection import apply_selection, condition, copy_info, copy_scores, rank_by_conditions


def make_group():
    files = [
        FileInfo(path=Path("D:/a/bb/photo.jpg"), size=10, mtime=200.0),        # 0：中間時間、深 3 層
        FileInfo(path=Path("D:/photo.jpg"), size=10, mtime=100.0),             # 1：最舊、最短、最淺
        FileInfo(path=Path("D:/z/very/long/path/photo.jpg"), size=10, mtime=300.0),  # 2：最新、最長、最深
    ]
    return DuplicateGroup(hash_value="h", size=10, files=files)


@pytest.mark.parametrize("cond_id, winner", [
    ("newer", 2), ("older", 1),
    ("sh_path", 1), ("lo_path", 2),
    ("shallow", 1), ("deep", 2),
    ("al_first", 0), ("al_last", 2),
    ("list_1st", 0),
])
def test_single_condition_winner(cond_id, winner):
    g = make_group()
    assert rank_by_conditions(g.files, [condition(cond_id)]) == winner


def test_no_condition_keeps_first():
    g = make_group()
    assert rank_by_conditions(g.files, []) == 0


def test_stacked_conditions_respect_priority():
    g = make_group()
    g.files[0].mtime = g.files[2].mtime = 300.0          # 0 與 2 同為最新
    # 第一優先「修改較新」平手 → 由第二優先「路徑較短」決定
    assert rank_by_conditions(g.files, [condition("newer"), condition("sh_path")]) == 0
    # 優先序反過來：先看路徑較短 → 1 勝出
    assert rank_by_conditions(g.files, [condition("sh_path"), condition("newer")]) == 1


def test_keep_mode_selects_all_but_winner():
    g = make_group()
    apply_selection(g, [condition("newer")], "keep")
    assert [fi.selected for fi in g.files] == [True, True, False]


def test_delete_mode_selects_only_winner():
    g = make_group()
    apply_selection(g, [condition("newer")], "delete")
    assert [fi.selected for fi in g.files] == [False, False, True]


def test_condition_result_reports_deciding_rule():
    g = make_group()
    g.files[0].mtime = g.files[2].mtime = 300.0
    assert apply_selection(g, [condition("newer"), condition("sh_path")], "keep") == "sh_path"
    assert apply_selection(g, [condition("sh_path")], "keep") == "sh_path"
    g.files[1].mtime = 300.0
    assert apply_selection(g, [condition("newer")], "keep") is None     # 全部平手 → 取列表第一
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


def group_of(*paths, mtimes=None):
    files = [FileInfo(path=Path(p), size=10, mtime=(mtimes or {}).get(p, 100.0)) for p in paths]
    return DuplicateGroup(hash_value="h", size=10, files=files)


def names_selected(g):
    return [fi.path.name for fi in g.files if fi.selected]


def test_copy_scores_only_count_copies_of_files_in_the_group():
    g = group_of("D:/a/photo (1).png", "D:/a/photo (2).png", "D:/a/photo.png", "D:/a/IMG_1 (1).png")
    # IMG_1 (1).png 在群組裡沒有原名那份，看不出是副本
    assert copy_scores(g.files) == [1, 1, 0, 0]
    g = group_of("D:/a/photo (1) (1).png", "D:/a/photo (1).png")
    assert copy_scores(g.files) == [1, 0]


def test_copy_name_keeps_original_name():
    g = group_of("D:/dl/photo (1).png", "D:/dl/photo (2).png", "D:/dl/photo - 複製.png",
                 "D:/dl/photo.png", "D:/dl/photo.png.dup1")
    apply_selection(g, [condition("copy_name")], "keep")
    assert names_selected(g) == ["photo (1).png", "photo (2).png", "photo - 複製.png",
                                 "photo.png.dup1"]


def test_copy_name_breaks_plus_vs_space_tie_that_path_length_cannot():
    # 「+」和空白一樣長：只用「路徑較短」會平手，取列表第一（字母序 a/ 在前 → 留下 + 那份）
    g = group_of("D:/a/my+photo.png", "D:/b/my photo.png")
    assert len(str(g.files[0].path)) == len(str(g.files[1].path))
    assert apply_selection(g, [condition("sh_path")], "keep") is None
    assert names_selected(g) == ["my photo.png"]
    # 先看「像副本的檔名」：留下空白那份
    assert apply_selection(g, [condition("copy_name"), condition("sh_path")], "keep") == "copy_name"
    assert names_selected(g) == ["my+photo.png"]
    g = group_of("D:/a/my%20photo.png", "D:/b/my photo.png")
    apply_selection(g, [condition("copy_name")], "keep")
    assert names_selected(g) == ["my%20photo.png"]


def test_copy_name_ties_fall_through_to_next_condition():
    # 兩個原名彼此無關：「像副本」平手 → 交給「修改較新」
    g = group_of("D:/a/IMG_1.png", "D:/b/vacation.png", "D:/b/vacation (1).png",
                 mtimes={"D:/b/vacation.png": 50.0})
    assert apply_selection(g, [condition("copy_name"), condition("newer")], "keep") == "newer"
    assert names_selected(g) == ["vacation.png", "vacation (1).png"]


def test_copy_name_direction_is_fixed_in_delete_mode():
    # 刪除模式：只勾選最像副本的那一份（智慧規則方向固定，不會反過來勾原名）
    g = group_of("D:/dl/photo (1) (1).png", "D:/dl/photo (1).png", "D:/dl/photo.png")
    apply_selection(g, [condition("copy_name")], "delete")
    assert names_selected(g) == ["photo (1) (1).png"]
