# 快速選取規則
from pathlib import Path

import pytest

from src.models import DuplicateGroup, FileInfo
from src.selection import (
    CustomRule, FolderList, RuleSet, apply_selection, condition, copy_info, copy_scores,
    dump_presets, folder_scores, load_presets, parse_custom_rules, rank_by_conditions, split_dirs,
)


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


# ── 資料夾優先序 ───────────────────────────────────────────────────────────
def paths_selected(g):
    return [fi.path.as_posix() for fi in g.files if fi.selected]


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


def test_folder_scores_follow_list_order_and_deepest_match():
    keep = FolderList(["D:\\Models", "收藏"])
    remove = FolderList(["_trash", "Downloads"])
    g = group_of("D:/Models/a.bin", "E:/收藏/a.bin", "F:/other/a.bin",
                 "E:/Downloads/a.bin", "F:/_trash/a.bin", "D:/Models/_trash/a.bin")
    # 越前面分數絕對值越大；D:\Models\_trash 兩邊都符合 → 比較深的 _trash 為準
    assert folder_scores(g.files, keep, remove) == [-2, -1, 0, 1, 2, 2]
    # 一樣深時算優先保留
    assert folder_scores(group_of("D:/models/a.bin").files,
                         FolderList(["models"]), FolderList(["mod"])) == [-1]


def test_folder_priority_keep_and_delete_mode():
    rules = RuleSet(conditions=["folders"], keep_dirs=["D:\\Models"], remove_dirs=["_trash", "_bak"])
    g = group_of("D:/Models/_bak/m.safetensors", "D:/Models/m.safetensors",
                 "E:/_trash/m.safetensors")
    assert apply_selection(g, rules.build(), "keep") == "folders"
    assert paths_selected(g) == ["D:/Models/_bak/m.safetensors", "E:/_trash/m.safetensors"]
    # 刪除模式：只勾最該移除的（_trash 排在 _bak 前面）
    apply_selection(g, rules.build(), "delete")
    assert paths_selected(g) == ["E:/_trash/m.safetensors"]


def test_folder_priority_without_folders_is_a_tie():
    g = group_of("D:/a/x.bin", "D:/b/x.bin")
    assert apply_selection(g, [condition("folders")], "keep") is None
    assert paths_selected(g) == ["D:/b/x.bin"]


def test_smart_rules_stack_in_priority_order():
    g = group_of("D:/Pictures/photo (1).png", "E:/Downloads/photo.png")
    rules = RuleSet(keep_dirs=["D:\\Pictures"], remove_dirs=["Downloads"])
    rules.conditions = ["copy_name", "folders"]          # 先看檔名
    apply_selection(g, rules.build(), "keep")
    assert paths_selected(g) == ["D:/Pictures/photo (1).png"]
    rules.conditions = ["folders", "copy_name"]          # 先看資料夾
    apply_selection(g, rules.build(), "keep")
    assert paths_selected(g) == ["E:/Downloads/photo.png"]


def test_rule_set_build_skips_unknown_ids():
    rules = RuleSet(conditions=["copy_name", "no_such_rule", "sh_path"])
    assert [c.id for c in rules.build()] == ["copy_name", "sh_path"]


# ── 參考資料夾 ─────────────────────────────────────────────────────────────
def test_reference_file_is_kept_and_other_copies_selected():
    ref = RuleSet(reference_dirs=["D:\\Master"]).reference()
    g = group_of("D:/Master/a.png", "E:/copy/a.png", "F:/b/a.png")
    # 只看「路徑較短」本來會留 F:/b/a.png；有參考資料夾時保留參考資料夾那份
    assert apply_selection(g, [condition("sh_path")], "keep", ref) == "reference"
    assert paths_selected(g) == ["E:/copy/a.png", "F:/b/a.png"]


def test_several_reference_files_are_all_kept():
    ref = FolderList(["Master"])
    g = group_of("D:/Master/a.png", "D:/Master/x/a.png", "E:/copy/a.png")
    assert apply_selection(g, [condition("deep")], "keep", ref) == "deep"
    assert paths_selected(g) == ["E:/copy/a.png"]


