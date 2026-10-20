# 快速選取規則：決定每個重複群組要「保留」哪一份、勾選哪些待移除。
# 純邏輯、不依賴 Qt，UI（main_window）與測試共用。
#
#   基本條件 — 依單一檔案的屬性比較（修改時間、路徑長短、目錄深淺、字母序、列表順序）
#   智慧規則 — 要看整個群組才能判斷（例如「像副本的檔名」要看群組裡有沒有原名那份）；
#              方向固定：值越大越該移除，保留模式與刪除模式都一樣
#
# 條件可疊加：依優先序逐一比較，前面的條件平手才看下一個；全部平手時取列表第一
# （群組內檔案已依路徑排序，結果每次一致）。

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote_plus

from .models import DuplicateGroup, FileInfo

# 每個 tuple：(id, 顯示標籤, 排序 key 函式, reverse=True 表示值越大越優先, 互斥條件 id)
# 智慧規則的排序值由 condition() 依整個群組計算，key 與 reverse 填 None
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
    ("copy_name", "像副本的檔名優先移除", None,               None,  None),
]
COND_BY_ID = {c[0]: c for c in CONDITIONS_DEF}
SMART_IDS = ("copy_name",)


@dataclass(frozen=True)
class Condition:
    """排序條件：keys(files) 算出群組內每個檔案的排序值。

    基本條件：reverse=True 表示值越大越優先成為「目標」（保留模式＝保留它，刪除模式＝勾選它）。
    智慧規則（smart=True）：值越大越該移除——保留模式取最小值，刪除模式取最大值。
    """
    id: str
    keys: Callable[[list], list]
    reverse: bool = False
    smart: bool = False


# ── 像副本的檔名 ───────────────────────────────────────────────────────────
# 瀏覽器重複下載、檔案總管複製、同步工具留下的複本標記。出現在任何一個「.」之前或檔名結尾，
# 所以「photo (1).png」「archive (1).tar.gz」「model.safetensors.dup1」都認得。
_N = r"[1-9]\d?"                                   # 1～99（「(2019)」「(0)」不算）
_OPEN, _CLOSE = r"[(（]", r"[)）]"                  # 半形或全形括號
_COPY_SUFFIX = re.compile(
    r"(?:"
    rf"\s?{_OPEN}{_N}{_CLOSE}"                                            # 「 (1)」「(1)」「（1）」
    rf"|\s?{_OPEN}(?:copy|another copy|\d+(?:st|nd|rd|th) copy){_CLOSE}"  # 「 (copy)」（Linux）
    rf"|\s*-\s*(?:複製|复制|副本|コピー|copy)(?:\s*{_OPEN}{_N}{_CLOSE})?"   # 「 - 複製」「 - Copy (2)」
    rf"|\s+copy(?:\s+{_N})?"                                              # 「 copy」「 copy 2」（macOS）
    r"|\.dup\d*"                                                          # 「.dup」「.dup1」
    r")(?=\.|$)",
    re.IGNORECASE)
_COPY_PREFIX = re.compile(rf"^copy(?:\s*{_OPEN}{_N}{_CLOSE})?\s+of\s+", re.IGNORECASE)  # 「Copy of 」


def copy_info(name: str) -> tuple[str, int]:
    """回傳 (去掉複本標記後的檔名, 標記數)。檔名比對不分大小寫。

    URL 編碼痕跡（「+」代替空白、「%20」等）算一個標記：「my+photo.png」→（「my photo.png」, 1）。
    標記可以疊好幾層：「photo (1) (1).png」→（「photo.png」, 2）。
    """
    s = unicodedata.normalize("NFC", name)
    count = 0
    if "+" in s or "%" in s:
        decoded = unquote_plus(s)
        if decoded != s:
            s, count = decoded, 1
    while True:
        s, n = _COPY_PREFIX.subn("", s, count=1)
        if not n:
            s, n = _COPY_SUFFIX.subn("", s, count=1)
        if not n:
            return s.casefold(), count
        count += 1


def copy_scores(files: list[FileInfo]) -> list[int]:
    """每個檔案「像副本」的程度：比群組裡同名（去掉標記後）的其他檔案多幾個標記。

    「photo.png」0、「photo (1).png」1、「photo (1) (1).png」2；
    群組裡沒有同名的其他檔案時一律 0（看不出誰是副本）。
    """
    infos = [copy_info(fi.path.name) for fi in files]
    least: dict[str, int] = {}
    for canon, count in infos:
        least[canon] = min(count, least.get(canon, count))
    return [count - least[canon] for canon, count in infos]


# ── 套用條件 ─────────────────────────────────────────────────────────────
def condition(cond_id: str) -> Condition:
    """依 id 建立排序條件。"""
    if cond_id == "copy_name":
        return Condition(cond_id, copy_scores, smart=True)
    _, _, key_fn, rev, _ = COND_BY_ID[cond_id]
    if key_fn is None:                          # 列表第一：維持群組內原本的順序
        return Condition(cond_id, lambda files: list(range(len(files))))
    return Condition(cond_id, lambda files: [key_fn(fi) for fi in files], rev)


def _rank(files: list, conditions: list, mode: str,
          candidates: list[int] | None) -> tuple[int | None, str | None]:
    """依優先序逐一比較，回傳 (勝出索引, 決定勝負的條件 id)。

    每個條件只留下最好的那幾個候選，平手才交給下一個條件；全部平手時取候選中的第一個，
    此時決定者為 None。等同以倒序套用條件的多鍵穩定排序。
    """
    best = list(range(len(files))) if candidates is None else list(candidates)
    if not best:
        return None, None
    decided = None
    for cond in conditions:
        if len(best) == 1:
            break
        keys = cond.keys(files)
        prefer_max = (mode == "delete") if cond.smart else cond.reverse
        target = (max if prefer_max else min)(keys[i] for i in best)
        best = [i for i in best if keys[i] == target]
        if len(best) == 1:
            decided = cond.id
    return best[0], decided


def rank_by_conditions(files: list, conditions: list, mode: str = "keep",
                       candidates: list[int] | None = None) -> int | None:
    """回傳「勝出」（成為目標）的檔案索引；candidates 限定只從這些索引裡選。"""
    return _rank(files, conditions, mode, candidates)[0]


def apply_selection(group: DuplicateGroup, conditions: list, mode: str) -> str | None:
    """依條件找出勝出檔案，更新群組內各檔案的 selected 旗標。

    mode='keep'   → 保留勝出者（勝出者不勾選，其餘勾選待刪）
    mode='delete' → 刪除勝出者（只勾選勝出者）
    回傳決定勝負的條件 id；None 表示條件都平手、取列表第一。
    """
    winner, decided = _rank(group.files, conditions, mode, None)
    for i, fi in enumerate(group.files):
        fi.selected = (i != winner) if mode == "keep" else (i == winner)
    return decided
