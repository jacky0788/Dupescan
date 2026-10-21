# 移除前的檢查：依標成「移除」的檔案建立「移除計畫」，在真正動檔案之前找出危險或過時的項目。
#
#   整組都標成移除 — 移除後一份都不剩 → 不處理這一組
#   系統或程式資料夾 — Windows、Program Files、AppData、.git、Ollama 模型 blobs 等由程式
#                     管理的位置，刪了可能讓程式壞掉 → 預設不移除，需另外確認
#   保護的資料夾   — 使用者指定的正本所在（自動選取不會標成移除）；萬一標了也視同受保護
#   掃描後有變動   — 檔案已不存在、大小或修改時間變了、開頭內容和保留的那份不同 → 一律略過
#   保留的那份不見了 — 該群組沒有任何一份「確定還在且沒變」的副本 → 該群組一律略過
#
# 純邏輯、不依賴 Qt，移除確認視窗與測試共用。

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import DuplicateGroup, FileInfo
from .selection import FolderList

HEAD_BYTES = 4096   # 快速複驗時比對的開頭長度
PROTECTED_NOTE = "在系統或程式資料夾"
REFERENCE_NOTE = "在保護的資料夾"

# 受保護資料夾（fnmatch 樣式，比對完整路徑，不分大小寫，/ 與 \ 視為相同）
DEFAULT_PROTECTED = [
    "?:/windows/*",
    "?:/program files/*",
    "?:/program files (x86)/*",
    "?:/programdata/*",
    "*/appdata/*",
    "*/.git/*",
    "*/node_modules/*",
    "*/site-packages/*",
    "*/steamapps/*",
    "*/ollama/models/blobs/*",
    "*/.ollama/models/blobs/*",
]

# PlanEntry.status
OK, SKIP, PROTECTED, KEEP = "ok", "skip", "protected", "keep"


@dataclass
class PlanEntry:
    file:   FileInfo
    status: str          # keep＝保留；ok＝會移除；skip＝自動略過；protected＝受保護（預設不移除）
    note:   str = ""


@dataclass
class PlanGroup:
    number:  int                     # 結果清單上的群組編號（顯示用）
    group:   DuplicateGroup
    entries: list[PlanEntry] = field(default_factory=list)
    whole_group: bool = False        # 所有副本都被勾選
    reference: str = ""              # 用來比對開頭內容的「保留的那份」

    def removable(self, allow_whole_group: bool, allow_protected: bool) -> list[PlanEntry]:
        if self.whole_group and not allow_whole_group:
            return []
        return [e for e in self.entries
                if e.status == OK or (e.status == PROTECTED and allow_protected)]


@dataclass
class DeletePlan:
    groups: list[PlanGroup]

    def removable(self, allow_whole_group: bool = False,
                  allow_protected: bool = False) -> list[tuple[PlanGroup, PlanEntry]]:
        return [(g, e) for g in self.groups
                for e in g.removable(allow_whole_group, allow_protected)]

    @property
    def whole_groups(self) -> list[PlanGroup]:
        return [g for g in self.groups if g.whole_group]

    def entries_with(self, status: str) -> list[PlanEntry]:
        return [e for g in self.groups for e in g.entries if e.status == status]


def is_protected(path, patterns=None) -> bool:
    p = str(path)
    return any(fnmatch.fnmatch(p, pat) for pat in (DEFAULT_PROTECTED if patterns is None else patterns))


def _changed(fi: FileInfo) -> str:
    """和掃描時相比是否有變動；回傳原因，沒變時回傳空字串。"""
    try:
        st = os.stat(fi.path)
    except FileNotFoundError:
        return "檔案已不存在"
    except OSError as exc:
        return f"無法讀取（{exc.strerror or exc}）"
    if st.st_size != fi.size:
        return "掃描後大小已變更"
    if fi.mtime_ns and st.st_mtime_ns != fi.mtime_ns:
        return "掃描後修改時間已變更"
    return ""


def _head(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read(HEAD_BYTES)
    except OSError:
        return None


def build_plan(groups: list[tuple[int, DuplicateGroup]], protected=None,
               verify_head: bool = True, reference_dirs: list[str] | None = None) -> DeletePlan:
    """依勾選狀態建立移除計畫。groups 為 (顯示編號, 群組)，只會處理有勾選檔案的群組。

    reference_dirs：保護的資料夾（寫法同選取規則，完整路徑或名稱片段），裡面的檔案視同受保護。
    """
    ref_dirs = FolderList(reference_dirs or [])
    plan = DeletePlan(groups=[])
    for number, g in groups:
        selected = [fi for fi in g.files if fi.selected]
        if not selected:
            continue
        pg = PlanGroup(number=number, group=g, whole_group=len(selected) == len(g.files))

        # 找一份「確定還在且沒變」的副本當比對基準：優先用保留的；整組全選時用第一份還在的
        kept = [fi for fi in g.files if not fi.selected]
        reference = next((fi for fi in kept if not _changed(fi)), None)
        if reference is None and pg.whole_group:
            reference = next((fi for fi in g.files if not _changed(fi)), None)
        ref_head = _head(reference.path) if (reference and verify_head) else None
        pg.reference = str(reference.path) if reference else ""

        for fi in g.files:
            if not fi.selected:
                pg.entries.append(PlanEntry(fi, KEEP, "" if fi is reference or not _changed(fi)
                                            else _changed(fi)))
                continue
            reason = _changed(fi)
            if not reason and reference is None:
                reason = "保留的那份已不存在或已變更"
            if not reason and verify_head and fi is not reference:
                head = _head(fi.path)
                if head is None:
                    reason = "無法讀取"
                elif ref_head is not None and head != ref_head:
                    reason = "開頭內容與保留的那份不同"
            if reason:
                pg.entries.append(PlanEntry(fi, SKIP, reason))
            elif is_protected(fi.path, protected):
                pg.entries.append(PlanEntry(fi, PROTECTED, PROTECTED_NOTE))
            elif ref_dirs and ref_dirs.contains(fi.path):
                pg.entries.append(PlanEntry(fi, PROTECTED, REFERENCE_NOTE))
            else:
                pg.entries.append(PlanEntry(fi, OK))
        plan.groups.append(pg)
    return plan
