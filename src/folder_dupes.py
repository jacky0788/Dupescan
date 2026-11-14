# 整個資料夾都是重複的：資料夾（含所有子資料夾）裡的每個檔案，在這個資料夾外面都還有一份一模一樣的。
# 這種資料夾整個移走不會少任何內容，可以一次把整個資料夾移到資源回收筒（連同空的子資料夾）。
#
# 判定直接看磁碟上現在的內容，不只看掃描結果（掃描時被篩掉的小檔、隱藏檔、排除的資料夾也要算進去）：
#   - 每個檔案都必須是某一組重複的成員，而且那一組在這個資料夾外面還有副本
#   - 系統自動產生的小檔（Thumbs.db、desktop.ini、.DS_Store…）與空檔案不影響判定，會跟著資料夾一起移除
#   - 有 junction／符號連結、雲端尚未下載的檔案、讀不到的子資料夾 → 不算
#   - 系統或程式資料夾、保護的資料夾裡的檔案 → 不算；磁碟根目錄、掃描範圍以外的資料夾 → 不列出
# 全部掃描時只列最上層：D:\備份 整個重複時，不另外列 D:\備份\2020。
#
# 移除前再檢查一次（verify_folders）：資料夾裡的檔案跟剛才一樣、沒有變動，
# 資料夾外面那份還在、開頭內容相同（需要時完整比對）；有任何一項不符合，整個資料夾都不移除。
#
# 純邏輯、不依賴 Qt，對話框與測試共用。
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .delete_plan import HEAD_BYTES, is_protected
from .models import DuplicateGroup, FileInfo
from .scanner import _CLOUD_ATTRS, EXCLUDED_DIR_NAMES, _full_hash, _is_junction
from .selection import FolderList

JUNK_NAMES = frozenset({"thumbs.db", "ehthumbs.db", "ehthumbs_vista.db", "desktop.ini", ".ds_store"})
_REPARSE_POINT = 0x400          # FILE_ATTRIBUTE_REPARSE_POINT
REPORT_EVERY = 200              # 每看過幾個資料夾更新一次進度

Report = Callable[..., None]


def _key(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _under(key: str, folder_key: str) -> bool:
    """key 是否在 folder_key 底下（兩者都是 _key() 的結果）。"""
    prefix = folder_key if folder_key.endswith(os.sep) else folder_key + os.sep
    return key.startswith(prefix)


def _is_volume_root(path: str) -> bool:
    drive, rest = os.path.splitdrive(os.path.normpath(path))
    return rest in ("", os.sep, "/")


@dataclass
class FolderFile:
    path: str                # 資料夾裡的這個檔案（磁碟上的路徑）
    file: FileInfo           # 掃描結果中對應的那份（硬連結時 path 可能是它的 aliases 之一）
    group: DuplicateGroup


@dataclass
class _Node:
    """一個資料夾的檢查結果（只記這一層，子資料夾另外記）。"""
    path: str
    ok: bool
    reason: str = ""
    files: list[FolderFile] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)      # 一起移除的系統小檔與空檔案
    children: list["_Node"] = field(default_factory=list)

    def iter_nodes(self) -> Iterator["_Node"]:
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(node.children)


@dataclass
class DupFolder:
    """整個都是重複的資料夾。"""
    path: str
    files: list[FolderFile]
    extra: list[str]
    subfolders: int
    elsewhere: list[tuple[str, int]]     # 資料夾外面的副本在哪些資料夾：(資料夾, 檔案數)，多的在前

    @property
    def key(self) -> str:
        return _key(self.path)

    @property
    def size(self) -> int:
        """移走後釋放的空間（同一個實體檔在資料夾裡有好幾個硬連結時只算一次）。"""
        seen: set[int] = set()
        total = 0
        for ff in self.files:
            if id(ff.file) not in seen:
                seen.add(id(ff.file))
                total += ff.file.size
        return total

    @property
    def name(self) -> str:
        return os.path.basename(os.path.normpath(self.path)) or self.path

    def outside_files(self) -> list[FileInfo]:
        """這個資料夾裡的檔案，在資料夾外面的副本（保留這個資料夾、改移除另一邊時會移除的檔案）。
        硬連結有任何一個路徑在資料夾裡的不算（移掉外面那個路徑不會釋放空間）。"""
        out, seen = [], set()
        for ff in self.files:
            if id(ff.group) in seen:
                continue
            seen.add(id(ff.group))
            for fi in ff.group.files:
                if not any(_under(_key(p), self.key) for p in [str(fi.path), *fi.aliases]):
                    out.append(fi)
        return out

    @property
    def outside_size(self) -> int:
        return sum(fi.size for fi in self.outside_files())

    def elsewhere_text(self) -> str:
        if not self.elsewhere:
            return ""
        first = self.elsewhere[0][0]
        return first if len(self.elsewhere) == 1 else f"{first} 等 {len(self.elsewhere):,} 個資料夾"


