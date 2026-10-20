# 快速選取規則：決定每個重複群組要「保留」哪一份、勾選哪些待移除。
# 純邏輯、不依賴 Qt，UI（main_window）與測試共用。

from .models import DuplicateGroup

# 每個 tuple：(id, 顯示標籤, 排序 key 函式, reverse=True 表示值越大越優先, 互斥條件 id)
CONDITIONS_DEF = [
    ("newer",    "修改較新", lambda fi: fi.mtime,             True,  "older"),
    ("older",    "修改較舊", lambda fi: fi.mtime,             False, "newer"),
    ("sh_path",  "路徑較短", lambda fi: len(str(fi.path)),    False, "lo_path"),
    ("lo_path",  "路徑較長", lambda fi: len(str(fi.path)),    True,  "sh_path"),
    ("shallow",  "目錄較淺", lambda fi: len(fi.path.parts),   False, "deep"),
    ("deep",     "目錄較深", lambda fi: len(fi.path.parts),   True,  "shallow"),
    ("al_first", "字母較前", lambda fi: str(fi.path).lower(), False, "al_last"),
    ("al_last",  "字母較後", lambda fi: str(fi.path).lower(), True,  "al_first"),
    ("list_1st", "列表第一", None,                             False, None),
]
COND_BY_ID = {c[0]: c for c in CONDITIONS_DEF}


def condition(cond_id: str) -> tuple:
    """回傳 (id, key_fn, reverse)，供 rank_by_conditions 使用。"""
    c = COND_BY_ID[cond_id]
    return (c[0], c[2], c[3])


def rank_by_conditions(files: list, conditions: list) -> int:
    """多條件穩定排序，回傳「勝出」檔案的索引。

    以倒序套用條件（優先序最低的先排），後套用的條件優先序更高，
    Python sort 的穩定性確保同優先序的結果不受干擾。
    """
    if not conditions:
        return 0
    indices = list(range(len(files)))
    for cond_id, key_fn, rev in reversed(conditions):
        if cond_id == "list_1st" or key_fn is None:
            indices.sort(key=lambda i: i)
        else:
            indices.sort(key=lambda i: key_fn(files[i]), reverse=rev)
    return indices[0]


def apply_selection(group: DuplicateGroup, conditions: list, mode: str):
    """依條件找出勝出檔案，更新群組內各檔案的 selected 旗標。

    mode='keep'   → 保留勝出者（勝出者不勾選，其餘勾選待刪）
    mode='delete' → 刪除勝出者（只勾選勝出者）
    """
    winner = rank_by_conditions(group.files, conditions)
    for i, fi in enumerate(group.files):
        fi.selected = (i != winner) if mode == "keep" else (i == winner)
