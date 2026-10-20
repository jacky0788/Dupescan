# 快速選取規則：決定每個重複群組要「保留」哪一份、勾選哪些待移除。
# 純邏輯、不依賴 Qt，UI（main_window）與測試共用。
#
#   基本條件 — 依單一檔案的屬性比較（修改時間、路徑長短、目錄深淺、字母序、列表順序）
#   智慧規則 — 要看整個群組才能判斷（例如「像副本的檔名」要看群組裡有沒有原名那份）；
#              方向固定：值越大越該移除，保留模式與刪除模式都一樣
#   參考資料夾 — 裡面的檔案永遠不會被勾選（見 apply_selection）
#
# 條件可疊加：依優先序逐一比較，前面的條件平手才看下一個；全部平手時取列表第一
# （群組內檔案已依路徑排序，結果每次一致）。

import json
import re
import unicodedata
from dataclasses import dataclass, field
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
    ("folders",   "資料夾優先序",         None,               None,  None),
    ("custom",    "自訂規則（正規表示式）", None,             None,  None),
]
COND_BY_ID = {c[0]: c for c in CONDITIONS_DEF}
SMART_IDS = ("copy_name", "folders", "custom")


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


# ── 資料夾清單 ───────────────────────────────────────────────────────────
_ABSOLUTE = re.compile(r"^(?:[a-z]:(?:/|$)|//)")      # 「d:/…」「d:」或 UNC「//server/…」


def split_dirs(text: str) -> list[str]:
    """把「_trash; D:\\Models」這類以 ; 分隔的輸入拆成清單（去掉空白與引號）。"""
    return [p for p in (part.strip().strip('"').strip() for part in text.split(";")) if p]


def _folder_of(path) -> str:
    """「D:\\Models\\a.bin」→「d:/models/」：檔案所在資料夾，比對用（不分大小寫、/ 分隔、結尾有 /）。"""
    s = str(path).replace("\\", "/").casefold()
    return s[:s.rfind("/") + 1]


class FolderList:
    """使用者列出的資料夾，不分大小寫，/ 與 \\ 視為相同。

    完整路徑（D:\\Models、\\\\NAS\\share）：資料夾本身與其下所有子資料夾。
    其他視為名稱片段（_trash、備份、Downloads、models\\blobs）：資料夾路徑中任何位置含有就算；
    要整個資料夾名稱相同，前後加上分隔符號，例如 \\old\\。
    """

    def __init__(self, dirs: list[str]):
        self.patterns: list[tuple[str, bool]] = []      # (樣式, 是否為完整路徑)
        for d in dirs:
            p = d.strip().strip('"').replace("\\", "/").casefold()
            absolute = bool(_ABSOLUTE.match(p))
            if absolute:
                p = p.rstrip("/")
            if p.strip("/"):
                self.patterns.append((p, absolute))

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def match_folder(self, folder: str) -> tuple[int, int] | None:
        """folder 為 _folder_of() 的結果。回傳 (比對到的最深一層, 樣式在清單中的位置)：
        好幾個樣式都符合時取最深（最具體）的，一樣深取清單前面的；都不符合回傳 None。"""
        best = None
        for index, (pat, absolute) in enumerate(self.patterns):
            if absolute:
                end = len(pat) if folder.startswith(pat + "/") else -1
            else:
                i = folder.rfind(pat)
                end = i + len(pat) if i >= 0 else -1
            if end < 0:
                continue
            depth = folder.count("/", 0, end - 1) + 1      # 比對結尾落在第幾層資料夾
            if best is None or depth > best[0]:
                best = (depth, index)
        return best

    def contains(self, path) -> bool:
        """檔案是否位於清單裡的資料夾。"""
        return self.match_folder(_folder_of(path)) is not None


