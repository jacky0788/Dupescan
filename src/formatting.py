# 顯示用的格式化：容量、剩餘時間、經過時間、日期。掃描引擎、介面與報表共用（不依賴 Qt）。
from datetime import datetime


def human_size(n: float) -> str:
    """1536 → "1.5 KB"（以 1024 為單位，和 Windows 檔案總管一致）。"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_eta(seconds: int) -> str:
    """剩餘時間：「約 3 分 20 秒」；負數表示還無法估算。"""
    if seconds < 0:
        return "計算中…"
    if seconds < 5:
        return "即將完成"
    if seconds < 60:
        return f"約 {seconds} 秒"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"約 {m} 分 {s} 秒" if s else f"約 {m} 分鐘"
    h, m = divmod(m, 60)
    return f"約 {h} 小時 {m} 分"


def format_duration(seconds: float) -> str:
    """經過時間：「12 秒」「1 分 20 秒」「2 小時 5 分」。"""
    seconds = max(int(round(seconds)), 0)
    if seconds < 60:
        return f"{seconds} 秒"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} 分 {s} 秒" if s else f"{m} 分鐘"
    h, m = divmod(m, 60)
    return f"{h} 小時 {m} 分"


def format_time(value) -> str:
    """ISO 字串或 timestamp → 「2026-10-21 14:03」；看不懂時原樣回傳（或空字串）。"""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value)
        else:
            dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value or "")
    return dt.strftime("%Y-%m-%d %H:%M")
