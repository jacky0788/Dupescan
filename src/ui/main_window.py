"""DupeScan – Main Window (PyQt6, dark theme)"""

import threading
from pathlib import Path

try:
    import humanize
    _HZ = True
except ImportError:
    _HZ = False

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QSize
from PyQt6.QtGui import QColor, QFont, QIcon, QPalette, QAction
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QProgressBar, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QStatusBar, QTableWidget,
    QTableWidgetItem, QToolBar, QVBoxLayout, QWidget, QAbstractItemView,
)

from ..scanner import Scanner
from ..models import DuplicateGroup, FileInfo


def human_size(n: int) -> str:
    if _HZ:
        return humanize.naturalsize(n, binary=True)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── Worker ────────────────────────────────────────────────────────────────────
class ScanWorker(QObject):
    progress = pyqtSignal(str, int, int)
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    def __init__(self, roots: list[str], min_size: int):
        super().__init__()
        self._roots = roots
        self._min_size = min_size
        self._scanner: Scanner | None = None

    def run(self):
        self._scanner = Scanner(
            roots=self._roots,
            min_size=self._min_size,
            on_progress=lambda m, c, t: self.progress.emit(m, c, t),
            on_done=lambda g: self.finished.emit(g),
            on_error=lambda e: self.error.emit(e),
        )
        self._scanner.scan()

    def stop(self):
        if self._scanner:
            self._scanner.stop()


# ── Palette / Style ───────────────────────────────────────────────────────────
DARK_BG     = "#1e1e2e"
PANEL_BG    = "#181825"
SURFACE     = "#313244"
ACCENT      = "#89b4fa"   # blue
ACCENT2     = "#a6e3a1"   # green
DANGER      = "#f38ba8"   # red
TEXT        = "#cdd6f4"
SUBTEXT     = "#a6adc8"
BORDER      = "#45475a"

STYLE = f"""
QMainWindow, QWidget {{
    background-color: {DARK_BG};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 8px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    color: {ACCENT};
    font-weight: bold;
}}
QPushButton {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 14px;
    min-height: 26px;
}}
QPushButton:hover {{
    background-color: #414459;
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT};
    color: {DARK_BG};
}}
QPushButton#btn_scan {{
    background-color: {ACCENT};
    color: {DARK_BG};
    font-weight: bold;
    border: none;
}}
QPushButton#btn_scan:hover {{
    background-color: #74a9f5;
}}
QPushButton#btn_delete {{
    background-color: {DANGER};
    color: {DARK_BG};
    font-weight: bold;
    border: none;
}}
QPushButton#btn_delete:hover {{
    background-color: #f06090;
}}
QPushButton#btn_delete:disabled {{
    background-color: {SURFACE};
    color: {SUBTEXT};
}}
QLineEdit, QSpinBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
QProgressBar {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {TEXT};
    height: 18px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}
QTableWidget {{
    background-color: {PANEL_BG};
    alternate-background-color: {DARK_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    gridline-color: {BORDER};
}}
QTableWidget::item {{
    padding: 4px 8px;
}}
QTableWidget::item:selected {{
    background-color: {SURFACE};
    color: {TEXT};
}}
QHeaderView::section {{
    background-color: {SURFACE};
    color: {SUBTEXT};
    border: none;
    border-right: 1px solid {BORDER};
    padding: 5px 8px;
    font-weight: bold;
}}
QScrollBar:vertical {{
    background: {PANEL_BG};
    width: 10px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QStatusBar {{
    background-color: {PANEL_BG};
    border-top: 1px solid {BORDER};
    color: {SUBTEXT};
}}
QCheckBox {{
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {PANEL_BG};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QLabel#badge_wasted {{
    background-color: {DANGER};
    color: {DARK_BG};
    border-radius: 4px;
    padding: 2px 8px;
    font-weight: bold;
}}
QLabel#badge_groups {{
    background-color: {ACCENT};
    color: {DARK_BG};
    border-radius: 4px;
    padding: 2px 8px;
    font-weight: bold;
}}
"""


