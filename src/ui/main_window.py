"""DupeScan – Main Window (PyQt6, dark theme)"""

import os
import datetime
from pathlib import Path

try:
    import humanize
    _HZ = True
except ImportError:
    _HZ = False

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QSize, QEvent, QPoint
from PyQt6.QtGui import QColor, QFont, QIcon, QPalette, QAction, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QProgressBar, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QStatusBar, QTableWidget,
    QTableWidgetItem, QToolBar, QVBoxLayout, QWidget, QAbstractItemView,
)

from ..scanner import Scanner, STEP_NAMES, TOTAL_STEPS, _format_eta
from ..models import DuplicateGroup, FileInfo
from ..logger import logger

# 圖片副檔名，用於懸停預覽
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.ico'}


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
    # msg, current, total, step(1-3), total_steps(3), eta_secs
    progress = pyqtSignal(str, int, int, int, int, int)
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
            on_progress=lambda m, c, t, s, ts, eta: self.progress.emit(m, c, t, s, ts, eta),
            on_done=lambda g: self.finished.emit(g),
            on_error=lambda e: self.error.emit(e),
        )
        self._scanner.scan()

    def stop(self):
        if self._scanner:
            self._scanner.stop()

    def pause(self):
        if self._scanner:
            self._scanner.pause()

    def resume(self):
        if self._scanner:
            self._scanner.resume()


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
QComboBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT};
}}
QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    color: {TEXT};
    selection-background-color: {SURFACE};
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


