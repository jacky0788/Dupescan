# 掃描結果的匯出與存檔：
#   export_csv     — 完整結果 CSV（每個檔案一列，Excel 直接開）
#   save_results   — 可再開啟的結果檔（gzip 壓縮的 JSON，含保留／移除的選擇），副檔名 .dupescan
#   load_results   — 讀回結果檔；檔案在存檔後可能已變動，移除前的檢查（delete_plan）會把關
import csv
import gzip
import json
from datetime import datetime
from pathlib import Path

from .models import DuplicateGroup, FileInfo

FORMAT_NAME    = "dupescan-results"
FORMAT_VERSION = 1
RESULT_SUFFIX  = ".dupescan"


def export_csv(path, groups: list[DuplicateGroup]):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM：Excel 直接開不亂碼
        w = csv.writer(f)
        w.writerow(["群組", "處理", "路徑", "資料夾", "檔名", "大小(bytes)", "修改時間",
                    "保留原因", "這組可釋放(bytes)", "硬連結路徑", "hash"])
        for number, g in enumerate(groups, 1):
            for fi in g.files:
                mtime = datetime.fromtimestamp(fi.mtime).isoformat(sep=" ", timespec="seconds")
                w.writerow([number, "移除" if fi.selected else "保留", str(fi.path), fi.folder, fi.name,
                            g.size, mtime, "" if fi.selected else g.keep_reason, g.wasted_bytes,
                            "; ".join(fi.aliases), g.hash_value])


def save_results(path, groups: list[DuplicateGroup], roots: list[str], scanned_at: str = "",
                 scan: dict | None = None):
    """scan：掃描設定與統計（可省略），讀回時放在 meta["scan"]。"""
    data = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "scanned_at": scanned_at,
        "roots": roots,
        "scan": scan or {},
        "groups": [
            {"hash": g.hash_value, "size": g.size, "reason": g.keep_reason,
             "files": [{"path": str(fi.path), "mtime": fi.mtime, "mtime_ns": fi.mtime_ns,
                        "dev": fi.dev, "ino": fi.ino, "selected": fi.selected,
                        "aliases": fi.aliases} for fi in g.files]}
            for g in groups
        ],
    }
    tmp = Path(str(path) + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(path)


def load_results(path) -> tuple[list[DuplicateGroup], dict]:
    """讀回結果檔，回傳 (群組, {"saved_at", "scanned_at", "roots", "scan"})；格式不對時拋出 ValueError。"""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, EOFError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取結果檔：{exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
        raise ValueError("這不是 DupeScan 的結果檔")
    if data.get("version", 0) > FORMAT_VERSION:
        raise ValueError("這個結果檔是較新版本的 DupeScan 存的，請先更新 DupeScan")
    groups = []
    try:
        for g in data.get("groups", []):
            files = [FileInfo(path=Path(f["path"]), size=g["size"], hash_full=g["hash"],
                              mtime=f.get("mtime", 0.0), mtime_ns=f.get("mtime_ns", 0),
                              dev=f.get("dev", 0), ino=f.get("ino", 0),
                              selected=f.get("selected", False), aliases=f.get("aliases", []))
                     for f in g["files"]]
            if len(files) > 1:
                groups.append(DuplicateGroup(hash_value=g["hash"], size=g["size"], files=files,
                                             keep_reason=str(g.get("reason") or "")))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"結果檔內容不完整：{exc}") from exc
    meta = {k: data.get(k) for k in ("saved_at", "scanned_at", "roots")}
    meta["roots"] = [r for r in meta["roots"] or [] if isinstance(r, str)]
    meta["scan"] = data.get("scan") if isinstance(data.get("scan"), dict) else {}
    return groups, meta
