# 錯誤與當機的回應：
#   未處理的例外（主執行緒、背景執行緒）→ 寫進 log，並跳出錯誤視窗（說明、複製錯誤內容、開啟 log 資料夾），
#     程式繼續執行；短時間內重複發生時合併在同一個視窗，不會一直跳。
#   Qt 自己的警告 → 寫進 log（除錯用）
#   上次沒有正常關閉 → 啟動時提示，附上當機紀錄（如果有）
import sys
import threading
import traceback

from PyQt6.QtCore import QObject, QtMsgType, Qt, pyqtSignal, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication, QPlainTextEdit

from ..logger import log_dir, logger
from ..session import read_crash_log
from .dialogs import Dialog, open_folder
from .theme import icon
from .widgets import button, label


def describe(exc_type, exc) -> str:
    """給使用者看的一句話（常見的錯誤換成白話）。"""
    if issubclass(exc_type, MemoryError):
        return "記憶體不足。請關閉其他程式，或一次掃描少一點資料夾。"
    if issubclass(exc_type, PermissionError):
        return f"沒有權限存取檔案或資料夾：{exc}"
    if issubclass(exc_type, OSError):
        return f"讀寫檔案時發生錯誤：{exc}"
    text = str(exc)
    return f"{exc_type.__name__}: {text}" if text else exc_type.__name__


class ErrorDialog(Dialog):
    def __init__(self, summary: str, details: str, parent=None):
        super().__init__("DupeScan 發生錯誤", parent, 600)
        self.count = 1
        self.add_heading("發生了沒有預期到的錯誤")
        self.summary = label(summary, wrap=True, selectable=True)
        self.lay.addWidget(self.summary)
        self.lay.addWidget(label("DupeScan 還可以繼續使用，你的檔案沒有被更動。如果一直發生，"
                                 "請把錯誤內容和 log 提供給開發者。", "muted", wrap=True))
        self.more = label("", "warning")
        self.more.hide()
        self.lay.addWidget(self.more)
        self.toggle = button("詳細內容", "expander", self._toggle)
        self.toggle.setIcon(icon("chevron-right"))
        self.lay.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.details = QPlainTextEdit(details)
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.details.setMinimumHeight(220)
        self.details.hide()
        self.lay.addWidget(self.details, 1)
        copy = button("複製錯誤內容", slot=self._copy)
        logs = button("開啟 log 資料夾", slot=lambda: open_folder(log_dir()))
        close = button("關閉", "primary", self.accept)
        self.add_buttons(close, left=[copy, logs])
        close.setDefault(True)

    def add_error(self, summary: str, details: str):
        """視窗還開著時又發生錯誤：累計次數、附上新的內容。"""
        self.count += 1
        self.more.setText(f"之後又發生了 {self.count - 1} 次錯誤（內容都附在下面）")
        self.more.show()
        self.details.appendPlainText("\n" + "─" * 40 + "\n" + details)

    def _toggle(self):
        show = self.details.isHidden()
        self.details.setVisible(show)
        self.toggle.setIcon(icon("chevron-down" if show else "chevron-right"))
        if show:
            self.resize(max(self.width(), 760), max(self.height(), 520))

    def _copy(self):
        QApplication.clipboard().setText(f"{self.summary.text()}\n\n{self.details.toPlainText()}\n\n"
                                         f"log：{logger.log_file}")


class ErrorReporter(QObject):
    """接住所有未處理的例外：寫 log、在主執行緒顯示錯誤視窗。"""
    _report = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dialog: ErrorDialog | None = None
        self._report.connect(self._show, Qt.ConnectionType.QueuedConnection)
        self.parent_window = None

    def install(self):
        sys.excepthook = self.excepthook
        threading.excepthook = lambda args: self.excepthook(args.exc_type, args.exc_value,
                                                            args.exc_traceback)
        qInstallMessageHandler(_qt_message)

    def uninstall(self):
        sys.excepthook = sys.__excepthook__
        threading.excepthook = threading.__excepthook__
        qInstallMessageHandler(None)

    def excepthook(self, exc_type, exc, tb):
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            logger.critical("未處理的例外：\n" + details)
        except Exception:
            pass
        if issubclass(exc_type, KeyboardInterrupt):
            return
        self._report.emit(describe(exc_type, exc), details)

    def _show(self, summary: str, details: str):
        if QApplication.instance() is None:
            return
        if self._dialog is not None and self._dialog.isVisible():
            self._dialog.add_error(summary, details)
            return
        parent = self.parent_window
        self._dialog = ErrorDialog(summary, details, parent if parent is not None and parent.isVisible() else None)
        self._dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._dialog.finished.connect(self._closed)
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def _closed(self):
        self._dialog = None


def _qt_message(mode, context, message):
    if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        logger.error(f"Qt：{message}")
    elif mode == QtMsgType.QtWarningMsg:
        logger.warning(f"Qt：{message}")


class CrashNoticeDialog(Dialog):
    """上次沒有正常關閉時的提示。"""

    def __init__(self, crashed: list[dict], has_job: bool, has_last: bool, parent=None):
        super().__init__("上次沒有正常關閉", parent, 560)
        self.add_heading("上次 DupeScan 沒有正常關閉",
                         "可能是當機、被強制結束，或電腦突然關機。你的檔案沒有被更動。")
        tips = []
        if has_job:
            tips.append("未完成的掃描可以在開始畫面按「繼續掃描」，已經比對過的檔案不必重新讀取。")
        if has_last:
            tips.append("上次掃描完成的結果有保存，可以在開始畫面開啟。")
        for tip in tips:
            self.lay.addWidget(label("• " + tip, wrap=True))
        log_text = "\n\n".join(t for t in (read_crash_log(c.get("crash_log")) for c in crashed) if t)
        if log_text:
            self.lay.addWidget(label("當機時的紀錄（提供給開發者可以幫助找出原因）：", "muted"))
            box = QPlainTextEdit(log_text)
            box.setReadOnly(True)
            box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            box.setMinimumHeight(180)
            self.lay.addWidget(box, 1)
        self.log_text = log_text
        logs = button("開啟 log 資料夾", slot=lambda: open_folder(log_dir()))
        ok = button("知道了", "primary", self.accept)
        self.add_buttons(ok, left=[logs])
        ok.setDefault(True)
