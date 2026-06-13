# 主視窗模組：PyQt6 現代化深色 UI。
#
# 結果以「單一虛擬化樹狀檢視」呈現（DuplicateTree）：
#   - 群組為頂層列，檔案為其子列；QTreeWidget 原生只渲染可見列（virtualized）
#   - 群組子列採「延遲建立」（lazy）：展開時才建立檔案列，未展開的群組幾乎零成本
#   - 勾選（待刪除）狀態存在資料層 FileInfo.selected，與 widget 解耦，
#     讓「全選 / 套用到所有群組」等批次操作不需要所有列都已建立
#
# 相較舊版「每個群組一個 QTableWidget」，大量重複時的記憶體與渲染成本大幅下降。

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
    _HZ = False  # 未安裝時退回自製格式化函式

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QObject, QEvent, QPoint
from PyQt6.QtGui import QColor, QFont, QPixmap, QAction
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QProgressBar, QScrollArea,
    QSizePolicy, QSpinBox, QStatusBar, QVBoxLayout, QWidget,
    QTreeWidget, QTreeWidgetItem, QMenu, QGraphicsDropShadowEffect,
)

from ..scanner import Scanner, STEP_NAMES, TOTAL_STEPS, _format_eta
from ..models import DuplicateGroup, FileInfo
from ..logger import logger

# 支援懸停圖片預覽的副檔名集合
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.ico'}


def human_size(n: int) -> str:
    """將 bytes 轉為人類可讀字串（優先使用 humanize，否則自製實作）。"""
    if _HZ:
        return humanize.naturalsize(n, binary=True)
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


# ── 快速選取條件定義 ────────────────────────────────────────────────────────────
# 每個 tuple：(id, 顯示標籤, 排序 key 函式, reverse=True 表示值越大越優先, 互斥條件 id)
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
    """多條件穩定排序，回傳「勝出」檔案的索引。

    以倒序套用條件（優先序最低的先排），後套用的條件優先序更高，
    Python sort 的穩定性確保同優先序的結果不受干擾。
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


def _apply_selection(group: DuplicateGroup, conditions: list, mode: str):
    """依條件找出勝出檔案，更新群組內各檔案的 selected 旗標。

    mode='keep'   → 保留勝出者（勝出者不勾選，其餘勾選待刪）
    mode='delete' → 刪除勝出者（只勾選勝出者）
    """
    winner = _rank_by_conditions(group.files, conditions)
    for i, fi in enumerate(group.files):
        fi.selected = (i != winner) if mode == "keep" else (i == winner)


# ── Worker（掃描 Worker，在 QThread 中執行）────────────────────────────────────
class ScanWorker(QObject):
    """Qt worker 包裝器，透過 moveToThread 搬到 QThread 執行掃描。

    Signal 是跨執行緒通訊的唯一介面：
      progress → 更新進度條與步驟標籤
      finished → 掃描完成，帶回結果群組清單
      error    → 掃描引擎例外，帶回錯誤訊息字串
      detected → 磁碟偵測完成，帶回 DiskProfile.summary() 字串
    """
    progress = pyqtSignal(str, int, int, int, int, int)
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)
    detected = pyqtSignal(str)

    def __init__(self, roots: list[str], min_size: int):
        super().__init__()
        self._roots    = roots
        self._min_size = min_size
        self._scanner: Scanner | None = None

    def run(self):
        """在 worker thread 執行：先偵測磁碟類型，再建立並啟動 Scanner。"""
        from ..disk_detect import detect_disk_profile

        profile = detect_disk_profile(Path(self._roots[0]))
        self.detected.emit(profile.summary())

        self._scanner = Scanner(
            roots=self._roots,
            min_size=self._min_size,
            pass2_workers=profile.pass2_workers,
            pass3_workers=profile.pass3_workers,
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


# ── 現代化深色主題配色 ────────────────────────────────────────────────────────
BG        = "#0f1117"   # 視窗最底層背景
PANEL     = "#161922"   # 面板 / 輸入框背景
SURFACE   = "#1e222e"   # 卡片表面、按鈕一般狀態
SURFACE_HI= "#272c3a"   # 表面 hover / 次要強調
BORDER    = "#2a2f3c"   # 邊框
BORDER_HI = "#3a4150"   # 強調邊框
TEXT      = "#e4e7ee"   # 主文字
SUBTEXT   = "#9aa3b2"   # 次要文字
FAINT     = "#6b7280"   # 更淡的提示文字
ACCENT    = "#6c8cff"   # 主強調（靛藍）
ACCENT_HI = "#88a2ff"   # 主強調 hover
GREEN     = "#4ade80"   # 成功 / SSD
DANGER    = "#f87171"   # 危險 / 刪除
DANGER_HI = "#fb8a8a"
ORANGE    = "#fbbf24"   # 路徑 / 警示

# 全域 Qt StyleSheet
STYLE = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Segoe UI Variable", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}
QToolTip {{
    background-color: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 4px 8px;
}}
QLabel {{ background: transparent; }}

QPushButton {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{ background-color: {SURFACE_HI}; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background-color: {ACCENT}; color: {BG}; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {FAINT}; background-color: {PANEL}; }}

QPushButton#primary {{
    background-color: {ACCENT};
    color: #0b0d14;
    font-weight: 600;
    border: none;
}}
QPushButton#primary:hover {{ background-color: {ACCENT_HI}; }}
QPushButton#primary:pressed {{ background-color: #5a78e0; }}

QPushButton#danger {{
    background-color: {DANGER};
    color: #1a0d0d;
    font-weight: 600;
    border: none;
}}
QPushButton#danger:hover {{ background-color: {DANGER_HI}; }}
QPushButton#danger:disabled {{ background-color: {PANEL}; color: {FAINT}; }}

QLineEdit, QSpinBox, QComboBox {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #0b0d14;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {BORDER_HI}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid {SUBTEXT}; margin-right: 8px; }}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 8px;
    color: {TEXT};
    padding: 4px;
    selection-background-color: {SURFACE_HI};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none;
    background: {SURFACE}; }}
QSpinBox::up-button {{ border-top-right-radius: 7px; }}
QSpinBox::down-button {{ border-bottom-right-radius: 7px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {SURFACE_HI}; }}

QProgressBar {{
    background-color: {PANEL};
    border: none;
    border-radius: 6px;
    text-align: center;
    color: {TEXT};
    height: 10px;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 6px;
}}

QTreeWidget {{
    background-color: {PANEL};
    alternate-background-color: #13161e;
    border: 1px solid {BORDER};
    border-radius: 12px;
    outline: none;
    padding: 4px;
}}
QTreeWidget::item {{
    padding: 5px 4px;
    border-radius: 6px;
    color: {TEXT};
}}
QTreeWidget::item:hover {{ background-color: {SURFACE}; }}
QTreeWidget::item:selected {{ background-color: {SURFACE_HI}; color: {TEXT}; }}
QHeaderView::section {{
    background-color: {PANEL};
    color: {SUBTEXT};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px 10px;
    font-weight: 600;
    font-size: 12px;
}}
QTreeWidget::indicator, QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1.5px solid {BORDER_HI};
    border-radius: 5px;
    background: {PANEL};
}}
QTreeWidget::indicator:checked, QCheckBox::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
    image: url(none);
}}
QTreeWidget::indicator:indeterminate {{ background: {SURFACE_HI}; border-color: {ACCENT}; }}
QTreeWidget::branch {{ background: transparent; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_HI}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_HI}; border-radius: 5px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}

QStatusBar {{
    background-color: {PANEL};
    border-top: 1px solid {BORDER};
    color: {SUBTEXT};
}}
QStatusBar::item {{ border: none; }}

QCheckBox {{ spacing: 6px; color: {TEXT}; }}
QCheckBox:disabled {{ color: {FAINT}; }}
QCheckBox::indicator:disabled {{ background: {SURFACE}; border-color: {BORDER}; }}

QMenu {{
    background-color: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 6px 24px 6px 14px; border-radius: 6px; color: {TEXT}; }}
QMenu::item:selected {{ background-color: {ACCENT}; color: #0b0d14; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QMenu::item:disabled {{ color: {FAINT}; }}
"""

