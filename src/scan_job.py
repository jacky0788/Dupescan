# 續掃紀錄：掃描開始時寫入、進行中定期更新，正常完成才清掉。
#
# 停止、出錯、當機或直接關閉視窗都會留下紀錄，下次開啟時「開始」畫面顯示「上次的掃描沒有完成」。
# 繼續掃描＝用同樣的設定重新開始：已經算過的 hash 都在雜湊快取裡（每 2 秒寫入一次），
# 所以只需要重新列一次目錄，比對過的檔案不會再讀。
#
# 掃描完成時另外把結果存成 last_results.dupescan（含一份小小的摘要 last_results.json），
# 之後當機也不會丟掉結果，「開始」畫面可以直接開啟上次的結果。
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .app_paths import data_dir
from .scan_options import ScanOptions

JOB_FILE           = "scan_job.json"
LAST_RESULTS_FILE  = "last_results.dupescan"
LAST_SUMMARY_FILE  = "last_results.json"

STATES = ("running", "stopped", "failed")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class ScanJob:
    options: ScanOptions
    state: str = "running"          # running＝進行中（或上次當機）、stopped＝使用者停止、failed＝出錯
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    step: int = 1                   # 停在第幾步（1–3）
    step_progress: float = 0.0      # 該步驟的完成比例（0–1；第 1 步無法估算時為 0）
    error: str = ""                 # 出錯時的訊息
    pid: int = field(default_factory=os.getpid)

    def progress_text(self) -> str:
        """「停在第 3 步（63%）」。"""
        pct = f"（{round(self.step_progress * 100)}%）" if self.step_progress > 0 else ""
        return f"停在第 {self.step} 步{pct}"

    def to_dict(self) -> dict:
        return {"options": self.options.to_dict(), "state": self.state,
                "started_at": self.started_at, "updated_at": self.updated_at,
                "step": self.step, "step_progress": self.step_progress,
                "error": self.error, "pid": self.pid}

    @classmethod
    def from_dict(cls, data) -> "ScanJob | None":
        if not isinstance(data, dict) or not isinstance(data.get("options"), dict):
            return None
        options = ScanOptions.from_dict(data["options"])
        if not options.roots:
            return None

        def number(key, default, kind):
            value = data.get(key)
            return value if isinstance(value, kind) and not isinstance(value, bool) else default

        state = data.get("state") if data.get("state") in STATES else "running"
        return cls(options=options, state=state,
                   started_at=str(data.get("started_at") or ""),
                   updated_at=str(data.get("updated_at") or ""),
                   step=min(max(number("step", 1, int), 1), 3),
                   step_progress=min(max(float(number("step_progress", 0.0, (int, float))), 0.0), 1.0),
                   error=str(data.get("error") or ""), pid=number("pid", 0, int))


# ── 續掃紀錄 ─────────────────────────────────────────────────────────────
def job_path() -> Path:
    return data_dir() / JOB_FILE


def save_job(job: ScanJob):
    job.updated_at = _now()
    _write_json(job_path(), job.to_dict())


def load_job() -> ScanJob | None:
    """讀回續掃紀錄；沒有或內容壞掉時回傳 None（壞掉的檔案順便刪掉）。"""
    path = job_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        data = None
    job = ScanJob.from_dict(data)
    if job is None:
        clear_job()
    return job


def clear_job():
    try:
        job_path().unlink()
    except FileNotFoundError:
        pass


# ── 上次的結果 ───────────────────────────────────────────────────────────
def last_results_path() -> Path:
    return data_dir() / LAST_RESULTS_FILE


def save_last_summary(roots: list[str], scanned_at: str, groups: int, wasted: int):
    _write_json(data_dir() / LAST_SUMMARY_FILE,
                {"roots": roots, "scanned_at": scanned_at, "groups": groups, "wasted": wasted})


def load_last_summary() -> dict | None:
    """上次結果的摘要（給「開始」畫面顯示）；結果檔不在或摘要壞掉時回傳 None。"""
    if not last_results_path().exists():
        return None
    try:
        data = json.loads((data_dir() / LAST_SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("groups"), int):
        return None
    return data
