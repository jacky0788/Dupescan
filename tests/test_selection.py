# 快速選取規則
from pathlib import Path

import pytest

from src.models import DuplicateGroup, FileInfo
from src.selection import apply_selection, condition, rank_by_conditions


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
