# 磁碟類型偵測模組：掃描開始前判斷目標路徑所在磁碟的種類，
# 據此決定 Scanner 的 Pass 2 / Pass 3 執行緒數量。
#
# 偵測方式：Windows 專用 ctypes DeviceIoControl（無需管理員權限，耗時 <10ms）。
# 非 Windows 或任何例外 → 直接回傳保守預設值（2 執行緒），不影響掃描主流程。

from __future__ import annotations
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiskProfile:
    """掃描執行緒策略，由 detect_disk_profile() 依磁碟類型回傳。"""
    type_name:     str   # UI 顯示名稱
    pass2_workers: int   # Pass 2（大量小型讀取）建議執行緒數
    pass3_workers: int   # Pass 3（少量大型讀取）建議執行緒數

    def summary(self) -> str:
        """產生 UI 進度列顯示用的字串。"""
        if self.pass2_workers == 1:
            return f"{self.type_name}（單執行緒）"
        return (f"{self.type_name}"
                f"（Pass2 ×{self.pass2_workers} / Pass3 ×{self.pass3_workers} 執行緒）")


# ── 各磁碟類型對應的執行緒策略 ────────────────────────────────────────────────
# HDD 維持 1 執行緒：多執行緒並發讀取會造成磁頭隨機尋軌，反而更慢。
# NVMe 支援高佇列深度，8 執行緒可讓 I/O 請求充分並行。
# SATA SSD 無尋軌損耗但頻寬較低，4 執行緒為實測平衡點。
# 未知磁碟（網路、虛擬、USB）保守使用 2 執行緒。
_FALLBACK    = DiskProfile("未知磁碟",  2, 1)
_HDD         = DiskProfile("HDD",       1, 1)
_NVME_SSD    = DiskProfile("NVMe SSD",  8, 4)
_SATA_SSD    = DiskProfile("SATA SSD",  4, 2)
_GENERIC_SSD = DiskProfile("SSD",       4, 2)


# ── 公開 API ───────────────────────────────────────────────────────────────────

def detect_disk_profile(path: Path) -> DiskProfile:
    """回傳 *path* 所在實體磁碟的 DiskProfile。

    任何例外（UNC 網路路徑、虛擬磁碟、非 Windows 系統）
    一律回傳保守的 _FALLBACK，不拋出例外。
    """
    if sys.platform != "win32":
        return _FALLBACK
    try:
        return _detect_windows(path)
    except Exception:
        return _FALLBACK


# ── Windows 實作 ───────────────────────────────────────────────────────────────

def _detect_windows(path: Path) -> DiskProfile:
    """Windows 偵測主流程：SeekPenalty → HDD/SSD → BusType → NVMe/SATA。"""
    device = _volume_device_path(path)
    if device is None:
        return _FALLBACK

    # 第一層：有尋軌延遲 → HDD；否則為 SSD（包含 NVMe/SATA）
    seek_penalty = _query_seek_penalty(device)
    if seek_penalty is None:
        return _FALLBACK
    if seek_penalty:
        return _HDD

    # 第二層：進一步區分 NVMe 與 SATA
    bus = _query_bus_type(device)
    if bus == "nvme":
        return _NVME_SSD
    if bus == "sata":
        return _SATA_SSD
    return _GENERIC_SSD


def _volume_device_path(path: Path) -> str | None:
    """將 *path* 轉換為 Win32 卷裝置路徑，例如 '\\\\.\\C:'。
    UNC / 網路路徑（以 \\\\ 開頭）回傳 None，由呼叫端 fallback。
    """
    try:
        drive = path.resolve().drive        # 'C:' / 'D:' …
        if not drive or drive.startswith("\\\\"):
            return None
        return f"\\\\.\\{drive}"
    except Exception:
        return None


