# 自動選取：每一組重複決定保留哪一份，其餘標成「移除」。純邏輯、不依賴 Qt。
#
# 依序套用條件，前面的條件分不出來（平手）才看下一個；全部平手時保留第一份（群組內檔案依路徑
# 排序，每次結果一致）。保護的資料夾裡的檔案一律保留：群組裡有保護的檔案時，「保留的那份」
# 從它們之中選，其他副本照常判斷。
#
#   優先移除的資料夾 — 在這些資料夾裡的副本優先移除（清單越前面越優先）
#   原始檔名         — 「photo (1).jpg」「photo - 複製.jpg」這類副本優先移除，保留原始檔名
#   其他情況         — 保留最舊的／最新的／路徑最短的

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import unquote_plus

from .models import DuplicateGroup, FileInfo


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
_COPY_HINT = re.compile(r"[(（\-+%]|copy|\.dup", re.IGNORECASE)   # 完全不含這些就不可能有標記（快速略過）


def copy_info(name: str) -> tuple[str, int]:
    """回傳 (去掉複本標記後的檔名, 標記數)。檔名比對不分大小寫。

    URL 編碼痕跡（「+」代替空白、「%20」等）算一個標記：「my+photo.png」→（「my photo.png」, 1）。
    標記可以疊好幾層：「photo (1) (1).png」→（「photo.png」, 2）。
    """
    s = unicodedata.normalize("NFC", name)
    if not _COPY_HINT.search(s):
        return s.casefold(), 0
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


def remove_scores(files: list[FileInfo], remove: FolderList) -> list[int]:
    """每個檔案「該移除」的程度：在優先移除的資料夾為正（清單越前面越大），不在為 0。"""
    n = len(remove.patterns)
    scores = []
    for fi in files:
        match = remove.match_folder(_folder_of(fi.path))
        scores.append(n - match[1] if match else 0)
    return scores


# ── 條件 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Condition:
    """排序條件：keys(files) 算出群組內每個檔案的分數，分數越低越優先保留。"""
    id: str
    keys: Callable[[list[FileInfo]], list]


_CONDITIONS: dict[str, Callable[[list[FileInfo]], list]] = {
    "older":     lambda files: [fi.mtime for fi in files],
    "newer":     lambda files: [-fi.mtime for fi in files],
    "sh_path":   lambda files: [len(str(fi.path)) for fi in files],
    "copy_name": copy_scores,
}


def condition(cond_id: str) -> Condition:
    """依 id 建立條件：older（保留最舊）、newer（最新）、sh_path（路徑最短）、copy_name（原始檔名）。"""
    return Condition(cond_id, _CONDITIONS[cond_id])


def folders_condition(remove_dirs: list[str]) -> Condition:
    remove = FolderList(remove_dirs)
    return Condition("folders", lambda files: remove_scores(files, remove))


def pick_keeper(files: list[FileInfo], conditions: list[Condition],
                candidates: list[int] | None = None) -> tuple[int | None, str | None]:
    """回傳 (保留的檔案索引, 決定的條件 id)；全部平手時取候選中的第一個，決定者為 None。

    每個條件只留下分數最低的那幾個候選，平手才交給下一個條件。
    """
    best = list(range(len(files))) if candidates is None else list(candidates)
    if not best:
        return None, None
    decided = None
    for cond in conditions:
        if len(best) == 1:
            break
        keys = cond.keys(files)
        low = min(keys[i] for i in best)
        best = [i for i in best if keys[i] == low]
        if len(best) == 1:
            decided = cond.id
    return best[0], decided


def apply_selection(group: DuplicateGroup, conditions: list[Condition],
                    protected: FolderList | None = None) -> str | None:
    """選出保留的那份，其他標成移除（FileInfo.selected）；保護的資料夾裡的檔案一律保留。

    回傳決定的條件 id；"protected"＝由保護的資料夾決定；None＝條件都平手、保留第一份。
    """
    files = group.files
    is_protected = [bool(protected) and protected.contains(fi.path) for fi in files]
    candidates = [i for i, p in enumerate(is_protected) if p] if any(is_protected) else None
    winner, decided = pick_keeper(files, conditions, candidates)
    if candidates is not None and len(candidates) == 1:
        decided = "protected"
    for i, fi in enumerate(files):
        fi.selected = i != winner and not is_protected[i]
    return decided


# ── 自動選取規則 ─────────────────────────────────────────────────────────
FALLBACKS = {"oldest": ("older", "最舊的"), "newest": ("newer", "最新的"),
             "shortest": ("sh_path", "路徑最短的")}

REASONS = {
    "protected": "在保護的資料夾",
    "folders": "不在優先移除的資料夾",
    "copy_name": "原始檔名",
    "older": "最舊的",
    "newer": "最新的",
    "sh_path": "路徑最短",
    None: "條件都相同，保留第一份",
}


@dataclass
class AutoRules:
    """自動選取規則：每一組要保留哪一份。"""
    copy_names: bool = True                                   # 保留原始檔，移除「(1)」「- 複製」這類副本
    protected_dirs: list[str] = field(default_factory=list)   # 保護的資料夾：裡面的檔案一律保留
    remove_dirs: list[str] = field(default_factory=list)      # 優先移除這些資料夾裡的副本
    fallback: str = "oldest"                                  # 其他情況保留：oldest／newest／shortest

    def conditions(self) -> list[Condition]:
        conds = []
        if self.remove_dirs:
            conds.append(folders_condition(self.remove_dirs))
        if self.copy_names:
            conds.append(condition("copy_name"))
        conds.append(condition(FALLBACKS.get(self.fallback, FALLBACKS["oldest"])[0]))
        return conds

    def protected(self) -> FolderList:
        return FolderList(self.protected_dirs)

    def to_dict(self) -> dict:
        return {"copy_names": self.copy_names, "protected_dirs": list(self.protected_dirs),
                "remove_dirs": list(self.remove_dirs), "fallback": self.fallback}

    @classmethod
    def from_dict(cls, data) -> "AutoRules":
        """讀回設定；缺少或型別不對的欄位用預設值（設定檔被改壞也不會出錯）。"""
        if not isinstance(data, dict):
            return cls()

        def strings(key):
            value = data.get(key)
            return [s for s in value if isinstance(s, str) and s.strip()] if isinstance(value, list) else []

        fallback = data.get("fallback")
        return cls(copy_names=data.get("copy_names") is not False,
                   protected_dirs=strings("protected_dirs"), remove_dirs=strings("remove_dirs"),
                   fallback=fallback if fallback in FALLBACKS else "oldest")


def auto_select(groups: list[DuplicateGroup], rules: AutoRules) -> Counter:
    """依規則替每一組選好保留哪一份（其餘標記為移除），並把原因寫進 keep_reason。

    回傳各原因的組數，例如 Counter({"原始檔名": 12, "最舊的": 30})。
    """
    conds = rules.conditions()
    protected = rules.protected()
    counts: Counter = Counter()
    for g in groups:
        decided = apply_selection(g, conds, protected)
        g.keep_reason = REASONS.get(decided, REASONS[None])
        counts[g.keep_reason] += 1
    return counts


def is_protected_file(fi: FileInfo, rules: AutoRules) -> bool:
    return bool(rules.protected_dirs) and rules.protected().contains(fi.path)
