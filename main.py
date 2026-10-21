# 程式進入點：準備 log 與當機紀錄、接住所有未處理的錯誤，再開啟主視窗。
import logging
import os
import sys
import traceback

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from src import __version__, session
from src.logger import log_dir, logger, setup_logging


def main() -> int:
    setup_logging()
    crash_log = session.enable_crash_log(log_dir())       # 原生層級當機（Qt 等）也留下呼叫堆疊
    crashed = session.start_session(crash_log)
    logger.info(f"DupeScan {__version__} 啟動（Python {sys.version.split()[0]}）")
    for info in crashed:
        logger.warning(f"上次沒有正常關閉（pid {info.get('pid')}，開始於 {info.get('started_at', '?')}）")

    app = QApplication(sys.argv)
    app.setApplicationName("DupeScan")
    app.setOrganizationName("DupeScan")

    from src.ui import theme
    from src.ui.app_settings import AppSettings
    from src.ui.app_window import AppWindow
    from src.ui.errors import ErrorReporter
    from src.ui.scan_controller import wait_unfinished

    reporter = ErrorReporter()
    reporter.install()                                  # 之後任何未處理的例外都會寫 log 並顯示錯誤視窗
    try:
        settings = AppSettings()
        theme.apply(app, theme.font_px(settings.text_size))
        window = AppWindow(settings, crashed)
    except Exception as exc:
        logger.critical("啟動失敗：\n" + traceback.format_exc())
        QMessageBox.critical(None, "DupeScan 無法啟動",
                             f"啟動時發生錯誤：{exc}\n\n詳細內容已寫入 log：\n{logger.log_file}")
        session.end_session()
        return 1
    reporter.parent_window = window
    window.show()
    QTimer.singleShot(0, window.startup_notices)
    code = app.exec()

    session.end_session()
    if not wait_unfinished(5000):
        # 掃描執行緒還卡在很慢的磁碟上：直接結束程序，避免 Qt 收尾時因為執行緒還在跑而閃退
        logger.warning("掃描執行緒沒有結束，直接結束程式")
        logging.shutdown()
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
