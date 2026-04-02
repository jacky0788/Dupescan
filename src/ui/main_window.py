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

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QEvent, QPoint
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
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


# ── Condition definitions ──────────────────────────────────────────────────────
# (id, label, key_fn, reverse=True means higher value is "winner", conflict_id)
CONDITIONS_DEF = [
    ("newer",    "修改較新", lambda fi: fi.mtime,             True,  "older"),
    ("older",    "修改較舊", lambda fi: fi.mtime,             False, "newer"),
    ("sh_path",  "路徑較短", lambda fi: len(str(fi.path)),    False, "lo_path"),
    ("lo_path",  "路徑較長", lambda fi: len(str(fi.path)),    True,  "sh_path"),
    ("shallow",  "目錄較淺", lambda fi: len(fi.path.parts),   False, "deep"),
    ("deep",     "目錄較深", lambda fi: len(fi.path.parts),   True,  "shallow"),
    ("al_first", "字母較前", lambda fi: str(fi.path).lower(), False, "al_last"),
    ("al_last",  "字母較後", lambda fi: str(fi.path).lower(), True,  "al_first"),
    ("list_1st", "列表第一", None,                             False, None),
]
_COND_BY_ID = {c[0]: c for c in CONDITIONS_DEF}


def _rank_by_conditions(files: list, conditions: list) -> int:
    """
    Stable multi-sort: apply conditions in reverse priority order.
    Returns index of 'winner' file.
    """
    if not conditions:
        return 0
    indices = list(range(len(files)))
    for cond_id, key_fn, rev in reversed(conditions):
        if cond_id == "list_1st" or key_fn is None:
            indices.sort(key=lambda i: i)
        else:
            indices.sort(key=lambda i: key_fn(files[i]), reverse=rev)
    return indices[0]


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
ACCENT   = "#89b4fa"
ACCENT2  = "#a6e3a1"
ACCENT3  = "#fab387"
DANGER   = "#f38ba8"
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
QPushButton {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 4px 10px;
    min-height: 22px;
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
QLineEdit, QSpinBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 3px 8px;
    color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 3px 8px;
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
    height: 16px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 3px; }}
