# 偵測上次是否正常關閉，以及原生層級當機的紀錄。
#
# 每個執行中的 DupeScan 在 %LOCALAPPDATA%\DupeScan\sessions\<pid>.lock 留一個檔，正常關閉時刪掉。
# 啟動時發現別的 lock 檔、而且那個程序已經不在了 → 上次沒有正常關閉（當機、被強制結束、停電…）。
# 另外用 faulthandler 把當機當下的 Python 呼叫堆疊寫進 logs\crash_<時間>.log，
# 連 Qt 或其他原生元件的崩潰都留得下紀錄；沒有當機時這個檔是空的，下次啟動會清掉。
import ctypes
import faulthandler
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .app_paths import data_dir

KEEP_CRASH_LOGS = 10
_crash_file = None   # faulthandler 寫入用的檔案（程式結束前一直開著）
_k32 = None


def _kernel32():
    """設好參數型別的 kernel32（64 位元下 handle 不會被截斷，也能正確取得錯誤碼）。"""
    global _k32
    if _k32 is None:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _k32 = k32
    return _k32


def pid_alive(pid: int) -> bool:
    """這個 pid 的程序是否還在執行。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        k32 = _kernel32()
        handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5               # 拒絕存取＝程序存在（例如系統管理員權限）
        try:
            from ctypes import wintypes
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                           # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sessions_dir() -> Path:
    path = data_dir() / "sessions"
    path.mkdir(exist_ok=True)
    return path


def start_session(crash_log: Path | None = None) -> list[dict]:
    """登記這次執行，回傳之前沒有正常關閉的執行紀錄（可能有多筆；正在執行的其他 DupeScan 不算）。

    每筆是 {"pid", "started_at", "crash_log"}；crash_log 指向那次的當機紀錄（可能是空的）。
    """
    directory = _sessions_dir()
    me = os.getpid()
    crashed = []
    for lock in directory.glob("*.lock"):
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        pid = info.get("pid") if isinstance(info.get("pid"), int) else _pid_from_name(lock)
        if pid == me or pid_alive(pid):
            continue
        info["pid"] = pid
        crashed.append(info)
        try:
            lock.unlink()
        except OSError:
            pass
    (directory / f"{me}.lock").write_text(json.dumps({
        "pid": me,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "crash_log": str(crash_log) if crash_log else "",
    }, ensure_ascii=False), encoding="utf-8")
    return crashed


def end_session():
    """正常關閉：刪掉這次的 lock 檔。"""
    try:
        (_sessions_dir() / f"{os.getpid()}.lock").unlink()
    except OSError:
        pass


def _pid_from_name(lock: Path) -> int:
    try:
        return int(lock.stem)
    except ValueError:
        return 0


# ── 當機紀錄 ─────────────────────────────────────────────────────────────
def enable_crash_log(log_dir: Path) -> Path | None:
    """建立本次的當機紀錄檔並啟用 faulthandler；清掉以前空的紀錄、只留最近幾份有內容的。"""
    global _crash_file
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"crash_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log"
        _crash_file = open(path, "w", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError:
        return None
    _cleanup_crash_logs(log_dir, keep=path)
    return path


def _cleanup_crash_logs(log_dir: Path, keep: Path):
    nonempty = []
    for p in sorted(log_dir.glob("crash_*.log")):
        if p == keep:
            continue
        try:
            if p.stat().st_size == 0:
                p.unlink()                     # 沒有當機的紀錄檔（其他執行中的程序還開著時刪不掉，沒關係）
            else:
                nonempty.append(p)
        except OSError:
            pass
    for p in nonempty[:-KEEP_CRASH_LOGS]:
        try:
            p.unlink()
        except OSError:
            pass


def read_crash_log(path: str | Path | None, limit: int = 4000) -> str:
    """當機紀錄的內容（沒有或是空的時回傳空字串）；太長時只取最後一段。"""
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-limit:]