class FolderFinder:
    """找出整個都是重複的資料夾。groups 為掃描結果，roots 為掃描的資料夾。"""

    def __init__(self, groups: list[DuplicateGroup], roots: list[str], protected=None,
                 reference_dirs: list[str] | None = None, report: Report | None = None):
        self.roots = [_key(r) for r in roots if r]
        self.protected = protected
        self.ref_dirs = FolderList(reference_dirs or [])
        self.report = report
        self.index: dict[str, tuple[FileInfo, DuplicateGroup]] = {}
        self.group_paths: dict[int, list[tuple[str, str]]] = {}     # id(群組) → [(key, 原本的路徑)]
        for g in groups:
            if len(g.files) < 2:
                continue
            paths = []
            for fi in g.files:
                for p in [str(fi.path), *fi.aliases]:
                    k = _key(p)
                    self.index[k] = (fi, g)
                    paths.append((k, p))
            self.group_paths[id(g)] = paths
        self._memo: dict[str, _Node] = {}
        self._seen = 0

    # ── 單一資料夾 ────────────────────────────────────────────────────
    def _root_of(self, key: str) -> str | None:
        for r in self.roots:
            if key == r or _under(key, r):
                return r
        return None

    def _blocked(self, path: str) -> str:
        if is_protected(path, self.protected):
            return "裡面有系統或程式資料夾的檔案"
        if self.ref_dirs and self.ref_dirs.contains(path):
            return "裡面有保護的資料夾的檔案"
        return ""

    def _node(self, path: str) -> _Node:
        key = _key(path)
        node = self._memo.get(key)
        if node is None:
            node = self._scan(path)
            self._memo[key] = node
        return node

    def _scan(self, path: str) -> _Node:
        self._seen += 1
        if self.report and self._seen % REPORT_EVERY == 0:
            self.report(f"正在檢查資料夾…（已看過 {self._seen:,} 個）")
        name = os.path.basename(path)

        def fail(reason: str) -> _Node:
            return _Node(path, False, reason)

        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as exc:
            return fail(f"無法讀取「{name}」（{exc.strerror or exc}）")
        node = _Node(path, True)
        dirs = []
        for e in entries:                     # 先看檔案：不符合就不必再往下看子資料夾
            try:
                if e.is_symlink() or (e.is_dir(follow_symlinks=False) and _is_junction(e)):
                    return fail(f"裡面有捷徑連結「{e.name}」（junction 或符號連結）")
                if e.is_dir(follow_symlinks=False):
                    if e.name.casefold() in EXCLUDED_DIR_NAMES:
                        return fail(f"裡面有系統資料夾「{e.name}」")
                    dirs.append(e)
                    continue
                if not e.is_file(follow_symlinks=False):
                    return fail(f"裡面有無法處理的項目「{e.name}」")
                st = e.stat(follow_symlinks=False)
            except OSError as exc:
                return fail(f"無法讀取「{e.name}」（{exc.strerror or exc}）")
            attrs = getattr(st, "st_file_attributes", 0)
            if attrs & _CLOUD_ATTRS:
                return fail(f"「{e.name}」是雲端尚未下載的檔案")
            if attrs & _REPARSE_POINT:
                return fail(f"「{e.name}」是特殊的連結檔")
            blocked = self._blocked(e.path)
            if blocked:
                return fail(blocked)
            hit = self.index.get(_key(e.path))
            if hit is None:
                if st.st_size == 0 or e.name.casefold() in JUNK_NAMES:
                    node.extra.append(e.path)
                    continue
                return fail(f"「{e.name}」在別處沒有一樣的檔案")
            fi, g = hit
            if st.st_size != fi.size:
                return fail(f"「{e.name}」掃描後大小已變更")
            node.files.append(FolderFile(e.path, fi, g))
        for e in dirs:
            child = self._node(e.path)
            if not child.ok:
                return fail(child.reason)
            node.children.append(child)
        return node

    def _outside(self, node: _Node, folder_key: str) -> tuple[str, Counter]:
        """每個檔案在 folder_key 外面都有副本嗎？回傳 (不符合的原因, 外面副本所在的資料夾)。"""
        where: Counter = Counter()
        checked: set[int] = set()
        for n in node.iter_nodes():
            for ff in n.files:
                if id(ff.group) in checked:
                    continue
                checked.add(id(ff.group))
                outside = [p for k, p in self.group_paths.get(id(ff.group), []) if not _under(k, folder_key)]
                if not outside:
                    return f"「{os.path.basename(ff.path)}」只在這個資料夾裡有副本", where
                where[os.path.dirname(outside[0])] += 1
        return "", where

    def check(self, path: str) -> tuple[DupFolder | None, str]:
        """檢查一個資料夾；回傳 (結果, 不符合的原因)。"""
        path = os.path.normpath(str(path))
        key = _key(path)
        if _is_volume_root(path):
            return None, "磁碟根目錄不能整個移除"
        if self._root_of(key) is None:
            return None, "不在掃描的範圍內"
        if os.path.basename(path).casefold() in EXCLUDED_DIR_NAMES:
            return None, "這是系統資料夾"
        node = self._node(path)
        if not node.ok:
            return None, node.reason
        files = [ff for n in node.iter_nodes() for ff in n.files]
        if not files:
            return None, "裡面沒有重複的檔案"
        reason, where = self._outside(node, key)
        if reason:
            return None, reason
        extra = [p for n in node.iter_nodes() for p in n.extra]
        subfolders = sum(1 for _ in node.iter_nodes()) - 1
        return DupFolder(path, files, extra, subfolders, where.most_common()), ""

    # ── 全部 ──────────────────────────────────────────────────────────
    def candidates(self) -> list[str]:
        """掃描範圍內、含有重複檔案的資料夾（含上層），淺的在前。"""
        found: dict[str, str] = {}
        for paths in self.group_paths.values():
            for k, p in paths:
                root = self._root_of(k)
                if root is None:
                    continue
                folder = os.path.dirname(p)
                while True:
                    fk = _key(folder)
                    if fk in found or not (fk == root or _under(fk, root)):
                        break
                    found[fk] = folder
                    parent = os.path.dirname(folder)
                    if parent == folder:
                        break
                    folder = parent
        return sorted(found.values(), key=lambda p: (_key(p).count(os.sep), _key(p)))

    def find_all(self) -> list[DupFolder]:
        """所有整個都是重複的資料夾，只列最上層（已列出的資料夾底下的不再列）。"""
        result: list[DupFolder] = []
        for path in self.candidates():
            key = _key(path)
            if any(_under(key, f.key) for f in result):
                continue
            folder, _ = self.check(path)
            if folder is not None:
                result.append(folder)
        result.sort(key=lambda f: -f.size)
        return result

    def find_for(self, folder: str) -> tuple[list[DupFolder], str]:
        """這個資料夾與它的上層（在掃描範圍內）中，整個都是重複的有哪些；
        回傳 (結果，淺的在前, 這個資料夾本身不符合的原因)。"""
        found, first_reason = [], ""
        path = os.path.normpath(str(folder))
        while True:
            df, reason = self.check(path)
            if df is not None:
                found.append(df)
            elif not first_reason and path == os.path.normpath(str(folder)):
                first_reason = reason
            parent = os.path.dirname(path)
            if parent == path or self._root_of(_key(parent)) is None:
                break
            path = parent
        return found[::-1], first_reason