def _open_volume(device: str):
    """以 CreateFileW 開啟卷裝置控制代碼。
    access=0（不請求讀寫權限）對 IOCTL 查詢已足夠，不需要管理員。
    """
    import ctypes
    FILE_SHARE_READ  = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING    = 3
    INVALID          = ctypes.c_void_p(-1).value
    h = ctypes.windll.kernel32.CreateFileW(
        device, 0,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    return h if h != INVALID else None


def _ioctl(h, ctl: int, in_buf, out_buf) -> bool:
    """DeviceIoControl 的薄包裝，回傳布林值表示成功與否。"""
    import ctypes, ctypes.wintypes
    br = ctypes.wintypes.DWORD(0)
    return bool(ctypes.windll.kernel32.DeviceIoControl(
        h, ctl,
        ctypes.byref(in_buf), ctypes.sizeof(in_buf),
        ctypes.byref(out_buf), ctypes.sizeof(out_buf),
        ctypes.byref(br), None,
    ))


def _query_seek_penalty(device: str) -> bool | None:
    """查詢 StorageDeviceSeekPenaltyProperty（PropertyId=7）。

    回傳：
      True  → 磁碟有尋軌延遲 → HDD
      False → 無尋軌延遲     → SSD（NVMe / SATA）
      None  → IOCTL 失敗，由呼叫端 fallback
    """
    import ctypes, ctypes.wintypes

    # IOCTL 輸入：StoragePropertyQuery 結構
    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_uint), ("QueryType", ctypes.c_uint),
                    ("_pad", ctypes.c_byte * 1)]

    # IOCTL 輸出：只需讀 IncursSeekPenalty 欄位
    class SeekPenaltyDescriptor(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_uint), ("Size", ctypes.c_uint),
                    ("IncursSeekPenalty", ctypes.wintypes.BOOL)]

    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    h = _open_volume(device)
    if h is None:
        return None
    try:
        q = Query(); q.PropertyId = 7; q.QueryType = 0  # PropertyId=7 = SeekPenalty
        r = SeekPenaltyDescriptor()
        if not _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, q, r):
            return None
        return bool(r.IncursSeekPenalty)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _query_bus_type(device: str) -> str:
    """查詢 StorageDeviceProperty（PropertyId=0）→ STORAGE_DEVICE_DESCRIPTOR.BusType。

    BusType 枚舉值來自 ntddstor.h：NVMe=17、SATA=11。
    只讀取結構的固定長度前綴（不含變長的 SerialNumber 等字串），
    因此 ctypes 結構不需要覆蓋完整的 STORAGE_DEVICE_DESCRIPTOR。
    回傳 'nvme' / 'sata' / 'other'。
    """
    import ctypes, ctypes.wintypes

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_uint), ("QueryType", ctypes.c_uint),
                    ("_pad", ctypes.c_byte * 1)]

    # STORAGE_DEVICE_DESCRIPTOR 固定長度前綴（不含結尾的變長 RawDeviceProperties）
    class DeviceDescriptorHeader(ctypes.Structure):
        _fields_ = [
            ("Version",               ctypes.c_uint),
            ("Size",                  ctypes.c_uint),
            ("DeviceType",            ctypes.c_byte),
            ("DeviceTypeModifier",    ctypes.c_byte),
            ("RemovableMedia",        ctypes.c_byte),
            ("CommandQueueing",       ctypes.c_byte),
            ("VendorIdOffset",        ctypes.c_uint),
            ("ProductIdOffset",       ctypes.c_uint),
            ("ProductRevisionOffset", ctypes.c_uint),
            ("SerialNumberOffset",    ctypes.c_uint),
            ("BusType",               ctypes.c_uint),   # STORAGE_BUS_TYPE 枚舉
            ("RawPropertiesLength",   ctypes.c_uint),
        ]

    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    BUS_NVME = 17   # BusTypeNvme（ntddstor.h）
    BUS_SATA = 11   # BusTypeSata

    h = _open_volume(device)
    if h is None:
        return "other"
    try:
        q = Query(); q.PropertyId = 0; q.QueryType = 0  # PropertyId=0 = StorageDeviceProperty
        r = DeviceDescriptorHeader()
        if not _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, q, r):
            return "other"
        if r.BusType == BUS_NVME:
            return "nvme"
        if r.BusType == BUS_SATA:
            return "sata"
        return "other"
    finally:
        ctypes.windll.kernel32.CloseHandle(h)
