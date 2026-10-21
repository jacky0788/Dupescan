# 程式的生命週期（用子程序測試）：正常啟動與結束、啟動失敗、掃描中被強制結束後可以繼續、
# 關閉視窗時掃描執行緒卡住也不會閃退
import subprocess
import sys
import time

import pytest
from PyQt6.QtCore import QSettings

from conftest import ROOT
from helpers import blob, wait_until
from src import scan_job, session

KB = 1024

# 共用開頭：offscreen、測試用的資料夾與設定檔（環境變數由 conftest 設定，子程序會繼承）
PRELUDE = r'''
import os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, sys.argv[1])
'''

MAIN_SCRIPT = PRELUDE + r'''
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox
import main as entry

print("PID", os.getpid(), flush=True)
mode = sys.argv[2]
if mode == "fail":
    import src.ui.app_window as aw
    def broken(*args, **kwargs):
        raise RuntimeError("故意的啟動錯誤")
    aw.AppWindow = broken
    QMessageBox.critical = staticmethod(lambda *a, **k: print("CRITICAL:", a[2].splitlines()[0], flush=True))

def fake_exec(self):
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        self.processEvents()
        time.sleep(0.02)
    titles = [w.windowTitle() for w in self.topLevelWidgets() if w.isVisible()]
    print("WINDOWS:", titles, flush=True)
    for w in self.topLevelWidgets():
        w.close()
    return 0
QApplication.exec = fake_exec
code = entry.main()
print("EXIT", code, flush=True)
'''


def run(script: str, *args, timeout=90):
    return subprocess.run([sys.executable, "-B", "-c", script, str(ROOT), *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout, cwd=ROOT)


def test_main_starts_shows_window_and_exits_cleanly(isolated_data_dir):
    proc = run(MAIN_SCRIPT, "normal")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WINDOWS: ['DupeScan']" in proc.stdout and "EXIT 0" in proc.stdout
    pid = int(proc.stdout.split("PID", 1)[1].split()[0])
    assert not (session._sessions_dir() / f"{pid}.lock").exists()        # 正常結束會清掉 lock 檔
    logs = sorted((isolated_data_dir / "logs").glob("dupescan_*.log"))
    assert logs and "啟動" in logs[-1].read_text(encoding="utf-8")       # log 寫在測試用的資料夾


def test_main_reports_startup_failure():
    proc = run(MAIN_SCRIPT, "fail")
    assert "CRITICAL: 啟動時發生錯誤：故意的啟動錯誤" in proc.stdout, proc.stdout + proc.stderr
    assert "EXIT 1" in proc.stdout


SLOW_SCAN_SCRIPT = PRELUDE + r'''
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from src import scanner as sc, session
from src.logger import log_dir
from src.scan_options import ScanOptions
from src.ui.app_window import AppWindow
from src.ui.scan_controller import wait_unfinished

mode, folder = sys.argv[2], sys.argv[3]
session.start_session(session.enable_crash_log(log_dir()))
state = {"hashing": 0}

def slow_full_hash(path, checkpoint=None):
    state["hashing"] += 1
    if mode == "stuck":                      # 不理會停止，模擬卡在很慢的磁碟上
        time.sleep(30)
    while True:                               # 一直讀不完，直到被停止或程序被結束
        if checkpoint:
            checkpoint(0)
        time.sleep(0.05)
sc._full_hash = slow_full_hash

win = AppWindow()
win.show()
win.home.set_options(ScanOptions(roots=[folder], min_size=0))
win.home.start_button.click()
end = time.monotonic() + 20
while not state["hashing"] and time.monotonic() < end:
    app.processEvents()
    time.sleep(0.01)
print("HASHING", flush=True)
if mode == "kill":
    time.sleep(30)                            # 等測試把這個程序強制結束
elif mode == "stuck":
    win.close()
    print("CLOSED", flush=True)
    if not wait_unfinished(500):
        print("EXIT VIA _exit", flush=True)
        session.end_session()
        os._exit(0)
    print("FINISHED NORMALLY", flush=True)
'''


@pytest.fixture
def slow_data(tree):
    data = blob(64 * KB, 7)                  # 大於 4 KB 才會進到整檔比對
    tree("data/a.bin", data)
    tree("data/b.bin", data)
    scan_job.clear_job()
    return tree.root / "data"


def test_killed_during_scan_can_resume(qapp, slow_data, tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-B", "-c", SLOW_SCAN_SCRIPT, str(ROOT), "kill", str(slow_data)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=ROOT)
    try:
        line = proc.stdout.readline()
        assert line.strip() == "HASHING", line + proc.stderr.read()
    finally:
        proc.kill()                           # 模擬當機或被工作管理員結束
        proc.wait(30)

    crashed = [c for c in session.start_session() if c.get("pid") == proc.pid]
    session.end_session()
    assert crashed, "應該偵測到上次沒有正常關閉"
    job = scan_job.load_job()
    assert job is not None and job.state == "running" and job.options.roots == [str(slow_data)]

    from src.ui.app_settings import AppSettings
    from src.ui.app_window import AppWindow
    from src.ui.errors import CrashNoticeDialog
    notices = []
    monkeypatch.setattr(CrashNoticeDialog, "exec", lambda self: notices.append(self) or 0)
    win = AppWindow(AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)), crashed)
    try:
        win.show()
        win.startup_notices()
        assert notices
        assert not win.home.resume_banner.isHidden()          # 被結束的程序已經不在，可以繼續
        win.home.resume_button.click()
        assert wait_until(qapp, lambda: win.stack.currentWidget() is win.results, 20)
        assert len(win.results.groups()) == 1 and scan_job.load_job() is None
    finally:
        win.close()
        wait_until(qapp, lambda: not win.controller.busy, 5)


def test_close_while_scan_thread_is_stuck_does_not_crash(slow_data):
    start = time.monotonic()
    proc = run(SLOW_SCAN_SCRIPT, "stuck", slow_data, timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "CLOSED" in proc.stdout and "EXIT VIA _exit" in proc.stdout, out
    assert "Destroyed while thread" not in out
    assert time.monotonic() - start < 60
    job = scan_job.load_job()
    assert job is not None and job.state == "stopped"            # 下次可以繼續
    scan_job.clear_job()