# ── 一起移除時的檢查 ─────────────────────────────────────────────────────
def conflict(selected: list[DupFolder], candidate: DupFolder) -> str:
    """candidate 能不能和已勾選的資料夾一起移除；可以時回傳空字串，不行時回傳原因。"""
    for f in selected:
        if f.key == candidate.key:
            continue
        if _under(candidate.key, f.key):
            return f"已經包含在「{f.name}」裡"
        if _under(f.key, candidate.key):
            return f"已勾選的「{f.name}」在這個資料夾裡"
    keys = [f.key for f in selected if f.key != candidate.key] + [candidate.key]
    for ff in candidate.files:
        if not _has_copy_outside(ff, keys):
            other = next((f for f in selected if any(ff.group is x.group for x in f.files)), None)
            where = f"「{other.name}」" if other else "其他勾選的資料夾"
            return f"「{os.path.basename(ff.path)}」的另一份在{where}裡，兩邊不能同時移除"
    return ""


def choice_conflict(removing: list[DupFolder], keeping: list[DupFolder],
                    candidate: DupFolder, keep: bool) -> str:
    """勾選 candidate 時能不能和其他已勾選的一起處理；可以時回傳空字串，不行時回傳原因。

    removing：整個移除的資料夾；keeping：保留、改移除另一邊副本的資料夾。
    keep 為 True 表示 candidate 要保留、改移除另一邊。
    """
    removing = [f for f in removing if f.key != candidate.key]
    keeping = [f for f in keeping if f.key != candidate.key]
    for f in keeping if keep else [*keeping, *removing]:
        if f.key == candidate.key:
            continue
        if _under(candidate.key, f.key):
            return f"已經包含在「{f.name}」裡"
        if _under(f.key, candidate.key):
            return f"已勾選的「{f.name}」在這個資料夾裡"
    if keep:
        for f in removing:
            if _under(candidate.key, f.key):
                return f"要保留的資料夾在要移除的「{f.name}」裡"
            if _under(f.key, candidate.key):
                return f"要移除的「{f.name}」在這個要保留的資料夾裡"
        return ""
    return conflict(removing, candidate)


