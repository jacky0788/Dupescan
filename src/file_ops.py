# 檔案處理：把重複檔移到資源回收筒或隔離資料夾。刻意不提供永久刪除。
#
# 資源回收筒：Windows SHFileOperationW（FOF_ALLOWUNDO），並加上 FOF_WANTNUKEWARNING——
#   萬一某個檔案進不了回收筒，Windows 會先跳出警告讓使用者拒絕，不會靜默永久刪除。
#   事先就知道進不了回收筒的檔案（網路／卸除式磁碟、路徑過長、回收筒停用或容量不足）
#   直接改走隔離資料夾。
# 隔離資料夾：搬到同一顆磁碟根目錄的 .dupescan_quarantine\<批次>\<原路徑>（同碟搬移瞬間完成），
#   每批附 manifest.json 可一鍵還原；何時清空由使用者自己決定。
# 每次操作都記一筆到 %LOCALAPPDATA%\DupeScan\journal.jsonl，供「操作紀錄」視窗還原。

import ctypes
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .app_paths import data_dir

QUARANTINE_DIR_NAME = ".dupescan_quarantine"
RECYCLE_BATCH       = 50     # 每次 SHFileOperationW 送出的檔案數（兼顧速度與進度更新）
MAX_SHELL_PATH      = 259    # 資源回收筒（Shell API）不支援超過 MAX_PATH 的路徑

ProgressCallback = Callable[[int, int], None]   # (已處理, 總數)


@dataclass
class OpResult:
    """單一檔案的處理結果。"""
    path:   str
    ok:     bool
    method: str = ""     # "recycle"｜"quarantine"｜"restore"
    dest:   str = ""     # 移到隔離區時的新路徑
    note:   str = ""     # 改走隔離區的原因，或失敗原因


def new_batch_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def quarantine_root_for(path: Path) -> Path:
    """檔案所在磁碟（或網路共用）根目錄下的隔離資料夾。"""
    return Path(path.anchor) / QUARANTINE_DIR_NAME


def _reason(exc: OSError) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def _long(path) -> str:
    """加上 \\\\?\\ 前綴，讓搬移在長路徑（> 260 字元）也能成功。"""
    p = os.path.abspath(str(path))
    if sys.platform != "win32" or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


# ── 資源回收筒 ─────────────────────────────────────────────────────────
_DRIVE_REMOVABLE, _DRIVE_REMOTE, _DRIVE_CDROM, _DRIVE_RAMDISK = 2, 4, 5, 6


def _drive_type(anchor: str) -> int:
    if sys.platform != "win32":
        return 0
    return ctypes.windll.kernel32.GetDriveTypeW(anchor)


def _bitbucket_settings(anchor: str) -> tuple[int | None, int | None]:
    """讀這顆磁碟的資源回收筒設定 (NukeOnDelete, MaxCapacity MB)；讀不到時回傳 (None, None)。"""
    if sys.platform != "win32":
        return None, None
    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(anchor, buf, 64):
        return None, None
    name = buf.value                                   # \\?\Volume{GUID}\
    guid = name[name.find("{"): name.find("}") + 1]
    if not guid:
        return None, None
    import winreg
    key = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            def value(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return None
            return value("NukeOnDelete"), value("MaxCapacity")
    except OSError:
        return None, None


def recycle_obstacle(path: Path, size: int, cache: dict | None = None) -> str:
    """回傳這個檔案「進不了資源回收筒」的原因；可以進回收筒時回傳空字串。"""
    if len(str(path)) > MAX_SHELL_PATH:
        return "路徑太長，資源回收筒不支援"
    anchor = path.anchor
    if not anchor:
        return "無法判斷所在磁碟"
    info = cache.get(anchor) if cache is not None else None
    if info is None:
        info = (_drive_type(anchor), *_bitbucket_settings(anchor))
        if cache is not None:
            cache[anchor] = info
    drive_type, nuke, capacity_mb = info
    if drive_type == _DRIVE_REMOTE:
        return "網路磁碟沒有資源回收筒"
    if drive_type in (_DRIVE_REMOVABLE, _DRIVE_CDROM, _DRIVE_RAMDISK):
        return "卸除式磁碟沒有資源回收筒"
    if nuke == 1:
        return "這顆磁碟的資源回收筒已停用"
    if capacity_mb is not None and size > capacity_mb * 1024 * 1024:
        return "檔案大於資源回收筒容量上限"
    return ""


if sys.platform == "win32":
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd",                  wintypes.HWND),
            ("wFunc",                 wintypes.UINT),
            ("pFrom",                 wintypes.LPCWSTR),
            ("pTo",                   wintypes.LPCWSTR),
            ("fFlags",                wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings",         wintypes.LPVOID),
            ("lpszProgressTitle",     wintypes.LPCWSTR),
        ]

_FO_DELETE           = 0x3
_FOF_SILENT          = 0x0004
_FOF_NOCONFIRMATION  = 0x0010
_FOF_ALLOWUNDO       = 0x0040
_FOF_NOERRORUI       = 0x0400
_FOF_WANTNUKEWARNING = 0x4000   # 進不了回收筒時先警告（部分覆蓋 NOCONFIRMATION）


def _shell_recycle(paths: list[str], hwnd: int | None) -> tuple[int, bool]:
    """一次把多個檔案送進資源回收筒，回傳 (錯誤碼, 使用者是否取消)。"""
    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0")   # 結尾兩個 NUL
    op = _SHFILEOPSTRUCTW()
    op.hwnd = hwnd or None
    op.wFunc = _FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_NOERRORUI
                 | _FOF_SILENT | _FOF_WANTNUKEWARNING)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc, bool(op.fAnyOperationsAborted)


