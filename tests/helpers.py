# 測試輔助函式
import random
import time


def blob(size: int, seed: int) -> bytes:
    """產生可重現的隨機內容。"""
    return random.Random(seed).randbytes(size)


def group_paths(groups, root) -> set[frozenset[str]]:
    """把掃描結果轉成「相對路徑集合的集合」，方便比對，不受順序影響。"""
    return {frozenset(fi.path.relative_to(root).as_posix() for fi in g.files) for g in groups}


def wait_until(app, cond, timeout: float = 10.0) -> bool:
    """一邊處理 Qt 事件一邊等待條件成立。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    app.processEvents()
    return cond()


_OPTION_KEYS = {"min_size", "include_hidden", "types", "custom_exts", "exclude_dirs", "skip_system"}


def make_scanner(roots, **kw):
    """測試用：Scanner(ScanOptions(roots=…, …), …) 的簡寫；min_size 預設 1 位元組。"""
    from src.scan_options import ScanOptions
    from src.scanner import Scanner
    opt = {k: kw.pop(k) for k in list(kw) if k in _OPTION_KEYS}
    opt.setdefault("min_size", 1)
    return Scanner(ScanOptions(roots=[str(r) for r in roots], **opt), **kw)