def keep_side_selector(keeping: list[DupFolder], removing: list[DupFolder] = ()
                       ) -> tuple[list[DuplicateGroup], Callable[[FileInfo], bool]]:
    """保留 keeping 這些資料夾、改移除另一邊的副本：回傳 (相關的群組, 判斷哪個檔案要移除)。

    要保留的資料夾裡的檔案一律不移除；整個移除的資料夾（removing）裡的檔案也不另外處理
    （跟著資料夾一起走）。每一組在保留的資料夾裡都至少有一份，所以不會一份都不剩。
    """
    groups, seen = [], set()
    for f in keeping:
        for ff in f.files:
            if id(ff.group) not in seen:
                seen.add(id(ff.group))
                groups.append(ff.group)
    keys = [f.key for f in keeping] + [f.key for f in removing]

    def is_selected(fi: FileInfo) -> bool:
        return not any(_under(_key(p), k) for p in [str(fi.path), *fi.aliases] for k in keys)
    return groups, is_selected


def _outside_paths(ff: FolderFile, folder_keys: list[str]) -> list[tuple[FileInfo | None, str]]:
    """這個檔案所屬的群組中，不在任何一個 folder_keys 底下的路徑：(對應的 FileInfo 或 None, 路徑)。"""
    out = []
    for fi in ff.group.files:
        for i, p in enumerate([str(fi.path), *fi.aliases]):
            k = _key(p)
            if not any(_under(k, fk) for fk in folder_keys):
                out.append((fi if i == 0 else None, p))
    return out


def _has_copy_outside(ff: FolderFile, folder_keys: list[str]) -> bool:
    return bool(_outside_paths(ff, folder_keys))


def _head(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read(HEAD_BYTES)
    except OSError:
        return None


def _unchanged(path: str, fi: FileInfo | None, size: int) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size != size:
        return False
    return not (fi is not None and fi.mtime_ns and st.st_mtime_ns != fi.mtime_ns)


def verify_folders(folders: list[DupFolder], finder: FolderFinder, full_verify: bool = False,
                   report: Report | None = None) -> tuple[list[DupFolder], list[tuple[DupFolder, str]]]:
    """移除前再檢查一次；回傳 (可以移除的, [(不移除的, 原因)])。

    finder 用來重新看一次資料夾現在的內容（要用新的 FolderFinder，不能沿用之前看過的結果）。
    """
    keys = [f.key for f in folders]
    digests: dict[str, str | None] = {}

    def digest(path: str) -> str | None:
        if path not in digests:
            try:
                digests[path] = _full_hash(Path(path))
            except OSError:
                digests[path] = None
        return digests[path]

    ok, skipped = [], []
    total = sum(len(f.files) for f in folders)
    done = 0
    for folder in folders:
        fresh, reason = finder.check(folder.path)
        if fresh is None:
            skipped.append((folder, f"掃描後有變動：{reason}"))
            done += len(folder.files)
            continue
        if {_key(ff.path) for ff in fresh.files} != {_key(ff.path) for ff in folder.files}:
            skipped.append((folder, "資料夾裡的檔案和剛才不一樣"))
            done += len(folder.files)
            continue
        problem = ""
        for ff in fresh.files:
            done += 1
            if report and (done % 50 == 0 or full_verify):
                report(f"正在比對內容…（{done:,} / {total:,}）", done, total)
            name = os.path.basename(ff.path)
            primary = _key(ff.path) == _key(ff.file.path)
            if not _unchanged(ff.path, ff.file if primary else None, ff.file.size):
                problem = f"「{name}」掃描後有變動"
                break
            reference = next((p for fi, p in _outside_paths(ff, keys) if _unchanged(p, fi, ff.file.size)), None)
            if reference is None:
                problem = f"「{name}」在別處的副本已不存在或已變更"
                break
            if _key(reference) in {_key(p) for p in [str(ff.file.path), *ff.file.aliases]}:
                continue                          # 外面那份是同一個實體檔（硬連結），內容一定相同
            head = _head(ff.path)
            if head is None or head != _head(reference):
                problem = f"「{name}」開頭內容和別處的副本不同"
                break
            if full_verify:
                expected = digest(reference)
                if expected is None or digest(ff.path) != expected:
                    problem = f"「{name}」內容和別處的副本不同"
                    break
        if problem:
            skipped.append((folder, problem))
        else:
            ok.append(fresh)
    return ok, skipped
