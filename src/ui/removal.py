# 移除檔案（在背景執行緒呼叫，見 tasks.run_task）：
#   1. 需要時先完整比對：要移除的檔案必須和「保留的那份」現在的內容完全相同（兩邊都重新計算，
#      不依賴掃描時的 hash，所以舊版本存的結果檔也能正確比對）
#   2. 送到資源回收筒或隔離資料夾（file_ops，不會永久刪除）
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