# 區段標題（側邊列可收折區段）共用樣式
_SECTION_HDR = (
    f"QPushButton{{text-align:left;background:{SURFACE};border:1px solid {BORDER};"
    f"border-radius:8px;color:{TEXT};font-weight:600;padding:8px 12px;font-size:12px;}}"
    f"QPushButton:hover{{background:{SURFACE_HI};border-color:{BORDER_HI};}}"
)
# 分段控制（segmented control：保留/刪除模式切換）的開/關樣式
_SEG_ON  = f"background:{ACCENT};color:#0b0d14;font-weight:600;border:none;border-radius:7px;padding:5px 12px;font-size:12px;"
_SEG_OFF = f"background:{SURFACE};color:{SUBTEXT};border:1px solid {BORDER};border-radius:7px;padding:5px 12px;font-size:12px;"


def _shadow(widget, blur=24, dy=4, alpha=70):
    """為 widget 加上柔和陰影，營造現代化層次感。"""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setXOffset(0)
    eff.setYOffset(dy)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)


def _badge(text: str, bg: str, fg: str = "#0b0d14") -> QLabel:
    """建立膠囊狀統計標籤（pill badge）。"""
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background:{bg};color:{fg};border-radius:10px;"
        f"padding:3px 12px;font-weight:600;font-size:12px;"
    )
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lbl


# ── ImagePreviewPopup（圖片懸停預覽浮動視窗）────────────────────────────────
class ImagePreviewPopup(QLabel):
    """滑鼠懸停在圖片檔名時彈出的縮圖預覽視窗。

    使用 ToolTip + FramelessWindowHint 讓視窗不出現在工作列，
    WA_ShowWithoutActivating 確保彈出時不搶走鍵盤焦點。
    快取機制：若 path 與上次相同且已顯示，只移動位置不重讀磁碟。
    """
    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"background:{PANEL};border:2px solid {ACCENT};border-radius:10px;padding:6px;"
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
        pix = pix.scaled(240, 240,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pix)
        self.resize(pix.width() + 16, pix.height() + 16)
        self.move(global_pos)
        self.show()

    def reset(self):
        self._last_path = ""
        self.hide()


# ── DuplicateTree（虛擬化結果樹）────────────────────────────────────────────
# 資料角色：在 column 0 上掛 python 物件，避免另外維護 item→物件對照表
_ROLE_KIND = Qt.ItemDataRole.UserRole       # "group" / "file"
_ROLE_OBJ  = Qt.ItemDataRole.UserRole + 1   # DuplicateGroup / FileInfo
_ROLE_POP  = Qt.ItemDataRole.UserRole + 2   # 群組是否已建立子列（lazy 標記）

_COL_NAME, _COL_PATH, _COL_SIZE, _COL_TIME = 0, 1, 2, 3


