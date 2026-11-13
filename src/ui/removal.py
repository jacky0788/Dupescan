# 移除檔案（在背景執行緒呼叫，見 tasks.run_task）：
#   1. 需要時先完整比對：要移除的檔案必須和「保留的那份」現在的內容完全相同（兩邊都重新計算，
#      不依賴掃描時的 hash，所以舊版本存的結果檔也能正確比對）
#   2. 送到資源回收筒或隔離資料夾（file_ops，不會永久刪除）
# 整個資料夾移除（remove_folders）：重新檢查資料夾（folder_dupes.verify_folders）後，把資料夾本身送走。
import os
from pathlib import Path

from .. import file_ops
from ..file_ops import OpResult
from ..scanner import _full_hash


def verify_items(items: list[tuple], report) -> tuple[list[tuple], list[OpResult]]:
    """items 為 [(FileInfo, 保留的那份的路徑)]；回傳 (確認相同的, 略過的結果)。"""
    digests: dict[str, str | None] = {}

    def digest(path: str) -> str | None:
        if path not in digests:
            try:
                digests[path] = _full_hash(Path(path))
            except OSError:
                digests[path] = None
        return digests[path]

    ok, skipped = [], []
    total = len(items)
    for i, (fi, reference) in enumerate(items):
        report(f"正在比對內容…（{i + 1:,} / {total:,}）", i, total)
        expected = digest(reference) if reference else None
        if expected is None:
            skipped.append(OpResult(str(fi.path), False, "verify", note="無法讀取保留的那份，已略過"))
        elif digest(str(fi.path)) != expected:
            skipped.append(OpResult(str(fi.path), False, "verify", note="內容和保留的那份不同，已略過"))
        else:
            ok.append((fi, reference))
    return ok, skipped


def remove_folders(folders: list, finder_factory, method: str, hwnd: int | None, full_verify: bool,
                   report) -> tuple[list, list[OpResult]]:
    """整個資料夾移除（folders 為 folder_dupes.DupFolder）：先重新檢查，再把資料夾本身送走。

    finder_factory() 產生新的 FolderFinder（重新看磁碟上現在的內容）。
    回傳 (移除成功的 DupFolder, 每個資料夾的結果)。
    """
    from ..folder_dupes import verify_folders
    report("正在確認資料夾裡的檔案還在、而且沒有變動…", 0, 0)
    todo, skipped = verify_folders(folders, finder_factory(), full_verify, report)
    results = [OpResult(f.path, False, "verify", note=reason) for f, reason in skipped]
    # 資料夾裡有超長路徑時，資源回收筒（Shell API）可能處理不了，直接改走隔離資料夾
    long_paths = [f for f in todo if any(len(p) > file_ops.MAX_SHELL_PATH
                                          for p in [*(ff.path for ff in f.files), *f.extra])]
    normal = [f for f in todo if f not in long_paths]
    for batch, how in ((normal, method), (long_paths, "quarantine")):
        if not batch:
            continue
        report("正在移除…", 0, len(batch))
        done = file_ops.remove_files(
            [(Path(f.path), f.size) for f in batch], how, hwnd,
            progress=lambda d, t: report(f"正在移除資料夾…（{d:,} / {t:,}）", d, t))
        if how != method:
            for r in done:
                if r.ok and not r.note:
                    r.note = "資料夾裡有超長路徑，資源回收筒不支援"
        results += done
    removed_keys = {os.path.normcase(r.path) for r in results if r.ok}
    removed = [f for f in todo if os.path.normcase(f.path) in removed_keys]
    return removed, results


def remove_items(items: list[tuple], method: str, hwnd: int | None, full_verify: bool,
                 report) -> list[OpResult]:
    """移除 items（[(FileInfo, 保留的那份的路徑)]），回傳每個檔案的結果。"""
    results: list[OpResult] = []
    todo = items
    if full_verify:
        todo, skipped = verify_items(items, report)
        results += skipped
    if todo:
        report("正在移除…", 0, len(todo))
        results += file_ops.remove_files(
            [(fi.path, fi.size) for fi, _ in todo], method, hwnd,
            progress=lambda done, total: report(f"正在移除…（{done:,} / {total:,}）", done, total))
    return results