def test_reference_file_is_never_the_delete_target():
    ref = FolderList(["D:\\Master"])
    g = group_of("D:/Master/a.png", "E:/old/a.png", "F:/new/a.png",
                 mtimes={"D:/Master/a.png": 1.0, "E:/old/a.png": 2.0})
    assert apply_selection(g, [condition("older")], "delete", ref) == "older"
    assert paths_selected(g) == ["E:/old/a.png"]
    g = group_of("D:/Master/a.png", "D:/Master/b/a.png")              # 整組都在參考資料夾
    assert apply_selection(g, [condition("older")], "delete", ref) == "reference"
    assert paths_selected(g) == []
    apply_selection(g, [condition("older")], "keep", ref)
    assert paths_selected(g) == []


def test_empty_reference_changes_nothing():
    g = group_of("D:/a/x.png", "D:/b/x.png")
    assert apply_selection(g, [condition("sh_path")], "keep", FolderList([])) is None
    assert paths_selected(g) == ["D:/b/x.png"]


# ── 自訂規則 ───────────────────────────────────────────────────────────────
def test_parse_custom_rules():
    text = "# 註解\n- \\.dup\\d+$\n\n+   ^D:/Models/\n－ /old/\n* 開頭不對\n-\n- (broken"
    rules, errors = parse_custom_rules(text)
    assert rules == [CustomRule("-", "\\.dup\\d+$"), CustomRule("+", "^D:/Models/"),
                     CustomRule("-", "/old/")]
    assert [e.split("：")[0] for e in errors] == ["第 6 行", "第 7 行", "第 8 行"]
    assert "正規表示式有誤" in errors[2]


def test_custom_rules_first_match_wins_and_both_separators_work():
    # 第 1 條用 / 寫、第 3 條用 \\ 寫，兩種都要能比對；不分大小寫
    rules = RuleSet(conditions=["custom"], custom="+ ^d:/models/\n- \\.dup\\d+$\n- \\\\old\\\\")
    g = group_of("D:/Models/m.safetensors.dup1", "E:/old/m.safetensors",
                 "F:/x/m.safetensors.dup1", "E:/y/m.safetensors")
    # 由上往下第一條符合的為準：D:/Models 那份同時符合第 1、2 條 → 算優先保留
    assert condition("custom", rules).keys(g.files) == [-3, 1, 2, 0]
    assert apply_selection(g, rules.build(), "keep") == "custom"
    assert paths_selected(g) == ["E:/old/m.safetensors", "F:/x/m.safetensors.dup1",
                                 "E:/y/m.safetensors"]
    apply_selection(g, rules.build(), "delete")
    assert paths_selected(g) == ["F:/x/m.safetensors.dup1"]


def test_custom_rules_skip_lines_with_errors():
    rules = RuleSet(conditions=["custom"], custom="- (broken\n- _bak")
    assert rules.custom_rules()[1]
    g = group_of("D:/_bak/a.bin", "D:/main/a.bin")
    apply_selection(g, rules.build(), "keep")
    assert paths_selected(g) == ["D:/_bak/a.bin"]
    assert condition("custom").keys(g.files) == [0, 0]          # 沒有規則 → 平手


# ── 預設集 ─────────────────────────────────────────────────────────────────
def test_rule_set_round_trip_and_tolerates_junk():
    rules = RuleSet(mode="delete", conditions=["copy_name", "folders", "older"],
                    keep_dirs=["D:\\Models"], remove_dirs=["_trash", "備份"],
                    reference_dirs=["E:\\正本"], custom="# 註解\n- \\.dup\\d+$")
    assert RuleSet.from_dict(rules.to_dict()) == rules
    junk = {"mode": "wipe", "conditions": ["older", 3, "nope", "older"], "keep_dirs": "D:\\x",
            "custom": None}
    assert RuleSet.from_dict(junk) == RuleSet(conditions=["older"])
    assert RuleSet.from_dict("not a dict") == RuleSet()


def test_presets_round_trip_keep_order():
    presets = {"模型整理": RuleSet(conditions=["folders"], remove_dirs=["_trash", "D:\\舊, 版本"]),
               "下載資料夾": RuleSet(mode="delete", conditions=["copy_name", "sh_path"])}
    loaded = load_presets(dump_presets(presets))
    assert list(loaded) == ["模型整理", "下載資料夾"]
    assert loaded == presets
    assert load_presets("") == {} and load_presets("{oops") == {} and load_presets("[1]") == {}
    text = '{"presets": [{"name": " "}, 5, {"name": " ok ", "conditions": ["newer"]}]}'
    assert load_presets(text) == {"ok": RuleSet(conditions=["newer"])}
