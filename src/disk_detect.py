"""
Disk type detection for adaptive scan threading strategy.

Windows: ctypes DeviceIoControl — SeekPenalty (HDD vs SSD) + BusType (NVMe vs SATA).
         No administrator privilege required; works on Windows 10/11.
Non-Windows or any failure: returns conservative fallback profile.
"""

from __future__ import annotations
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiskProfile:
    type_name:     str   # human-readable label shown in UI
    pass2_workers: int   # threads for Pass 2 (many small reads)
    pass3_workers: int   # threads for Pass 3 (few large reads)

    def summary(self) -> str:
        if self.pass2_workers == 1:
            return f"{self.type_name}（單執行緒）"
        return (f"{self.type_name}"
                f"（Pass2 ×{self.pass2_workers} / Pass3 ×{self.pass3_workers} 執行緒）")


# ── Predefined profiles ────────────────────────────────────────────────────────
_FALLBACK    = DiskProfile("未知磁碟",  2, 1)
_HDD         = DiskProfile("HDD",       1, 1)
_NVME_SSD    = DiskProfile("NVMe SSD",  8, 4)
_SATA_SSD    = DiskProfile("SATA SSD",  4, 2)
_GENERIC_SSD = DiskProfile("SSD",       4, 2)


# ── Public API ─────────────────────────────────────────────────────────────────

def detect_disk_profile(path: Path) -> DiskProfile:
    """Return a DiskProfile for the physical disk that hosts *path*.

    Detection is near-instant (<10 ms) and requires no extra dependencies.
    Any exception — missing drive, network path, virtualised disk — returns
    the conservative fallback (2 threads).
    """
    if sys.platform != "win32":
        return _FALLBACK
    try:
        return _detect_windows(path)
    except Exception:
        return _FALLBACK


# ── Windows implementation ─────────────────────────────────────────────────────

def _detect_windows(path: Path) -> DiskProfile:
    device = _volume_device_path(path)
    if device is None:
        return _FALLBACK

    seek_penalty = _query_seek_penalty(device)
    if seek_penalty is None:
        return _FALLBACK

    if seek_penalty:
        return _HDD

    bus = _query_bus_type(device)
    if bus == "nvme":
        return _NVME_SSD
    if bus == "sata":
        return _SATA_SSD
    return _GENERIC_SSD


def _volume_device_path(path: Path) -> str | None:
    """Map *path* to its Win32 volume device string, e.g. '\\\\.\\C:'."""
    try:
        drive = path.resolve().drive        # 'C:' / 'D:' …
        if not drive or drive.startswith("\\\\"):
            return None                     # UNC / network path — skip
        return f"\\\\.\\{drive}"
    except Exception:
        return None


def _open_volume(device: str):
    """Open a volume handle for DeviceIoControl (no admin needed)."""
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
    import ctypes, ctypes.wintypes
    br = ctypes.wintypes.DWORD(0)
    return bool(ctypes.windll.kernel32.DeviceIoControl(
        h, ctl,
        ctypes.byref(in_buf), ctypes.sizeof(in_buf),
        ctypes.byref(out_buf), ctypes.sizeof(out_buf),
        ctypes.byref(br), None,
    ))


def _query_seek_penalty(device: str) -> bool | None:
    """
    Query StorageDeviceSeekPenaltyProperty (PropertyId=7).
    Returns True → HDD (has seek penalty), False → SSD, None → error.
    """
    import ctypes, ctypes.wintypes

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_uint), ("QueryType", ctypes.c_uint),
                    ("_pad", ctypes.c_byte * 1)]

    class SeekPenaltyDescriptor(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_uint), ("Size", ctypes.c_uint),
                    ("IncursSeekPenalty", ctypes.wintypes.BOOL)]

    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    h = _open_volume(device)
    if h is None:
        return None
    try:
        q = Query(); q.PropertyId = 7; q.QueryType = 0
        r = SeekPenaltyDescriptor()
        if not _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, q, r):
            return None
        return bool(r.IncursSeekPenalty)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _query_bus_type(device: str) -> str:
    """
    Query StorageDeviceProperty (PropertyId=0) → STORAGE_DEVICE_DESCRIPTOR.BusType.
    Returns 'nvme', 'sata', or 'other'.
    BusType enum: NVMe=17, SATA=11 (from ntddstor.h STORAGE_BUS_TYPE).
    """
    import ctypes, ctypes.wintypes

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_uint), ("QueryType", ctypes.c_uint),
                    ("_pad", ctypes.c_byte * 1)]

    # Fixed-size prefix of STORAGE_DEVICE_DESCRIPTOR (variable tail not needed)
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
            ("BusType",               ctypes.c_uint),   # STORAGE_BUS_TYPE
            ("RawPropertiesLength",   ctypes.c_uint),
        ]

    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    BUS_NVME = 17
    BUS_SATA = 11

    h = _open_volume(device)
    if h is None:
        return "other"
    try:
        q = Query(); q.PropertyId = 0; q.QueryType = 0
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
