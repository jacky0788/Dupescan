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
import functools
from html import escape
import math
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path

try:
    import humanize
    _HZ = True
except ImportError:
    _HZ = False  # 未安裝時退回自製格式化函式

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QObject, QEvent, QPoint, QSettings
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QProgressDialog,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QProgressBar, QScrollArea,
    QSizePolicy, QSpinBox, QStatusBar, QStyledItemDelegate, QVBoxLayout, QWidget,
    QTreeWidget, QTreeWidgetItem, QMenu, QGraphicsDropShadowEffect,
)

from .. import __version__
from ..app_paths import data_dir
from ..delete_plan import SKIP, build_plan
from . import delete_dialog
from .theme import (
    BG, PANEL, SURFACE, SURFACE_HI, BORDER, BORDER_HI, TEXT, SUBTEXT, FAINT,
    ACCENT, GREEN, DANGER, ORANGE, STYLE,
)
from ..hash_cache import HashCache, clear_cache
from ..results_io import RESULT_SUFFIX, export_csv, load_results, save_results
from ..scan_options import ScanOptions
from ..scanner import Scanner, ScanStats, STEP_NAMES, _format_eta
from ..models import DuplicateGroup, FileInfo
from ..logger import logger
from ..selection import (
    COND_BY_ID as _COND_BY_ID,
    FolderList,
    RuleSet,
    apply_selection as _apply_selection,
    condition as _condition,
    dump_presets,
    load_presets,
    parse_custom_rules,
    split_dirs,
)

# 支援懸停圖片預覽的副檔名集合
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.ico'}


def default_settings() -> QSettings:
    """設定檔：%APPDATA%\\DupeScan\\DupeScan.ini（測試可用環境變數 DUPESCAN_SETTINGS 指到別處）。"""
    path = os.environ.get("DUPESCAN_SETTINGS")
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "DupeScan", "DupeScan")


SIZE_UNITS = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}


def _unit_combo(default: str) -> QComboBox:
    """大小單位下拉選單（KB／MB／GB）。"""
    combo = QComboBox()
    combo.addItems(list(SIZE_UNITS))
    combo.setCurrentText(default)
    combo.setFixedWidth(70)
    return combo


def _describe_decisions(decided: Counter, conditions: list) -> str:
    """「已套用到 10 個群組：像副本的檔名優先移除 3、路徑較短 5、平手取列表第一 2」"""
    parts = [f"參考資料夾 {decided['reference']:,}"] if decided["reference"] else []
    parts += [f"{_COND_BY_ID[c.id][1]} {decided[c.id]:,}" for c in conditions if decided[c.id]]
    if decided[None]:
        parts.append(f"平手取列表第一 {decided[None]:,}")
    total = sum(decided.values())
    return f"已套用到 {total:,} 個群組" + (f"：{'、'.join(parts)}" if parts else "")


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


# ── Worker（掃描 Worker，在 QThread 中執行）────────────────────────────────────
class ScanWorker(QObject):
    """Qt worker 包裝器，透過 moveToThread 搬到 QThread 執行掃描。

    Signal 是跨執行緒通訊的唯一介面，第一個參數都是 scan_id（主視窗據此忽略舊掃描的訊號）：
      progress  → 更新進度條與步驟標籤
      finished  → 掃描完成，帶回結果群組清單與 ScanStats
      cancelled → 使用者停止掃描（不是「沒有重複」）
      error     → 掃描引擎例外，帶回錯誤訊息字串
      detected  → 磁碟偵測完成，帶回 DiskProfile.summary() 字串
      ended     → run() 結束（不論結果），接到 QThread.quit 讓執行緒收尾
    """
    progress  = pyqtSignal(int, str, int, int, int, int, int)
    finished  = pyqtSignal(int, list, object)
    cancelled = pyqtSignal(int)
    error     = pyqtSignal(int, str)
    detected  = pyqtSignal(int, str)
    ended     = pyqtSignal()

    def __init__(self, scan_id: int, roots: list[str], min_size: int = 1,
                 use_cache: bool = True, **options):
        super().__init__()
        self.scan_id = scan_id
        # 舊介面的欄位轉成 ScanOptions（新介面完成後整個 ScanWorker 會換掉）
        scan_options = ScanOptions(
            roots=list(roots), min_size=min_size,
            custom_exts=[e.lstrip(".").lower() for e in options.get("include_exts", [])],
            exclude_dirs=[d for d in options.get("exclude_dirs", []) if os.path.isabs(d)],
            include_hidden=options.get("include_hidden", False))
        # Scanner 在建構時就建立：run() 開始前呼叫 stop() 也會生效
        self._scanner = Scanner(
            scan_options,
            cache=HashCache() if use_cache else None,
            on_progress=lambda m, c, t, s, ts, eta: self.progress.emit(scan_id, m, c, t, s, ts, eta),
            on_done=lambda g, st: self.finished.emit(scan_id, g, st),
            on_error=lambda e: self.error.emit(scan_id, e),
            on_cancel=lambda: self.cancelled.emit(scan_id),
        )

    def run(self):
        """在 worker thread 執行：偵測每個掃描路徑所在磁碟的類型決定執行緒數，再啟動 Scanner。"""
        try:
            from ..disk_detect import detect_disk_profile
            workers: dict[int, tuple[int, int]] = {}
            labels: list[tuple[str, str]] = []
            for root in self._scanner.roots:
                try:
                    dev = os.stat(root).st_dev
                except OSError:
                    continue
                if dev in workers:
                    continue
                profile = detect_disk_profile(root)
                workers[dev] = (profile.pass2_workers, profile.pass3_workers)
                labels.append((root.anchor.rstrip("\\") or str(root), profile.summary()))
            if labels:
                self._scanner.workers_by_dev = workers
                self._scanner.pass2_workers, self._scanner.pass3_workers = next(iter(workers.values()))
                summary = (labels[0][1] if len(labels) == 1
                           else "・".join(f"{drive} {text}" for drive, text in labels))
                self.detected.emit(self.scan_id, summary)
            self._scanner.scan()
        finally:
            self.ended.emit()

    def stop(self):
        self._scanner.stop()

    def pause(self):
        self._scanner.pause()

    def resume(self):
        self._scanner.resume()


# 關閉視窗時仍未結束的掃描執行緒：保留參照到程式結束，避免 QThread 在執行中被回收而閃退
_UNFINISHED_THREADS: list = []


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