# ── Image Preview Popup ───────────────────────────────────────────────────────
class ImagePreviewPopup(QLabel):
    """滑鼠懸停時顯示的圖片預覽浮動視窗。"""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"background:{PANEL_BG};border:2px solid {ACCENT};"
            f"border-radius:8px;padding:6px;"
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self._last_path: str = ""

    def show_image(self, path: str, global_pos: QPoint):
        if path == self._last_path and self.isVisible():
            self.move(global_pos)
            return
        self._last_path = path
        pix = QPixmap(path)
        if pix.isNull():
            self.hide()
            return
        pix = pix.scaled(
            220, 220,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(pix)
        self.resize(pix.width() + 16, pix.height() + 16)
        self.move(global_pos)
        self.show()

    def reset(self):
        self._last_path = ""
        self.hide()


# ── Group Card ────────────────────────────────────────────────────────────────
class GroupCard(QFrame):
    """One card per duplicate group."""

    def __init__(self, group: DuplicateGroup, index: int,
                 preview_popup: ImagePreviewPopup, parent=None):
        super().__init__(parent)
        self.group = group
        self._preview_popup = preview_popup
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

        # 滑鼠追蹤，用於圖片懸停預覽
        self.table.viewport().setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        # 點擊檔案名稱開啟檔案
        self.table.cellClicked.connect(self._on_cell_clicked)

        self.checkboxes: list[QCheckBox] = []
        for row, fi in enumerate(group.files):
            cb = QCheckBox()
            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.addWidget(cb)
            cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, cb_widget)
            self.checkboxes.append(cb)

            # 檔案名稱欄：藍色底線，看起來可點擊
            name_item = QTableWidgetItem(fi.name)
            name_item.setForeground(QColor(ACCENT))
            name_font = QFont()
            name_font.setUnderline(True)
            name_item.setFont(name_font)
            name_item.setToolTip(f"點擊開啟: {fi.path}")
            self.table.setItem(row, 1, name_item)

            self.table.setItem(row, 2, QTableWidgetItem(fi.folder))
            self.table.setItem(row, 3, QTableWidgetItem(human_size(fi.size)))
            mtime = datetime.datetime.fromtimestamp(fi.mtime).strftime("%Y-%m-%d %H:%M")
            self.table.setItem(row, 4, QTableWidgetItem(mtime))
            self.table.setRowHeight(row, 34)

        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 130)
        root.addWidget(self.table)

    # ── 點擊開啟檔案 ──────────────────────────────────────────────────
    def _on_cell_clicked(self, row: int, col: int):
        """點擊檔案名稱欄（col=1）時，用預設程式開啟該檔案。"""
        if col == 1 and 0 <= row < len(self.group.files):
            fi = self.group.files[row]
            try:
                os.startfile(str(fi.path))
                logger.info(f"開啟檔案: {fi.path}")
            except Exception as e:
                logger.warning(f"無法開啟檔案 {fi.path}: {e}")
                QMessageBox.warning(
                    self, "無法開啟", f"無法開啟檔案：\n{fi.path}\n\n{e}"
                )

    # ── 圖片懸停預覽 (event filter) ───────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is self.table.viewport():
            etype = event.type()
            if etype == QEvent.Type.MouseMove:
                pos = event.pos()
                row = self.table.rowAt(pos.y())
                col = self.table.columnAt(pos.x())
                if row >= 0 and col == 1 and row < len(self.group.files):
                    fi = self.group.files[row]
                    if fi.path.suffix.lower() in IMAGE_EXTENSIONS and fi.path.exists():
                        gpos = self.table.viewport().mapToGlobal(pos)
                        self._preview_popup.show_image(str(fi.path), gpos + QPoint(20, 10))
                    else:
                        self._preview_popup.reset()
                else:
                    self._preview_popup.reset()
            elif etype == QEvent.Type.Leave:
                self._preview_popup.reset()
        return super().eventFilter(obj, event)

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
    _progress_signal = pyqtSignal(str, int, int, int, int, int)
    _done_signal     = pyqtSignal(list)
    _error_signal    = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DupeScan — 重複檔案掃描工具")
        self.resize(1100, 780)
        self.setMinimumSize(800, 600)

        self._all_groups: list[DuplicateGroup] = []   # 完整掃描結果（未篩選）
        self._groups:     list[DuplicateGroup] = []   # 目前顯示中的（篩選後）
        self._cards:      list[GroupCard]      = []
        self._thread:     QThread | None       = None
        self._worker:     ScanWorker | None    = None
        self._scanning:   bool                 = False
        self._paused:     bool                 = False
        self._scan_id:    int                  = 0    # 防止舊 thread 的 signal 污染新掃描

        self._preview_popup = ImagePreviewPopup()

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
        self.btn_scan.clicked.connect(self._toggle_scan)

        self.btn_pause = QPushButton("⏸  暫停")
        self.btn_pause.setFixedHeight(32)
        self.btn_pause.setVisible(False)
        self.btn_pause.clicked.connect(self._toggle_pause)

        self.btn_reset = QPushButton("🔄  重置")
        self.btn_reset.setFixedHeight(32)
        self.btn_reset.setToolTip("停止掃描並清除所有結果，回到初始狀態")
        self.btn_reset.clicked.connect(self._reset_all)

        opt_row.addStretch()
        opt_row.addWidget(self.btn_reset)
        opt_row.addWidget(self.btn_pause)
        opt_row.addWidget(self.btn_scan)
        cfg_layout.addLayout(opt_row)

        main_layout.addWidget(cfg_box)

        # ── Progress ────────────────────────────────────────────────────
        self.progress_section = QWidget()
        prog_layout = QVBoxLayout(self.progress_section)
        prog_layout.setContentsMargins(0, 0, 0, 0)
        prog_layout.setSpacing(4)

        step_row = QHBoxLayout()
        self.step_label = QLabel("")
        self.step_label.setStyleSheet(
            f"color:{ACCENT};font-weight:bold;font-size:13px;"
        )
        self.eta_label = QLabel("")
        self.eta_label.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        step_row.addWidget(self.step_label)
        step_row.addStretch()
        step_row.addWidget(self.eta_label)
        prog_layout.addLayout(step_row)

        self.progress_bar = QProgressBar()
        prog_layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        prog_layout.addWidget(self.progress_label)

        self.progress_section.setVisible(False)
        main_layout.addWidget(self.progress_section)

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

        # ── Filter / Sort bar ────────────────────────────────────────────
        self.filter_sort_box = QGroupBox("篩選與排序")
        fs_layout = QHBoxLayout(self.filter_sort_box)
        fs_layout.setContentsMargins(10, 6, 10, 8)
        fs_layout.setSpacing(8)

        fs_layout.addWidget(QLabel("副檔名篩選:"))
        self.ext_filter_edit = QLineEdit()
        self.ext_filter_edit.setPlaceholderText("jpg,png… (留空顯示全部)")
        self.ext_filter_edit.setFixedWidth(190)
        self.ext_filter_edit.textChanged.connect(self._apply_filter_sort)
        fs_layout.addWidget(self.ext_filter_edit)

        fs_layout.addSpacing(16)
        fs_layout.addWidget(QLabel("排序依據:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "群組大小（大 → 小）",
            "群組大小（小 → 大）",
            "檔案數量（多 → 少）",
            "檔案數量（少 → 多）",
            "副檔名 A → Z",
            "副檔名 Z → A",
        ])
        self.sort_combo.setFixedWidth(190)
        self.sort_combo.currentIndexChanged.connect(self._apply_filter_sort)
        fs_layout.addWidget(self.sort_combo)

        fs_layout.addStretch()
        self.filter_sort_box.setVisible(False)
        main_layout.addWidget(self.filter_sort_box)

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

    # ── Filter / Sort / Rebuild ───────────────────────────────────────
    def _apply_filter_sort(self):
        """依目前的篩選與排序設定重建卡片列表。"""
        if not self._all_groups:
            return

        # 副檔名篩選
        ext_text = self.ext_filter_edit.text().strip().lower()
        if ext_text:
            exts = {e.strip().lstrip('.') for e in ext_text.split(',') if e.strip()}
            groups = [
                g for g in self._all_groups
                if any(fi.path.suffix.lstrip('.').lower() in exts for fi in g.files)
            ]
        else:
            groups = list(self._all_groups)

        # 排序
        idx = self.sort_combo.currentIndex()
        sort_opts = [
            (lambda g: g.size,                                               True),
            (lambda g: g.size,                                               False),
            (lambda g: len(g.files),                                         True),
            (lambda g: len(g.files),                                         False),
            (lambda g: g.files[0].path.suffix.lower() if g.files else '',   False),
            (lambda g: g.files[0].path.suffix.lower() if g.files else '',   True),
        ]
        key_fn, reverse = sort_opts[idx]
        groups.sort(key=key_fn, reverse=reverse)

        self._rebuild_cards(groups)

    def _rebuild_cards(self, groups: list[DuplicateGroup]):
        """清除舊卡片，依 groups 重建。"""
        # 清除預覽（避免殘留）
        self._preview_popup.reset()

        while self.results_layout.count() > 1:
            item = self.results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._groups = groups

        if not groups:
            no_match = QLabel("無符合條件的群組")
            no_match.setStyleSheet(
                f"color:{SUBTEXT};font-size:15px;padding:30px;"
                f"qproperty-alignment:AlignCenter;"
            )
            no_match.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.results_layout.insertWidget(0, no_match)
            return

        for i, group in enumerate(groups):
            card = GroupCard(group, i, self._preview_popup)
            self._cards.append(card)
            self.results_layout.insertWidget(i, card)

    # ── Actions ───────────────────────────────────────────────────────
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "選擇資料夾", "")
        if path:
            self.path_edit.setText(path)

    def _toggle_scan(self):
        """掃描按鈕：閒置時開始，掃描中時取消。"""
        if self._scanning:
            self._cancel_scan()
        else:
            self._start_scan()

    def _start_scan(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "請先選擇要掃描的路徑。")
            return
        if not Path(path).exists():
            QMessageBox.warning(self, "路徑錯誤", f"路徑不存在：{path}")
            return

        # 確保前一個 thread 完全結束
        self._kill_thread()

        self._clear_results()
        self.summary_widget.setVisible(False)
        self.filter_sort_box.setVisible(False)
        self.progress_bar.setRange(0, 0)
        self.progress_section.setVisible(True)
        self._set_scanning(True)
        self._paused = False

        # 每次掃描遞增 ID，讓 _on_thread_finished 能辨別舊 thread 的 stale signal
        self._scan_id += 1
        current_scan_id = self._scan_id

        min_bytes = self.min_size_spin.value() * 1024
        logger.info(f"掃描開始 — 路徑: {path}, 最小大小: {min_bytes} B")

        self._worker = ScanWorker(roots=[path], min_size=min_bytes)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)

        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        # 用 lambda 捕捉 current_scan_id，防止 race condition
        self._thread.finished.connect(
            lambda sid=current_scan_id: self._on_thread_finished(sid)
        )
        self._thread.start()

    def _cancel_scan(self):
        """送 stop 訊號；thread.finished 會恢復 UI。"""
        if self._worker:
            self._worker.stop()
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("取消中...")
        self.btn_pause.setVisible(False)
        self.status_bar.showMessage("正在取消掃描...")
        logger.info("掃描已取消（使用者操作）")

    def _toggle_pause(self):
        if not self._scanning or not self._worker:
            return
        if self._paused:
            self._worker.resume()
            self._paused = False
            self.btn_pause.setText("⏸  暫停")
            self.btn_pause.setStyleSheet("")
            self.step_label.setStyleSheet(f"color:{ACCENT};font-weight:bold;font-size:13px;")
            self.status_bar.showMessage("掃描繼續中...")
            logger.info("掃描已繼續")
        else:
            self._worker.pause()
            self._paused = True
            self.btn_pause.setText("▶  繼續")
            self.btn_pause.setStyleSheet(
                f"background:{ACCENT2};color:{DARK_BG};font-weight:bold;"
                f"border:none;border-radius:5px;padding:5px 14px;min-height:26px;"
            )
            self.step_label.setStyleSheet(
                f"color:{ACCENT2};font-weight:bold;font-size:13px;"
            )
            self.step_label.setText(self.step_label.text() + "  ⏸ 已暫停")
            self.status_bar.showMessage("掃描已暫停")
            logger.info("掃描已暫停")

    def _reset_all(self):
        """強制停止掃描，清除所有結果，回到乾淨的閒置狀態。"""
        self._kill_thread()
        self._clear_results()
        self.summary_widget.setVisible(False)
        self.filter_sort_box.setVisible(False)
        self.progress_section.setVisible(False)
        self.progress_label.setText("")
        self.step_label.setText("")
        self.eta_label.setText("")
        self._set_scanning(False)
        self._paused = False
        self.status_bar.showMessage("已重置")
        logger.info("已重置（使用者操作）")

    def _kill_thread(self):
        """同步停止 worker thread（最多等 5 秒）。"""
        if self._worker:
            self._worker.stop()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(5000)
        self._thread = None
        self._worker = None

    def _set_scanning(self, scanning: bool):
        self._scanning = scanning
        if scanning:
            self.btn_scan.setText("✕  取消掃描")
            self.btn_scan.setObjectName("btn_stop_inline")
            self.btn_scan.setStyleSheet(
                f"background-color:{DANGER};color:{DARK_BG};"
                f"font-weight:bold;border:none;"
                f"border-radius:5px;padding:5px 14px;min-height:26px;"
            )
            self.btn_pause.setVisible(True)
            self.btn_pause.setText("⏸  暫停")
            self.btn_pause.setStyleSheet("")
        else:
            self.btn_scan.setText("▶  開始掃描")
            self.btn_scan.setObjectName("btn_scan")
            self.btn_scan.setStyleSheet("")
            self.btn_pause.setVisible(False)
        self.btn_scan.setEnabled(True)

    # ── Slots ─────────────────────────────────────────────────────────
    def _on_thread_finished(self, scan_id: int):
        """QThread.finished 接收者。

        用 scan_id 判斷是否為當前掃描的 thread，
        避免舊 thread 的 stale signal 覆蓋新掃描的 _thread/_worker 參照。
        """
        if scan_id != self._scan_id:
            return   # 舊 thread 的殘留 signal，直接忽略

        self._thread = None
        self._worker = None
        self._paused = False
        if self._scanning:
            # Thread 結束但未收到 _on_done（如取消中途結束）
            self._set_scanning(False)
            self.progress_section.setVisible(False)
            self.progress_label.setText("")
            self.step_label.setText("")
            self.eta_label.setText("")
            self.status_bar.showMessage("掃描已取消")

    def _on_progress(self, msg: str, cur: int, total: int,
                     step: int, total_steps: int, eta_secs: int):
        if not self._scanning:
            return

        step_name = STEP_NAMES.get(step, f"步驟 {step}")
        self.step_label.setText(f"步驟 {step}/{total_steps} — {step_name}")

        if eta_secs == 0:
            self.eta_label.setText("完成")
        elif eta_secs > 0:
            self.eta_label.setText(f"預估剩餘: {_format_eta(eta_secs)}")
        else:
            self.eta_label.setText("預估剩餘: 計算中...")

        self.progress_label.setText(msg)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(cur)
        else:
            self.progress_bar.setRange(0, 0)

    def _on_done(self, groups: list):
        if not self._scanning:
            return
        self._set_scanning(False)
        self._paused = False
        self.progress_section.setVisible(False)
        self.progress_label.setText("")
        self.step_label.setText("")
        self.eta_label.setText("")

        self._all_groups = groups

        if not groups:
            logger.info("掃描完成 — 未發現重複檔案")
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
        logger.info(
            f"掃描完成 — {len(groups)} 個重複群組，"
            f"共浪費 {human_size(total_wasted)}（{total_wasted} B）"
        )
        for g in groups:
            logger.debug(
                f"  群組 hash={g.hash_value[:16]}  size={human_size(g.size)}  "
                f"files={len(g.files)}  wasted={human_size(g.wasted_bytes)}"
            )

        self.lbl_groups.setText(f"{len(groups)} 個重複群組")
        self.lbl_wasted.setText(f"浪費空間: {human_size(total_wasted)}")
        self.summary_widget.setVisible(True)
        self.btn_delete.setEnabled(True)

        # 顯示篩選/排序列，並套用（重建卡片）
        self.filter_sort_box.setVisible(True)
        self._apply_filter_sort()

        self.status_bar.showMessage(
            f"掃描完成 — 找到 {len(groups)} 個重複群組，浪費 {human_size(total_wasted)}"
        )

    def _on_error(self, msg: str):
        if not self._scanning:
            return
        self._set_scanning(False)
        self._paused = False
        self.progress_section.setVisible(False)
        self.progress_label.setText("")
        logger.error(f"掃描錯誤: {msg}")
        QMessageBox.critical(self, "掃描錯誤", msg)
        self.status_bar.showMessage(f"錯誤: {msg}")

    def _clear_results(self):
        self._preview_popup.reset()
        while self.results_layout.count() > 1:
            item = self.results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._groups.clear()
        self._all_groups.clear()

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

        logger.info(f"開始刪除 {len(to_delete)} 個檔案")
        success, fail = 0, 0
        for p in to_delete:
            try:
                p.unlink()
                logger.info(f"  刪除: {p}")
                success += 1
            except OSError as e:
                logger.warning(f"  刪除失敗: {p}  原因: {e}")
                fail += 1

        logger.info(f"刪除完成 — 成功 {success} 個，失敗 {fail} 個")
        QMessageBox.information(
            self, "刪除完成",
            f"成功刪除 {success} 個檔案" + (f"，失敗 {fail} 個" if fail else "")
        )
        self.status_bar.showMessage(f"已刪除 {success} 個檔案")
        if not self._scanning:
            self._start_scan()
