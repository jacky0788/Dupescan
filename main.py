# 程式進入點：建立 Qt 應用程式與主視窗，進入事件迴圈
import sys
import traceback

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.logger import logger, setup_logging
from src.ui.main_window import MainWindow


def _excepthook(exc_type, exc, tb):
    """未處理的例外：寫進 log 並提示，而不是讓程式悄悄結束（用 pythonw 啟動時看不到主控台）。"""
    logger.critical("未處理的例外：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
    if QApplication.instance() is not None:
        QMessageBox.critical(
            None, "DupeScan 發生錯誤",
            f"{exc_type.__name__}: {exc}\n\n詳細內容已寫入 log：\n{logger.log_file}")


def main():
    setup_logging()
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    app.setApplicationName("DupeScan")
    app.setOrganizationName("DupeScan")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