class _FontDelegate(QStyledItemDelegate):
    """依樹的目前字級繪製每一列（群組列加粗、大 1px）：改字級只要重繪，不必重建所有列。"""

    def __init__(self, tree: "DuplicateTree"):
        super().__init__(tree)
        self._tree = tree

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        is_group = not index.parent().isValid()
        option.font.setPixelSize(self._tree._font_size + (1 if is_group else 0))
        option.font.setBold(is_group)


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
        # 右鍵選單要用的目前規則（側邊列的條件與資料夾設定）與預設集，由主視窗指定
        self.rules_provider = RuleSet
        self.presets_provider = dict
        self._reference = FolderList([])   # 參考資料夾：檔名後面標 📌

        self.setColumnCount(4)
        self.setHeaderLabels(["名稱", "路徑（雙擊開啟・Ctrl＋點擊在檔案總管定位）", "大小", "修改時間"])
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(True)
        self.setExpandsOnDoubleClick(True)
        self.setIndentation(18)
        self.setAnimated(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)   # 有目前列，鍵盤才能操作
        self.setItemDelegate(_FontDelegate(self))
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)   # 長路徑從中間省略，保留結尾的資料夾名稱
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
    def populate(self, groups: list[DuplicateGroup], expanded: set[int] | None = None):
        """以 groups 重建樹。只建立群組頂層列；子列待展開時才建立。

        勾選狀態保存在 FileInfo.selected，因此排序/篩選後重建仍會反映既有選取。
        expanded：要維持展開的群組（id(DuplicateGroup)），移除檔案後重建時保留原本的檢視。
        """
        self._preview.reset()
        self._guard = True
        self.clear()
        self._groups = groups
        self.setUpdatesEnabled(False)
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
            gi.setChildIndicatorPolicy(show_indicator)
            items.append(gi)
        self.addTopLevelItems(items)
        # setFirstColumnSpanned / 初始勾選態需在 item 已屬於 view 後設定
        for gi, g in zip(items, groups):
            gi.setFirstColumnSpanned(True)
            self._set_group_state(gi, g)
        self.setUpdatesEnabled(True)
        self._guard = False
        if expanded:
            for gi, g in zip(items, groups):
                if id(g) in expanded:
                    gi.setExpanded(True)      # 觸發 _on_expanded 建立子列
        self.setHeaderHidden(not groups)
        self._placeholder.setVisible(not groups)
        self._placeholder.raise_()
        self.checksChanged.emit()

    def expanded_group_ids(self) -> set[int]:
        """目前展開中的群組（id(DuplicateGroup)）。"""
        ids = set()
        for i in range(self.topLevelItemCount()):
            gi = self.topLevelItem(i)
            if gi.isExpanded():
                ids.add(id(gi.data(0, _ROLE_OBJ)))
        return ids

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
        item.setText(_COL_PATH, fi.folder)
        item.setText(_COL_SIZE, human_size(fi.size))
        mt = datetime.datetime.fromtimestamp(fi.mtime).strftime("%Y-%m-%d %H:%M")
        item.setText(_COL_TIME, mt)

        item.setToolTip(_COL_NAME, f"雙擊以預設程式開啟：{fi.path}")
        item.setToolTip(_COL_PATH, f"Ctrl＋點擊在檔案總管中定位：{fi.path}")
        if fi.aliases:
            # 硬連結：同一份資料的其他路徑，不列為重複（刪掉也不會省空間）
            note = (f"\n\n另有 {len(fi.aliases)} 條硬連結路徑指向同一份資料：\n"
                    + "\n".join(fi.aliases[:10]))
            item.setToolTip(_COL_NAME, item.toolTip(_COL_NAME) + note)
            item.setText(_COL_NAME, f"{fi.name}  ⛓{len(fi.aliases) + 1}")
        if self._reference and self._reference.contains(fi.path):
            item.setText(_COL_NAME, item.text(_COL_NAME) + "  📌")
            item.setToolTip(_COL_NAME, item.toolTip(_COL_NAME)
                            + "\n\n📌 位於參考資料夾：規則與「全部勾選」不會勾選它")

    def set_reference(self, reference: FolderList):
        """參考資料夾變更：重新標示已建立的檔案列。"""
        self._reference = reference
        self._guard = True
        for i in range(self.topLevelItemCount()):
            gi = self.topLevelItem(i)
            if gi.data(0, _ROLE_POP):
                for j in range(gi.childCount()):
                    ch = gi.child(j)
                    self._fill_file_item(ch, ch.data(0, _ROLE_OBJ))
        self._guard = False

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
    def select_all(self, sel: bool, reference: FolderList | None = None):
        """全部勾選／取消；勾選時略過參考資料夾裡的檔案。"""
        for g in self._groups:
            for fi in g.files:
                fi.selected = sel and not (reference and reference.contains(fi.path))
        self.refresh_checks()

    def apply_conditions_all(self, conditions: list, mode: str,
                             reference: FolderList | None = None) -> Counter:
        """對所有顯示中的群組套用條件；回傳各條件決定了幾組（None＝平手取列表第一）。"""
        decided = Counter(_apply_selection(g, conditions, mode, reference) for g in self._groups)
        self.refresh_checks()
        return decided

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
        # 單擊路徑欄不再直接開檔案總管（容易誤觸）；按住 Ctrl 點擊才定位
        ctrl = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier
        if item.data(0, _ROLE_KIND) == "file" and col == _COL_PATH and ctrl:
            self._locate(item.data(0, _ROLE_OBJ))

    def keyPressEvent(self, event):
        """空白鍵：切換目前這一列的勾選（檔案列切換自己，群組列切換整組）。"""
        item = self.currentItem()
        if event.key() == Qt.Key.Key_Space and item is not None:
            if item.data(0, _ROLE_KIND) == "file":
                fi = item.data(0, _ROLE_OBJ)
                item.setCheckState(_COL_NAME, Qt.CheckState.Unchecked if fi.selected
                                   else Qt.CheckState.Checked)
            else:
                g = item.data(0, _ROLE_OBJ)
                self._toggle_group(item, g, not all(fi.selected for fi in g.files))
            return
        super().keyPressEvent(event)

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
            # 不經過 shell：路徑中的 % 或 & 不會被 cmd 解讀
            subprocess.Popen(f'explorer /select,"{fi.path}"')
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
        menu = self._context_menu(item) if item is not None else None
        if menu is not None:
            menu.exec(self.viewport().mapToGlobal(pos))

    def _context_menu(self, item: QTreeWidgetItem) -> QMenu | None:
        kind = item.data(0, _ROLE_KIND)
        group_item = item if kind == "group" else item.parent()
        if group_item is None:
            return None
        g: DuplicateGroup = group_item.data(0, _ROLE_OBJ)

        menu = QMenu(self)
        if kind == "file":
            fi: FileInfo = item.data(0, _ROLE_OBJ)
            act_open = menu.addAction("開啟檔案")
            act_open.triggered.connect(lambda: self._open(fi))
            act_loc = menu.addAction("在檔案總管中顯示")
            act_loc.triggered.connect(lambda: self._locate(fi))
            menu.addSeparator()

        # 快速選取子選單（保留某條件勝出者，其餘標記待刪；參考資料夾裡的檔案不會被勾選）
        rules: RuleSet = self.rules_provider()
        reference = rules.reference()
        quick = menu.addMenu("快速選取（保留…）")

        def add_quick(parent: QMenu, text: str, conditions: list, mode: str = "keep", ref=reference):
            act = parent.addAction(text)
            act.triggered.connect(
                lambda _, gg=g, gi=group_item, c=conditions, m=mode, r=ref:
                    self._quick_group(gi, gg, c, m, r))
            return act

        for cid in ("newer", "older", "sh_path", "shallow", "lo_path"):
            add_quick(quick, f"保留{_COND_BY_ID[cid][1]}者", [_condition(cid)])
        quick.addSeparator()
        add_quick(quick, "保留原始檔名（像副本的優先移除）", [_condition("copy_name")])
        has_dirs = bool(rules.keep_dirs or rules.remove_dirs)
        add_quick(quick, "依資料夾優先序" if has_dirs else "依資料夾優先序（請先在側邊列設定資料夾）",
                  [_condition("folders", rules)]).setEnabled(has_dirs)
        custom, custom_errors = rules.custom_rules()
        custom_text = ("依自訂規則（有錯誤，請先修正）" if custom_errors else
                       "依自訂規則" if custom else "依自訂規則（請先在側邊列設定）")
        add_quick(quick, custom_text, [_condition("custom", rules)]).setEnabled(
            bool(custom) and not custom_errors)
        active = rules.build()
        blocked = "custom" in rules.conditions and bool(custom_errors)
        add_quick(menu, "套用側邊列的條件到此群組" + ("（自訂規則有錯誤）" if blocked else ""),
                  active, rules.mode).setEnabled(bool(active) and not blocked)
        presets: dict[str, RuleSet] = self.presets_provider()
        if presets:
            sub = menu.addMenu("套用預設集到此群組")
            for name, preset in presets.items():
                conditions = preset.build()
                broken = "custom" in preset.conditions and bool(preset.custom_rules()[1])
                add_quick(sub, name, conditions, preset.mode, preset.reference()).setEnabled(
                    bool(conditions) and not broken)
        menu.addSeparator()
        a_all = menu.addAction("勾選此群組全部")
        a_all.triggered.connect(lambda: self._toggle_group(group_item, g, True))
        a_none = menu.addAction("取消此群組全部")
        a_none.triggered.connect(lambda: self._toggle_group(group_item, g, False))
        return menu

    def _quick_group(self, gi: QTreeWidgetItem, g: DuplicateGroup, conditions: list,
                     mode: str = "keep", reference: FolderList | None = None):
        _apply_selection(g, conditions, mode, reference)
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
        """改字級：由 _FontDelegate 套用，只需重新排版與重繪，不重建列。"""
        self._font_size = size
        self.setUniformRowHeights(False)     # 讓 view 依新字級重新計算列高
        self.setUniformRowHeights(True)
        self.doItemsLayout()
        self.viewport().update()