def folder_scores(files: list[FileInfo], keep: FolderList, remove: FolderList) -> list[int]:
    """每個檔案「該移除」的程度：在優先移除的資料夾為正、優先保留的為負、都不在為 0。

    清單越前面的資料夾，分數的絕對值越大。同一個檔案兩邊都符合時，以比對到比較深（比較具體）
    的那邊為準——「D:\\Models」優先保留、「_trash」優先移除，D:\\Models\\_trash 裡的檔案算優先
    移除；一樣深時算優先保留。
    """
    scores = []
    for fi in files:
        folder = _folder_of(fi.path)
        k, r = keep.match_folder(folder), remove.match_folder(folder)
        if r is not None and (k is None or r[0] > k[0]):
            scores.append(len(remove.patterns) - r[1])
        elif k is not None:
            scores.append(-(len(keep.patterns) - k[1]))
        else:
            scores.append(0)
    return scores


# ── 自訂規則 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CustomRule:
    """以正規表示式比對完整路徑（不分大小寫；資料夾分隔寫 / 或 \\\\ 都可以）。"""
    action: str        # "+"＝優先保留、"-"＝優先移除
    pattern: str


def parse_custom_rules(text: str) -> tuple[list[CustomRule], list[str]]:
    """每行一條：「- 樣式」符合的優先移除、「+ 樣式」符合的優先保留；空行與 # 開頭的行略過。

    +／- 後面的空白會忽略（要比對開頭的空白請寫 \\s）。回傳 (規則, 錯誤訊息)，有錯的行不會出現在規則裡。
    """
    rules, errors = [], []
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        action = {"+": "+", "＋": "+", "-": "-", "－": "-"}.get(s[0])
        pattern = s[1:].strip()
        if action is None:
            errors.append(f"第 {no} 行：要以 +（優先保留）或 -（優先移除）開頭")
            continue
        if not pattern:
            errors.append(f"第 {no} 行：+／- 後面要接正規表示式")
            continue
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            errors.append(f"第 {no} 行：正規表示式有誤（{exc}）")
            continue
        rules.append(CustomRule(action, pattern))
    return rules, errors


def _compile_custom(rules: list[CustomRule]) -> list[tuple[str, re.Pattern]]:
    compiled = []
    for r in rules:
        try:
            compiled.append((r.action, re.compile(r.pattern, re.IGNORECASE)))
        except re.error:
            pass                       # parse_custom_rules 已經擋下；這裡只是保險
    return compiled


def custom_scores(files: list[FileInfo], compiled: list[tuple[str, re.Pattern]]) -> list[int]:
    """每個檔案「該移除」的程度：由上往下第一條符合的規則決定——優先移除為正、優先保留為負，
    越上面的規則分數絕對值越大；都不符合為 0。

    路徑同時以原本的形式（D:\\a\\b.png）與 / 分隔的形式（D:/a/b.png）比對，任一個符合就算。
    """
    n = len(compiled)
    scores = []
    for fi in files:
        native = str(fi.path)
        slash = native.replace("\\", "/")
        score = 0
        for i, (action, rx) in enumerate(compiled):
            if rx.search(native) or (slash != native and rx.search(slash)):
                score = (n - i) if action == "-" else -(n - i)
                break
        scores.append(score)
    return scores


# ── 規則組合 ─────────────────────────────────────────────────────────────
@dataclass
class RuleSet:
    """一組選取規則：操作模式、條件 id（依優先序）與智慧規則需要的設定。"""
    mode: str = "keep"                                         # keep＝保留目標；delete＝刪除目標
    conditions: list[str] = field(default_factory=list)
    keep_dirs: list[str] = field(default_factory=list)         # 優先保留的資料夾（越前面越優先）
    remove_dirs: list[str] = field(default_factory=list)       # 優先移除的資料夾（越前面越優先移除）
    reference_dirs: list[str] = field(default_factory=list)    # 參考資料夾：裡面的檔案不會被勾選
    custom: str = ""                                           # 自訂規則原文，每行一條（含註解）

    def build(self) -> list[Condition]:
        """依優先序建立排序條件（不認得的 id 略過）。"""
        return [condition(cid, self) for cid in self.conditions if cid in COND_BY_ID]

    def reference(self) -> FolderList:
        return FolderList(self.reference_dirs)

    def custom_rules(self) -> tuple[list[CustomRule], list[str]]:
        """(自訂規則, 錯誤訊息)，見 parse_custom_rules。"""
        return parse_custom_rules(self.custom)

    def to_dict(self) -> dict:
        """存成預設集用（可直接 json.dumps）。"""
        return {"mode": self.mode, "conditions": list(self.conditions),
                "keep_dirs": list(self.keep_dirs), "remove_dirs": list(self.remove_dirs),
                "reference_dirs": list(self.reference_dirs), "custom": self.custom}

    @classmethod
    def from_dict(cls, data) -> "RuleSet":
        """讀回預設集：缺少或型別不對的欄位用預設值，不認得的條件 id 略過（設定檔被改壞也不會出錯）。"""
        if not isinstance(data, dict):
            return cls()

        def strings(key: str) -> list[str]:
            value = data.get(key)
            return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []

        custom = data.get("custom")
        return cls(mode=data.get("mode") if data.get("mode") in ("keep", "delete") else "keep",
                   conditions=[c for c in dict.fromkeys(strings("conditions")) if c in COND_BY_ID],
                   keep_dirs=strings("keep_dirs"), remove_dirs=strings("remove_dirs"),
                   reference_dirs=strings("reference_dirs"),
                   custom=custom if isinstance(custom, str) else "")


