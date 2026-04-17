# 程式進入點：建立 Qt 應用程式與主視窗，進入事件迴圈
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from src.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DupeScan")
    app.setOrganizationName("DupeScan")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