# ── MainWindow（主視窗）──────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    """應用程式主視窗，負責掃描流程管理、結果篩選排序、批次選取、刪除與報表匯出。

    掃描 Race Condition 防護：每次 _start_scan 遞增 _scan_id，ScanWorker 的每個 signal
    都帶著自己的 scan_id，與目前的 _scan_id 不符就忽略（舊掃描的遲到訊號不會蓋掉新結果）。
    執行緒生命週期：worker.ended → thread.quit；(thread, worker) 存在 _threads，
    直到該執行緒的 finished 送達 _on_thread_finished 才釋放，絕不讓執行中的 QThread 被回收。
    """

    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self._settings = settings if settings is not None else default_settings()
        self.setWindowTitle(f"DupeScan {__version__} — 重複檔案掃描工具")
        self.resize(1320, 840)
        self.setMinimumSize(960, 600)

        self._all_groups: list[DuplicateGroup] = []   # 完整未篩選結果
        self._last_roots: list[str] = []               # 最近一次掃描的路徑（儲存結果用）
        self._scanned_at: str = ""
        self._groups:     list[DuplicateGroup] = []   # 目前顯示（篩選/排序後）

        self._thread:  QThread | None    = None
        self._worker:  ScanWorker | None = None
        self._scanning: bool = False
        self._paused:   bool = False
        self._scan_id:  int  = 0
        # 每次掃描的 (thread, worker)，以 scan_id 為鍵；一律等 finished 訊號送達才釋放：
        # 執行中的 QThread 被回收，或 finished 送達前就刪掉它，Qt 都會直接終止程式
        self._threads: dict[int, tuple[QThread, ScanWorker]] = {}
        self._removal: tuple | None = None   # 背景移除中的 (thread, worker, 進度視窗)

        self._global_qs_mode: str  = "keep"
        self._presets: dict[str, RuleSet] = {}         # 具名預設集（設定檔 select/presets）
        self._sidebar_open:   bool = True
        self._sb_filter_open: bool = True
        self._sb_qs_open:     bool = True
        self._sb_scan_open:   bool = True

        self._preview_popup = ImagePreviewPopup()

        self._build_ui()
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)        # 可以把資料夾拖進視窗加入掃描路徑
        self._load_settings()

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
        self.tree.rules_provider = self._current_rules
        self.tree.presets_provider = lambda: self._presets
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
        open_btn = QPushButton("📂  開啟結果")
        open_btn.setToolTip("開啟之前儲存的掃描結果（含勾選狀態），接著處理")
        open_btn.clicked.connect(lambda: self._open_results())
        lay.addWidget(open_btn)
        history = QPushButton("↩  操作紀錄")
        history.setToolTip("查看移除過的檔案；移到隔離資料夾的可以一鍵還原")
        history.clicked.connect(self._show_history)
        lay.addWidget(history)
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
        self.path_edit.setPlaceholderText(
            "選擇要掃描的資料夾或磁碟機…（多個路徑用 ; 分隔，也可以把資料夾拖進視窗）")
        self.path_edit.setMinimumWidth(220)
        self.path_edit.returnPressed.connect(self._toggle_scan)
        row.addWidget(self.path_edit, 1)

        self.btn_browse = QPushButton("瀏覽")
        self.btn_browse.setToolTip("選擇要掃描的資料夾（取代目前的路徑）")
        self.btn_browse.clicked.connect(lambda: self._browse(append=False))
        row.addWidget(self.btn_browse)
        self.btn_add_path = QPushButton("＋")
        self.btn_add_path.setToolTip("再加一個資料夾一起掃描（可找出不同資料夾、不同磁碟之間的重複）")
        self.btn_add_path.setFixedWidth(36)
        self.btn_add_path.clicked.connect(lambda: self._browse(append=True))
        row.addWidget(self.btn_add_path)

        row.addSpacing(6)
        min_lbl = QLabel("最小")
        min_lbl.setStyleSheet(f"color:{SUBTEXT};")
        row.addWidget(min_lbl)
        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 1_000_000)
        self.min_size_spin.setValue(1)
        self.min_size_spin.setFixedWidth(84)
        row.addWidget(self.min_size_spin)
        self.min_unit_combo = _unit_combo("KB")
        row.addWidget(self.min_unit_combo)

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
        # 掃描時無法讀取的檔案數（滑鼠移上去看明細）；沒有時隱藏
        self.lbl_skipped  = _badge("", ORANGE)
        self.lbl_skipped.setVisible(False)
        sl.addWidget(self.lbl_groups)
        sl.addWidget(self.lbl_wasted)
        sl.addWidget(self.lbl_selected)
        sl.addWidget(self.lbl_skipped)
        sl.addStretch()

        self.btn_export = QPushButton("📊  匯出／儲存")
        self.btn_export.setEnabled(False)
        export_menu = QMenu(self.btn_export)
        export_menu.addAction("HTML 分析報表（圓餅圖）", self._export_report)
        export_menu.addAction("完整結果 CSV（Excel 可開）…", lambda: self._export_csv())
        export_menu.addSeparator()
        export_menu.addAction("儲存結果（含勾選，之後可再開啟）…", lambda: self._save_results())
        self.btn_export.setMenu(export_menu)
        self.btn_delete = QPushButton("🗑  移除已勾選")
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

        # ── 區段：掃描設定 ────────────────────────────────────────────
        self._sb_scan_hdr = QPushButton("▾  掃描設定")
        self._sb_scan_hdr.setStyleSheet(_SECTION_HDR)
        self._sb_scan_hdr.clicked.connect(self._toggle_scan_panel)
        bl.addWidget(self._sb_scan_hdr)

        self._sb_scan_body = QWidget()
        sl = QVBoxLayout(self._sb_scan_body)
        sl.setContentsMargins(4, 2, 4, 4)
        sl.setSpacing(6)
        sl.addWidget(self._mini_label("只掃這些副檔名"))
        self.include_ext_edit = QLineEdit()
        self.include_ext_edit.setPlaceholderText("例如 jpg, png, mp4（留空＝全部）")
        sl.addWidget(self.include_ext_edit)
        sl.addWidget(self._mini_label("排除副檔名"))
        self.exclude_ext_edit = QLineEdit()
        self.exclude_ext_edit.setPlaceholderText("例如 tmp, log")
        sl.addWidget(self.exclude_ext_edit)
        sl.addWidget(self._mini_label("排除資料夾（名稱或完整路徑，以 ; 分隔）"))
        self.exclude_dir_edit = QLineEdit()
        self.exclude_dir_edit.setPlaceholderText("例如 node_modules; D:\\Temp")
        sl.addWidget(self.exclude_dir_edit)
        sl.addWidget(self._mini_label("最大檔案大小（0＝不限）"))
        max_row = QHBoxLayout()
        max_row.setSpacing(6)
        self.max_size_spin = QSpinBox()
        self.max_size_spin.setRange(0, 1_000_000)
        self.max_size_spin.setValue(0)
        self.max_unit_combo = _unit_combo("GB")
        max_row.addWidget(self.max_size_spin, 1)
        max_row.addWidget(self.max_unit_combo)
        sl.addLayout(max_row)
        self.cb_hidden = QCheckBox("包含隱藏／系統檔")
        self.cb_hidden.setToolTip("預設略過「.」開頭與 Windows 隱藏／系統屬性的檔案和資料夾")
        sl.addWidget(self.cb_hidden)
        cache_row = QHBoxLayout()
        cache_row.setSpacing(6)
        self.cb_cache = QCheckBox("使用雜湊快取")
        self.cb_cache.setChecked(True)
        self.cb_cache.setToolTip("記住上次算過的 hash：檔案沒變就不必再讀一次，重掃同一顆磁碟快很多")
        self.btn_clear_cache = QPushButton("清除快取")
        self.btn_clear_cache.clicked.connect(self._clear_hash_cache)
        cache_row.addWidget(self.cb_cache, 1)
        cache_row.addWidget(self.btn_clear_cache)
        sl.addLayout(cache_row)
        bl.addWidget(self._sb_scan_body)

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
        # 打字時不要每個字都重建整棵樹（6 萬多個群組時每次要 1 秒以上）：停手 0.3 秒才套用
        self._filter_timer = QTimer(self, singleShot=True, interval=300)
        self._filter_timer.timeout.connect(self._apply_filter_sort)
        self.ext_filter_edit.textChanged.connect(lambda _: self._filter_timer.start())
        fb.addWidget(self.ext_filter_edit)

        fb.addWidget(self._mini_label("排序方式"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "可省空間（大→小）", "可省空間（小→大）",
            "單檔大小（大→小）", "單檔大小（小→大）",
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

        qb.addWidget(self._mini_label("預設集（存好的規則組合）"))
        self.preset_combo = QComboBox()
        self.preset_combo.setPlaceholderText("（還沒有預設集）")
        self.preset_combo.setToolTip("選好之後按「套用」：載入這組規則並套用到所有群組")
        qb.addWidget(self.preset_combo)
        pr = QHBoxLayout(); pr.setSpacing(6)
        self.btn_preset_apply = QPushButton("套用")
        self.btn_preset_apply.setToolTip("載入這組規則（操作模式、條件、資料夾、自訂規則）並套用到所有群組")
        self.btn_preset_apply.clicked.connect(self._apply_preset)
        self.btn_preset_save = QPushButton("另存…")
        self.btn_preset_save.setToolTip("把目前的操作模式、條件與設定存成具名預設集")
        self.btn_preset_save.clicked.connect(self._save_preset)
        self.btn_preset_delete = QPushButton("刪除")
        self.btn_preset_delete.setToolTip("刪除選取的預設集（只刪這組設定，不會動到任何檔案）")
        self.btn_preset_delete.clicked.connect(self._delete_preset)
        for b in (self.btn_preset_apply, self.btn_preset_save, self.btn_preset_delete):
            b.setStyleSheet("padding:6px 4px;")
            pr.addWidget(b)
        qb.addLayout(pr)

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
        self._global_cond.referenceChanged.connect(self._sync_reference)
        qb.addWidget(self._global_cond)

        btn_apply_all = QPushButton("套用到所有群組")
        btn_apply_all.setObjectName("primary")
        btn_apply_all.clicked.connect(self._global_apply_conditions)
        qb.addWidget(btn_apply_all)

        row2 = QHBoxLayout(); row2.setSpacing(6)
        btn_all = QPushButton("全部勾選")
        btn_all.setToolTip("勾選所有群組的每一份（參考資料夾裡的檔案除外）")
        btn_all.clicked.connect(self._select_all)
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

    # ── 掃描路徑與設定 ────────────────────────────────────────────────
    def _scan_paths(self) -> list[str]:
        """輸入框裡的掃描路徑（以 ; 分隔；整串本身就是一個存在的路徑時視為單一路徑）。"""
        text = self.path_edit.text().strip()
        if text and os.path.exists(text.strip('"')):
            return [text.strip('"')]
        paths: list[str] = []
        for part in text.split(";"):
            part = part.strip().strip('"')
            if part and part not in paths:
                paths.append(part)
        return paths

    def _add_paths(self, new_paths: list[str], append: bool = True):
        paths = self._scan_paths() if append else []
        for p in new_paths:
            p = os.path.normpath(p)
            if p not in paths:
                paths.append(p)
        self.path_edit.setText("; ".join(paths))

    def _scan_options(self) -> dict:
        def split(text: str, sep: str) -> list[str]:
            return [t.strip() for t in text.split(sep) if t.strip()]
        max_size = self.max_size_spin.value() * SIZE_UNITS[self.max_unit_combo.currentText()]
        return {
            "min_size": self.min_size_spin.value() * SIZE_UNITS[self.min_unit_combo.currentText()],
            "max_size": max_size or None,
            "include_hidden": self.cb_hidden.isChecked(),
            "include_exts": split(self.include_ext_edit.text(), ","),
            "exclude_exts": split(self.exclude_ext_edit.text(), ","),
            "exclude_dirs": split(self.exclude_dir_edit.text(), ";"),
        }

    @staticmethod
    def _describe_options(o: dict, use_cache: bool) -> str:
        parts = [f"最小大小: {o['min_size']:,} B"]
        if o["max_size"]:
            parts.append(f"最大大小: {o['max_size']:,} B")
        if o["include_exts"]:
            parts.append(f"只掃: {', '.join(o['include_exts'])}")
        if o["exclude_exts"]:
            parts.append(f"排除副檔名: {', '.join(o['exclude_exts'])}")
        if o["exclude_dirs"]:
            parts.append(f"排除資料夾: {'; '.join(o['exclude_dirs'])}")
        parts.append("包含隱藏檔" if o["include_hidden"] else "略過隱藏檔")
        parts.append("使用快取" if use_cache else "不使用快取")
        return "，".join(parts)

    def _toggle_scan_panel(self):
        self._sb_scan_open = not self._sb_scan_open
        self._sb_scan_body.setVisible(self._sb_scan_open)
        self._sb_scan_hdr.setText("▾  掃描設定" if self._sb_scan_open else "▸  掃描設定")

    def _clear_hash_cache(self):
        if self._threads:
            QMessageBox.information(self, "提示", "掃描進行中，請等掃描結束再清除快取。")
            return
        reply = QMessageBox.question(
            self, "清除雜湊快取",
            "清除後，下次掃描會重新讀取所有檔案內容。\n（只是 DupeScan 自己的快取，不會動到你的檔案）\n\n確定要清除嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_cache()
        logger.info("已清除雜湊快取")
        self.status_bar.showMessage("已清除雜湊快取")

    def dragEnterEvent(self, event):
        if not self._scanning and any(u.isLocalFile() and os.path.isdir(u.toLocalFile())
                                      for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        dirs = [u.toLocalFile() for u in event.mimeData().urls()
                if u.isLocalFile() and os.path.isdir(u.toLocalFile())]
        if dirs and not self._scanning:
            self._add_paths(dirs, append=True)
            event.acceptProposedAction()

    # ── 記住設定 ──────────────────────────────────────────────────────
    def _load_settings(self):
        """還原上次關閉時的視窗大小、路徑、掃描設定、檢視與快速選取條件。"""
        st = self._settings
        geometry = st.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.path_edit.setText(st.value("scan/paths", "", str))
        self.min_size_spin.setValue(st.value("scan/min_size", 1, int))
        self.min_unit_combo.setCurrentText(st.value("scan/min_unit", "KB", str))
        self.max_size_spin.setValue(st.value("scan/max_size", 0, int))
        self.max_unit_combo.setCurrentText(st.value("scan/max_unit", "GB", str))
        self.include_ext_edit.setText(st.value("scan/include_exts", "", str))
        self.exclude_ext_edit.setText(st.value("scan/exclude_exts", "", str))
        self.exclude_dir_edit.setText(st.value("scan/exclude_dirs", "", str))
        self.cb_hidden.setChecked(st.value("scan/include_hidden", False, bool))
        self.cb_cache.setChecked(st.value("scan/use_cache", True, bool))
        self.font_size_spin.setValue(st.value("view/font_size", 13, int))
        self.sort_combo.setCurrentIndex(st.value("view/sort", 0, int))
        self._set_global_mode(st.value("select/mode", "keep", str))
        self._global_cond.keep_dirs_edit.setText(st.value("select/keep_dirs", "", str))
        self._global_cond.remove_dirs_edit.setText(st.value("select/remove_dirs", "", str))
        self._global_cond.reference_edit.setText(st.value("select/reference_dirs", "", str))
        self._global_cond.custom_edit.setPlainText(st.value("select/custom_rules", "", str))
        self._sync_reference()
        self._presets = load_presets(st.value("select/presets", "", str))
        self._refresh_presets(st.value("select/preset", "", str))
        for cond_id in st.value("select/conditions", [], list) or []:
            cb = self._global_cond._checkboxes.get(cond_id)
            if cb is not None and cb.isEnabled():
                cb.setChecked(True)
        for key, opened, toggle in (("ui/sidebar_open", self._sidebar_open, self._toggle_sidebar),
                                    ("ui/scan_panel_open", self._sb_scan_open, self._toggle_scan_panel),
                                    ("ui/filter_panel_open", self._sb_filter_open, self._toggle_filter_panel),
                                    ("ui/qs_panel_open", self._sb_qs_open, self._toggle_qs_panel)):
            if st.value(key, True, bool) != opened:
                toggle()

    def _save_settings(self):
        st = self._settings
        st.setValue("window/geometry", self.saveGeometry())
        st.setValue("scan/paths", self.path_edit.text())
        st.setValue("scan/min_size", self.min_size_spin.value())
        st.setValue("scan/min_unit", self.min_unit_combo.currentText())
        st.setValue("scan/max_size", self.max_size_spin.value())
        st.setValue("scan/max_unit", self.max_unit_combo.currentText())
        st.setValue("scan/include_exts", self.include_ext_edit.text())
        st.setValue("scan/exclude_exts", self.exclude_ext_edit.text())
        st.setValue("scan/exclude_dirs", self.exclude_dir_edit.text())
        st.setValue("scan/include_hidden", self.cb_hidden.isChecked())
        st.setValue("scan/use_cache", self.cb_cache.isChecked())
        st.setValue("view/font_size", self.font_size_spin.value())
        st.setValue("view/sort", self.sort_combo.currentIndex())
        st.setValue("select/mode", self._global_qs_mode)
        st.setValue("select/conditions", list(self._global_cond._checked_order))
        st.setValue("select/keep_dirs", self._global_cond.keep_dirs_edit.text())
        st.setValue("select/remove_dirs", self._global_cond.remove_dirs_edit.text())
        st.setValue("select/reference_dirs", self._global_cond.reference_edit.text())
        st.setValue("select/custom_rules", self._global_cond.custom_edit.toPlainText())
        st.setValue("select/preset", self.preset_combo.currentText())   # 預設集本身變動時就已存好
        st.setValue("ui/sidebar_open", self._sidebar_open)
        st.setValue("ui/scan_panel_open", self._sb_scan_open)
        st.setValue("ui/filter_panel_open", self._sb_filter_open)
        st.setValue("ui/qs_panel_open", self._sb_qs_open)
        st.sync()

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

    def _current_rules(self) -> RuleSet:
        """側邊列目前的規則：操作模式＋條件面板。"""
        rules = self._global_cond.rule_set()
        rules.mode = self._global_qs_mode
        return rules

    def _global_apply_conditions(self):
        message = self._apply_rules_to_all(self._current_rules())
        if message:
            self.status_bar.showMessage(message)

    def _apply_rules_to_all(self, rules: RuleSet) -> str | None:
        """套用到所有顯示中的群組，回傳狀態列訊息；沒有條件或自訂規則有誤時提示並回傳 None。"""
        conditions = rules.build()
        if not conditions:
            QMessageBox.information(self, "提示", "請先勾選至少一個條件。")
            return None
        errors = rules.custom_rules()[1]
        if "custom" in rules.conditions and errors:
            # 有錯的行會被略過，套用到所有群組前先擋下，免得留下錯的那份
            QMessageBox.warning(self, "自訂規則有誤", "請先修正自訂規則：\n\n" + "\n".join(errors[:10]))
            return None
        # 6 萬多個群組、智慧規則全開時約 1 秒
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            decided = self.tree.apply_conditions_all(conditions, rules.mode, rules.reference())
        finally:
            QApplication.restoreOverrideCursor()
        return _describe_decisions(decided, conditions)

    # ── 預設集 ────────────────────────────────────────────────────────
    def _load_rules(self, rules: RuleSet):
        """把一組規則放進側邊列（操作模式＋條件面板）。"""
        self._set_global_mode(rules.mode)
        self._global_cond.set_rule_set(rules)
        self._sync_reference()

    def _apply_preset(self):
        """一鍵套用：載入選取的預設集，有結果時直接套用到所有群組。"""
        name = self.preset_combo.currentText()
        rules = self._presets.get(name)
        if rules is None:
            return
        self._load_rules(rules)
        if not self.tree._groups or not rules.conditions:
            self.status_bar.showMessage(f"已載入預設集「{name}」")
            return
        message = self._apply_rules_to_all(self._current_rules())
        if message:
            self.status_bar.showMessage(f"預設集「{name}」— {message}")

    def _save_preset(self):
        rules = self._current_rules()
        errors = rules.custom_rules()[1]
        if "custom" in rules.conditions and errors:
            QMessageBox.warning(self, "自訂規則有誤", "請先修正自訂規則再儲存：\n\n" + "\n".join(errors[:10]))
            return
        name, ok = QInputDialog.getText(self, "另存預設集", "預設集名稱：",
                                        text=self.preset_combo.currentText())
        name = name.strip()
        if not ok or not name:
            return
        if name in self._presets and QMessageBox.question(
                self, "取代預設集", f"已經有「{name}」，要用目前的規則取代嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._presets[name] = rules
        self._store_presets(select=name)
        self.status_bar.showMessage(f"已儲存預設集「{name}」")

    def _delete_preset(self):
        name = self.preset_combo.currentText()
        if name not in self._presets:
            return
        if QMessageBox.question(
                self, "刪除預設集", f"要刪除預設集「{name}」嗎？\n（只刪除這組設定，不會動到任何檔案）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        del self._presets[name]
        self._store_presets()
        self.status_bar.showMessage(f"已刪除預設集「{name}」")

    def _store_presets(self, select: str | None = None):
        """預設集一有變動就寫進設定檔（不等關閉視窗），並更新下拉選單。"""
        self._settings.setValue("select/presets", dump_presets(self._presets))
        self._settings.sync()
        self._refresh_presets(select)

    def _refresh_presets(self, select: str | None = None):
        combo = self.preset_combo
        current = select or combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(list(self._presets))
        combo.setCurrentIndex(combo.findText(current) if current in self._presets
                              else (0 if self._presets else -1))
        combo.blockSignals(False)
        self.btn_preset_apply.setEnabled(bool(self._presets))
        self.btn_preset_delete.setEnabled(bool(self._presets))

    def _select_all(self):
        """「全部勾選」：參考資料夾裡的檔案不會被勾選。"""
        self.tree.select_all(True, self._current_rules().reference())

    def _sync_reference(self):
        self.tree.set_reference(self._current_rules().reference())

    # ── 文字大小調整 ──────────────────────────────────────────────────
    def _apply_font_size(self, size: int):
        self.tree.set_font_size(size)

    # ── 篩選 / 排序 ───────────────────────────────────────────────────
    def _apply_filter_sort(self, keep_view: bool = False):
        """依篩選與排序條件重建結果樹；keep_view 時保留展開狀態與捲動位置。"""
        if not self._all_groups and not keep_view:
            return
        ext_text = self.ext_filter_edit.text().strip().lower()
        if ext_text:
            exts = {e.strip().lstrip('.') for e in ext_text.split(',') if e.strip()}
            groups = [g for g in self._all_groups
                      if any(fi.path.suffix.lstrip('.').lower() in exts for fi in g.files)]
        else:
            groups = list(self._all_groups)

        sort_opts = [
            (lambda g: g.wasted_bytes, True),
            (lambda g: g.wasted_bytes, False),
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
        if keep_view:
            expanded = self.tree.expanded_group_ids()
            scroll = self.tree.verticalScrollBar().value()
            self.tree.populate(groups, expanded)
            self.tree.verticalScrollBar().setValue(scroll)
        else:
            self.tree.populate(groups)

    # ── 操作動作 ──────────────────────────────────────────────────────
    def _browse(self, append: bool = False):
        path = QFileDialog.getExistingDirectory(self, "選擇資料夾", "")
        if path:
            self._add_paths([path], append)

    def _toggle_scan(self):
        if self._scanning: self._cancel_scan()
        else: self._start_scan()

    def _start_scan(self):
        paths = self._scan_paths()
        if not paths:
            QMessageBox.warning(self, "提示", "請先選擇要掃描的路徑。"); return
        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            QMessageBox.warning(self, "路徑錯誤", "路徑不存在：\n" + "\n".join(missing)); return

        self._stop_worker()
        self._clear_results()
        self.summary_widget.setVisible(False)
        self._sb_filter_body.setVisible(False)
        self._sb_qs_body.setVisible(False)
        self.progress_bar.setRange(0, 0)
        self.progress_section.setVisible(True)
        self._set_scanning(True)
        self._paused = False

        self._scan_id += 1
        options = self._scan_options()
        use_cache = self.cb_cache.isChecked()
        logger.info(f"掃描開始 — 路徑: {'; '.join(paths)}；{self._describe_options(options, use_cache)}")

        self._last_roots = paths
        worker = ScanWorker(self._scan_id, paths, use_cache=use_cache, **options)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_done)
        worker.cancelled.connect(self._on_cancelled)
        worker.error.connect(self._on_error)
        worker.detected.connect(self._on_disk_detected)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ended.connect(thread.quit)          # 掃描結束 → 執行緒的事件迴圈也結束
        thread.finished.connect(functools.partial(self._on_thread_finished, self._scan_id))
        self._threads[self._scan_id] = (thread, worker)
        self._worker, self._thread = worker, thread
        thread.start()

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
        self._stop_worker()
        self._clear_results()
        self.summary_widget.setVisible(False)
        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_qs_body.setVisible(self._sb_qs_open)
        self._hide_progress()
        self._set_scanning(False)
        self._paused = False
        self.status_bar.showMessage("已重置")
        logger.info("已重置（使用者操作）")

    def _stop_worker(self, wait_ms: int = 2000):
        """停止目前的掃描並等它結束（讀檔區塊之間就會停，通常不到 0.1 秒）。

        若在時限內等不到（例如網路磁碟卡住）也不會強行處理：參照留在 _threads，
        等它自己結束、finished 送達後再釋放；之後的訊號會因 scan_id 不符而被忽略。
        """
        worker, thread = self._worker, self._thread
        self._worker = self._thread = None
        if worker is not None:
            worker.stop()
        if thread is not None and thread.isRunning() and not thread.wait(wait_ms):
            logger.warning("背景掃描執行緒未在時限內結束，改在背景等待它結束")

    def closeEvent(self, event):
        """關閉視窗前記住設定並停止掃描；仍未結束的執行緒保留參照到程式結束，避免閃退。"""
        self._save_settings()
        self._stop_worker(wait_ms=5000)
        for thread, worker in list(self._threads.values()):
            worker.stop()
            thread.wait(3000)
        if self._removal:
            self._removal[0].wait(10000)
        _UNFINISHED_THREADS.extend(p for p in self._threads.values() if p[0].isRunning())
        self._preview_popup.close()
        super().closeEvent(event)

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
        # 掃描中不能改路徑與掃描設定
        for w in (self.path_edit, self.btn_browse, self.btn_add_path, self.min_size_spin,
                  self.min_unit_combo, self._sb_scan_body, self.btn_clear_cache):
            w.setEnabled(not scanning)

    # ── Signal 接收者（Slots）────────────────────────────────────────
    def _on_thread_finished(self, scan_id: int):
        """掃描執行緒結束：此時才釋放 (thread, worker) 的參照。"""
        pair = self._threads.pop(scan_id, None)
        if pair is None:
            return
        thread, _ = pair
        thread.wait()                              # finished 發出後執行緒還在收尾，等它真正結束
        if thread is self._thread:
            self._thread = self._worker = None
            if self._scanning:                     # 正常不會發生：沒收到結果訊號就結束了
                self._set_scanning(False)
                self._hide_progress()
                self.status_bar.showMessage("掃描已中止")

    def _hide_progress(self):
        """隱藏進度區並清空各標籤（掃描結束、取消、錯誤、重置共用）。"""
        self.progress_section.setVisible(False)
        self.progress_label.setText("")
        self.disk_label.setText("")
        self.step_label.setText("")
        self.eta_label.setText("")

    def _on_disk_detected(self, sid: int, summary: str):
        if sid != self._scan_id: return
        self.disk_label.setText(f"💽 {summary}")
        self.status_bar.showMessage(f"磁碟類型: {summary}")
        logger.info(f"磁碟偵測: {summary}")

    def _on_progress(self, sid, msg, cur, total, step, total_steps, eta_secs):
        if sid != self._scan_id or not self._scanning: return
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

    def _on_done(self, sid: int, groups: list, stats: ScanStats):
        if sid != self._scan_id or not self._scanning: return
        self._set_scanning(False)
        self._paused = False
        self._hide_progress()
        self._all_groups = groups
        self._scanned_at = datetime.datetime.now().isoformat(timespec="seconds")
        self._log_scan_stats(stats)
        skipped = f"（略過 {stats.unreadable:,} 個無法讀取的項目）" if stats.unreadable else ""
        if stats.cache_hits_full:
            skipped += f"（{stats.cache_hits_full:,} 個檔案沿用上次的比對結果）"

        if not groups:
            logger.info("掃描完成 — 未發現重複檔案")
            self.status_bar.showMessage(f"掃描完成 — 未發現重複檔案！{skipped}")
            self.tree.set_placeholder("✅  未發現重複檔案")
            self.tree.populate([])
            return

        total_wasted = sum(g.wasted_bytes for g in groups)
        logger.info(f"掃描完成 — {len(groups)} 個重複群組，共浪費 {human_size(total_wasted)}")

        self._update_summary()
        self.lbl_skipped.setVisible(stats.unreadable > 0)
        if stats.unreadable:
            self.lbl_skipped.setText(f"⚠ 略過 {stats.unreadable:,} 個")
            more = stats.unreadable - len(stats.unreadable_samples[:30])
            self.lbl_skipped.setToolTip(
                "以下項目無法讀取，未納入比對：\n" + "\n".join(stats.unreadable_samples[:30])
                + (f"\n… 另外 {more:,} 個（完整清單見 log）" if more > 0 else ""))
        self.summary_widget.setVisible(True)
        self.tree.setHeaderHidden(False)

        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_qs_body.setVisible(self._sb_qs_open)

        self._apply_filter_sort()
        self.status_bar.showMessage(
            f"掃描完成 — 找到 {len(groups)} 個重複群組，可省 {human_size(total_wasted)}{skipped}")

    def _log_scan_stats(self, stats: ScanStats):
        logger.info(
            f"掃描統計 — 比對 {stats.files_seen:,} 個檔案、候選 {stats.candidates:,} 個；"
            f"略過：無法讀取 {stats.unreadable:,}、連結／junction {stats.skipped_links:,}、"
            f"隱藏／系統 {stats.skipped_hidden:,}、雲端佔位檔 {stats.skipped_cloud:,}、"
            f"排除資料夾 {stats.skipped_excluded:,}、正在寫入 {stats.skipped_changing:,}；"
            f"合併硬連結 {stats.same_file_merged:,}；快取沿用：前段 {stats.cache_hits_partial:,}、"
            f"完整 {stats.cache_hits_full:,}")
        for line in stats.unreadable_samples:
            logger.info(f"  無法讀取: {line}")

    def _on_cancelled(self, sid: int):
        """使用者停止掃描：回到閒置狀態，明確顯示「已取消」而不是「沒有重複」。"""
        if sid != self._scan_id or not self._scanning: return
        self._set_scanning(False)
        self._paused = False
        self._hide_progress()
        self.tree.set_placeholder("掃描已取消")
        self.tree.populate([])
        self.status_bar.showMessage("掃描已取消")

    def _on_error(self, sid: int, msg: str):
        if sid != self._scan_id or not self._scanning: return
        self._set_scanning(False)
        self._paused = False
        self._hide_progress()
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
        self.lbl_skipped.setVisible(False)

    # ── 勾選統計 UI 更新 ──────────────────────────────────────────────
    def _update_selection_ui(self):
        n, b = self.tree.selected_stats()
        self.lbl_selected.setText(f"已選 {n:,}・{human_size(b)}" if n else "已選 0")
        self.btn_delete.setText(f"🗑  移除已勾選（{n}）" if n else "🗑  移除已勾選")
        self.btn_delete.setEnabled(n > 0)

    def _delete_selected(self):
        """先建立移除計畫並顯示預覽，確認後在背景移到資源回收筒或隔離資料夾（不會永久刪除）。"""
        groups = [(i + 1, g) for i, g in enumerate(self.tree._groups)
                  if any(fi.selected for fi in g.files)]
        if not groups:
            QMessageBox.information(self, "提示", "請先勾選要移除的檔案。"); return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # 會重新檢查每個檔案是否在掃描後變動；參考資料夾裡的檔案視同受保護
            plan = build_plan(groups, reference_dirs=self._current_rules().reference_dirs)
        finally:
            QApplication.restoreOverrideCursor()

        dlg = delete_dialog.DeletePlanDialog(plan, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        for e in plan.entries_with(SKIP):
            logger.info(f"  略過: {e.file.path}（{e.note}）")
        self._run_removal(dlg.selected(), dlg.method(), dlg.full_verify())

    def _run_removal(self, items: list, method: str, full_verify: bool):
        if not items:
            return
        where = "資源回收筒" if method == "recycle" else "隔離資料夾"
        logger.info(f"開始移除 {len(items)} 個檔案（{where}"
                    + ("，先完整比對內容）" if full_verify else "）"))
        progress = QProgressDialog(f"正在移到{where}…", None, 0, len(items), self)
        progress.setWindowTitle("移除中")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = delete_dialog.RemovalWorker(items, method, int(self.winId()), full_verify)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ended.connect(thread.quit)
        worker.progress.connect(self._on_removal_progress)
        worker.finished.connect(self._on_removal_done)
        worker.error.connect(self._on_removal_error)
        thread.finished.connect(self._on_removal_thread_finished)
        self._removal = (thread, worker, progress)
        self.btn_delete.setEnabled(False)
        self.btn_scan.setEnabled(False)
        thread.start()
        progress.show()

    def _on_removal_progress(self, phase: str, done: int, total: int):
        if self._removal:
            progress = self._removal[2]
            progress.setMaximum(max(total, 1))
            progress.setValue(done)
            progress.setLabelText(f"{phase}… {done:,} / {total:,}")

    def _finish_removal_ui(self):
        if self._removal:
            self._removal[2].close()
        self.btn_scan.setEnabled(True)
        self._update_selection_ui()

    def _on_removal_done(self, results: list):
        self._finish_removal_ui()
        self._apply_removal(results)
        self._report_removal(results)

    def _apply_removal(self, results: list):
        """把已移除的檔案從結果中拿掉（不重新掃描）：剩不到 2 份的群組直接消失。"""
        removed = {r.path for r in results if r.ok}
        if not removed:
            return
        for g in self._all_groups:
            g.files = [fi for fi in g.files if str(fi.path) not in removed]
        self._all_groups = [g for g in self._all_groups if len(g.files) > 1]
        self._update_summary()
        self._apply_filter_sort(keep_view=True)
        logger.info(f"結果已更新（未重新掃描）— 剩 {len(self._all_groups)} 個群組")

    def _update_summary(self):
        """依目前的 _all_groups 更新摘要列的群組數與可省空間。"""
        groups = self._all_groups
        self.lbl_groups.setText(f"{len(groups)} 個群組")
        self.lbl_wasted.setText(f"可省 {human_size(sum(g.wasted_bytes for g in groups))}")
        self.btn_export.setEnabled(bool(groups))
        if not groups:
            self.tree.set_placeholder("✅  這次找到的重複檔都處理完了")

    def _on_removal_error(self, msg: str):
        self._finish_removal_ui()
        logger.error(f"移除時發生錯誤: {msg}")
        QMessageBox.critical(self, "移除時發生錯誤",
                             f"{msg}\n\n已處理的檔案請到「操作紀錄」或 log 查看。")

    def _on_removal_thread_finished(self):
        if self._removal:
            self._removal[0].wait()
            self._removal = None

    def _report_removal(self, results: list):
        """記錄並顯示移除結果：回收筒／隔離區／未移除各幾個，以及原因。"""
        recycled = [r for r in results if r.ok and r.method == "recycle"]
        moved    = [r for r in results if r.ok and r.method == "quarantine"]
        failed   = [r for r in results if not r.ok]
        for r in recycled:
            logger.info(f"  移到資源回收筒: {r.path}")
        for r in moved:
            logger.info(f"  移到隔離資料夾: {r.path} → {r.dest}" + (f"（{r.note}）" if r.note else ""))
        for r in failed:
            logger.warning(f"  沒有移除: {r.path}  原因: {r.note}")
        logger.info(f"移除完成 — 資源回收筒 {len(recycled)} 個、隔離資料夾 {len(moved)} 個、"
                    f"沒有移除 {len(failed)} 個")

        lines = [f"已移到資源回收筒：{len(recycled)} 個"]
        if moved:
            reasons = sorted({r.note for r in moved if r.note})
            lines.append(f"已移到隔離資料夾：{len(moved)} 個"
                         + (f"（{'、'.join(reasons)}）" if reasons else ""))
            lines.append("隔離資料夾的檔案可從「操作紀錄」一鍵還原。")
        if failed:
            lines.append(f"\n沒有移除：{len(failed)} 個")
            lines += [f"  {r.path}（{r.note}）" for r in failed[:10]]
            if len(failed) > 10:
                lines.append(f"  … 另外 {len(failed) - 10} 個（完整清單見 log）")
        QMessageBox.information(self, "移除完成", "\n".join(lines))
        self.status_bar.showMessage(
            f"已移除 {len(recycled) + len(moved)} 個檔案" + (f"，{len(failed)} 個沒有移除" if failed else ""))

    def _show_history(self):
        from .history_dialog import HistoryDialog
        HistoryDialog(self).exec()

    # ── 結果匯出與存檔 ────────────────────────────────────────────────
    def _export_csv(self, path: str | None = None):
        if not self._all_groups:
            return
        if path is None:
            path, _ = QFileDialog.getSaveFileName(
                self, "匯出完整結果", "dupescan_結果.csv", "CSV (*.csv)")
            if not path:
                return
        export_csv(path, self._groups)
        logger.info(f"匯出結果 CSV: {path}")
        self.status_bar.showMessage(f"已匯出 {len(self._groups):,} 個群組到 {path}")

    def _save_results(self, path: str | None = None):
        if not self._all_groups:
            return
        if path is None:
            path, _ = QFileDialog.getSaveFileName(
                self, "儲存結果", "dupescan_結果" + RESULT_SUFFIX,
                f"DupeScan 結果 (*{RESULT_SUFFIX})")
            if not path:
                return
        save_results(path, self._all_groups, self._last_roots, self._scanned_at)
        logger.info(f"儲存結果: {path}（{len(self._all_groups):,} 個群組）")
        self.status_bar.showMessage(f"已儲存結果到 {path}，之後可用「開啟結果」接著處理")

    def _open_results(self, path: str | None = None):
        if self._scanning:
            QMessageBox.information(self, "提示", "請先停止掃描再開啟結果檔。")
            return
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "開啟結果", "", f"DupeScan 結果 (*{RESULT_SUFFIX})")
            if not path:
                return
        try:
            groups, meta = load_results(path)
        except ValueError as exc:
            QMessageBox.warning(self, "無法開啟", str(exc))
            return
        self._stop_worker()
        self._clear_results()
        self._last_roots = meta.get("roots") or []
        self._scanned_at = meta.get("scanned_at") or ""
        if self._last_roots:
            self.path_edit.setText("; ".join(self._last_roots))
        self._all_groups = groups
        self._update_summary()
        self.summary_widget.setVisible(True)
        self.tree.setHeaderHidden(not groups)
        self._sb_filter_body.setVisible(self._sb_filter_open)
        self._sb_qs_body.setVisible(self._sb_qs_open)
        self._apply_filter_sort()
        when = self._scanned_at.replace("T", " ") or "不明"
        logger.info(f"開啟結果檔: {path}（掃描於 {when}，{len(groups):,} 個群組）")
        self.status_bar.showMessage(
            f"已開啟結果檔（掃描於 {when}）：{len(groups):,} 個群組。"
            f"檔案可能已變動，移除前會重新檢查。")

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
                f'font-size="13" fill="#e4e7ee" font-weight="bold">{escape(title)}</text>')
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
                f'{escape(short)} ({pct:.1f}%)</text>')
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
                f"<tr><td><strong>{escape(ext)}</strong></td>"
                f"<td>{d['groups']}</td><td>{d['files']}</td>"
                f"<td>{human_size(d['total_size'])}</td>"
                f"<td style='color:#f87171'>{human_size(d['wasted'])}</td>"
                f"<td><div style='display:flex;align-items:center;gap:8px;'>"
                f"<div class='pb'><div class='pf' style='width:{min(pct,100):.1f}%'></div></div>"
                f"<span>{pct:.1f}%</span></div></td></tr>\n")

        page = f"""<!DOCTYPE html>
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
<div class="meta">掃描路徑：{escape(scan_path)}&nbsp;&nbsp;|&nbsp;&nbsp;產生時間：{scan_time}</div>
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

        # 報表存到固定位置（%LOCALAPPDATA%\DupeScan\reports），只保留最近 20 份
        reports = data_dir() / "reports"
        reports.mkdir(exist_ok=True)
        report_path = reports / f"dupescan_report_{datetime.datetime.now():%Y%m%d_%H%M%S}.html"
        report_path.write_text(page, encoding="utf-8")
        for old in sorted(reports.glob("dupescan_report_*.html"))[:-20]:
            old.unlink(missing_ok=True)

        webbrowser.open(report_path.as_uri())
        self.status_bar.showMessage(f"報表已匯出並開啟: {report_path}")
        logger.info(f"匯出報表: {report_path}")