def _com_init() -> bool:
    """在目前執行緒初始化 COM（Shell API 需要）；回傳是否需要對應的 CoUninitialize。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.restype = ctypes.c_long
    return ole32.CoInitializeEx(None, 0x2) in (0, 1)   # S_OK／S_FALSE


def _recycle(items: list[tuple[Path, int]], hwnd, on_done: Callable[[int], None]
             ) -> tuple[list[OpResult], list[tuple[Path, int, str]]]:
    """送資源回收筒；回傳 (成功／取消的結果, 要改走隔離區的檔案與原因)。"""
    results: list[OpResult] = []
    fallback: list[tuple[Path, int, str]] = []
    com = _com_init()
    try:
        for i in range(0, len(items), RECYCLE_BATCH):
            chunk = items[i:i + RECYCLE_BATCH]
            rc, aborted = _shell_recycle([str(p) for p, _ in chunk], hwnd)
            left = [(p, s) for p, s in chunk if os.path.lexists(p)]
            if left and rc and not aborted and len(chunk) > 1:
                # 整批中某個檔案出錯時 Shell 會整批停下，剩下的逐一重試以找出真正失敗的檔案
                for p, s in left:
                    rc1, aborted1 = _shell_recycle([str(p)], hwnd)
                    aborted = aborted or aborted1
                    rc = rc1 if os.path.lexists(p) else rc
                left = [(p, s) for p, s in left if os.path.lexists(p)]
            gone = {str(p) for p, _ in chunk} - {str(p) for p, _ in left}
            results += [OpResult(str(p), True, "recycle") for p, _ in chunk if str(p) in gone]
            for p, s in left:
                if aborted:
                    results.append(OpResult(str(p), False, "recycle", note="已在 Windows 提示中取消"))
                else:
                    fallback.append((p, s, f"資源回收筒無法處理（代碼 {rc:#x}）"))
            on_done(len(chunk))
    finally:
        if com:
            ctypes.windll.ole32.CoUninitialize()
    return results, fallback


# ── 隔離資料夾 ─────────────────────────────────────────────────────────
def _write_manifest(batch_dir: Path, batch_id: str, items: list[dict]):
    manifest = batch_dir / "manifest.json"
    data = {"batch": batch_id, "created": datetime.now().isoformat(timespec="seconds"),
            "items": []}
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
    data["items"].extend(items)
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, manifest)


def quarantine(paths: list[Path], batch_id: str,
               on_done: Callable[[int], None] | None = None) -> list[OpResult]:
    """把檔案搬到同一顆磁碟的隔離資料夾，並寫入可還原的 manifest。"""
    results: list[OpResult] = []
    manifests: dict[Path, list[dict]] = {}
    for p in paths:
        batch_dir = quarantine_root_for(p) / batch_id
        dest = batch_dir.joinpath(*p.parts[1:])
        try:
            st = os.stat(_long(p))
            os.makedirs(_long(dest.parent), exist_ok=True)
            os.rename(_long(p), _long(dest))          # 同一顆磁碟：只改目錄項目，瞬間完成
        except OSError as exc:
            results.append(OpResult(str(p), False, "quarantine", note=_reason(exc)))
        else:
            results.append(OpResult(str(p), True, "quarantine", dest=str(dest)))
            manifests.setdefault(batch_dir, []).append(
                {"original": str(p), "stored": str(dest),
                 "size": st.st_size, "mtime_ns": st.st_mtime_ns})
        if on_done:
            on_done(1)
    for batch_dir, items in manifests.items():
        _write_manifest(batch_dir, batch_id, items)
    return results


def restore_batch(manifest_path: Path) -> list[OpResult]:
    """把一批隔離的檔案搬回原位置（原位置已有檔案時跳過，不覆蓋）。"""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    results: list[OpResult] = []
    for item in data["items"]:
        if item.get("restored"):
            continue
        stored, original = Path(item["stored"]), Path(item["original"])
        if not os.path.lexists(_long(stored)):
            results.append(OpResult(str(original), False, "restore", note="隔離區裡找不到這個檔案"))
            continue
        if os.path.lexists(_long(original)):
            results.append(OpResult(str(original), False, "restore", note="原位置已有同名檔案"))
            continue
        try:
            os.makedirs(_long(original.parent), exist_ok=True)
            os.rename(_long(stored), _long(original))
        except OSError as exc:
            results.append(OpResult(str(original), False, "restore", note=_reason(exc)))
        else:
            item["restored"] = True
            results.append(OpResult(str(original), True, "restore"))
    Path(manifest_path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _remove_empty_dirs(Path(manifest_path).parent)
    return results


def manifest_status(manifest_path: Path) -> tuple[int, int]:
    """回傳 (已還原數, 總數)；manifest 不存在時回傳 (0, 0)。"""
    try:
        items = json.loads(Path(manifest_path).read_text(encoding="utf-8"))["items"]
    except (OSError, ValueError, KeyError):
        return 0, 0
    return sum(1 for it in items if it.get("restored")), len(items)


def _remove_empty_dirs(batch_dir: Path):
    """還原後清掉批次資料夾裡留下的空資料夾（只移除空資料夾，不碰任何檔案）。"""
    for dirpath, _, _ in sorted(os.walk(batch_dir), key=lambda t: -len(t[0])):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


# ── 對外入口與操作紀錄 ─────────────────────────────────────────────────
def remove_files(items: list[tuple[Path, int]], method: str = "recycle", hwnd: int | None = None,
                 progress: ProgressCallback | None = None) -> list[OpResult]:
    """把檔案移到資源回收筒（method="recycle"）或隔離資料夾（"quarantine"），不會永久刪除。

    items 為 (路徑, 大小)。回收筒放不進去的檔案自動改走隔離資料夾，並在 note 註明原因。
    """
    total, done = len(items), 0
    batch_id = new_batch_id()

    def advance(n: int):
        nonlocal done
        done += n
        if progress:
            progress(done, total)

    results: list[OpResult] = []
    to_quarantine: list[tuple[Path, int, str]] = []
    if method == "recycle" and sys.platform == "win32":
        cache: dict = {}
        eligible: list[tuple[Path, int]] = []
        for p, size in items:
            reason = recycle_obstacle(p, size, cache)
            if reason:
                to_quarantine.append((p, size, reason))
            else:
                eligible.append((p, size))
        recycled, fallback = _recycle(eligible, hwnd, advance)
        results += recycled
        to_quarantine += fallback
        done -= len(fallback)            # 改走隔離區的檔案稍後再計入進度
    else:
        to_quarantine = [(p, s, "") for p, s in items]

    if to_quarantine:
        moved = quarantine([p for p, _, _ in to_quarantine], batch_id, advance)
        for res, (_, _, reason) in zip(moved, to_quarantine):
            if res.ok and reason:
                res.note = reason
            results.append(res)

    _record(batch_id, method, items, results)
    return results


def journal_path() -> Path:
    return data_dir() / "journal.jsonl"


def _record(batch_id: str, method: str, items, results: list[OpResult]):
    sizes = {str(p): s for p, s in items}
    manifests = sorted({str(quarantine_root_for(Path(r.path)) / batch_id / "manifest.json")
                        for r in results if r.ok and r.method == "quarantine"})
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "batch": batch_id,
        "method": method,
        "recycled": sum(1 for r in results if r.ok and r.method == "recycle"),
        "quarantined": sum(1 for r in results if r.ok and r.method == "quarantine"),
        "failed": sum(1 for r in results if not r.ok),
        "bytes": sum(sizes.get(r.path, 0) for r in results if r.ok),
        "manifests": manifests,
    }
    with open(journal_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_journal() -> list[dict]:
    """讀取操作紀錄，新的在前。"""
    try:
        lines = journal_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries[::-1]