class DuplicateTree(QTreeWidget):
    """單一虛擬化樹狀結果檢視。

    - 頂層列 = 重複群組（摘要列），子列 = 群組內各檔案
    - 子列延遲建立：群組展開時才建立檔案列（lazy populate）
    - 勾選狀態存於 FileInfo.selected；群組列勾選框反映「子檔案已勾選比例」
    - checksChanged signal 在任何勾選變動時發出，供主視窗更新統計與刪除按鈕
    """
    checksChanged = pyqtSignal()

    def __init__(self, preview_popup: ImagePreviewPopup, parent=None):
        super().__init__(parent)
        self._preview = preview_popup
        self._groups: list[DuplicateGroup] = []
        self._font_size = 13
        self._guard = False   # 防止程式化 setCheckState 觸發的 itemChanged 遞迴

        self.setColumnCount(4)
        self.setHeaderLabels(["名稱", "路徑（雙擊開啟・點擊定位）", "大小", "修改時間"])
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(True)
        self.setExpandsOnDoubleClick(True)
        self.setIndentation(18)
        self.setAnimated(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        hdr = self.header()
        hdr.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(_COL_PATH, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(_COL_SIZE, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(_COL_TIME, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(_COL_NAME, 300)
        self.setColumnWidth(_COL_SIZE, 96)
        self.setColumnWidth(_COL_TIME, 140)

        self.itemChanged.connect(self._on_item_changed)
        self.itemExpanded.connect(self._on_expanded)
        self.itemClicked.connect(self._on_clicked)
        self.itemDoubleClicked.connect(self._on_double_clicked)
        self.customContextMenuRequested.connect(self._show_menu)

        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

        # 空狀態提示（覆蓋在 viewport 上，由 resizeEvent 維持置中）
        self._placeholder = QLabel("選擇資料夾並開始掃描，重複檔案會顯示在這裡", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(f"color:{FAINT};font-size:14px;background:transparent;")
        self._placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # ── 建立 / 重建 ───────────────────────────────────────────────────
    def populate(self, groups: list[DuplicateGroup]):
        """以 groups 重建樹。只建立群組頂層列；子列待展開時才建立。

        勾選狀態保存在 FileInfo.selected，因此排序/篩選後重建仍會反映既有選取。
        """
        self._preview.reset()
        self._guard = True
        self.clear()
        self._groups = groups
        self.setUpdatesEnabled(False)
        # 群組標題共用同一個 QFont（避免每列各配置一個，加速大量群組的建立）
        gfont = QFont(); gfont.setPixelSize(self._font_size + 1); gfont.setBold(True)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
        show_indicator = QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator

        # 先建立分離（detached）的 item，再以 addTopLevelItems 一次加入；
        # 避免逐一 addChild 觸發 view 的逐筆插入訊號，大量群組時快數倍。
        items = []
        for idx, g in enumerate(groups):
            gi = QTreeWidgetItem()
            gi.setData(0, _ROLE_KIND, "group")
            gi.setData(0, _ROLE_OBJ, g)
            gi.setText(_COL_NAME, self._group_caption(idx, g))
            gi.setFlags(flags)
            gi.setFont(_COL_NAME, gfont)
            gi.setChildIndicatorPolicy(show_indicator)
            items.append(gi)
        self.addTopLevelItems(items)
        # setFirstColumnSpanned / 初始勾選態需在 item 已屬於 view 後設定
        for gi, g in zip(items, groups):
            gi.setFirstColumnSpanned(True)
            self._set_group_state(gi, g)
        self.setUpdatesEnabled(True)
        self._guard = False
        self.setHeaderHidden(not groups)
        self._placeholder.setVisible(not groups)
        self._placeholder.raise_()
        self.checksChanged.emit()

    def set_placeholder(self, text: str):
        self._placeholder.setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 讓空狀態提示覆蓋於 viewport 區域並維持置中
        self._placeholder.setGeometry(self.viewport().geometry())

    def _group_caption(self, idx: int, g: DuplicateGroup) -> str:
        return (f"群組 #{idx + 1}      {len(g.files)} 個重複      "
                f"每個 {human_size(g.size)}      可省 {human_size(g.wasted_bytes)}")

    def _fill_file_item(self, item: QTreeWidgetItem, fi: FileInfo):
        """填入單一檔案子列的內容與樣式。"""
        item.setData(0, _ROLE_KIND, "file")
        item.setData(0, _ROLE_OBJ, fi)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(_COL_NAME,
            Qt.CheckState.Checked if fi.selected else Qt.CheckState.Unchecked)

        item.setText(_COL_NAME, fi.name)
        item.setForeground(_COL_NAME, QColor(ACCENT))
        item.setText(_COL_PATH, fi.folder)
        item.setForeground(_COL_PATH, QColor(ORANGE))
        item.setText(_COL_SIZE, human_size(fi.size))
        item.setForeground(_COL_SIZE, QColor(SUBTEXT))
        mt = datetime.datetime.fromtimestamp(fi.mtime).strftime("%Y-%m-%d %H:%M")
        item.setText(_COL_TIME, mt)
        item.setForeground(_COL_TIME, QColor(SUBTEXT))

        nf = QFont(); nf.setPixelSize(self._font_size)
        item.setFont(_COL_NAME, nf)
        item.setFont(_COL_PATH, nf)
        item.setToolTip(_COL_NAME, f"雙擊以預設程式開啟：{fi.path}")
        item.setToolTip(_COL_PATH, f"點擊在檔案總管中定位：{fi.path}")

    def _on_expanded(self, item: QTreeWidgetItem):
        """群組首次展開時才建立子列（lazy）。"""
        if item.data(0, _ROLE_KIND) != "group" or item.data(0, _ROLE_POP):
            return
        g: DuplicateGroup = item.data(0, _ROLE_OBJ)
        self._guard = True
        item.takeChildren()
        for fi in g.files:
            self._fill_file_item(QTreeWidgetItem(item), fi)
        item.setData(0, _ROLE_POP, True)
        self._guard = False

    # ── 勾選狀態管理 ──────────────────────────────────────────────────
    def _on_item_changed(self, item: QTreeWidgetItem, col: int):
        if self._guard or col != _COL_NAME:
            return
        kind = item.data(0, _ROLE_KIND)
        if kind == "file":
            fi: FileInfo = item.data(0, _ROLE_OBJ)
            fi.selected = (item.checkState(_COL_NAME) == Qt.CheckState.Checked)
            parent = item.parent()
            if parent is not None:
                self._set_group_state(parent, parent.data(0, _ROLE_OBJ))
            self.checksChanged.emit()
        elif kind == "group":
            g: DuplicateGroup = item.data(0, _ROLE_OBJ)
            state = item.checkState(_COL_NAME)
            if state == Qt.CheckState.PartiallyChecked:
                return   # 程式化設定的中間態，非使用者操作
            sel = (state == Qt.CheckState.Checked)
            for fi in g.files:
                fi.selected = sel
            self._sync_children(item, sel)
            self.checksChanged.emit()

    def _set_group_state(self, gi: QTreeWidgetItem, g: DuplicateGroup):
        """依群組內已勾選比例設定群組列的三態勾選框（不發 signal）。"""
        n = len(g.files)
        s = sum(1 for fi in g.files if fi.selected)
        state = (Qt.CheckState.Unchecked if s == 0 else
                 Qt.CheckState.Checked if s == n else
                 Qt.CheckState.PartiallyChecked)
        prev = self._guard
        self._guard = True
        gi.setCheckState(_COL_NAME, state)
        self._guard = prev

    def _sync_children(self, gi: QTreeWidgetItem, sel: bool):
        """將群組列下已建立的檔案子列勾選框同步為 sel。"""
        prev = self._guard
        self._guard = True
        for i in range(gi.childCount()):
            ch = gi.child(i)
            if ch.data(0, _ROLE_KIND) == "file":
                ch.setCheckState(_COL_NAME,
                    Qt.CheckState.Checked if sel else Qt.CheckState.Unchecked)
        self._guard = prev

    def refresh_checks(self):
        """資料層 selected 被批次修改後，刷新所有群組列與其已建立子列。"""
        for i in range(self.topLevelItemCount()):
            gi = self.topLevelItem(i)
            g = gi.data(0, _ROLE_OBJ)
            self._set_group_state(gi, g)
            if gi.data(0, _ROLE_POP):
                self._guard = True
                for j in range(gi.childCount()):
                    ch = gi.child(j)
                    fi = ch.data(0, _ROLE_OBJ)
                    if fi is not None:
                        ch.setCheckState(_COL_NAME,
                            Qt.CheckState.Checked if fi.selected
                            else Qt.CheckState.Unchecked)
                self._guard = False
        self.checksChanged.emit()

    # ── 批次選取操作（作用於資料層，再刷新檢視）──────────────────────
    def select_all(self, sel: bool):
        for g in self._groups:
            for fi in g.files:
                fi.selected = sel
        self.refresh_checks()

    def apply_conditions_all(self, conditions: list, mode: str):
        for g in self._groups:
            _apply_selection(g, conditions, mode)
        self.refresh_checks()

    def selected_stats(self) -> tuple[int, int]:
        """回傳 (已勾選檔案數, 可釋放 bytes)。"""
        n = b = 0
        for g in self._groups:
            for fi in g.files:
                if fi.selected:
                    n += 1; b += fi.size
        return n, b

    # ── 滑鼠互動 ──────────────────────────────────────────────────────
    def _on_clicked(self, item: QTreeWidgetItem, col: int):
        if item.data(0, _ROLE_KIND) == "file" and col == _COL_PATH:
            self._locate(item.data(0, _ROLE_OBJ))

    def _on_double_clicked(self, item: QTreeWidgetItem, col: int):
        if item.data(0, _ROLE_KIND) == "file":
            self._open(item.data(0, _ROLE_OBJ))

    def _open(self, fi: FileInfo):
        try:
            os.startfile(str(fi.path))
            logger.info(f"開啟檔案: {fi.path}")
        except Exception as e:
            logger.warning(f"無法開啟 {fi.path}: {e}")
            QMessageBox.warning(self, "無法開啟", f"無法開啟檔案：\n{fi.path}\n\n{e}")

    def _locate(self, fi: FileInfo):
        try:
            subprocess.Popen(f'explorer /select,"{fi.path}"', shell=True)
            logger.info(f"在檔案總管定位: {fi.path}")
        except Exception as e:
            logger.warning(f"無法定位 {fi.path}: {e}")
            QMessageBox.warning(self, "無法開啟", f"無法開啟資料夾：\n{fi.folder}\n\n{e}")

    def eventFilter(self, obj, event):
        """攔截 viewport 滑鼠移動以顯示圖片預覽。"""
        if obj is self.viewport():
            et = event.type()
            if et == QEvent.Type.MouseMove:
                pos = event.pos()
                item = self.itemAt(pos)
                col = self.columnAt(pos.x())
                shown = False
                if item is not None and item.data(0, _ROLE_KIND) == "file" \
                        and col == _COL_NAME:
                    fi: FileInfo = item.data(0, _ROLE_OBJ)
                    if fi.path.suffix.lower() in IMAGE_EXTENSIONS and fi.path.exists():
                        gp = self.viewport().mapToGlobal(pos)
                        self._preview.show_image(str(fi.path), gp + QPoint(20, 12))
                        shown = True
                if not shown:
                    self._preview.reset()
            elif et == QEvent.Type.Leave:
                self._preview.reset()
        return super().eventFilter(obj, event)

    # ── 右鍵選單（快速選取 / 開啟 / 定位）─────────────────────────────
    def _show_menu(self, pos: QPoint):
        item = self.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, _ROLE_KIND)
        group_item = item if kind == "group" else item.parent()
        if group_item is None:
            return
        g: DuplicateGroup = group_item.data(0, _ROLE_OBJ)

        menu = QMenu(self)
        if kind == "file":
            fi: FileInfo = item.data(0, _ROLE_OBJ)
            act_open = menu.addAction("開啟檔案")
            act_open.triggered.connect(lambda: self._open(fi))
            act_loc = menu.addAction("在檔案總管中顯示")
            act_loc.triggered.connect(lambda: self._locate(fi))
            menu.addSeparator()

        # 快速選取子選單（保留某條件勝出者，其餘標記待刪）
        quick = menu.addMenu("快速選取（保留…）")
        for cid in ("newer", "older", "sh_path", "shallow", "lo_path"):
            label = _COND_BY_ID[cid][1].replace("修改", "修改").replace("路徑", "路徑")
            act = quick.addAction(f"保留{_COND_BY_ID[cid][1]}者")
            cond = [(cid, _COND_BY_ID[cid][2], _COND_BY_ID[cid][3])]
            act.triggered.connect(
                lambda _, gg=g, gi=group_item, c=cond: self._quick_group(gi, gg, c))
        menu.addSeparator()
        a_all = menu.addAction("勾選此群組全部")
        a_all.triggered.connect(lambda: self._toggle_group(group_item, g, True))
        a_none = menu.addAction("取消此群組全部")
        a_none.triggered.connect(lambda: self._toggle_group(group_item, g, False))
        menu.exec(self.viewport().mapToGlobal(pos))

    def _quick_group(self, gi: QTreeWidgetItem, g: DuplicateGroup, conditions: list):
        _apply_selection(g, conditions, "keep")
        self._set_group_state(gi, g)
        if gi.data(0, _ROLE_POP):
            self._guard = True
            for j in range(gi.childCount()):
                ch = gi.child(j)
                fi = ch.data(0, _ROLE_OBJ)
                if fi is not None:
                    ch.setCheckState(_COL_NAME,
                        Qt.CheckState.Checked if fi.selected else Qt.CheckState.Unchecked)
            self._guard = False
        self.checksChanged.emit()

    def _toggle_group(self, gi: QTreeWidgetItem, g: DuplicateGroup, sel: bool):
        for fi in g.files:
            fi.selected = sel
        self._set_group_state(gi, g)
        self._sync_children(gi, sel)
        self.checksChanged.emit()

    # ── 字體大小 ──────────────────────────────────────────────────────
    def set_font_size(self, size: int):
        self._font_size = size
        self.populate(self._groups)   # 重建以套用新字級（collapsed，成本低）


# ── MainWindow（主視窗）──────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    """應用程式主視窗，負責掃描流程管理、結果篩選排序、批次選取、刪除與報表匯出。

    掃描 Race Condition 防護：每次 _start_scan 遞增 _scan_id；thread.finished
    以 lambda 捕捉當時 scan_id，_on_thread_finished 比對不符則忽略舊 signal。
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DupeScan — 重複檔案掃描工具")
        self.resize(1320, 840)
        self.setMinimumSize(960, 600)

        self._all_groups: list[DuplicateGroup] = []   # 完整未篩選結果
        self._groups:     list[DuplicateGroup] = []   # 目前顯示（篩選/排序後）

        self._thread:  QThread | None    = None
        self._worker:  ScanWorker | None = None
        self._scanning: bool = False
        self._paused:   bool = False
        self._scan_id:  int  = 0

        self._global_qs_mode: str  = "keep"
        self._sidebar_open:   bool = True
        self._sb_filter_open: bool = True
        self._sb_qs_open:     bool = True

        self._preview_popup = ImagePreviewPopup()

        self._build_ui()
        self.setStyleSheet(STYLE)

    # ── UI 建構 ───────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(16, 14, 16, 10)
        main.setSpacing(12)

        main.addWidget(self._build_header())
        main.addWidget(self._build_toolbar())
        main.addWidget(self._build_progress())
        main.addWidget(self._build_summary())

        # 主內容區：[側邊列][切換條][結果樹]
        content = QWidget()
        cl = QHBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self._sidebar_widget = self._build_sidebar()
        cl.addWidget(self._sidebar_widget)

        self._sidebar_tab = QPushButton("◀")
        self._sidebar_tab.setFixedWidth(16)
        self._sidebar_tab.setToolTip("展開/收起功能列")
        self._sidebar_tab.setStyleSheet(
            f"QPushButton{{background:{SURFACE};border:none;color:{SUBTEXT};"
            f"font-size:9px;border-radius:0;}}"
            f"QPushButton:hover{{background:{ACCENT};color:{BG};}}"
        )
        self._sidebar_tab.clicked.connect(self._toggle_sidebar)
        cl.addWidget(self._sidebar_tab)

        self.tree = DuplicateTree(self._preview_popup)
        self.tree.checksChanged.connect(self._update_selection_ui)
        self.tree.setHeaderHidden(True)   # 初始無結果時隱藏表頭
        cl.addWidget(self.tree, 1)

        main.addWidget(content, 1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就緒")

    def _build_header(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 0, 2, 0)
        title = QLabel("DupeScan")
        title.setStyleSheet(
            f"font-size:23px;font-weight:800;color:{TEXT};letter-spacing:0.5px;")
        sub = QLabel("重複檔案掃描工具")
        sub.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        lay.addWidget(title)
        lay.addSpacing(10)
        lay.addWidget(sub)
        lay.addStretch()
        return w

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setStyleSheet(
            f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:12px;}}")
        _shadow(bar, blur=20, dy=3, alpha=60)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)

        lbl = QLabel("路徑")
        lbl.setStyleSheet(f"color:{SUBTEXT};")
        row.addWidget(lbl)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("選擇要掃描的資料夾或磁碟機…")
        self.path_edit.setMinimumWidth(220)
        self.path_edit.returnPressed.connect(self._toggle_scan)
        row.addWidget(self.path_edit, 1)

        browse_btn = QPushButton("瀏覽")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(browse_btn)

        row.addSpacing(6)
        min_lbl = QLabel("最小")
        min_lbl.setStyleSheet(f"color:{SUBTEXT};")
        row.addWidget(min_lbl)
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 1_000_000)
        self.min_size_spin.setValue(1)
        self.min_size_spin.setSuffix(" KB")
        self.min_size_spin.setFixedWidth(96)
        row.addWidget(self.min_size_spin)

        row.addSpacing(10)
        self.btn_reset = QPushButton("重置")
        self.btn_reset.setToolTip("停止掃描並清除所有結果")
        self.btn_reset.clicked.connect(self._reset_all)
        row.addWidget(self.btn_reset)

        self.btn_pause = QPushButton("⏸  暫停")
        self.btn_pause.setVisible(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        row.addWidget(self.btn_pause)

        self.btn_scan = QPushButton("▶  開始掃描")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.setMinimumWidth(120)
        self.btn_scan.clicked.connect(self._toggle_scan)
        row.addWidget(self.btn_scan)
        return bar

    def _build_progress(self) -> QWidget:
        self.progress_section = QFrame()
        self.progress_section.setStyleSheet(
            f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:12px;}}")
        pl = QVBoxLayout(self.progress_section)
        pl.setContentsMargins(14, 10, 14, 12)
        pl.setSpacing(6)

        top = QHBoxLayout()
        self.step_label = QLabel("")
        self.step_label.setStyleSheet(f"color:{ACCENT};font-weight:600;font-size:13px;")
        self.eta_label = QLabel("")
        self.eta_label.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        self.disk_label = QLabel("")
        self.disk_label.setStyleSheet(
            f"color:{GREEN};font-size:11px;background:{SURFACE};"
            f"border:1px solid {BORDER};border-radius:8px;padding:2px 10px;")
        top.addWidget(self.step_label)
        top.addStretch()
        top.addWidget(self.disk_label)
        top.addSpacing(8)
        top.addWidget(self.eta_label)
        pl.addLayout(top)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        pl.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color:{SUBTEXT};font-size:12px;")
        pl.addWidget(self.progress_label)

        self.progress_section.setVisible(False)
        return self.progress_section

    def _build_summary(self) -> QWidget:
        self.summary_widget = QFrame()
        self.summary_widget.setStyleSheet(
            f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:12px;}}")
        sl = QHBoxLayout(self.summary_widget)
        sl.setContentsMargins(14, 10, 14, 10)
        sl.setSpacing(8)

        self.lbl_groups   = _badge("0 個群組", ACCENT)
        self.lbl_wasted   = _badge("可省 0 B", DANGER)
        self.lbl_selected = _badge("已選 0", SURFACE_HI, TEXT)
        sl.addWidget(self.lbl_groups)
        sl.addWidget(self.lbl_wasted)
        sl.addWidget(self.lbl_selected)
        sl.addStretch()

        self.btn_export = QPushButton("📊  匯出報表")
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip("分析重複檔案分布並匯出 HTML 報表（含圓餅圖）")
        self.btn_export.clicked.connect(self._export_report)
        self.btn_delete = QPushButton("🗑  刪除已勾選")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)
        sl.addWidget(self.btn_export)
        sl.addWidget(self.btn_delete)

        self.summary_widget.setVisible(False)
        return self.summary_widget

    def _build_sidebar(self) -> QWidget:
        sb = QFrame()
        sb.setFixedWidth(258)
        sb.setStyleSheet(
            f"QFrame#sb{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;}}")
        sb.setObjectName("sb")
        lay = QVBoxLayout(sb)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        hdr = QLabel("功能列")
        hdr.setStyleSheet(
            f"font-size:13px;font-weight:700;color:{TEXT};padding-bottom:2px;")
        lay.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 4, 0)
        bl.setSpacing(8)

        # ── 區段：篩選與排序 ──────────────────────────────────────────
        self._sb_filter_hdr = QPushButton("▾  篩選與排序")
        self._sb_filter_hdr.setStyleSheet(_SECTION_HDR)
        self._sb_filter_hdr.clicked.connect(self._toggle_filter_panel)
        bl.addWidget(self._sb_filter_hdr)

        self._sb_filter_body = QWidget()
        fb = QVBoxLayout(self._sb_filter_body)
        fb.setContentsMargins(4, 2, 4, 4)
        fb.setSpacing(6)

        fb.addWidget(self._mini_label("副檔名篩選"))
        self.ext_filter_edit = QLineEdit()
        self.ext_filter_edit.setPlaceholderText("jpg, png…（留空＝全部）")
        self.ext_filter_edit.textChanged.connect(self._apply_filter_sort)
        fb.addWidget(self.ext_filter_edit)

        fb.addWidget(self._mini_label("排序方式"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "群組大小（大→小）", "群組大小（小→大）",
            "檔案數量（多→少）", "檔案數量（少→多）",
            "副檔名 A→Z",        "副檔名 Z→A",
        ])
        self.sort_combo.currentIndexChanged.connect(self._apply_filter_sort)
        fb.addWidget(self.sort_combo)

        fb.addWidget(self._mini_label("內容文字大小"))
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(9, 18)
        self.font_size_spin.setValue(13)
        self.font_size_spin.setSuffix(" px")
        self.font_size_spin.valueChanged.connect(self._apply_font_size)
        fb.addWidget(self.font_size_spin)
        bl.addWidget(self._sb_filter_body)

        # ── 區段：批量快速選取 ────────────────────────────────────────
        self._sb_qs_hdr = QPushButton("▾  批量快速選取")
        self._sb_qs_hdr.setStyleSheet(_SECTION_HDR)
        self._sb_qs_hdr.clicked.connect(self._toggle_qs_panel)
        bl.addWidget(self._sb_qs_hdr)

        self._sb_qs_body = QWidget()
        qb = QVBoxLayout(self._sb_qs_body)
        qb.setContentsMargins(4, 2, 4, 4)
        qb.setSpacing(6)

        qb.addWidget(self._mini_label("操作模式"))
        gm = QHBoxLayout(); gm.setSpacing(6)
        self._g_btn_keep = QPushButton("保留目標")
        self._g_btn_keep.clicked.connect(lambda: self._set_global_mode("keep"))
        self._g_btn_delete = QPushButton("刪除目標")
        self._g_btn_delete.clicked.connect(lambda: self._set_global_mode("delete"))
        gm.addWidget(self._g_btn_keep)
        gm.addWidget(self._g_btn_delete)
        qb.addLayout(gm)
        self._update_global_mode_btns()

        qb.addWidget(self._mini_label("依條件套用（可疊加，依勾選順序）"))
        self._global_cond = ConditionPanel()
        qb.addWidget(self._global_cond)

        btn_apply_all = QPushButton("套用到所有群組")
        btn_apply_all.setObjectName("primary")
        btn_apply_all.clicked.connect(self._global_apply_conditions)
        qb.addWidget(btn_apply_all)

        row2 = QHBoxLayout(); row2.setSpacing(6)
        btn_all = QPushButton("全部勾選")
        btn_all.clicked.connect(lambda: self.tree.select_all(True))
        btn_none = QPushButton("全部取消")
        btn_none.clicked.connect(lambda: self.tree.select_all(False))
        row2.addWidget(btn_all)
        row2.addWidget(btn_none)
        qb.addLayout(row2)
        bl.addWidget(self._sb_qs_body)

        bl.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        return sb

    def _mini_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size:11px;color:{SUBTEXT};")
        return lbl

    # ── 側邊列展開/收折 ───────────────────────────────────────────────
    def _toggle_sidebar(self):
        self._sidebar_open = not self._sidebar_open
        self._sidebar_widget.setVisible(self._sidebar_open)
        self._sidebar_tab.setText("◀" if self._sidebar_open else "▶")

    def _toggle_filter_panel(self):
        self._sb_filter_open = not self._sb_filter_open
        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_filter_hdr.setText(
            "▾  篩選與排序" if self._sb_filter_open else "▸  篩選與排序")

    def _toggle_qs_panel(self):
        self._sb_qs_open = not self._sb_qs_open
        self._sb_qs_body.setVisible(self._sb_qs_open)
        self._sb_qs_hdr.setText(
            "▾  批量快速選取" if self._sb_qs_open else "▸  批量快速選取")

    # ── 全局快速選取 ──────────────────────────────────────────────────
    def _set_global_mode(self, mode: str):
        self._global_qs_mode = mode
        self._update_global_mode_btns()

    def _update_global_mode_btns(self):
        keep = self._global_qs_mode == "keep"
        self._g_btn_keep.setStyleSheet(_SEG_ON if keep else _SEG_OFF)
        self._g_btn_delete.setStyleSheet(_SEG_OFF if keep else _SEG_ON)

    def _global_apply_conditions(self):
        conditions = self._global_cond.get_active()
        if not conditions:
            QMessageBox.information(self, "提示", "請先勾選至少一個條件。")
            return
        self.tree.apply_conditions_all(conditions, self._global_qs_mode)

    # ── 文字大小調整 ──────────────────────────────────────────────────
    def _apply_font_size(self, size: int):
        if self._groups:
            self.tree.set_font_size(size)
        else:
            self.tree._font_size = size

    # ── 篩選 / 排序 ───────────────────────────────────────────────────
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

        sort_opts = [
            (lambda g: g.size, True),
            (lambda g: g.size, False),
            (lambda g: len(g.files), True),
            (lambda g: len(g.files), False),
            (lambda g: g.files[0].path.suffix.lower() if g.files else '', False),
            (lambda g: g.files[0].path.suffix.lower() if g.files else '', True),
        ]
        key_fn, rev = sort_opts[self.sort_combo.currentIndex()]
        groups.sort(key=key_fn, reverse=rev)
        self._groups = groups
        self.tree.populate(groups)

    # ── 操作動作 ──────────────────────────────────────────────────────
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
        self._worker.detected.connect(self._on_disk_detected)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(
            lambda sid=current_scan_id: self._on_thread_finished(sid))
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
            self.btn_pause.setText("⏸  暫停")
            self.btn_pause.setStyleSheet("")
            self.step_label.setStyleSheet(f"color:{ACCENT};font-weight:600;font-size:13px;")
            self.status_bar.showMessage("掃描繼續中…")
            logger.info("掃描已繼續")
        else:
            self._worker.pause()
            self._paused = True
            self.btn_pause.setText("▶  繼續")
            self.btn_pause.setStyleSheet(
                f"background:{GREEN};color:#0b0d14;font-weight:600;border:none;"
                f"border-radius:8px;padding:6px 14px;")
            self.step_label.setStyleSheet(f"color:{GREEN};font-weight:600;font-size:13px;")
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
        self.disk_label.setText("")
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
            self.btn_scan.setObjectName("danger")
            self.btn_pause.setVisible(True)
            self.btn_pause.setText("⏸  暫停")
            self.btn_pause.setStyleSheet("")
        else:
            self.btn_scan.setText("▶  開始掃描")
            self.btn_scan.setObjectName("primary")
            self.btn_pause.setVisible(False)
        # objectName 變更後重新 polish，讓 #primary / #danger 樣式即時生效
        self.btn_scan.style().unpolish(self.btn_scan)
        self.btn_scan.style().polish(self.btn_scan)
        self.btn_scan.setEnabled(True)

    # ── Signal 接收者（Slots）────────────────────────────────────────
    def _on_thread_finished(self, scan_id: int):
        if scan_id != self._scan_id: return
        self._thread = None
        self._worker = None
        self._paused = False
        if self._scanning:
            self._set_scanning(False)
            self.progress_section.setVisible(False)
            self.progress_label.setText("")
            self.disk_label.setText("")
            self.step_label.setText("")
            self.eta_label.setText("")
            self.status_bar.showMessage("掃描已取消")

    def _on_disk_detected(self, summary: str):
        self.disk_label.setText(f"💽 {summary}")
        self.status_bar.showMessage(f"磁碟類型: {summary}")
        logger.info(f"磁碟偵測: {summary}")

    def _on_progress(self, msg, cur, total, step, total_steps, eta_secs):
        if not self._scanning: return
        step_name = STEP_NAMES.get(step, f"步驟 {step}")
        self.step_label.setText(f"步驟 {step}/{total_steps} · {step_name}")
        if eta_secs == 0:
            self.eta_label.setText("完成")
        elif eta_secs > 0:
            self.eta_label.setText(f"預估剩餘 {_format_eta(eta_secs)}")
        else:
            self.eta_label.setText("預估剩餘 計算中…")
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
        self.disk_label.setText("")
        self.step_label.setText("")
        self.eta_label.setText("")
        self._all_groups = groups

        if not groups:
            logger.info("掃描完成 — 未發現重複檔案")
            self.status_bar.showMessage("掃描完成 — 未發現重複檔案！")
            self.tree.set_placeholder("✅  未發現重複檔案")
            self.tree.populate([])
            return

        total_wasted = sum(g.wasted_bytes for g in groups)
        logger.info(f"掃描完成 — {len(groups)} 個重複群組，共浪費 {human_size(total_wasted)}")

        self.lbl_groups.setText(f"{len(groups)} 個群組")
        self.lbl_wasted.setText(f"可省 {human_size(total_wasted)}")
        self.summary_widget.setVisible(True)
        self.btn_export.setEnabled(True)
        self.tree.setHeaderHidden(False)

        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_qs_body.setVisible(self._sb_qs_open)

        self._apply_filter_sort()
        self.status_bar.showMessage(
            f"掃描完成 — 找到 {len(groups)} 個重複群組，可省 {human_size(total_wasted)}")

    def _on_error(self, msg: str):
        if not self._scanning: return
        self._set_scanning(False)
        self._paused = False
        self.progress_section.setVisible(False)
        self.progress_label.setText("")
        self.disk_label.setText("")
        logger.error(f"掃描錯誤: {msg}")
        QMessageBox.critical(self, "掃描錯誤", msg)
        self.status_bar.showMessage(f"錯誤: {msg}")

    def _clear_results(self):
        self._preview_popup.reset()
        self.tree.set_placeholder("選擇資料夾並開始掃描，重複檔案會顯示在這裡")
        self.tree.populate([])
        self._groups = []
        self._all_groups = []
        self.btn_delete.setEnabled(False)
        self.btn_export.setEnabled(False)

    # ── 勾選統計 UI 更新 ──────────────────────────────────────────────
    def _update_selection_ui(self):
        n, b = self.tree.selected_stats()
        self.lbl_selected.setText(f"已選 {n}")
        self.btn_delete.setText(f"🗑  刪除已勾選（{n}）" if n else "🗑  刪除已勾選")
        self.btn_delete.setEnabled(n > 0)

    def _delete_selected(self):
        to_delete: list[Path] = [
            fi.path for g in self.tree._groups for fi in g.files if fi.selected]

        if not to_delete:
            QMessageBox.information(self, "提示", "請先勾選要刪除的檔案。"); return

        msg = f"確定要刪除以下 {len(to_delete)} 個檔案？\n\n"
        msg += "\n".join(str(p) for p in to_delete[:20])
        if len(to_delete) > 20:
            msg += f"\n… 另外 {len(to_delete) - 20} 個檔案"

        reply = QMessageBox.question(
            self, "確認刪除", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
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
            f"成功刪除 {success} 個檔案" + (f"，失敗 {fail} 個" if fail else ""))
        self.status_bar.showMessage(f"已刪除 {success} 個檔案")
        if not self._scanning:
            self._start_scan()

    # ── 報表匯出 ──────────────────────────────────────────────────────
    def _make_pie_svg(self, slices: list, title: str = "") -> str:
        """依 [(label, value), ...] 產生帶圖例的 SVG 圓餅圖字串。"""
        COLORS = [
            "#6c8cff", "#4ade80", "#fbbf24", "#f87171", "#c084fc",
            "#34d399", "#f0abfc", "#60a5fa", "#a3e635", "#fb923c",
            "#2a2f3c", "#3a4150", "#9aa3b2", "#6b7280", "#e4e7ee",
        ]
        total = sum(v for _, v in slices)
        if total == 0:
            return "<p style='color:#9aa3b2'>無資料</p>"

        W, H = 520, 340
        cx, cy, r = 165, 170, 140
        lines = [f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">']
        if title:
            lines.append(
                f'<text x="{W // 2}" y="16" text-anchor="middle" '
                f'font-size="13" fill="#e4e7ee" font-weight="bold">{title}</text>')
        start = -math.pi / 2
        legend_y = 36
        for i, (label, value) in enumerate(slices):
            if value == 0:
                continue
            color = COLORS[i % len(COLORS)]
            ang = 2 * math.pi * value / total
            end = start + ang
            x1 = cx + r * math.cos(start); y1 = cy + r * math.sin(start)
            x2 = cx + r * math.cos(end);   y2 = cy + r * math.sin(end)
            large = 1 if ang > math.pi else 0
            path = (f"M{cx},{cy} L{x1:.1f},{y1:.1f} "
                    f"A{r},{r} 0 {large},1 {x2:.1f},{y2:.1f} Z")
            lines.append(f'<path d="{path}" fill="{color}" stroke="#0f1117" stroke-width="1.5"/>')
            pct = value / total * 100
            if pct > 4:
                mid = start + ang / 2
                lx = cx + r * 0.65 * math.cos(mid)
                ly = cy + r * 0.65 * math.sin(mid)
                lines.append(
                    f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                    f'dominant-baseline="middle" font-size="10" fill="white">{pct:.1f}%</text>')
            lines.append(
                f'<rect x="325" y="{legend_y - 10}" width="13" height="13" '
                f'fill="{color}" rx="3"/>')
            short = (label[:12] + "…") if len(label) > 13 else label
            lines.append(
                f'<text x="344" y="{legend_y}" font-size="11" fill="#e4e7ee">'
                f'{short} ({pct:.1f}%)</text>')
            legend_y += 20
            start = end

        lines.append("</svg>")
        return "\n".join(lines)

    def _export_report(self):
        """彙整副檔名統計，生成兩張圓餅圖，輸出 HTML 報表並以瀏覽器開啟。"""
        if not self._all_groups:
            QMessageBox.information(self, "提示", "請先執行掃描以取得資料。")
            return

        ext_data: dict = defaultdict(lambda: {
            "group_hashes": set(), "files": 0, "total_size": 0, "wasted": 0})
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
            key=lambda x: x[1]["wasted"], reverse=True)

        total_groups = len(self._all_groups)
        total_files  = sum(len(g.files) for g in self._all_groups)
        total_size   = sum(g.size * len(g.files) for g in self._all_groups)
        total_wasted = sum(g.wasted_bytes for g in self._all_groups)

        pie_wasted = self._make_pie_svg(
            [(ext, d["wasted"]) for ext, d in rows if d["wasted"] > 0],
            "浪費空間分布（依副檔名）")
        pie_count = self._make_pie_svg(
            [(ext, d["files"]) for ext, d in rows if d["files"] > 0],
            "重複檔案數量分布（依副檔名）")

        scan_path = self.path_edit.text()
        scan_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        tbl_rows = ""
        for ext, d in rows:
            pct = d["wasted"] / total_wasted * 100 if total_wasted > 0 else 0
            tbl_rows += (
                f"<tr><td><strong>{ext}</strong></td>"
                f"<td>{d['groups']}</td><td>{d['files']}</td>"
                f"<td>{human_size(d['total_size'])}</td>"
                f"<td style='color:#f87171'>{human_size(d['wasted'])}</td>"
                f"<td><div style='display:flex;align-items:center;gap:8px;'>"
                f"<div class='pb'><div class='pf' style='width:{min(pct,100):.1f}%'></div></div>"
                f"<span>{pct:.1f}%</span></div></td></tr>\n")

        html = f"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8"><title>DupeScan 分析報表</title>
