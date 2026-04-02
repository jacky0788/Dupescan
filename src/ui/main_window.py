"""DupeScan – Main Window (PyQt6, dark theme)"""

import os
import subprocess
import datetime
import math
import tempfile
import webbrowser
from collections import defaultdict
from pathlib import Path

try:
    import humanize
    _HZ = True
except ImportError:
    _HZ = False

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QSize, QEvent, QPoint
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QProgressBar, QScrollArea,
    QSizePolicy, QSpinBox, QStatusBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
)

from ..scanner import Scanner, STEP_NAMES, TOTAL_STEPS, _format_eta
from ..models import DuplicateGroup, FileInfo
from ..logger import logger

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.ico'}


def human_size(n: int) -> str:
    if _HZ:
        return humanize.naturalsize(n, binary=True)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _path_depth(p: Path) -> int:
    return len(p.parts)


# ── Worker ────────────────────────────────────────────────────────────────────
class ScanWorker(QObject):
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
        if self._scanner: self._scanner.stop()

    def pause(self):
        if self._scanner: self._scanner.pause()

    def resume(self):
        if self._scanner: self._scanner.resume()


# ── Palette / Style ───────────────────────────────────────────────────────────
DARK_BG  = "#1e1e2e"
PANEL_BG = "#181825"
SURFACE  = "#313244"
ACCENT   = "#89b4fa"   # blue
ACCENT2  = "#a6e3a1"   # green
ACCENT3  = "#fab387"   # peach
DANGER   = "#f38ba8"   # red
TEXT     = "#cdd6f4"
SUBTEXT  = "#a6adc8"
BORDER   = "#45475a"

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
    padding-top: 6px;
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
QPushButton#btn_scan:hover {{ background-color: #74a9f5; }}
QPushButton#btn_delete {{
    background-color: {DANGER};
    color: {DARK_BG};
    font-weight: bold;
    border: none;
}}
QPushButton#btn_delete:hover {{ background-color: #f06090; }}
QPushButton#btn_delete:disabled {{ background-color: {SURFACE}; color: {SUBTEXT}; }}
QPushButton#sec_header {{
    background-color: {SURFACE};
    color: {ACCENT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 12px;
    font-weight: bold;
    text-align: left;
    min-height: 28px;
}}
QPushButton#sec_header:hover {{
    background-color: #414459;
    border-color: {ACCENT};
}}
QLineEdit, QSpinBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT};
}}
QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
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
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 3px; }}
QTableWidget {{
    background-color: {PANEL_BG};
    alternate-background-color: {DARK_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    gridline-color: {BORDER};
}}
QTableWidget::item {{ padding: 4px 8px; }}
QTableWidget::item:selected {{ background-color: {SURFACE}; color: {TEXT}; }}
QHeaderView::section {{
    background-color: {SURFACE};
    color: {SUBTEXT};
    border: none;
    border-right: 1px solid {BORDER};
    padding: 5px 8px;
    font-weight: bold;
}}
QSplitter::handle {{
    background-color: {BORDER};
    height: 4px;
    border-radius: 2px;
}}
QSplitter::handle:hover {{ background-color: {ACCENT}; }}
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
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QStatusBar {{
    background-color: {PANEL_BG};
    border-top: 1px solid {BORDER};
    color: {SUBTEXT};
}}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {PANEL_BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
"""

_BTN_KEEP_ON  = f"background:{ACCENT};color:{DARK_BG};font-weight:bold;border:none;border-radius:4px;padding:2px 10px;font-size:11px;"
_BTN_KEEP_OFF = f"background:{SURFACE};color:{TEXT};border:1px solid {BORDER};border-radius:4px;padding:2px 10px;font-size:11px;"
_BTN_DEL_ON   = f"background:{DANGER};color:{DARK_BG};font-weight:bold;border:none;border-radius:4px;padding:2px 10px;font-size:11px;"
_BTN_DEL_OFF  = f"background:{SURFACE};color:{TEXT};border:1px solid {BORDER};border-radius:4px;padding:2px 10px;font-size:11px;"
_BTN_STRAT    = f"padding:2px 8px;font-size:11px;background:{SURFACE};border:1px solid {BORDER};border-radius:4px;color:{TEXT};"


# ── CollapsibleSection ────────────────────────────────────────────────────────
class CollapsibleSection(QFrame):
    """折疊式 UI 區塊，點擊標題展開/收起內容。"""

    def __init__(self, title: str, initially_collapsed: bool = True, parent=None):
        super().__init__(parent)
        self._title = title
        self._collapsed = initially_collapsed
        self.setFrameShape(QFrame.Shape.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self._header = QPushButton(self._label())
        self._header.setObjectName("sec_header")
        self._header.clicked.connect(self._toggle)
        outer.addWidget(self._header)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 4, 0, 0)
        self._body_layout.setSpacing(4)
        self._body.setVisible(not initially_collapsed)
        outer.addWidget(self._body)

    def _label(self) -> str:
        return f"  {'▶' if self._collapsed else '▼'}  {self._title}"

    def _toggle(self):
        self._collapsed = not self._collapsed
        self._body.setVisible(not self._collapsed)
        self._header.setText(self._label())

    def add_widget(self, w: QWidget):
        self._body_layout.addWidget(w)

    def add_layout(self, lay):
        self._body_layout.addLayout(lay)

    def expand(self):
        if self._collapsed:
            self._toggle()

    def collapse(self):
        if not self._collapsed:
            self._toggle()


# ── Image Preview Popup ───────────────────────────────────────────────────────
class ImagePreviewPopup(QLabel):
    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"background:{PANEL_BG};border:2px solid {ACCENT};border-radius:8px;padding:6px;"
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self._last_path: str = ""

    def show_image(self, path: str, global_pos: QPoint):
        if path == self._last_path and self.isVisible():
            self.move(global_pos); return
        self._last_path = path
        pix = QPixmap(path)
        if pix.isNull():
            self.hide(); return
        pix = pix.scaled(220, 220,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
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
                 preview_popup: ImagePreviewPopup,
                 font_size: int = 13, parent=None):
        super().__init__(parent)
        self.group = group
        self._preview_popup = preview_popup
        self._font_size = font_size
        self._qs_mode = "keep"       # "keep" or "delete"
        self._qs_open = False        # quick-select panel open?

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
        root.setSpacing(5)

        # ── Header ────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title_lbl = QLabel(
            f"  群組 #{index + 1}  ·  {len(group.files)} 個重複  ·  "
            f"每個 {human_size(group.size)}"
        )
        title_lbl.setStyleSheet(f"color:{TEXT};font-weight:bold;")
        wasted_lbl = QLabel(f"浪費 {human_size(group.wasted_bytes)}")
        wasted_lbl.setStyleSheet(
            f"background:{DANGER};color:{DARK_BG};border-radius:4px;padding:2px 8px;font-weight:bold;"
        )
        hash_lbl = QLabel(f"Hash: {group.hash_value[:16]}…")
        hash_lbl.setStyleSheet(f"color:{SUBTEXT};font-size:11px;")
        hdr.addWidget(title_lbl)
        hdr.addStretch()
        hdr.addWidget(hash_lbl)
        hdr.addWidget(wasted_lbl)
        root.addLayout(hdr)

        # ── Quick-select toggle button ────────────────────────────────
        self._qs_toggle_btn = QPushButton("▶  快速選取")
        self._qs_toggle_btn.setFixedHeight(22)
        self._qs_toggle_btn.setStyleSheet(
            f"text-align:left;padding:2px 8px;font-size:11px;color:{SUBTEXT};"
            f"background:transparent;border:none;"
        )
        self._qs_toggle_btn.clicked.connect(self._toggle_qs)
        root.addWidget(self._qs_toggle_btn)

        # ── Quick-select panel (hidden by default) ────────────────────
        self._qs_panel = QWidget()
        qs_layout = QVBoxLayout(self._qs_panel)
        qs_layout.setContentsMargins(0, 2, 0, 2)
        qs_layout.setSpacing(4)

        # Mode toggle row
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        lbl_mode = QLabel("操作模式:")
        lbl_mode.setStyleSheet(f"color:{SUBTEXT};font-size:11px;")
        mode_row.addWidget(lbl_mode)
        mode_row.addSpacing(6)
        self._btn_mode_keep = QPushButton("保留目標")
        self._btn_mode_keep.setFixedHeight(22)
        self._btn_mode_keep.clicked.connect(lambda: self._set_mode("keep"))
        self._btn_mode_delete = QPushButton("刪除目標")
        self._btn_mode_delete.setFixedHeight(22)
        self._btn_mode_delete.clicked.connect(lambda: self._set_mode("delete"))
        mode_row.addWidget(self._btn_mode_keep)
        mode_row.addWidget(self._btn_mode_delete)
        mode_row.addSpacing(16)

        # Strategy buttons
        strat_lbl = QLabel("策略:")
        strat_lbl.setStyleSheet(f"color:{SUBTEXT};font-size:11px;")
        mode_row.addWidget(strat_lbl)
        for label, fn in [
            ("修改最新", self._newest),
            ("修改最舊", self._oldest),
            ("路徑最短", self._shortest_path),
            ("路徑最長", self._longest_path),
            ("目錄最淺", self._shallowest),
            ("目錄最深", self._deepest),
            ("字母最前", self._alpha_first),
            ("字母最後", self._alpha_last),
            ("列表第一", self._first),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setStyleSheet(_BTN_STRAT)
            b.clicked.connect(fn)
            mode_row.addWidget(b)

        mode_row.addSpacing(12)
        sep = QLabel("|")
        sep.setStyleSheet(f"color:{BORDER};")
        mode_row.addWidget(sep)
        mode_row.addSpacing(4)

        for label, fn in [("全選", self._select_all), ("全不選", self._deselect_all)]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setStyleSheet(_BTN_STRAT)
            b.clicked.connect(fn)
            mode_row.addWidget(b)

        mode_row.addStretch()
        qs_layout.addLayout(mode_row)

        self._qs_panel.setVisible(False)
        root.addWidget(self._qs_panel)
        self._update_mode_btns()

        # ── File table ────────────────────────────────────────────────
        self.table = QTableWidget(len(group.files), 5)
        self.table.setHorizontalHeaderLabels(
            ["刪除", "檔案名稱", "路徑（點擊開啟資料夾）", "大小", "修改時間"]
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        _row_h = max(24, int(34 * font_size / 13))
        self.table.setFixedHeight(min(_row_h * len(group.files) + 30, int(260 * font_size / 13)))
        self.table.viewport().setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        self.table.cellClicked.connect(self._on_cell_clicked)

        self.checkboxes: list[QCheckBox] = []
        for row, fi in enumerate(group.files):
            cb = QCheckBox()
            cb_w = QWidget()
            cb_l = QHBoxLayout(cb_w)
            cb_l.addWidget(cb)
            cb_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb_l.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, cb_w)
            self.checkboxes.append(cb)

            name_item = QTableWidgetItem(fi.name)
            name_item.setForeground(QColor(ACCENT))
            nf = QFont(); nf.setUnderline(True)
            name_item.setFont(nf)
            name_item.setToolTip(f"點擊以預設程式開啟: {fi.path}")
            self.table.setItem(row, 1, name_item)

            path_item = QTableWidgetItem(fi.folder)
            path_item.setForeground(QColor(ACCENT3))
            pf = QFont(); pf.setUnderline(True)
            path_item.setFont(pf)
            path_item.setToolTip(f"點擊在檔案總管中定位: {fi.path}")
            self.table.setItem(row, 2, path_item)

            self.table.setItem(row, 3, QTableWidgetItem(human_size(fi.size)))
            mtime = datetime.datetime.fromtimestamp(fi.mtime).strftime("%Y-%m-%d %H:%M")
            self.table.setItem(row, 4, QTableWidgetItem(mtime))
            self.table.setRowHeight(row, _row_h)

        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 130)
        root.addWidget(self.table)

    # ── Quick-select panel toggle ─────────────────────────────────────
    def _toggle_qs(self):
        self._qs_open = not self._qs_open
        self._qs_panel.setVisible(self._qs_open)
        self._qs_toggle_btn.setText(
            "▼  快速選取" if self._qs_open else "▶  快速選取"
        )

    # ── Mode management ───────────────────────────────────────────────
    def _set_mode(self, mode: str):
        self._qs_mode = mode
        self._update_mode_btns()

    def _update_mode_btns(self):
        if self._qs_mode == "keep":
            self._btn_mode_keep.setStyleSheet(_BTN_KEEP_ON)
            self._btn_mode_delete.setStyleSheet(_BTN_DEL_OFF)
        else:
            self._btn_mode_keep.setStyleSheet(_BTN_KEEP_OFF)
            self._btn_mode_delete.setStyleSheet(_BTN_DEL_ON)

    # ── Strategy dispatch ─────────────────────────────────────────────
    def _apply(self, idx: int):
        """套用目前模式：保留→其他打勾；刪除→只打勾 idx。"""
        if self._qs_mode == "keep":
            for i, cb in enumerate(self.checkboxes):
                cb.setChecked(i != idx)
        else:
            for i, cb in enumerate(self.checkboxes):
                cb.setChecked(i == idx)

    def _newest(self):
        self._apply(max(range(len(self.group.files)), key=lambda i: self.group.files[i].mtime))

    def _oldest(self):
        self._apply(min(range(len(self.group.files)), key=lambda i: self.group.files[i].mtime))

    def _shortest_path(self):
        self._apply(min(range(len(self.group.files)), key=lambda i: len(str(self.group.files[i].path))))

    def _longest_path(self):
        self._apply(max(range(len(self.group.files)), key=lambda i: len(str(self.group.files[i].path))))

    def _shallowest(self):
        self._apply(min(range(len(self.group.files)), key=lambda i: _path_depth(self.group.files[i].path)))

    def _deepest(self):
        self._apply(max(range(len(self.group.files)), key=lambda i: _path_depth(self.group.files[i].path)))

    def _alpha_first(self):
        self._apply(min(range(len(self.group.files)), key=lambda i: str(self.group.files[i].path).lower()))

    def _alpha_last(self):
        self._apply(max(range(len(self.group.files)), key=lambda i: str(self.group.files[i].path).lower()))

    def _first(self):
        self._apply(0)

    def _select_all(self):
        for cb in self.checkboxes: cb.setChecked(True)

    def _deselect_all(self):
        for cb in self.checkboxes: cb.setChecked(False)

    # ── Cell click ────────────────────────────────────────────────────
    def _on_cell_clicked(self, row: int, col: int):
        if not (0 <= row < len(self.group.files)):
            return
        fi = self.group.files[row]
        if col == 1:
            try:
                os.startfile(str(fi.path))
                logger.info(f"開啟檔案: {fi.path}")
            except Exception as e:
                logger.warning(f"無法開啟 {fi.path}: {e}")
                QMessageBox.warning(self, "無法開啟", f"無法開啟檔案：\n{fi.path}\n\n{e}")
        elif col == 2:
            try:
                subprocess.Popen(f'explorer /select,"{fi.path}"', shell=True)
                logger.info(f"在檔案總管定位: {fi.path}")
            except Exception as e:
                logger.warning(f"無法定位 {fi.path}: {e}")
                QMessageBox.warning(self, "無法開啟", f"無法開啟資料夾：\n{fi.folder}\n\n{e}")

    # ── Hover preview ─────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is self.table.viewport():
            et = event.type()
            if et == QEvent.Type.MouseMove:
                pos = event.pos()
                row = self.table.rowAt(pos.y())
                col = self.table.columnAt(pos.x())
                if row >= 0 and col == 1 and row < len(self.group.files):
                    fi = self.group.files[row]
                    if fi.path.suffix.lower() in IMAGE_EXTENSIONS and fi.path.exists():
                        gp = self.table.viewport().mapToGlobal(pos)
                        self._preview_popup.show_image(str(fi.path), gp + QPoint(20, 10))
                    else:
                        self._preview_popup.reset()
                else:
                    self._preview_popup.reset()
            elif et == QEvent.Type.Leave:
                self._preview_popup.reset()
        return super().eventFilter(obj, event)

    def selected_paths(self) -> list[Path]:
        return [self.group.files[i].path for i, cb in enumerate(self.checkboxes) if cb.isChecked()]


# ── Main Window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    _progress_signal = pyqtSignal(str, int, int, int, int, int)
    _done_signal     = pyqtSignal(list)
    _error_signal    = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DupeScan — 重複檔案掃描工具")
        self.resize(1100, 780)
        self.setMinimumSize(800, 580)

        self._all_groups: list[DuplicateGroup] = []
        self._groups:     list[DuplicateGroup] = []
        self._cards:      list[GroupCard]      = []
        self._thread:     QThread | None       = None
        self._worker:     ScanWorker | None    = None
        self._scanning:   bool                 = False
        self._paused:     bool                 = False
        self._scan_id:    int                  = 0
        self._global_qs_mode: str              = "keep"
        self._font_size:  int                  = 13

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
        main = QVBoxLayout(central)
        main.setContentsMargins(14, 14, 14, 14)
        main.setSpacing(8)

        # ── Title ──────────────────────────────────────────────────────
        title_lbl = QLabel("DupeScan")
        title_lbl.setStyleSheet(
            f"font-size:22px;font-weight:bold;color:{ACCENT};letter-spacing:1px;"
        )
        main.addWidget(title_lbl)

        # ── Scan config (single compact row) ───────────────────────────
        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(6)

        cfg_row.addWidget(QLabel("路徑:"))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("選擇要掃描的資料夾或磁碟機…")
        self.path_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        cfg_row.addWidget(self.path_edit, 1)

        browse_btn = QPushButton("瀏覽")
        browse_btn.setFixedWidth(60)
        browse_btn.clicked.connect(self._browse)
        cfg_row.addWidget(browse_btn)

        cfg_row.addSpacing(10)
        cfg_row.addWidget(QLabel("最小:"))
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 1_000_000)
        self.min_size_spin.setValue(1)
        self.min_size_spin.setSuffix(" KB")
        self.min_size_spin.setFixedWidth(90)
        cfg_row.addWidget(self.min_size_spin)

        cfg_row.addSpacing(10)

        self.btn_reset = QPushButton("🔄")
        self.btn_reset.setFixedWidth(36)
        self.btn_reset.setFixedHeight(30)
        self.btn_reset.setToolTip("重置：停止掃描並清除所有結果")
        self.btn_reset.clicked.connect(self._reset_all)
        cfg_row.addWidget(self.btn_reset)

        self.btn_pause = QPushButton("⏸")
        self.btn_pause.setFixedWidth(36)
        self.btn_pause.setFixedHeight(30)
        self.btn_pause.setVisible(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        cfg_row.addWidget(self.btn_pause)

        self.btn_scan = QPushButton("▶  開始掃描")
        self.btn_scan.setObjectName("btn_scan")
        self.btn_scan.setFixedHeight(30)
        self.btn_scan.clicked.connect(self._toggle_scan)
        cfg_row.addWidget(self.btn_scan)

        main.addLayout(cfg_row)

        # ── Progress ────────────────────────────────────────────────────
        self.progress_section = QWidget()
        prog_l = QVBoxLayout(self.progress_section)
        prog_l.setContentsMargins(0, 0, 0, 0)
        prog_l.setSpacing(3)

        step_row = QHBoxLayout()
        self.step_label = QLabel("")
        self.step_label.setStyleSheet(f"color:{ACCENT};font-weight:bold;font-size:13px;")
        self.eta_label = QLabel("")
        self.eta_label.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        step_row.addWidget(self.step_label)
        step_row.addStretch()
        step_row.addWidget(self.eta_label)
        prog_l.addLayout(step_row)
        self.progress_bar = QProgressBar()
        prog_l.addWidget(self.progress_bar)
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        prog_l.addWidget(self.progress_label)
        self.progress_section.setVisible(False)
        main.addWidget(self.progress_section)

        # ── Summary bar ─────────────────────────────────────────────────
        self.summary_widget = QWidget()
        sum_l = QHBoxLayout(self.summary_widget)
        sum_l.setContentsMargins(0, 0, 0, 0)
        self.lbl_groups = QLabel("0 個重複群組")
        self.lbl_wasted = QLabel("浪費空間: 0 B")
        for lbl, bg in [(self.lbl_groups, ACCENT), (self.lbl_wasted, DANGER)]:
            lbl.setStyleSheet(
                f"background:{bg};color:{DARK_BG};border-radius:4px;"
                f"padding:3px 10px;font-weight:bold;"
            )
        self.btn_delete = QPushButton("🗑  刪除已勾選")
        self.btn_delete.setObjectName("btn_delete")
        self.btn_delete.setFixedHeight(30)
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_export = QPushButton("📊  匯出報表")
        self.btn_export.setFixedHeight(30)
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip("分析重複檔案分布並匯出 HTML 報表（含圓餅圖）")
        self.btn_export.clicked.connect(self._export_report)
        sum_l.addWidget(self.lbl_groups)
        sum_l.addWidget(self.lbl_wasted)
        sum_l.addStretch()
        sum_l.addWidget(self.btn_export)
        sum_l.addSpacing(6)
        sum_l.addWidget(self.btn_delete)
        self.summary_widget.setVisible(False)
        main.addWidget(self.summary_widget)

        # ── 控制面板 + 結果區 ────────────────────────────────────────────
        # 上方：篩選/排序 + 批量選取（CollapsibleSection，高度自適應）
        mid_widget = QWidget()
        mid_layout = QVBoxLayout(mid_widget)
        mid_layout.setContentsMargins(0, 0, 0, 0)
        mid_layout.setSpacing(4)

        # Filter/Sort section
        self.filter_sort_sec = CollapsibleSection("篩選與排序", initially_collapsed=True)
        fs_w = QWidget()
        fs_l = QHBoxLayout(fs_w)
        fs_l.setContentsMargins(4, 2, 4, 2)
        fs_l.setSpacing(8)
        fs_l.addWidget(QLabel("副檔名篩選:"))
        self.ext_filter_edit = QLineEdit()
        self.ext_filter_edit.setPlaceholderText("jpg,png… (留空顯示全部)")
        self.ext_filter_edit.setFixedWidth(180)
        self.ext_filter_edit.textChanged.connect(self._apply_filter_sort)
        fs_l.addWidget(self.ext_filter_edit)
        fs_l.addSpacing(16)
        fs_l.addWidget(QLabel("排序:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "群組大小（大→小）", "群組大小（小→大）",
            "檔案數量（多→少）", "檔案數量（少→多）",
            "副檔名 A→Z",       "副檔名 Z→A",
        ])
        self.sort_combo.setFixedWidth(180)
        self.sort_combo.currentIndexChanged.connect(self._apply_filter_sort)
        fs_l.addWidget(self.sort_combo)
        fs_l.addSpacing(16)
        fs_l.addWidget(QLabel("文字大小:"))
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(9, 18)
        self.font_size_spin.setValue(13)
        self.font_size_spin.setSuffix(" px")
        self.font_size_spin.setFixedWidth(72)
        self.font_size_spin.setToolTip("調整文字大小（較小時每頁可顯示更多群組）")
        self.font_size_spin.valueChanged.connect(self._apply_font_size)
        fs_l.addWidget(self.font_size_spin)
        fs_l.addStretch()
        self.filter_sort_sec.add_widget(fs_w)

        # Global quick-select section
        self.global_select_sec = CollapsibleSection("批量快速選取", initially_collapsed=True)
        gs_w = QWidget()
        gs_l = QVBoxLayout(gs_w)
        gs_l.setContentsMargins(4, 2, 4, 2)
        gs_l.setSpacing(4)

        # Global mode row
        gm_row = QHBoxLayout()
        gm_row.setSpacing(4)
        gm_lbl = QLabel("操作模式:")
        gm_lbl.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        gm_row.addWidget(gm_lbl)
        self._g_btn_keep = QPushButton("保留目標")
        self._g_btn_keep.setFixedHeight(26)
        self._g_btn_keep.clicked.connect(lambda: self._set_global_mode("keep"))
        self._g_btn_delete = QPushButton("刪除目標")
        self._g_btn_delete.setFixedHeight(26)
        self._g_btn_delete.clicked.connect(lambda: self._set_global_mode("delete"))
        gm_row.addWidget(self._g_btn_keep)
        gm_row.addWidget(self._g_btn_delete)
        gm_row.addSpacing(16)

        strat_lbl = QLabel("策略:")
        strat_lbl.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        gm_row.addWidget(strat_lbl)
        for label, method in [
            ("修改最新", "_newest"),  ("修改最舊", "_oldest"),
            ("路徑最短", "_shortest_path"), ("路徑最長", "_longest_path"),
            ("目錄最淺", "_shallowest"),    ("目錄最深", "_deepest"),
            ("字母最前", "_alpha_first"),   ("字母最後", "_alpha_last"),
            ("列表第一", "_first"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(26)
            b.setStyleSheet(f"padding:3px 8px;font-size:12px;background:{SURFACE};"
                            f"border:1px solid {BORDER};border-radius:4px;color:{TEXT};")
            b.clicked.connect(lambda _=False, m=method: self._global_apply(m))
            gm_row.addWidget(b)
        gm_row.addSpacing(10)
        sep2 = QLabel("|"); sep2.setStyleSheet(f"color:{BORDER};")
        gm_row.addWidget(sep2)
        for label, fn in [("全部勾選", self._global_select_all), ("全部取消", self._global_deselect_all)]:
            b = QPushButton(label)
            b.setFixedHeight(26)
            b.setStyleSheet(f"padding:3px 10px;font-size:12px;background:{SURFACE};"
                            f"border:1px solid {BORDER};border-radius:4px;color:{TEXT};")
            b.clicked.connect(fn)
            gm_row.addWidget(b)
        gm_row.addStretch()
        gs_l.addLayout(gm_row)
        self.global_select_sec.add_widget(gs_w)
        self._update_global_mode_btns()

        mid_layout.addWidget(self.filter_sort_sec)
        mid_layout.addWidget(self.global_select_sec)

        # 下方：結果捲動區（固定位置，佔滿剩餘空間）
        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_widget = QWidget()
        self.results_layout = QVBoxLayout(self.results_widget)
        self.results_layout.setSpacing(6)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.addStretch()
        self._results_scroll.setWidget(self.results_widget)

        main.addWidget(mid_widget, 0)       # 控制面板不拉伸
        main.addWidget(self._results_scroll, 1)  # 結果區佔滿剩餘空間

        # ── Status bar ──────────────────────────────────────────────────
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就緒")

    # ── Global quick-select mode ──────────────────────────────────────
    def _set_global_mode(self, mode: str):
        self._global_qs_mode = mode
        self._update_global_mode_btns()

    def _update_global_mode_btns(self):
        if self._global_qs_mode == "keep":
            self._g_btn_keep.setStyleSheet(_BTN_KEEP_ON)
            self._g_btn_delete.setStyleSheet(_BTN_DEL_OFF)
        else:
            self._g_btn_keep.setStyleSheet(_BTN_KEEP_OFF)
            self._g_btn_delete.setStyleSheet(_BTN_DEL_ON)

    def _global_apply(self, method: str):
        """以 _global_qs_mode 對所有卡片套用 method 策略。"""
        mode = self._global_qs_mode
        for card in self._cards:
            old = card._qs_mode
            card._qs_mode = mode
            getattr(card, method)()
            card._qs_mode = old

    def _global_select_all(self):
        for card in self._cards: card._select_all()

    def _global_deselect_all(self):
        for card in self._cards: card._deselect_all()

    # ── Filter / Sort / Rebuild ───────────────────────────────────────
    def _apply_filter_sort(self):
        if not self._all_groups:
            return
        ext_text = self.ext_filter_edit.text().strip().lower()
        if ext_text:
            exts = {e.strip().lstrip('.') for e in ext_text.split(',') if e.strip()}
            groups = [g for g in self._all_groups
                      if any(fi.path.suffix.lstrip('.').lower() in exts for fi in g.files)]
        else:
            groups = list(self._all_groups)

        idx = self.sort_combo.currentIndex()
        sort_opts = [
            (lambda g: g.size, True),
            (lambda g: g.size, False),
            (lambda g: len(g.files), True),
            (lambda g: len(g.files), False),
            (lambda g: g.files[0].path.suffix.lower() if g.files else '', False),
            (lambda g: g.files[0].path.suffix.lower() if g.files else '', True),
        ]
        key_fn, rev = sort_opts[idx]
        groups.sort(key=key_fn, reverse=rev)
        self._rebuild_cards(groups)

    def _rebuild_cards(self, groups: list[DuplicateGroup]):
        self._preview_popup.reset()
        while self.results_layout.count() > 1:
            item = self.results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._groups = groups

        if not groups:
            no_match = QLabel("無符合條件的群組")
            no_match.setStyleSheet(f"color:{SUBTEXT};font-size:15px;padding:30px;")
            no_match.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.results_layout.insertWidget(0, no_match)
            return

        for i, group in enumerate(groups):
            card = GroupCard(group, i, self._preview_popup, self._font_size)
            self._cards.append(card)
            self.results_layout.insertWidget(i, card)

    # ── Actions ───────────────────────────────────────────────────────
    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "選擇資料夾", "")
        if path:
            self.path_edit.setText(path)

    def _toggle_scan(self):
        if self._scanning: self._cancel_scan()
        else: self._start_scan()

    def _start_scan(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "請先選擇要掃描的路徑。"); return
        if not Path(path).exists():
            QMessageBox.warning(self, "路徑錯誤", f"路徑不存在：{path}"); return

        self._kill_thread()
        self._clear_results()
        self.summary_widget.setVisible(False)
        self.filter_sort_sec.setVisible(False)
        self.global_select_sec.setVisible(False)
        self.progress_bar.setRange(0, 0)
        self.progress_section.setVisible(True)
        self._set_scanning(True)
        self._paused = False

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
        self._thread.finished.connect(
            lambda sid=current_scan_id: self._on_thread_finished(sid)
        )
        self._thread.start()

    def _cancel_scan(self):
        if self._worker: self._worker.stop()
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("取消中…")
        self.btn_pause.setVisible(False)
        self.status_bar.showMessage("正在取消掃描…")
        logger.info("掃描已取消（使用者操作）")

    def _toggle_pause(self):
        if not self._scanning or not self._worker: return
        if self._paused:
            self._worker.resume()
            self._paused = False
            self.btn_pause.setText("⏸")
            self.btn_pause.setStyleSheet("")
            self.step_label.setStyleSheet(f"color:{ACCENT};font-weight:bold;font-size:13px;")
            self.status_bar.showMessage("掃描繼續中…")
            logger.info("掃描已繼續")
        else:
            self._worker.pause()
            self._paused = True
            self.btn_pause.setText("▶")
            self.btn_pause.setStyleSheet(
                f"background:{ACCENT2};color:{DARK_BG};font-weight:bold;border:none;border-radius:5px;"
            )
            self.step_label.setStyleSheet(f"color:{ACCENT2};font-weight:bold;font-size:13px;")
            self.step_label.setText(self.step_label.text() + "  ⏸ 已暫停")
            self.status_bar.showMessage("掃描已暫停")
            logger.info("掃描已暫停")

    def _reset_all(self):
        self._kill_thread()
        self._clear_results()
        self.summary_widget.setVisible(False)
        self.filter_sort_sec.setVisible(False)
        self.global_select_sec.setVisible(False)
        self.progress_section.setVisible(False)
        self.progress_label.setText("")
        self.step_label.setText("")
        self.eta_label.setText("")
        self._set_scanning(False)
        self._paused = False
        self.status_bar.showMessage("已重置")
        logger.info("已重置（使用者操作）")

    def _kill_thread(self):
        if self._worker: self._worker.stop()
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
                f"background-color:{DANGER};color:{DARK_BG};font-weight:bold;border:none;"
                f"border-radius:5px;padding:5px 14px;min-height:26px;"
            )
            self.btn_pause.setVisible(True)
            self.btn_pause.setText("⏸")
            self.btn_pause.setStyleSheet("")
        else:
            self.btn_scan.setText("▶  開始掃描")
            self.btn_scan.setObjectName("btn_scan")
            self.btn_scan.setStyleSheet("")
            self.btn_pause.setVisible(False)
        self.btn_scan.setEnabled(True)

    # ── Slots ─────────────────────────────────────────────────────────
    def _on_thread_finished(self, scan_id: int):
        if scan_id != self._scan_id: return
        self._thread = None
        self._worker = None
        self._paused = False
        if self._scanning:
            self._set_scanning(False)
            self.progress_section.setVisible(False)
            self.progress_label.setText("")
            self.step_label.setText("")
            self.eta_label.setText("")
            self.status_bar.showMessage("掃描已取消")

    def _on_progress(self, msg: str, cur: int, total: int,
                     step: int, total_steps: int, eta_secs: int):
        if not self._scanning: return
        step_name = STEP_NAMES.get(step, f"步驟 {step}")
        self.step_label.setText(f"步驟 {step}/{total_steps} — {step_name}")
        if eta_secs == 0:
            self.eta_label.setText("完成")
        elif eta_secs > 0:
            self.eta_label.setText(f"預估剩餘: {_format_eta(eta_secs)}")
        else:
            self.eta_label.setText("預估剩餘: 計算中…")
        self.progress_label.setText(msg)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(cur)
        else:
            self.progress_bar.setRange(0, 0)

    def _on_done(self, groups: list):
        if not self._scanning: return
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
                f"color:{ACCENT2};font-size:18px;font-weight:bold;padding:40px;"
            )
            no_dup.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.results_layout.insertWidget(0, no_dup)
            return

        total_wasted = sum(g.wasted_bytes for g in groups)
        logger.info(
            f"掃描完成 — {len(groups)} 個重複群組，共浪費 {human_size(total_wasted)}"
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
        self.btn_export.setEnabled(True)

        # 顯示折疊式控制面板（預設收起）
        self.filter_sort_sec.setVisible(True)
        self.global_select_sec.setVisible(True)
        self._apply_filter_sort()

        self.status_bar.showMessage(
            f"掃描完成 — 找到 {len(groups)} 個重複群組，浪費 {human_size(total_wasted)}"
        )

    def _on_error(self, msg: str):
        if not self._scanning: return
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
        self.btn_delete.setEnabled(False)
        self.btn_export.setEnabled(False)

    def _delete_selected(self):
        to_delete: list[Path] = []
        for card in self._cards:
            to_delete.extend(card.selected_paths())

        if not to_delete:
            QMessageBox.information(self, "提示", "請先勾選要刪除的檔案。"); return

        msg = f"確定要刪除以下 {len(to_delete)} 個檔案？\n\n"
        msg += "\n".join(str(p) for p in to_delete[:20])
        if len(to_delete) > 20:
            msg += f"\n… 另外 {len(to_delete) - 20} 個檔案"

        reply = QMessageBox.question(
            self, "確認刪除", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes: return

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

    # ── Font size ─────────────────────────────────────────────────────
    def _apply_font_size(self, size: int):
        self._font_size = size
        new_style = STYLE.replace("font-size: 13px", f"font-size: {size}px")
        self.setStyleSheet(new_style)
        if self._groups:
            self._rebuild_cards(self._groups)

    # ── Export Report ─────────────────────────────────────────────────
    def _make_pie_svg(self, slices: list, title: str = "") -> str:
        """slices: [(label, value), ...] 自動配色，回傳 SVG 字串"""
        COLORS = [
            "#89b4fa", "#a6e3a1", "#fab387", "#f38ba8", "#cba6f7",
            "#94e2d5", "#f9e2af", "#89dceb", "#b4befe", "#eba0ac",
            "#45475a", "#585b70", "#a6adc8", "#7f849c", "#cdd6f4",
        ]
        total = sum(v for _, v in slices)
        if total == 0:
            return "<p style='color:#a6adc8'>無資料</p>"

        W, H = 520, 340
        cx, cy, r = 165, 170, 140

        lines = [f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">']
        if title:
            lines.append(
                f'<text x="{W // 2}" y="18" text-anchor="middle" '
                f'font-size="13" fill="#cdd6f4" font-weight="bold">{title}</text>'
            )

        start = -math.pi / 2
        legend_y = 36
        for i, (label, value) in enumerate(slices):
            if value == 0:
                continue
            color = COLORS[i % len(COLORS)]
            ang = 2 * math.pi * value / total
            end = start + ang
            x1 = cx + r * math.cos(start)
            y1 = cy + r * math.sin(start)
            x2 = cx + r * math.cos(end)
            y2 = cy + r * math.sin(end)
            large = 1 if ang > math.pi else 0
            path = (f"M{cx},{cy} L{x1:.1f},{y1:.1f} "
                    f"A{r},{r} 0 {large},1 {x2:.1f},{y2:.1f} Z")
            lines.append(
                f'<path d="{path}" fill="{color}" '
                f'stroke="#1e1e2e" stroke-width="1.5"/>'
            )
            pct = value / total * 100
            if pct > 4:
                mid = start + ang / 2
                lx = cx + r * 0.65 * math.cos(mid)
                ly = cy + r * 0.65 * math.sin(mid)
                lines.append(
                    f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                    f'dominant-baseline="middle" font-size="10" fill="white">'
                    f'{pct:.1f}%</text>'
                )
            # Legend on the right
            lines.append(
                f'<rect x="325" y="{legend_y - 10}" width="13" height="13" '
                f'fill="{color}" rx="2"/>'
            )
            short = (label[:12] + "…") if len(label) > 13 else label
            lines.append(
                f'<text x="344" y="{legend_y}" font-size="11" fill="#cdd6f4">'
                f'{short} ({pct:.1f}%)</text>'
            )
            legend_y += 20
            start = end

        lines.append("</svg>")
        return "\n".join(lines)

    def _export_report(self):
        if not self._all_groups:
            QMessageBox.information(self, "提示", "請先執行掃描以取得資料。")
            return

        # ── Aggregate by extension ────────────────────────────────────
        ext_data: dict = defaultdict(lambda: {
            "group_hashes": set(), "files": 0, "total_size": 0, "wasted": 0
        })

        for g in self._all_groups:
            # Determine primary extension (most common in group)
            ext_count: dict[str, int] = {}
            for fi in g.files:
                ext = fi.path.suffix.lower() or "(無副檔名)"
                ext_count[ext] = ext_count.get(ext, 0) + 1
            primary = max(ext_count, key=lambda e: ext_count[e])

            for fi in g.files:
                ext = fi.path.suffix.lower() or "(無副檔名)"
                ext_data[ext]["group_hashes"].add(g.hash_value)
                ext_data[ext]["files"] += 1
                ext_data[ext]["total_size"] += fi.size

            ext_data[primary]["wasted"] += g.wasted_bytes

        # Convert sets → counts, sort by wasted desc
        rows = []
        for ext, d in ext_data.items():
            rows.append((ext, {
                "groups":     len(d["group_hashes"]),
                "files":      d["files"],
                "total_size": d["total_size"],
                "wasted":     d["wasted"],
            }))
        rows.sort(key=lambda x: x[1]["wasted"], reverse=True)

        total_groups  = len(self._all_groups)
        total_files   = sum(len(g.files) for g in self._all_groups)
        total_size    = sum(g.size * len(g.files) for g in self._all_groups)
        total_wasted  = sum(g.wasted_bytes for g in self._all_groups)

        pie_wasted_svg = self._make_pie_svg(
            [(ext, d["wasted"]) for ext, d in rows if d["wasted"] > 0],
            "浪費空間分布（依副檔名）"
        )
        pie_count_svg = self._make_pie_svg(
            [(ext, d["files"]) for ext, d in rows if d["files"] > 0],
            "重複檔案數量分布（依副檔名）"
        )

        scan_path = self.path_edit.text()
        scan_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Build HTML ────────────────────────────────────────────────
        table_rows_html = ""
        for ext, d in rows:
            pct = d["wasted"] / total_wasted * 100 if total_wasted > 0 else 0
            bar_w = min(pct, 100)
            table_rows_html += (
                f"<tr>"
                f"<td><strong>{ext}</strong></td>"
                f"<td>{d['groups']}</td>"
                f"<td>{d['files']}</td>"
                f"<td>{human_size(d['total_size'])}</td>"
                f"<td style='color:#f38ba8'>{human_size(d['wasted'])}</td>"
                f"<td>"
                f"<div style='display:flex;align-items:center;gap:8px;'>"
                f"<div class='pct-bar'><div class='pct-fill' "
                f"style='width:{bar_w:.1f}%'></div></div>"
                f"<span>{pct:.1f}%</span></div>"
                f"</td></tr>\n"
            )

        html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>DupeScan 分析報表</title>
<style>
*{{box-sizing:border-box;}}
body{{font-family:"Segoe UI",sans-serif;background:#1e1e2e;color:#cdd6f4;
     margin:0;padding:24px;}}
h1{{color:#89b4fa;font-size:22px;margin-bottom:4px;}}
h2{{color:#89b4fa;font-size:15px;margin-top:28px;
    border-bottom:1px solid #45475a;padding-bottom:6px;}}
.meta{{color:#a6adc8;font-size:12px;margin-bottom:20px;}}
.summary-grid{{display:grid;grid-template-columns:repeat(4,1fr);
               gap:12px;margin-bottom:24px;}}
.stat-card{{background:#181825;border:1px solid #45475a;border-radius:8px;
            padding:16px;text-align:center;}}
.stat-value{{font-size:20px;font-weight:bold;color:#89b4fa;}}
.stat-label{{font-size:11px;color:#a6adc8;margin-top:4px;}}
.charts{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;}}
.chart-box{{background:#181825;border:1px solid #45475a;
            border-radius:8px;padding:14px;}}
table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px;}}
th{{background:#313244;padding:9px 12px;text-align:left;
    font-size:11px;color:#a6adc8;font-weight:600;}}
td{{padding:8px 12px;border-bottom:1px solid #313244;}}
tr:hover td{{background:#252535;}}
.pct-bar{{background:#313244;border-radius:3px;height:8px;width:110px;}}
.pct-fill{{background:#89b4fa;height:8px;border-radius:3px;}}
footer{{color:#585b70;font-size:11px;margin-top:30px;}}
</style>
</head>
<body>
<h1>DupeScan 分析報表</h1>
<div class="meta">掃描路徑：{scan_path}&nbsp;&nbsp;|&nbsp;&nbsp;產生時間：{scan_time}</div>

<div class="summary-grid">
  <div class="stat-card">
    <div class="stat-value">{total_groups}</div>
    <div class="stat-label">重複群組數</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{total_files}</div>
    <div class="stat-label">重複檔案總數</div>
  </div>
  <div class="stat-card">
    <div class="stat-value" style="color:#f38ba8">{human_size(total_wasted)}</div>
    <div class="stat-label">可釋放空間</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{human_size(total_size)}</div>
    <div class="stat-label">重複檔案總大小</div>
  </div>
</div>

<h2>重複檔案分布圖</h2>
<div class="charts">
  <div class="chart-box">{pie_wasted_svg}</div>
  <div class="chart-box">{pie_count_svg}</div>
</div>

<h2>副檔名詳細分析</h2>
<table>
  <thead><tr>
    <th>副檔名</th><th>群組數</th><th>重複檔案數</th>
    <th>總大小</th><th>可釋放空間</th><th>占總浪費空間</th>
  </tr></thead>
  <tbody>
{table_rows_html}  </tbody>
</table>
<footer>由 DupeScan 自動產生</footer>
</body>
</html>"""

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False,
            encoding="utf-8", prefix="dupescan_report_"
        ) as f:
            f.write(html)
            tmp_path = f.name

        webbrowser.open(f"file:///{tmp_path.replace(os.sep, '/')}")
        self.status_bar.showMessage(f"報表已匯出並開啟: {tmp_path}")
        logger.info(f"匯出報表: {tmp_path}")