# ── 預設集 ───────────────────────────────────────────────────────────────
# 具名的 RuleSet，依建立順序保存；UI 存在 QSettings 的 select/presets（一段 JSON 字串）。
def dump_presets(presets: dict[str, RuleSet]) -> str:
    items = [{"name": name, **rules.to_dict()} for name, rules in presets.items()]
    return json.dumps({"version": 1, "presets": items}, ensure_ascii=False)


def load_presets(text: str) -> dict[str, RuleSet]:
    """dump_presets 的反向；內容壞掉時回傳空的（或略過壞掉的那幾筆），不拋例外。"""
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    items = data.get("presets") if isinstance(data, dict) else None
    presets: dict[str, RuleSet] = {}
    for item in items if isinstance(items, list) else []:
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip():
            presets[name.strip()] = RuleSet.from_dict(item)
    return presets


# ── 套用條件 ─────────────────────────────────────────────────────────────
def condition(cond_id: str, rules: RuleSet | None = None) -> Condition:
    """依 id 建立排序條件；智慧規則需要的設定（資料夾清單等）從 rules 取。"""
    if cond_id == "copy_name":
        return Condition(cond_id, copy_scores, smart=True)
    if cond_id == "folders":
        rules = rules or RuleSet()
        keep, remove = FolderList(rules.keep_dirs), FolderList(rules.remove_dirs)
        return Condition(cond_id, lambda files: folder_scores(files, keep, remove), smart=True)
    if cond_id == "custom":
        compiled = _compile_custom((rules or RuleSet()).custom_rules()[0])
        return Condition(cond_id, lambda files: custom_scores(files, compiled), smart=True)
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


def apply_selection(group: DuplicateGroup, conditions: list, mode: str,
                    reference: FolderList | None = None) -> str | None:
    """依條件找出勝出檔案，更新群組內各檔案的 selected 旗標。

    mode='keep'   → 保留勝出者（勝出者不勾選，其餘勾選待刪）
    mode='delete' → 刪除勝出者（只勾選勝出者）
    reference：參考資料夾，裡面的檔案永遠不會被勾選。保留模式下，群組裡有參考資料夾的檔案時，
      保留的那份從它們之中選（其他副本照常勾選）；刪除模式下只從參考資料夾以外的檔案挑目標。
    回傳決定勝負的條件 id；"reference"＝由參考資料夾決定；None＝條件都平手、取列表第一。
    """
    files = group.files
    is_ref = [bool(reference) and reference.contains(fi.path) for fi in files]
    if not any(is_ref):
        candidates = None
    elif mode == "keep":
        candidates = [i for i, r in enumerate(is_ref) if r]
    else:
        candidates = [i for i, r in enumerate(is_ref) if not r]
    winner, decided = _rank(files, conditions, mode, candidates)
    if candidates is not None and len(candidates) <= 1:
        decided = "reference"
    for i, fi in enumerate(files):
        fi.selected = (i != winner and not is_ref[i]) if mode == "keep" else (i == winner)
    return decided