<style>
*{{box-sizing:border-box;}}
body{{font-family:"Segoe UI",sans-serif;background:#0f1117;color:#e4e7ee;margin:0;padding:28px;}}
h1{{color:#6c8cff;font-size:23px;margin-bottom:4px;}}
h2{{color:#6c8cff;font-size:15px;margin-top:28px;border-bottom:1px solid #2a2f3c;padding-bottom:6px;}}
.meta{{color:#9aa3b2;font-size:12px;margin-bottom:20px;}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px;}}
.card{{background:#161922;border:1px solid #2a2f3c;border-radius:12px;padding:18px;text-align:center;}}
.val{{font-size:21px;font-weight:bold;color:#6c8cff;}}
.lbl{{font-size:11px;color:#9aa3b2;margin-top:4px;}}
.charts{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;}}
.cbox{{background:#161922;border:1px solid #2a2f3c;border-radius:12px;padding:16px;}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;}}
th{{background:#1e222e;padding:10px 12px;text-align:left;font-size:11px;color:#9aa3b2;}}
td{{padding:9px 12px;border-bottom:1px solid #1e222e;}}
tr:hover td{{background:#1a1e28;}}
.pb{{background:#1e222e;border-radius:4px;height:8px;width:110px;}}
.pf{{background:#6c8cff;height:8px;border-radius:4px;}}
footer{{color:#6b7280;font-size:11px;margin-top:30px;}}
</style></head><body>
<h1>DupeScan 分析報表</h1>
<div class="meta">掃描路徑：{scan_path}&nbsp;&nbsp;|&nbsp;&nbsp;產生時間：{scan_time}</div>
<div class="grid">
  <div class="card"><div class="val">{total_groups}</div><div class="lbl">重複群組數</div></div>
  <div class="card"><div class="val">{total_files}</div><div class="lbl">重複檔案總數</div></div>
  <div class="card"><div class="val" style="color:#f87171">{human_size(total_wasted)}</div><div class="lbl">可釋放空間</div></div>
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
            encoding="utf-8", prefix="dupescan_report_") as f:
            f.write(html)
            tmp_path = f.name

        webbrowser.open(f"file:///{tmp_path.replace(os.sep, '/')}")
        self.status_bar.showMessage(f"報表已匯出並開啟: {tmp_path}")
        logger.info(f"匯出報表: {tmp_path}")


# ── ConditionPanel（側邊列批量快速選取用的多條件勾選面板）────────────────────
class ConditionPanel(QWidget):
    """可重用的條件勾選面板，以 _checked_order 記錄勾選順序作為優先序依據。

    互斥機制：勾選某條件時，衝突條件的 QCheckBox 會被 disable，
    取消時再恢復可用，避免矛盾條件同時生效（例如「較新」和「較舊」）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked_order: list[str] = []
        self._checkboxes: dict[str, QCheckBox] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)

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
        cb = QCheckBox(_COND_BY_ID[cond_id][1])
        cb.setStyleSheet(f"font-size:12px;color:{TEXT};min-width:78px;")
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
        return [(cid, _COND_BY_ID[cid][2], _COND_BY_ID[cid][3])
                for cid in self._checked_order]

    def clear_all(self):
        for cb in self._checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.setEnabled(True)
            cb.blockSignals(False)
        self._checked_order.clear()