QTableWidget {{
    background-color: {PANEL_BG};
    alternate-background-color: {DARK_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    gridline-color: {BORDER};
}}
QTableWidget::item {{ padding: 3px 8px; }}
QTableWidget::item:selected {{ background-color: {SURFACE}; color: {TEXT}; }}
QHeaderView::section {{
    background-color: {SURFACE};
    color: {SUBTEXT};
    border: none;
    border-right: 1px solid {BORDER};
    padding: 4px 8px;
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
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar:horizontal {{
    background: {PANEL_BG};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
QStatusBar {{
    background-color: {PANEL_BG};
    border-top: 1px solid {BORDER};
    color: {SUBTEXT};
}}
QCheckBox {{ spacing: 5px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {PANEL_BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox:disabled {{ color: {BORDER}; }}
QCheckBox::indicator:disabled {{ background: {SURFACE}; border-color: {BORDER}; opacity: 0.4; }}
"""

_BTN_KEEP_ON  = f"background:{ACCENT};color:{DARK_BG};font-weight:bold;border:none;border-radius:4px;padding:2px 8px;font-size:12px;"
_BTN_KEEP_OFF = f"background:{SURFACE};color:{TEXT};border:1px solid {BORDER};border-radius:4px;padding:2px 8px;font-size:12px;"
_BTN_DEL_ON   = f"background:{DANGER};color:{DARK_BG};font-weight:bold;border:none;border-radius:4px;padding:2px 8px;font-size:12px;"
_BTN_DEL_OFF  = f"background:{SURFACE};color:{TEXT};border:1px solid {BORDER};border-radius:4px;padding:2px 8px;font-size:12px;"


# ── ConditionPanel ────────────────────────────────────────────────────────────
class ConditionPanel(QWidget):
    """Multi-condition checkbox panel; tracks check-order for priority sorting."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked_order: list[str] = []
        self._checkboxes: dict[str, QCheckBox] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 4)
        lay.setSpacing(3)

        hint = QLabel("勾選條件（依優先順序疊加）：")
        hint.setStyleSheet(f"color:{SUBTEXT};font-size:11px;")
        lay.addWidget(hint)

        pairs = [("newer", "older"), ("sh_path", "lo_path"),
                 ("shallow", "deep"), ("al_first", "al_last")]
        for id1, id2 in pairs:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(self._make_cb(id1))
            row.addWidget(self._make_cb(id2))
            row.addStretch()
            lay.addLayout(row)

        lay.addWidget(self._make_cb("list_1st"))

    def _make_cb(self, cond_id: str) -> QCheckBox:
        label = _COND_BY_ID[cond_id][1]
        cb = QCheckBox(label)
        cb.setStyleSheet(f"font-size:12px; color:{TEXT}; min-width:75px;")
        cb.stateChanged.connect(lambda s, cid=cond_id: self._on_toggle(cid, s))
        self._checkboxes[cond_id] = cb
        return cb

    def _on_toggle(self, cond_id: str, state: int):
        checked = (state == Qt.CheckState.Checked.value)
        conflict = _COND_BY_ID[cond_id][4]
        if checked:
            if cond_id not in self._checked_order:
                self._checked_order.append(cond_id)
            if conflict and conflict in self._checkboxes:
                cb = self._checkboxes[conflict]
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.setEnabled(False)
                cb.blockSignals(False)
                if conflict in self._checked_order:
                    self._checked_order.remove(conflict)
        else:
            if cond_id in self._checked_order:
                self._checked_order.remove(cond_id)
            if conflict and conflict in self._checkboxes:
                self._checkboxes[conflict].setEnabled(True)

    def get_active(self) -> list:
        """Returns [(id, key_fn, reverse), ...] in checked priority order."""
        return [(cid, _COND_BY_ID[cid][2], _COND_BY_ID[cid][3]) for cid in self._checked_order]

    def clear_all(self):
        for cb in self._checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.setEnabled(True)
            cb.blockSignals(False)
        self._checked_order.clear()


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
        self._qs_mode = "keep"
        self._qs_open = False

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
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(4)

        # ── Header ────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title_lbl = QLabel(
            f"  群組 #{index + 1}  ·  {len(group.files)} 個重複  ·  "
            f"每個 {human_size(group.size)}"
        )
        title_lbl.setStyleSheet(f"color:{TEXT};font-weight:bold;")
        wasted_lbl = QLabel(f"浪費 {human_size(group.wasted_bytes)}")
        wasted_lbl.setStyleSheet(
            f"background:{DANGER};color:{DARK_BG};border-radius:4px;"
            f"padding:1px 6px;font-weight:bold;font-size:11px;"
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
        self._qs_toggle_btn.setFixedHeight(20)
        self._qs_toggle_btn.setStyleSheet(
            f"text-align:left;padding:1px 8px;font-size:11px;color:{SUBTEXT};"
            f"background:transparent;border:none;"
        )
        self._qs_toggle_btn.clicked.connect(self._toggle_qs)
        root.addWidget(self._qs_toggle_btn)

        # ── Quick-select panel (hidden by default) ────────────────────
        self._qs_panel = QWidget()
        qs_lay = QVBoxLayout(self._qs_panel)
        qs_lay.setContentsMargins(0, 2, 0, 2)
        qs_lay.setSpacing(4)

        # Mode toggle row
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        lbl_m = QLabel("操作模式:")
        lbl_m.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        mode_row.addWidget(lbl_m)
        self._btn_mode_keep = QPushButton("保留目標")
        self._btn_mode_keep.setFixedHeight(22)
        self._btn_mode_keep.clicked.connect(lambda: self._set_mode("keep"))
        self._btn_mode_delete = QPushButton("刪除目標")
        self._btn_mode_delete.setFixedHeight(22)
        self._btn_mode_delete.clicked.connect(lambda: self._set_mode("delete"))
        mode_row.addWidget(self._btn_mode_keep)
        mode_row.addWidget(self._btn_mode_delete)
        mode_row.addStretch()
        qs_lay.addLayout(mode_row)

        # Condition checkboxes
        self._cond_panel = ConditionPanel()
        qs_lay.addWidget(self._cond_panel)

        # Action row
        act_row = QHBoxLayout()
        act_row.setSpacing(4)
        btn_apply = QPushButton("套用條件")
        btn_apply.setFixedHeight(24)
        btn_apply.setStyleSheet(
            f"background:{ACCENT};color:{DARK_BG};font-weight:bold;border:none;"
            f"border-radius:4px;padding:2px 10px;font-size:12px;"
        )
        btn_apply.clicked.connect(self._apply_conditions)
        sep = QLabel("|")
        sep.setStyleSheet(f"color:{BORDER};")
        btn_all = QPushButton("全選")
        btn_all.setFixedHeight(24)
        btn_all.setStyleSheet(f"padding:2px 8px;font-size:12px;background:{SURFACE};"
                               f"border:1px solid {BORDER};border-radius:4px;color:{TEXT};")
        btn_all.clicked.connect(self._select_all)
        btn_none = QPushButton("全不選")
        btn_none.setFixedHeight(24)
        btn_none.setStyleSheet(f"padding:2px 8px;font-size:12px;background:{SURFACE};"
                                f"border:1px solid {BORDER};border-radius:4px;color:{TEXT};")
        btn_none.clicked.connect(self._deselect_all)
        act_row.addWidget(btn_apply)
        act_row.addWidget(sep)
        act_row.addWidget(btn_all)
        act_row.addWidget(btn_none)
        act_row.addStretch()
        qs_lay.addLayout(act_row)

        self._qs_panel.setVisible(False)
        root.addWidget(self._qs_panel)
        self._update_mode_btns()

        # ── File table ────────────────────────────────────────────────
        _row_h = max(24, int(34 * font_size / 13))
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
        self.table.setFixedHeight(min(_row_h * len(group.files) + 30,
                                      int(260 * font_size / 13)))
        self.table.viewport().setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        self.table.cellClicked.connect(self._on_cell_clicked)

        tbl_font = QFont()
        tbl_font.setPixelSize(font_size)
        self.table.setFont(tbl_font)

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

    # ── Quick-select internals ────────────────────────────────────────
    def _toggle_qs(self):
        self._qs_open = not self._qs_open
        self._qs_panel.setVisible(self._qs_open)
        self._qs_toggle_btn.setText(
            "▼  快速選取" if self._qs_open else "▶  快速選取"
        )

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

    def _apply_conditions(self):
        conditions = self._cond_panel.get_active()
        if not conditions:
            return
        self._apply_with(conditions, self._qs_mode)

    def _apply_with(self, conditions: list, mode: str):
        winner = _rank_by_conditions(self.group.files, conditions)
        if mode == "keep":
            for i, cb in enumerate(self.checkboxes):
                cb.setChecked(i != winner)
        else:
            for i, cb in enumerate(self.checkboxes):
                cb.setChecked(i == winner)

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
        self.resize(1280, 820)
        self.setMinimumSize(880, 580)

        self._all_groups: list[DuplicateGroup] = []
        self._groups:     list[DuplicateGroup] = []
        self._cards:      list[GroupCard]      = []
        self._thread:     QThread | None       = None
        self._worker:     ScanWorker | None    = None
        self._scanning:   bool                 = False
        self._paused:     bool                 = False
        self._scan_id:    int                  = 0
        self._font_size:  int                  = 13
        self._global_qs_mode: str              = "keep"
        self._sidebar_open:   bool             = True
        self._sb_filter_open: bool             = True
        self._sb_qs_open:     bool             = True

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
        main.setContentsMargins(10, 8, 10, 8)
        main.setSpacing(5)

        # ── Title ──────────────────────────────────────────────────────
        title_lbl = QLabel("DupeScan")
        title_lbl.setStyleSheet(
            f"font-size:20px;font-weight:bold;color:{ACCENT};letter-spacing:1px;"
        )
        main.addWidget(title_lbl)

        # ── Scan config row ─────────────────────────────────────────────
        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(6)

        cfg_row.addWidget(QLabel("路徑:"))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("選擇要掃描的資料夾或磁碟機…")
        self.path_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.path_edit.setMinimumWidth(200)
        cfg_row.addWidget(self.path_edit, 1)

        browse_btn = QPushButton("瀏覽")
        browse_btn.setFixedWidth(58)
        browse_btn.setFixedHeight(28)
        browse_btn.clicked.connect(self._browse)
        cfg_row.addWidget(browse_btn)

        cfg_row.addSpacing(8)
        min_lbl = QLabel("最小:")
        cfg_row.addWidget(min_lbl)
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 1_000_000)
        self.min_size_spin.setValue(1)
        self.min_size_spin.setSuffix(" KB")
        self.min_size_spin.setFixedWidth(88)
        self.min_size_spin.setFixedHeight(28)
        cfg_row.addWidget(self.min_size_spin)

        cfg_row.addSpacing(8)

        self.btn_reset = QPushButton("重置")
        self.btn_reset.setFixedWidth(52)
        self.btn_reset.setFixedHeight(28)
        self.btn_reset.setToolTip("重置：停止掃描並清除所有結果")
        self.btn_reset.clicked.connect(self._reset_all)
        cfg_row.addWidget(self.btn_reset)

        self.btn_pause = QPushButton("⏸")
        self.btn_pause.setFixedWidth(36)
        self.btn_pause.setFixedHeight(28)
        self.btn_pause.setVisible(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        cfg_row.addWidget(self.btn_pause)

        self.btn_scan = QPushButton("▶  開始掃描")
        self.btn_scan.setObjectName("btn_scan")
        self.btn_scan.setFixedHeight(28)
        self.btn_scan.setMinimumWidth(100)
        self.btn_scan.clicked.connect(self._toggle_scan)
        cfg_row.addWidget(self.btn_scan)

        main.addLayout(cfg_row)

        # ── Progress ────────────────────────────────────────────────────
        self.progress_section = QWidget()
        prog_l = QVBoxLayout(self.progress_section)
        prog_l.setContentsMargins(0, 0, 0, 0)
        prog_l.setSpacing(2)
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
        sum_l.setSpacing(5)

        self.lbl_groups = QLabel("0 個群組")
        self.lbl_wasted = QLabel("浪費: 0 B")
        for lbl, bg in [(self.lbl_groups, ACCENT), (self.lbl_wasted, DANGER)]:
            lbl.setStyleSheet(
                f"background:{bg};color:{DARK_BG};border-radius:4px;"
                f"padding:2px 8px;font-weight:bold;font-size:11px;"
            )
            lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.btn_export = QPushButton("📊 匯出報表")
        self.btn_export.setFixedHeight(26)
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip("分析重複檔案分布並匯出 HTML 報表（含圓餅圖）")
        self.btn_export.clicked.connect(self._export_report)

        self.btn_delete = QPushButton("🗑 刪除已勾選")
        self.btn_delete.setObjectName("btn_delete")
        self.btn_delete.setFixedHeight(26)
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)

        sum_l.addWidget(self.lbl_groups)
        sum_l.addWidget(self.lbl_wasted)
        sum_l.addStretch()
        sum_l.addWidget(self.btn_export)
        sum_l.addWidget(self.btn_delete)
        self.summary_widget.setVisible(False)
        main.addWidget(self.summary_widget)

        # ── Content area: sidebar + results (80%) ───────────────────────
        content_widget = QWidget()
        content_lay = QHBoxLayout(content_widget)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(0)

        # Build sidebar
        self._sidebar_widget = self._build_sidebar()
        content_lay.addWidget(self._sidebar_widget)

        # Toggle strip
        self._sidebar_tab = QPushButton("◀")
        self._sidebar_tab.setFixedWidth(14)
        self._sidebar_tab.setToolTip("展開/收起功能列")
        self._sidebar_tab.setStyleSheet(
            f"QPushButton{{background:{SURFACE};border:none;"
            f"border-left:1px solid {BORDER};border-right:1px solid {BORDER};"
            f"color:{SUBTEXT};font-size:9px;border-radius:0px;padding:0px;}}"
            f"QPushButton:hover{{background:{ACCENT};color:{DARK_BG};}}"
        )
        self._sidebar_tab.clicked.connect(self._toggle_sidebar)
        content_lay.addWidget(self._sidebar_tab)

        # Results scroll area
        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_widget = QWidget()
        self.results_layout = QVBoxLayout(self.results_widget)
        self.results_layout.setSpacing(6)
        self.results_layout.setContentsMargins(8, 0, 0, 0)
        self.results_layout.addStretch()
        self._results_scroll.setWidget(self.results_widget)
        content_lay.addWidget(self._results_scroll, 1)

        main.addWidget(content_widget, 1)

        # ── Status bar ──────────────────────────────────────────────────
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就緒")

    def _build_sidebar(self) -> QWidget:
        sb = QWidget()
        sb.setFixedWidth(248)
        sb.setStyleSheet(
            f"QWidget#sidebar_root{{background:{PANEL_BG};"
            f"border-right:1px solid {BORDER};}}"
        )
        sb.setObjectName("sidebar_root")

        sb_lay = QVBoxLayout(sb)
        sb_lay.setContentsMargins(8, 8, 8, 8)
        sb_lay.setSpacing(6)

        hdr = QLabel("功能列")
        hdr.setStyleSheet(
            f"font-size:13px;font-weight:bold;color:{ACCENT};"
            f"padding-bottom:4px;border-bottom:1px solid {BORDER};"
        )
        sb_lay.addWidget(hdr)

        # ── Scroll area for sidebar content ───────────────────────────
        sb_scroll = QScrollArea()
        sb_scroll.setWidgetResizable(True)
        sb_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sb_body = QWidget()
        sb_body_lay = QVBoxLayout(sb_body)
        sb_body_lay.setContentsMargins(0, 0, 4, 0)
        sb_body_lay.setSpacing(6)

        # ── Section: 篩選與排序 ────────────────────────────────────────
        self._sb_filter_hdr = QPushButton("▼  篩選與排序")
        self._sb_filter_hdr.setStyleSheet(
            f"QPushButton{{text-align:left;background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:4px;color:{ACCENT};font-weight:bold;padding:5px 8px;font-size:12px;}}"
            f"QPushButton:hover{{background:#414459;}}"
        )
        self._sb_filter_hdr.clicked.connect(self._toggle_filter_panel)
        sb_body_lay.addWidget(self._sb_filter_hdr)

        self._sb_filter_body = QWidget()
        fb = QVBoxLayout(self._sb_filter_body)
        fb.setContentsMargins(4, 2, 4, 4)
        fb.setSpacing(6)

        ext_lbl = QLabel("副檔名篩選:")
        ext_lbl.setStyleSheet(f"font-size:11px;color:{SUBTEXT};")
        self.ext_filter_edit = QLineEdit()
        self.ext_filter_edit.setPlaceholderText("jpg,png… (留空顯示全部)")
        self.ext_filter_edit.textChanged.connect(self._apply_filter_sort)
        fb.addWidget(ext_lbl)
        fb.addWidget(self.ext_filter_edit)

        sort_lbl = QLabel("排序方式:")
        sort_lbl.setStyleSheet(f"font-size:11px;color:{SUBTEXT};")
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "群組大小（大→小）", "群組大小（小→大）",
            "檔案數量（多→少）", "檔案數量（少→多）",
            "副檔名 A→Z",        "副檔名 Z→A",
        ])
        self.sort_combo.currentIndexChanged.connect(self._apply_filter_sort)
        fb.addWidget(sort_lbl)
        fb.addWidget(self.sort_combo)

        font_row = QHBoxLayout()
        font_lbl = QLabel("文字大小:")
        font_lbl.setStyleSheet(f"font-size:11px;color:{SUBTEXT};")
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(9, 18)
        self.font_size_spin.setValue(13)
        self.font_size_spin.setSuffix(" px")
        self.font_size_spin.setFixedWidth(70)
        self.font_size_spin.setToolTip("僅調整群組內表格的字體大小（較小時可顯示更多群組）")
        self.font_size_spin.valueChanged.connect(self._apply_font_size)
        font_row.addWidget(font_lbl)
        font_row.addWidget(self.font_size_spin)
        font_row.addStretch()
        fb.addLayout(font_row)

        sb_body_lay.addWidget(self._sb_filter_body)

        # ── Section: 批量快速選取 ──────────────────────────────────────
        self._sb_qs_hdr = QPushButton("▼  批量快速選取")
        self._sb_qs_hdr.setStyleSheet(
            f"QPushButton{{text-align:left;background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:4px;color:{ACCENT};font-weight:bold;padding:5px 8px;font-size:12px;}}"
            f"QPushButton:hover{{background:#414459;}}"
        )
        self._sb_qs_hdr.clicked.connect(self._toggle_qs_panel)
        sb_body_lay.addWidget(self._sb_qs_hdr)

        self._sb_qs_body = QWidget()
        qb = QVBoxLayout(self._sb_qs_body)
        qb.setContentsMargins(4, 2, 4, 4)
        qb.setSpacing(4)

        gm_row = QHBoxLayout()
        gm_row.setSpacing(4)
        gm_lbl = QLabel("操作模式:")
        gm_lbl.setStyleSheet(f"font-size:11px;color:{SUBTEXT};")
        gm_row.addWidget(gm_lbl)
        self._g_btn_keep = QPushButton("保留目標")
        self._g_btn_keep.setFixedHeight(22)
        self._g_btn_keep.clicked.connect(lambda: self._set_global_mode("keep"))
        self._g_btn_delete = QPushButton("刪除目標")
        self._g_btn_delete.setFixedHeight(22)
        self._g_btn_delete.clicked.connect(lambda: self._set_global_mode("delete"))
        gm_row.addWidget(self._g_btn_keep)
        gm_row.addWidget(self._g_btn_delete)
        gm_row.addStretch()
        qb.addLayout(gm_row)
        self._update_global_mode_btns()

        self._global_cond_panel = ConditionPanel()
        qb.addWidget(self._global_cond_panel)

        ga_row = QHBoxLayout()
        ga_row.setSpacing(4)
        btn_apply_all = QPushButton("套用到所有群組")
        btn_apply_all.setFixedHeight(24)
        btn_apply_all.setStyleSheet(
            f"background:{ACCENT};color:{DARK_BG};font-weight:bold;border:none;"
            f"border-radius:4px;padding:2px 8px;font-size:12px;"
        )
        btn_apply_all.clicked.connect(self._global_apply_conditions)
        ga_row.addWidget(btn_apply_all)
        ga_row.addStretch()
        qb.addLayout(ga_row)

        ga_row2 = QHBoxLayout()
        ga_row2.setSpacing(4)
        btn_all = QPushButton("全部勾選")
        btn_all.setFixedHeight(24)
        btn_all.setStyleSheet(f"padding:2px 6px;font-size:12px;background:{SURFACE};"
                               f"border:1px solid {BORDER};border-radius:4px;color:{TEXT};")
        btn_all.clicked.connect(self._global_select_all)
        btn_none = QPushButton("全部取消")
        btn_none.setFixedHeight(24)
        btn_none.setStyleSheet(f"padding:2px 6px;font-size:12px;background:{SURFACE};"
                                f"border:1px solid {BORDER};border-radius:4px;color:{TEXT};")
        btn_none.clicked.connect(self._global_deselect_all)
        ga_row2.addWidget(btn_all)
        ga_row2.addWidget(btn_none)
        ga_row2.addStretch()
        qb.addLayout(ga_row2)

        sb_body_lay.addWidget(self._sb_qs_body)
        sb_body_lay.addStretch()

        sb_scroll.setWidget(sb_body)
        sb_lay.addWidget(sb_scroll, 1)

        return sb

    # ── Sidebar toggle ────────────────────────────────────────────────
    def _toggle_sidebar(self):
        self._sidebar_open = not self._sidebar_open
        self._sidebar_widget.setVisible(self._sidebar_open)
        self._sidebar_tab.setText("◀" if self._sidebar_open else "▶")

    def _toggle_filter_panel(self):
        self._sb_filter_open = not self._sb_filter_open
        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_filter_hdr.setText(
            "▼  篩選與排序" if self._sb_filter_open else "▶  篩選與排序"
        )

    def _toggle_qs_panel(self):
        self._sb_qs_open = not self._sb_qs_open
        self._sb_qs_body.setVisible(self._sb_qs_open)
        self._sb_qs_hdr.setText(
            "▼  批量快速選取" if self._sb_qs_open else "▶  批量快速選取"
        )

    # ── Global quick-select ───────────────────────────────────────────
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

    def _global_apply_conditions(self):
        conditions = self._global_cond_panel.get_active()
        if not conditions:
            QMessageBox.information(self, "提示", "請先勾選至少一個條件。")
            return
        for card in self._cards:
            card._apply_with(conditions, self._global_qs_mode)

    def _global_select_all(self):
        for card in self._cards: card._select_all()

    def _global_deselect_all(self):
        for card in self._cards: card._deselect_all()

    # ── Font size (cards only) ────────────────────────────────────────
    def _apply_font_size(self, size: int):
        self._font_size = size
        if self._groups:
            self._rebuild_cards(self._groups)

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
        self._sb_filter_body.setVisible(False)
        self._sb_qs_body.setVisible(False)
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
        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_qs_body.setVisible(self._sb_qs_open)
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
                f"border-radius:5px;padding:4px 10px;min-height:22px;"
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
        logger.info(f"掃描完成 — {len(groups)} 個重複群組，共浪費 {human_size(total_wasted)}")
        for g in groups:
            logger.debug(
                f"  群組 hash={g.hash_value[:16]}  size={human_size(g.size)}  "
                f"files={len(g.files)}  wasted={human_size(g.wasted_bytes)}"
            )

        self.lbl_groups.setText(f"{len(groups)} 個群組")
        self.lbl_wasted.setText(f"浪費: {human_size(total_wasted)}")
        self.summary_widget.setVisible(True)
        self.btn_delete.setEnabled(True)
        self.btn_export.setEnabled(True)

        # Restore sidebar section visibility
        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_qs_body.setVisible(self._sb_qs_open)

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

    # ── Export Report ─────────────────────────────────────────────────
    def _make_pie_svg(self, slices: list, title: str = "") -> str:
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
                f'<text x="{W // 2}" y="16" text-anchor="middle" '
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
            lines.append(f'<path d="{path}" fill="{color}" stroke="#1e1e2e" stroke-width="1.5"/>')
            pct = value / total * 100
            if pct > 4:
                mid = start + ang / 2
                lx = cx + r * 0.65 * math.cos(mid)
                ly = cy + r * 0.65 * math.sin(mid)
                lines.append(
                    f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                    f'dominant-baseline="middle" font-size="10" fill="white">{pct:.1f}%</text>'
                )
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

        ext_data: dict = defaultdict(lambda: {
            "group_hashes": set(), "files": 0, "total_size": 0, "wasted": 0
        })
        for g in self._all_groups:
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

        rows = sorted(
            [(ext, {"groups": len(d["group_hashes"]), "files": d["files"],
                    "total_size": d["total_size"], "wasted": d["wasted"]})
             for ext, d in ext_data.items()],
            key=lambda x: x[1]["wasted"], reverse=True
        )

        total_groups = len(self._all_groups)
        total_files  = sum(len(g.files) for g in self._all_groups)
        total_size   = sum(g.size * len(g.files) for g in self._all_groups)
        total_wasted = sum(g.wasted_bytes for g in self._all_groups)

        pie_wasted  = self._make_pie_svg(
            [(ext, d["wasted"]) for ext, d in rows if d["wasted"] > 0],
            "浪費空間分布（依副檔名）"
        )
        pie_count   = self._make_pie_svg(
            [(ext, d["files"]) for ext, d in rows if d["files"] > 0],
            "重複檔案數量分布（依副檔名）"
        )

        scan_path = self.path_edit.text()
        scan_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        tbl_rows = ""
        for ext, d in rows:
            pct = d["wasted"] / total_wasted * 100 if total_wasted > 0 else 0
            tbl_rows += (
                f"<tr><td><strong>{ext}</strong></td>"
                f"<td>{d['groups']}</td><td>{d['files']}</td>"
                f"<td>{human_size(d['total_size'])}</td>"
                f"<td style='color:#f38ba8'>{human_size(d['wasted'])}</td>"
                f"<td><div style='display:flex;align-items:center;gap:8px;'>"
                f"<div class='pb'><div class='pf' style='width:{min(pct,100):.1f}%'></div></div>"
                f"<span>{pct:.1f}%</span></div></td></tr>\n"
            )

        html = f"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8"><title>DupeScan 分析報表</title>
<style>
*{{box-sizing:border-box;}}
body{{font-family:"Segoe UI",sans-serif;background:#1e1e2e;color:#cdd6f4;margin:0;padding:24px;}}
h1{{color:#89b4fa;font-size:22px;margin-bottom:4px;}}
h2{{color:#89b4fa;font-size:15px;margin-top:28px;border-bottom:1px solid #45475a;padding-bottom:6px;}}
.meta{{color:#a6adc8;font-size:12px;margin-bottom:20px;}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px;}}
.card{{background:#181825;border:1px solid #45475a;border-radius:8px;padding:16px;text-align:center;}}
.val{{font-size:20px;font-weight:bold;color:#89b4fa;}}
.lbl{{font-size:11px;color:#a6adc8;margin-top:4px;}}
.charts{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;}}
.cbox{{background:#181825;border:1px solid #45475a;border-radius:8px;padding:14px;}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;}}
th{{background:#313244;padding:9px 12px;text-align:left;font-size:11px;color:#a6adc8;}}
td{{padding:8px 12px;border-bottom:1px solid #313244;}}
tr:hover td{{background:#252535;}}
.pb{{background:#313244;border-radius:3px;height:8px;width:110px;}}
.pf{{background:#89b4fa;height:8px;border-radius:3px;}}
footer{{color:#585b70;font-size:11px;margin-top:30px;}}
</style></head><body>
<h1>DupeScan 分析報表</h1>
<div class="meta">掃描路徑：{scan_path}&nbsp;&nbsp;|&nbsp;&nbsp;產生時間：{scan_time}</div>
<div class="grid">
  <div class="card"><div class="val">{total_groups}</div><div class="lbl">重複群組數</div></div>
  <div class="card"><div class="val">{total_files}</div><div class="lbl">重複檔案總數</div></div>
  <div class="card"><div class="val" style="color:#f38ba8">{human_size(total_wasted)}</div><div class="lbl">可釋放空間</div></div>
  <div class="card"><div class="val">{human_size(total_size)}</div><div class="lbl">重複檔案總大小</div></div>
</div>
<h2>重複檔案分布圖</h2>
<div class="charts">
  <div class="cbox">{pie_wasted}</div>
  <div class="cbox">{pie_count}</div>
</div>
<h2>副檔名詳細分析</h2>
<table><thead><tr>
  <th>副檔名</th><th>群組數</th><th>重複檔案數</th>
  <th>總大小</th><th>可釋放空間</th><th>占總浪費空間</th>
</tr></thead><tbody>
{tbl_rows}</tbody></table>
<footer>由 DupeScan 自動產生</footer>
</body></html>"""

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False,
            encoding="utf-8", prefix="dupescan_report_"
        ) as f:
            f.write(html)
            tmp_path = f.name

        webbrowser.open(f"file:///{tmp_path.replace(os.sep, '/')}")
        self.status_bar.showMessage(f"報表已匯出並開啟: {tmp_path}")
        logger.info(f"匯出報表: {tmp_path}")