# ── ConditionPanel（側邊列批量快速選取用的多條件勾選面板）────────────────────
class ConditionPanel(QWidget):
    """可重用的條件勾選面板，以 _checked_order 記錄勾選順序作為優先序依據。

    互斥機制：勾選某條件時，衝突條件的 QCheckBox 會被 disable，
    取消時再恢復可用，避免矛盾條件同時生效（例如「較新」和「較舊」）。
    智慧規則和基本條件共用同一個優先序，最下面一行顯示目前的順序。
    參考資料夾不是排序條件，有填就一律生效；內容改變（停手 0.3 秒）時發出 referenceChanged。
    """
    referenceChanged = pyqtSignal()

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

        smart = QLabel("智慧規則（方向固定，兩種模式都一樣）")
        smart.setToolTip("「優先移除」的檔案在兩種模式下都比較容易被勾選：\n"
                         "保留目標模式留下最不該移除的那份；刪除目標模式只勾最該移除的那份。")
        smart.setWordWrap(True)
        smart.setStyleSheet(f"font-size:11px;color:{SUBTEXT};padding-top:4px;")
        lay.addWidget(smart)
        copy_cb = self._make_cb("copy_name")
        copy_cb.setToolTip(
            "同一組裡有「photo.png」和「photo (1).png」「photo - 複製.png」「Copy of photo.png」\n"
            "「photo.png.dup1」「my+photo.png」「my%20photo.png」這類檔名時，保留原名那份，\n"
            "像副本的優先移除。檔名之間沒有這種關係時算平手，交給下一個條件。")
        lay.addWidget(copy_cb)

        folders_cb = self._make_cb("folders")
        folders_cb.setToolTip(
            "檔案在「優先保留」的資料夾就留下，在「優先移除」的就勾選；清單越前面越優先。\n"
            "可填完整路徑（D:\\Models，含子資料夾）或名稱片段（_trash、備份、Downloads：\n"
            "資料夾路徑中含有這段文字就算）。同一個檔案兩邊都符合時，以比較深的那層為準。")
        lay.addWidget(folders_cb)
        self._folders_box = QWidget()
        fl = QVBoxLayout(self._folders_box)
        fl.setContentsMargins(20, 0, 0, 2)
        fl.setSpacing(3)
        fl.addWidget(self._hint("優先保留（; 分隔，越前面越優先）"))
        self.keep_dirs_edit = QLineEdit()
        self.keep_dirs_edit.setPlaceholderText("例如 D:\\Models; 收藏")
        fl.addWidget(self.keep_dirs_edit)
        fl.addWidget(self._hint("優先移除（; 分隔，越前面越優先）"))
        self.remove_dirs_edit = QLineEdit()
        self.remove_dirs_edit.setPlaceholderText("例如 _trash; _bak; 備份; Downloads")
        fl.addWidget(self.remove_dirs_edit)
        lay.addWidget(self._folders_box)

        custom_cb = self._make_cb("custom")
        custom_cb.setToolTip(
            "以正規表示式比對完整路徑（不分大小寫；資料夾分隔寫 / 就好，也接受 \\\\）。\n"
            "每行一條：- 開頭＝符合的優先移除，+ 開頭＝符合的優先保留；# 開頭是註解。\n"
            "由上往下，第一條符合的規則決定，越上面越優先。")
        lay.addWidget(custom_cb)
        self._custom_box = QWidget()
        cl = QVBoxLayout(self._custom_box)
        cl.setContentsMargins(20, 0, 0, 2)
        cl.setSpacing(3)
        cl.addWidget(self._hint("每行一條：- 優先移除、+ 優先保留"))
        self.custom_edit = QPlainTextEdit()
        self.custom_edit.setPlaceholderText("- \\.dup\\d+$\n- /舊版本/\n+ ^D:/Models/")
        self.custom_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.custom_edit.setTabChangesFocus(True)
        self.custom_edit.setFixedHeight(84)
        self.custom_edit.textChanged.connect(self._check_custom)
        cl.addWidget(self.custom_edit)
        self._custom_error = QLabel()
        self._custom_error.setWordWrap(True)
        self._custom_error.setStyleSheet(f"font-size:11px;color:{DANGER};")
        self._custom_error.setVisible(False)
        cl.addWidget(self._custom_error)
        lay.addWidget(self._custom_box)

        ref_hint = self._hint("參考資料夾（裡面的檔案不會被勾選）")
        ref_hint.setStyleSheet(ref_hint.styleSheet() + "padding-top:4px;")
        lay.addWidget(ref_hint)
        self.reference_edit = QLineEdit()
        self.reference_edit.setPlaceholderText("例如 D:\\Master; 正本（; 分隔）")
        self.reference_edit.setToolTip(
            "放正本的資料夾：套用規則、右鍵快速選取、「全部勾選」都不會勾選裡面的檔案；\n"
            "群組裡有參考資料夾的檔案時，其他地方的副本照常勾選。手動勾了也會在移除前預覽\n"
            "被當成受保護、預設不移除。寫法和資料夾優先序相同（完整路徑或名稱片段）。")
        self._ref_timer = QTimer(self, singleShot=True, interval=300)
        self._ref_timer.timeout.connect(self.referenceChanged.emit)
        self.reference_edit.textChanged.connect(lambda _: self._ref_timer.start())
        lay.addWidget(self.reference_edit)

        self._order_label = QLabel()
        self._order_label.setWordWrap(True)
        self._order_label.setStyleSheet(f"font-size:11px;color:{SUBTEXT};padding-top:2px;")
        lay.addWidget(self._order_label)
        self._update_order_label()

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"font-size:11px;color:{SUBTEXT};")
        return lbl

    def _make_cb(self, cond_id: str) -> QCheckBox:
        cb = QCheckBox(_COND_BY_ID[cond_id][1])
        cb.setStyleSheet(f"font-size:12px;color:{TEXT};min-width:78px;")
        cb.stateChanged.connect(lambda s, cid=cond_id: self._on_toggle(cid, s))
        self._checkboxes[cond_id] = cb
        return cb

    def _update_order_label(self):
        """更新「優先序」說明，並只在勾選時顯示智慧規則的設定欄位。"""
        if self._checked_order:
            steps = [f"{i}. {_COND_BY_ID[cid][1]}" for i, cid in enumerate(self._checked_order, 1)]
            self._order_label.setText("優先序：" + " → ".join(steps))
        else:
            self._order_label.setText("優先序：（尚未勾選條件）")
        self._folders_box.setVisible("folders" in self._checked_order)
        self._custom_box.setVisible("custom" in self._checked_order)

    def _check_custom(self):
        """自訂規則一邊打一邊檢查，有錯的行列在輸入框下方。"""
        errors = parse_custom_rules(self.custom_edit.toPlainText())[1]
        shown = errors[:5] + ([f"…另外 {len(errors) - 5} 行有誤"] if len(errors) > 5 else [])
        self._custom_error.setText("\n".join(shown))
        self._custom_error.setVisible(bool(errors))

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
        self._update_order_label()

    def rule_set(self) -> RuleSet:
        """面板上目前的規則（操作模式由主視窗另外填）。"""
        return RuleSet(conditions=list(self._checked_order),
                       keep_dirs=split_dirs(self.keep_dirs_edit.text()),
                       remove_dirs=split_dirs(self.remove_dirs_edit.text()),
                       reference_dirs=split_dirs(self.reference_edit.text()),
                       custom=self.custom_edit.toPlainText())

    def set_rule_set(self, rules: RuleSet):
        """載入一組規則（預設集）：清掉目前的勾選，填入設定欄位，再依優先序勾選條件。"""
        for cid in reversed(list(self._checked_order)):
            self._checkboxes[cid].setChecked(False)
        self.keep_dirs_edit.setText("; ".join(rules.keep_dirs))
        self.remove_dirs_edit.setText("; ".join(rules.remove_dirs))
        self.reference_edit.setText("; ".join(rules.reference_dirs))
        self.custom_edit.setPlainText(rules.custom)
        for cid in rules.conditions:
            cb = self._checkboxes.get(cid)
            if cb is not None and cb.isEnabled():      # 互斥的條件以前面的為準
                cb.setChecked(True)