# ── Group Card ────────────────────────────────────────────────────────────────
class GroupCard(QFrame):
    """One card per duplicate group."""

    def __init__(self, group: DuplicateGroup, index: int, parent=None):
        super().__init__(parent)
        self.group = group
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(f"""
            GroupCard {{
                background-color: {PANEL_BG};
                border: 1px solid {BORDER};
                border-radius: 8px;
                margin: 4px 0;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        # ── Header ────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel(f"  群組 #{index + 1}  ·  {len(group.files)} 個重複  ·  "
                       f"每個 {human_size(group.size)}")
        title.setStyleSheet(f"color: {TEXT}; font-weight: bold;")

        wasted_lbl = QLabel(f"浪費 {human_size(group.wasted_bytes)}")
        wasted_lbl.setStyleSheet(
            f"background:{DANGER};color:{DARK_BG};border-radius:4px;"
            f"padding:2px 8px;font-weight:bold;"
        )

        hash_lbl = QLabel(f"Hash: {group.hash_value[:16]}…")
        hash_lbl.setStyleSheet(f"color:{SUBTEXT};font-size:11px;")

        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(hash_lbl)
        hdr.addWidget(wasted_lbl)
        root.addLayout(hdr)

        # ── Quick-select row ──────────────────────────────────────────
        qs = QHBoxLayout()
        qs.addWidget(QLabel("快速選取:"))
        for label, fn in [
            ("保留最新", self._keep_newest),
            ("保留最舊", self._keep_oldest),
            ("保留第一個", self._keep_first),
            ("全選", self._select_all),
            ("全不選", self._deselect_all),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(24)
            btn.setStyleSheet(
                f"padding:2px 10px;font-size:12px;"
                f"background:{SURFACE};border:1px solid {BORDER};"
                f"border-radius:4px;color:{TEXT};"
            )
            btn.clicked.connect(fn)
            qs.addWidget(btn)
        qs.addStretch()
        root.addLayout(qs)

        # ── File table ────────────────────────────────────────────────
        self.table = QTableWidget(len(group.files), 5)
        self.table.setHorizontalHeaderLabels(
            ["刪除", "檔案名稱", "路徑", "大小", "修改時間"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setFixedHeight(min(38 * len(group.files) + 30, 260))

        self.checkboxes: list[QCheckBox] = []
        import datetime
        for row, fi in enumerate(group.files):
            cb = QCheckBox()
            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.addWidget(cb)
            cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, cb_widget)
            self.checkboxes.append(cb)

            self.table.setItem(row, 1, QTableWidgetItem(fi.name))
            self.table.setItem(row, 2, QTableWidgetItem(fi.folder))
            self.table.setItem(row, 3, QTableWidgetItem(human_size(fi.size)))
            mtime = datetime.datetime.fromtimestamp(fi.mtime).strftime("%Y-%m-%d %H:%M")
            self.table.setItem(row, 4, QTableWidgetItem(mtime))
            self.table.setRowHeight(row, 34)

        # col 0 width
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 130)
        root.addWidget(self.table)

    # ── Selection helpers ─────────────────────────────────────────────
    def _keep_first(self):
        for i, cb in enumerate(self.checkboxes):
            cb.setChecked(i != 0)

    def _keep_newest(self):
        newest = max(range(len(self.group.files)),
                     key=lambda i: self.group.files[i].mtime)
        for i, cb in enumerate(self.checkboxes):
            cb.setChecked(i != newest)

    def _keep_oldest(self):
        oldest = min(range(len(self.group.files)),
                     key=lambda i: self.group.files[i].mtime)
        for i, cb in enumerate(self.checkboxes):
            cb.setChecked(i != oldest)

    def _select_all(self):
        for cb in self.checkboxes:
            cb.setChecked(True)

    def _deselect_all(self):
        for cb in self.checkboxes:
            cb.setChecked(False)

    def selected_paths(self) -> list[Path]:
        return [
            self.group.files[i].path
            for i, cb in enumerate(self.checkboxes)
            if cb.isChecked()
        ]


# ── Main Window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    _progress_signal = pyqtSignal(str, int, int)
    _done_signal     = pyqtSignal(list)
    _error_signal    = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DupeScan — 重複檔案掃描工具")
        self.resize(1100, 780)
        self.setMinimumSize(800, 600)

        self._groups: list[DuplicateGroup] = []
        self._cards:  list[GroupCard]      = []
        self._thread: QThread | None       = None
        self._worker: ScanWorker | None    = None

        self._progress_signal.connect(self._on_progress)
        self._done_signal.connect(self._on_done)
        self._error_signal.connect(self._on_error)

        self._build_ui()
        self.setStyleSheet(STYLE)

    # ── UI construction ───────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # ── Top bar ────────────────────────────────────────────────────
        top = QHBoxLayout()
        title_lbl = QLabel("DupeScan")
        title_lbl.setStyleSheet(
            f"font-size:22px;font-weight:bold;color:{ACCENT};letter-spacing:1px;"
        )
        top.addWidget(title_lbl)
        top.addStretch()
        main_layout.addLayout(top)

        # ── Scan config ────────────────────────────────────────────────
        cfg_box = QGroupBox("掃描設定")
        cfg_layout = QVBoxLayout(cfg_box)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("選擇要掃描的資料夾或磁碟機...")
        browse_btn = QPushButton("瀏覽")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(QLabel("路徑:"))
        path_row.addWidget(self.path_edit)
        path_row.addWidget(browse_btn)
        cfg_layout.addLayout(path_row)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("最小檔案大小:"))
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 1_000_000)
        self.min_size_spin.setValue(1)
        self.min_size_spin.setSuffix(" KB")
        self.min_size_spin.setFixedWidth(100)
        opt_row.addWidget(self.min_size_spin)
        opt_row.addSpacing(20)

        self.btn_scan = QPushButton("▶  開始掃描")
        self.btn_scan.setObjectName("btn_scan")
        self.btn_scan.setFixedHeight(32)
        self.btn_scan.clicked.connect(self._start_scan)

        self.btn_stop = QPushButton("■  停止")
        self.btn_stop.setFixedHeight(32)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_scan)

        opt_row.addStretch()
        opt_row.addWidget(self.btn_stop)
        opt_row.addWidget(self.btn_scan)
        cfg_layout.addLayout(opt_row)

        main_layout.addWidget(cfg_box)

        # ── Progress ────────────────────────────────────────────────────
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        main_layout.addWidget(self.progress_bar)
        main_layout.addWidget(self.progress_label)

        # ── Summary bar ─────────────────────────────────────────────────
        self.summary_widget = QWidget()
        sum_layout = QHBoxLayout(self.summary_widget)
        sum_layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_groups  = QLabel("0 個重複群組")
        self.lbl_wasted  = QLabel("浪費空間: 0 B")
        self.lbl_groups.setStyleSheet(
            f"background:{ACCENT};color:{DARK_BG};border-radius:4px;"
            f"padding:3px 10px;font-weight:bold;"
        )
        self.lbl_wasted.setStyleSheet(
            f"background:{DANGER};color:{DARK_BG};border-radius:4px;"
            f"padding:3px 10px;font-weight:bold;"
        )

        self.btn_delete = QPushButton("🗑  刪除已勾選")
        self.btn_delete.setObjectName("btn_delete")
        self.btn_delete.setFixedHeight(32)
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)

        sum_layout.addWidget(self.lbl_groups)
        sum_layout.addWidget(self.lbl_wasted)
        sum_layout.addStretch()
        sum_layout.addWidget(self.btn_delete)
        self.summary_widget.setVisible(False)
        main_layout.addWidget(self.summary_widget)

        # ── Results scroll area ─────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.results_widget = QWidget()
        self.results_layout = QVBoxLayout(self.results_widget)
        self.results_layout.setSpacing(6)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.addStretch()

        scroll.setWidget(self.results_widget)
        main_layout.addWidget(scroll, 1)

        # ── Status bar ──────────────────────────────────────────────────
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就緒")

    # ── Actions ───────────────────────────────────────────────────────
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "選擇資料夾", "")
        if path:
            self.path_edit.setText(path)

    def _start_scan(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "請先選擇要掃描的路徑。")
            return
        if not Path(path).exists():
            QMessageBox.warning(self, "路徑錯誤", f"路徑不存在：{path}")
            return

        # Clear previous results
        self._clear_results()
        self.summary_widget.setVisible(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)

        min_bytes = self.min_size_spin.value() * 1024

        self._worker = ScanWorker(roots=[path], min_size=min_bytes)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)

        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _stop_scan(self):
        if self._worker:
            self._worker.stop()
        self._reset_scan_ui()
        self.status_bar.showMessage("掃描已停止")

    def _reset_scan_ui(self):
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")

    # ── Slots ─────────────────────────────────────────────────────────
    def _on_progress(self, msg: str, cur: int, total: int):
        self.progress_label.setText(msg)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(cur)
        else:
            self.progress_bar.setRange(0, 0)

    def _on_done(self, groups: list):
        self._reset_scan_ui()
        self._groups = groups
        self._cards.clear()

        if not groups:
            self.status_bar.showMessage("掃描完成 — 未發現重複檔案！")
            no_dup = QLabel("✅  未發現重複檔案")
            no_dup.setStyleSheet(
                f"color:{ACCENT2};font-size:18px;font-weight:bold;"
                f"padding:40px;qproperty-alignment:AlignCenter;"
            )
            no_dup.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.results_layout.insertWidget(0, no_dup)
            return

        total_wasted = sum(g.wasted_bytes for g in groups)
        self.lbl_groups.setText(f"{len(groups)} 個重複群組")
        self.lbl_wasted.setText(f"浪費空間: {human_size(total_wasted)}")
        self.summary_widget.setVisible(True)
        self.btn_delete.setEnabled(True)

        for i, group in enumerate(groups):
            card = GroupCard(group, i)
            self._cards.append(card)
            self.results_layout.insertWidget(i, card)

        self.status_bar.showMessage(
            f"掃描完成 — 找到 {len(groups)} 個重複群組，浪費 {human_size(total_wasted)}"
        )

    def _on_error(self, msg: str):
        self._reset_scan_ui()
        QMessageBox.critical(self, "掃描錯誤", msg)
        self.status_bar.showMessage(f"錯誤: {msg}")

    def _clear_results(self):
        while self.results_layout.count() > 1:
            item = self.results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._groups.clear()

    def _delete_selected(self):
        to_delete: list[Path] = []
        for card in self._cards:
            to_delete.extend(card.selected_paths())

        if not to_delete:
            QMessageBox.information(self, "提示", "請先勾選要刪除的檔案。")
            return

        msg = f"確定要刪除以下 {len(to_delete)} 個檔案？\n\n"
        msg += "\n".join(str(p) for p in to_delete[:20])
        if len(to_delete) > 20:
            msg += f"\n… 另外 {len(to_delete) - 20} 個檔案"

        reply = QMessageBox.question(
            self, "確認刪除", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        success, fail = 0, 0
        for p in to_delete:
            try:
                p.unlink()
                success += 1
            except OSError as e:
                fail += 1

        QMessageBox.information(
            self, "刪除完成",
            f"成功刪除 {success} 個檔案" + (f"，失敗 {fail} 個" if fail else "")
        )
        self.status_bar.showMessage(f"已刪除 {success} 個檔案")
        # Refresh scan
        self._start_scan()
