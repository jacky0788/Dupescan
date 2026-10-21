# 續掃紀錄、上次結果摘要、異常關閉偵測與當機紀錄
import json
import os
import subprocess
import sys

import pytest

from src import scan_job, session
from src.scan_options import ScanOptions


def test_job_round_trip_and_clear():
    job = scan_job.ScanJob(ScanOptions(roots=["D:/a"], types=["image"]))
    job.step, job.step_progress, job.state = 3, 0.634, "stopped"
    scan_job.save_job(job)
    loaded = scan_job.load_job()
    assert loaded.options == job.options and loaded.state == "stopped"
    assert loaded.progress_text() == "停在第 3 步（63%）"
    scan_job.clear_job()
    assert scan_job.load_job() is None


def test_bad_job_files_are_ignored_and_removed():
    path = scan_job.job_path()
    path.write_text("{broken", encoding="utf-8")
    assert scan_job.load_job() is None and not path.exists()
    path.write_text(json.dumps({"options": {"roots": []}}), encoding="utf-8")
    assert scan_job.load_job() is None                       # 沒有資料夾的紀錄沒有意義
    path.write_text(json.dumps({"options": {"roots": ["D:/a"]}, "step": "x",
                                "step_progress": 7, "state": "weird"}), encoding="utf-8")
    job = scan_job.load_job()
    assert (job.step, job.step_progress, job.state) == (1, 1.0, "running")
    scan_job.clear_job()


def test_last_results_summary():
    results = scan_job.last_results_path()
    results.unlink(missing_ok=True)
    assert scan_job.load_last_summary() is None
    results.write_bytes(b"x")
    scan_job.save_last_summary(["D:/a"], "2026-10-21T10:00:00", 3, 1234)
    assert scan_job.load_last_summary()["groups"] == 3
    results.unlink()
    assert scan_job.load_last_summary() is None              # 結果檔不在就不顯示摘要


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_pid_alive():
    assert session.pid_alive(os.getpid())
    assert not session.pid_alive(_dead_pid())
    assert not session.pid_alive(0)


def test_start_session_reports_only_dead_sessions():
    sessions = session._sessions_dir()
    stale = _dead_pid()
    (sessions / f"{stale}.lock").write_text(json.dumps({"pid": stale, "crash_log": ""}))
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (sessions / f"{alive.pid}.lock").write_text(json.dumps({"pid": alive.pid}))
        crashed = session.start_session()
        assert stale in [c["pid"] for c in crashed]
        assert alive.pid not in [c["pid"] for c in crashed]
        assert (sessions / f"{alive.pid}.lock").exists()    # 另一個還在執行的不動
        assert (sessions / f"{os.getpid()}.lock").exists()
        session.end_session()
        assert not (sessions / f"{os.getpid()}.lock").exists()
    finally:
        alive.kill()
        alive.wait()
        (sessions / f"{alive.pid}.lock").unlink(missing_ok=True)


CRASH_SCRIPT = r'''
import ctypes, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)   # 不要跳出 Windows 錯誤回報視窗
from src import session
log = session.enable_crash_log(Path(sys.argv[2]))
session.start_session(log)
import faulthandler
faulthandler._sigsegv()                                 # 模擬原生層級的當機
'''


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 專用")
def test_native_crash_is_recorded_and_detected(tmp_path):
    from conftest import ROOT
    script = tmp_path / "crash.py"
    script.write_text(CRASH_SCRIPT, encoding="utf-8")
    logs = tmp_path / "logs"
    proc = subprocess.run([sys.executable, "-B", str(script), str(ROOT), str(logs)],
                          capture_output=True, timeout=60)
    assert proc.returncode != 0
    crashed = [c for c in session.start_session()
               if str(c.get("crash_log", "")).startswith(str(logs))]
    session.end_session()
    assert len(crashed) == 1
    text = session.read_crash_log(crashed[0]["crash_log"])
    assert "fatal" in text.lower() and "crash.py" in text
