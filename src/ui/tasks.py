# 在背景執行緒做一件會花時間的事（建立移除計畫、移除檔案），同時顯示進度視窗，畫面不會卡住。
import traceback

from PyQt6.QtCore import QEventLoop, QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QProgressDialog


class TaskError(Exception):
    """背景工作拋出的例外；details 是完整的呼叫堆疊。"""

    def __init__(self, error: BaseException, details: str):
        super().__init__(str(error) or type(error).__name__)
        self.error = error
        self.details = details


class _Worker(QObject):
    progress = pyqtSignal(str, int, int)
    done = pyqtSignal(object)
    failed = pyqtSignal(object, str)
    ended = pyqtSignal()

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn(lambda text, done=0, total=0: self.progress.emit(text, done, total))
        except Exception as exc:
            self.failed.emit(exc, traceback.format_exc())
        else:
            self.done.emit(result)
        finally:
            self.ended.emit()


def run_task(parent, title: str, text: str, fn, delay_ms: int = 300):
    """在背景執行 fn(report) 並等它完成，回傳 fn 的結果；fn 拋出例外時改拋 TaskError。

    report(文字, 已完成, 總數) 更新進度視窗；總數為 0 時顯示忙碌動畫。等待期間進度視窗是
    模態的（不能操作其他視窗），但畫面會持續更新。
    """
    dialog = QProgressDialog(text, "", 0, 0, parent)
    dialog.setWindowTitle(title)
    dialog.setCancelButton(None)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setMinimumDuration(delay_ms)
    dialog.setMinimumWidth(420)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setValue(0)

    outcome: dict = {}
    loop = QEventLoop()
    thread = QThread()
    worker = _Worker(fn)
    worker.moveToThread(thread)

    def on_progress(label: str, done: int, total: int):
        if label:
            dialog.setLabelText(label)
        if total > 0:
            dialog.setMaximum(total)
            dialog.setValue(min(done, total))

    queued = Qt.ConnectionType.QueuedConnection          # 一律回到主執行緒處理
    worker.progress.connect(on_progress, queued)
    worker.done.connect(lambda result: outcome.setdefault("result", result), queued)
    worker.failed.connect(lambda exc, details: outcome.setdefault("error", (exc, details)), queued)
    worker.ended.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.finished.connect(loop.quit, queued)
    thread.started.connect(worker.run)
    thread.start()
    loop.exec()
    thread.wait()
    dialog.close()
    dialog.deleteLater()
    worker.deleteLater()
    thread.deleteLater()
    if "error" in outcome:
        raise TaskError(*outcome["error"])
    return outcome.get("result")
