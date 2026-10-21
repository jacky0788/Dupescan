# 「結果」畫面：每組重複一列標題，底下每份檔案前面一顆「保留／移除」膠囊（點一下切換）。
#
#   每組至少保留一份；保護的資料夾裡的檔案一律保留（膠囊顯示「保護」）。
#   上方：標題（幾組重複、最多可釋放多少）、掃描資訊、無法讀取的檔案；搜尋、類型、排序。
#   下方固定列：「將移除 N 個檔案，釋放 X」＋「自動選取規則…」＋「移到資源回收筒」。
#
# 清單用 QTreeView＋自訂 model：只畫看得到的列，幾萬組也不會卡；勾選狀態存在 FileInfo.selected。
import os
import subprocess
from dataclasses import dataclass, field

from PyQt6.QtCore import (
    QAbstractItemModel, QEvent, QModelIndex, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLineEdit, QMenu,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeView, QVBoxLayout, QWidget,
)

from ..formatting import format_duration, format_time, human_size
from ..models import DuplicateGroup, FileInfo
from ..scan_options import FILE_TYPES, ScanOptions, file_type_of
from ..selection import REASONS, AutoRules, apply_selection, auto_select
from .app_settings import SORTS
from .theme import (
    ACCENT, BORDER, PILL_KEEP, PILL_PROTECTED, PILL_REMOVE, TEXT, TEXT_2, TEXT_3, set_role,
)
from .widgets import ImagePreview, button, label

COL_ACTION, COL_NAME, COL_FOLDER, COL_TIME, COL_NOTE = range(5)
HEADERS = ["", "檔名", "資料夾", "修改時間", "說明"]
COLUMNS = len(HEADERS)
_FLAGS = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
_NO_FLAGS = Qt.ItemFlag.NoItemFlags
ROLE_KIND = Qt.ItemDataRole.UserRole          # "group"／"file"
ROLE_OBJ = Qt.ItemDataRole.UserRole + 1       # DuplicateGroup／FileInfo
ROLE_STATE = Qt.ItemDataRole.UserRole + 2     # 檔案："keep"／"remove"／"protected"
ROLE_GROUP = Qt.ItemDataRole.UserRole + 3     # 檔案所屬的群組

KEEP, REMOVE, PROTECTED = "keep", "remove", "protected"
PILL = {KEEP: ("保留", PILL_KEEP), REMOVE: ("移除", PILL_REMOVE), PROTECTED: ("保護", PILL_PROTECTED)}
MANUAL = "手動選擇"
KEEP_ALL = "全部保留"
OTHER_TYPE = "other"
GROUP_BG = "#1f232a"


@dataclass
class ResultsInfo:
    """結果的來源資訊（標題列顯示、重新掃描、存檔用）。"""
    roots: list[str]
    scanned_at: str = ""
    options: ScanOptions | None = None
    files_seen: int | None = None
    elapsed: float | None = None
    unreadable: int = 0
    unreadable_samples: list[str] = field(default_factory=list)
    cache_note: str = ""
    source: str = ""                 # 從結果檔開啟時的檔案路徑

    @classmethod
    def from_summary(cls, summary) -> "ResultsInfo":
        st = summary.stats
        return cls(roots=list(summary.options.roots), scanned_at=summary.scanned_at,
                   options=summary.options, files_seen=st.files_seen, elapsed=summary.elapsed,
                   unreadable=st.unreadable, unreadable_samples=list(st.unreadable_samples),
                   cache_note=st.cache_note)

    @classmethod
    def from_meta(cls, meta: dict, source: str = "") -> "ResultsInfo":
        scan = meta.get("scan") or {}
        options = ScanOptions.from_dict(scan["options"]) if isinstance(scan.get("options"), dict) else None
        if options is not None and not options.roots:
            options = None

        def number(key, kind):
            value = scan.get(key)
            return value if isinstance(value, kind) and not isinstance(value, bool) else None

        samples = scan.get("unreadable_samples")
        return cls(roots=list(meta.get("roots") or []), scanned_at=str(meta.get("scanned_at") or ""),
                   options=options, files_seen=number("files_seen", int),
                   elapsed=number("elapsed", (int, float)), unreadable=number("unreadable", int) or 0,
                   unreadable_samples=[s for s in samples if isinstance(s, str)] if isinstance(samples, list) else [],
                   cache_note=str(scan.get("cache_note") or ""), source=source)

    def to_scan_dict(self) -> dict:
        return {"options": self.options.to_dict() if self.options else None,
                "elapsed": self.elapsed, "files_seen": self.files_seen,
                "unreadable": self.unreadable, "unreadable_samples": self.unreadable_samples,
                "cache_note": self.cache_note}

    def rescan_options(self) -> ScanOptions:
        return self.options or ScanOptions(roots=list(self.roots))


# ── Model ────────────────────────────────────────────────────────────────
class ResultsModel(QAbstractItemModel):
    """兩層：群組 → 檔案。只有符合搜尋與類型的群組會出現（_rows），依排序方式排列。"""
    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups: list[DuplicateGroup] = []
        self._rows: list[DuplicateGroup] = []
        self._row_of: dict[int, int] = {}
        self._haystack: dict[int, str] = {}
        self._protected_ids: set[int] = set()
        self.rules = AutoRules()
        self.query = ""
        self.type_key = ""
        self.sort_key = "wasted"

    # ── 資料 ──────────────────────────────────────────────────────────
    @property
    def groups(self) -> list[DuplicateGroup]:
        return self._groups

    @property
    def visible_groups(self) -> list[DuplicateGroup]:
        return self._rows

    def set_groups(self, groups: list[DuplicateGroup], rules: AutoRules):
        self._groups = list(groups)
        self._haystack.clear()
        self.rules = rules
        self._update_protected()
        self.refresh()

    def _update_protected(self):
        protected = self.rules.protected()
        self._protected_ids = set()
        if not protected:
            return
        for g in self._groups:
            for fi in g.files:
                if protected.contains(fi.path):
                    self._protected_ids.add(id(fi))
                    fi.selected = False                   # 保護的檔案一律保留

    def is_protected(self, fi: FileInfo) -> bool:
        return id(fi) in self._protected_ids

    def state(self, fi: FileInfo) -> str:
        if id(fi) in self._protected_ids:
            return PROTECTED
        return REMOVE if fi.selected else KEEP

    # ── 篩選與排序 ────────────────────────────────────────────────────
    def set_view(self, query: str | None = None, type_key: str | None = None, sort_key: str | None = None):
        if query is not None:
            self.query = query.strip().casefold()
        if type_key is not None:
            self.type_key = type_key
        if sort_key is not None:
            self.sort_key = sort_key
        self.refresh()

    def _matches(self, g: DuplicateGroup) -> bool:
        if self.type_key:
            types = {file_type_of(fi.name) or OTHER_TYPE for fi in g.files}
            if self.type_key not in types:
                return False
        if self.query:
            hay = self._haystack.get(id(g))
            if hay is None:
                hay = "\n".join(str(fi.path) for fi in g.files).casefold()
                self._haystack[id(g)] = hay
            return self.query in hay
        return True

    def _sort_key(self):
        if self.sort_key == "count":
            return lambda g: (-len(g.files), -g.wasted_bytes)
        if self.sort_key == "name":
            return lambda g: (self.kept_file(g).name.casefold(), -g.wasted_bytes)
        return lambda g: -g.wasted_bytes

    def refresh(self):
        self.beginResetModel()
        rows = [g for g in self._groups if self._matches(g)]
        rows.sort(key=self._sort_key())
        self._rows = rows
        self._row_of = {id(g): i for i, g in enumerate(rows)}
        self.endResetModel()

    # ── QAbstractItemModel ───────────────────────────────────────────
    # 這幾個方法每次重新整理會被呼叫幾十萬次，盡量寫得簡單快速
    def index(self, row, column, parent=QModelIndex()):
        if column < 0 or column >= COLUMNS or row < 0:
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, None) if row < len(self._rows) else QModelIndex()
        if parent.internalPointer() is not None:
            return QModelIndex()
        g = self._rows[parent.row()]
        return self.createIndex(row, column, g) if row < len(g.files) else QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        g = index.internalPointer()
        if g is None:
            return QModelIndex()
        row = self._row_of.get(id(g))
        return self.createIndex(row, 0, None) if row is not None else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return len(self._rows)
        if parent.column() == 0 and parent.internalPointer() is None:
            return len(self._rows[parent.row()].files)
        return 0

    def columnCount(self, parent=QModelIndex()):
        return COLUMNS

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        return _FLAGS if index.isValid() else _NO_FLAGS

    def group_at(self, index) -> DuplicateGroup | None:
        if not index.isValid():
            return None
        g = index.internalPointer()
        return self._rows[index.row()] if g is None else g

    def file_at(self, index) -> FileInfo | None:
        if not index.isValid() or index.internalPointer() is None:
            return None
        return index.internalPointer().files[index.row()]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        parent_group = index.internalPointer()
        if parent_group is None:
            g = self._rows[index.row()]
            if role == ROLE_KIND:
                return "group"
            if role in (ROLE_OBJ, ROLE_GROUP):
                return g
            if role == Qt.ItemDataRole.DisplayRole and index.column() == 0:
                return self.kept_file(g).name
            return None
        g = parent_group
        fi = g.files[index.row()]
        col = index.column()
        if role == ROLE_KIND:
            return "file"
        if role == ROLE_OBJ:
            return fi
        if role == ROLE_GROUP:
            return g
        if role == ROLE_STATE:
            return self.state(fi)
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_ACTION:
                return PILL[self.state(fi)][0]
            if col == COL_NAME:
                return fi.name + (f"（另有 {len(fi.aliases)} 個硬連結）" if fi.aliases else "")
            if col == COL_FOLDER:
                return fi.folder
            if col == COL_TIME:
                return format_time(fi.mtime) if fi.mtime else ""
            if col == COL_NOTE:
                return self.note(g, fi)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_ACTION:
                return ("在保護的資料夾，一律保留" if self.is_protected(fi)
                        else "點一下切換「保留／移除」（空白鍵也可以）")
            if col in (COL_NAME, COL_FOLDER):
                tip = str(fi.path)
                if fi.aliases:
                    tip += "\n\n同一個檔案的其他路徑（硬連結，不佔額外空間）：\n" + "\n".join(fi.aliases)
                return tip
            if col == COL_NOTE:
                return self.note(g, fi) or None
        if role == Qt.ItemDataRole.ForegroundRole and col in (COL_FOLDER, COL_TIME, COL_NOTE):
            return QColor(TEXT_2)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == COL_TIME:
            return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        return None

    # ── 顯示用 ────────────────────────────────────────────────────────
    def kept_file(self, g: DuplicateGroup) -> FileInfo:
        """群組的代表：第一份保留的檔案（都標成移除時取第一份）。"""
        return next((fi for fi in g.files if not fi.selected), g.files[0])

    def note(self, g: DuplicateGroup, fi: FileInfo) -> str:
        if self.is_protected(fi):
            return REASONS["protected"]
        if fi.selected:
            return ""
        if fi is self.kept_file(g):
            return g.keep_reason
        return ""

    @staticmethod
    def removing(g: DuplicateGroup) -> int:
        return sum(1 for fi in g.files if fi.selected)

    # ── 修改選取 ──────────────────────────────────────────────────────
    def toggle(self, fi: FileInfo, g: DuplicateGroup) -> str:
        """切換保留／移除；回傳 "ok"、"protected"（保護的檔案）或 "last"（這組只剩這份保留）。"""
        if self.is_protected(fi):
            return "protected"
        if fi.selected:
            fi.selected = False
        else:
            if not any(not other.selected for other in g.files if other is not fi):
                return "last"
            fi.selected = True
        g.keep_reason = MANUAL
        self._group_changed(g)
        return "ok"

    def keep_only(self, fi: FileInfo, g: DuplicateGroup):
        for other in g.files:
            other.selected = other is not fi and not self.is_protected(other)
        g.keep_reason = MANUAL
        self._group_changed(g)

    def keep_all(self, g: DuplicateGroup):
        for fi in g.files:
            fi.selected = False
        g.keep_reason = KEEP_ALL
        self._group_changed(g)

    def reselect(self, g: DuplicateGroup):
        decided = apply_selection(g, self.rules.conditions(), self.rules.protected())
        g.keep_reason = REASONS.get(decided, REASONS[None])
        self._group_changed(g)

    def apply_rules(self, rules: AutoRules):
        """套用新規則到所有群組（手動的調整會被取代）；回傳各原因的組數。"""
        self.rules = rules
        counts = auto_select(self._groups, rules)
        self._update_protected()
        self.refresh()
        self.selection_changed.emit()
        return counts

    def remove_paths(self, removed: set[str]):
        """移除完成後：把已經不在的檔案拿掉；剩一份的群組不再是重複，整組拿掉。"""
        kept_groups = []
        for g in self._groups:
            g.files = [fi for fi in g.files if str(fi.path) not in removed]
            if len(g.files) > 1:
                kept_groups.append(g)
        self._groups = kept_groups
        self._haystack.clear()
        self.refresh()
        self.selection_changed.emit()

    def _group_changed(self, g: DuplicateGroup):
        row = self._row_of.get(id(g))
        if row is not None:
            top = self.index(row, 0)
            self.dataChanged.emit(top, self.index(row, len(HEADERS) - 1))
            self.dataChanged.emit(self.index(0, 0, top),
                                  self.index(len(g.files) - 1, len(HEADERS) - 1, top))
        self.selection_changed.emit()

    def totals(self) -> tuple[int, int, int]:
        """(要移除的檔案數, 會釋放的位元組, 其中不在目前篩選結果裡的檔案數)。"""
        count = size = hidden = 0
        for g in self._groups:
            n = self.removing(g)
            if n:
                count += n
                size += n * g.size
                if id(g) not in self._row_of:
                    hidden += n
        return count, size, hidden


# ── 繪製 ─────────────────────────────────────────────────────────────────
class ResultsDelegate(QStyledItemDelegate):
    """檔案列第一欄畫成「保留／移除／保護」膠囊；列高一致（清單可以只畫看得到的部分）。"""

    def __init__(self, view: "ResultsView"):
        super().__init__(view)
        self._view = view

    def row_height(self) -> int:
        return self._view.fontMetrics().height() + 16

    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), self.row_height())

    def paint(self, painter, option, index):
        if index.column() == COL_ACTION and index.data(ROLE_KIND) == "file":
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            opt.text = ""
            style = opt.widget.style() if opt.widget else None
            if style is not None:
                style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
            self.paint_pill(painter, option.rect, index.data(ROLE_STATE),
                            bool(option.state & QStyle.StateFlag.State_MouseOver))
            return
        super().paint(painter, option, index)

    def paint_pill(self, painter: QPainter, cell: QRect, state: str, hover: bool):
        text, (bg, fg) = PILL[state]
        fm = self._view.fontMetrics()
        w, h = max(fm.horizontalAdvance(text) + 22, 50), min(fm.height() + 6, cell.height() - 6)
        rect = QRect(cell.left() + 6, cell.center().y() - h // 2 + 1, w, h)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(bg))
        painter.setPen(QPen(QColor(fg), 1) if hover and state != PROTECTED else Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, h / 2, h / 2)
        font = QFont(self._view.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(fg))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class ResultsView(QTreeView):
    """結果清單：點膠囊切換、點群組列展開／收合、雙擊開啟檔案、停在圖片上顯示縮圖。"""
    toggle_requested = pyqtSignal(object, object)       # FileInfo, DuplicateGroup
    open_requested = pyqtSignal(object)                 # FileInfo
    menu_requested = pyqtSignal(object, object, QPoint)  # FileInfo 或 None, DuplicateGroup, 全域座標

    def __init__(self, model: ResultsModel, parent=None):
        super().__init__(parent)
        self.setModel(model)
        self._model = model
        self.delegate = ResultsDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setUniformRowHeights(True)
        self.setRootIsDecorated(True)
        self.setIndentation(18)
        self.setAllColumnsShowFocus(True)
        self.setExpandsOnDoubleClick(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        hdr = self.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(COL_ACTION, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_FOLDER, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_NOTE, QHeaderView.ResizeMode.Interactive)
        self.preview = ImagePreview(self)
        self._press: tuple[QModelIndex, bool] | None = None
        self.update_column_widths()

    def update_column_widths(self):
        fm = self.fontMetrics()
        # 檔案列在第二層，第一欄前面有兩層縮排；膠囊畫在縮排之後
        self.setColumnWidth(COL_ACTION, 2 * self.indentation() + fm.horizontalAdvance("保留") + 40)
        self.setColumnWidth(COL_NAME, fm.horizontalAdvance("M") * 26)
        self.setColumnWidth(COL_TIME, fm.horizontalAdvance("2026-10-21 14:03") + 24)
        self.setColumnWidth(COL_NOTE, fm.horizontalAdvance("不在優先移除的資料夾") + 24)

    # ── 群組列：整列自己畫（背景、展開箭頭、檔名、份數與可釋放空間）────
    def drawRow(self, painter, option, index):
        if index.data(ROLE_KIND) != "group":
            super().drawRow(painter, option, index)
            return
        g = index.data(ROLE_OBJ)
        rect = QRect(option.rect)
        painter.save()
        painter.fillRect(rect, QColor(GROUP_BG))
        painter.setPen(QColor(BORDER))
        painter.drawLine(rect.topLeft(), rect.topRight())
        if self.currentIndex().isValid() and self.currentIndex() == index and self.hasFocus():
            painter.setPen(QPen(QColor(ACCENT), 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
        item = self.visualRect(index)
        indent = self.indentation()
        self.drawBranches(painter, QRect(item.left() - indent, rect.top(), indent, rect.height()), index)
        fm = self.fontMetrics()
        left, right = item.left() + 6, rect.right() - 14
        model = self._model
        removing = model.removing(g)
        tail = (f"移除 {removing} 份，釋放 {human_size(removing * g.size)}" if removing else "全部保留")
        tail_w = fm.horizontalAdvance(tail)
        painter.setPen(QColor(TEXT_2 if removing else TEXT_3))
        painter.drawText(QRect(right - tail_w, rect.top(), tail_w, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, tail)
        info = f"{len(g.files)} 份 · 每份 {human_size(g.size)}"
        info_w = fm.horizontalAdvance(info)
        name_room = max(right - tail_w - 24 - info_w - 16 - left, 40)
        bold = QFont(self.font())
        bold.setBold(True)
        painter.setFont(bold)
        bfm = painter.fontMetrics()
        name = bfm.elidedText(model.kept_file(g).name, Qt.TextElideMode.ElideMiddle, name_room)
        painter.setPen(QColor(TEXT))
        name_w = bfm.horizontalAdvance(name)
        painter.drawText(QRect(left, rect.top(), name_w + 2, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        painter.setFont(self.font())
        painter.setPen(QColor(TEXT_2))
        painter.drawText(QRect(left + name_w + 16, rect.top(), info_w + 2, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, info)
        painter.restore()

    # ── 滑鼠與鍵盤 ────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        on_branch = index.isValid() and event.position().x() < self.visualRect(index).left()
        self._press = (index, on_branch) if event.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        press, self._press = self._press, None
        super().mouseReleaseEvent(event)
        if press is None or event.button() != Qt.MouseButton.LeftButton:
            return
        index = self.indexAt(event.position().toPoint())
        if not index.isValid() or press[0] != index or press[1]:
            return
        if index.data(ROLE_KIND) == "group":
            self.setExpanded(index, not self.isExpanded(index))
        elif index.column() == COL_ACTION:
            self.toggle_requested.emit(index.data(ROLE_OBJ), index.data(ROLE_GROUP))

    def mouseDoubleClickEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index.data(ROLE_KIND) == "file" and index.column() != COL_ACTION:
            self.open_requested.emit(index.data(ROLE_OBJ))
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        index = self.currentIndex()
        if index.isValid() and index.data(ROLE_KIND) == "file":
            if event.key() == Qt.Key.Key_Space:
                self.toggle_requested.emit(index.data(ROLE_OBJ), index.data(ROLE_GROUP))
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.open_requested.emit(index.data(ROLE_OBJ))
                return
        elif index.isValid() and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.setExpanded(index, not self.isExpanded(index))
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        index = self.indexAt(event.pos())
        if not index.isValid():
            return
        self.setCurrentIndex(index)
        fi = index.data(ROLE_OBJ) if index.data(ROLE_KIND) == "file" else None
        self.menu_requested.emit(fi, index.data(ROLE_GROUP), event.globalPos())

    def viewportEvent(self, event):
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            index = self.indexAt(event.position().toPoint())
            fi = index.data(ROLE_OBJ) if index.isValid() and index.data(ROLE_KIND) == "file" else None
            if fi is not None and index.column() in (COL_NAME, COL_FOLDER) and ImagePreview.supports(fi.path):
                self.preview.show_for(fi.path, event.globalPosition().toPoint())
            else:
                self.preview.reset()
        elif kind in (QEvent.Type.Leave, QEvent.Type.Wheel, QEvent.Type.MouseButtonPress):
            self.preview.reset()
        return super().viewportEvent(event)

    def hideEvent(self, event):
        self.preview.reset()
        super().hideEvent(event)


# ── 畫面 ─────────────────────────────────────────────────────────────────
class ResultsPage(QWidget):
    home_requested = pyqtSignal()
    rescan_requested = pyqtSignal()
    rules_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    unreadable_requested = pyqtSignal()
    open_file_requested = pyqtSignal(object)            # FileInfo
    reveal_file_requested = pyqtSignal(object)          # FileInfo
    selection_edited = pyqtSignal()                     # 使用者改了保留／移除（存檔用）
    message = pyqtSignal(str, str)                      # (文字, 種類)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.info: ResultsInfo | None = None
        self.model = ResultsModel(self)
        self.model.selection_changed.connect(self._update_bottom)
        self._collapsed: set[int] = set()       # 使用者收合的群組（重新整理後維持收合）
        self._restoring = False
        self.remove_label = "移到資源回收筒"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── 標題列 ────────────────────────────────────────────────────
        head = QWidget()
        head_lay = QVBoxLayout(head)
        head_lay.setContentsMargins(24, 18, 24, 10)
        head_lay.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.title = label("", "title")
        row.addWidget(self.title, 1)
        row.addWidget(button("重新掃描", slot=self.rescan_requested.emit,
                             tooltip="用同樣的設定再掃一次（已經比對過、沒有變動的檔案不必再讀）"))
        row.addWidget(button("回到開始", slot=self.home_requested.emit))
        head_lay.addLayout(row)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(12)
        self.meta = label("", "muted")
        self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        meta_row.addWidget(self.meta)
        self.unreadable_button = button("", "link", self.unreadable_requested.emit)
        meta_row.addWidget(self.unreadable_button)
        meta_row.addStretch()
        head_lay.addLayout(meta_row)
        self.notice = label("", "warning", wrap=True)
        self.notice.hide()
        head_lay.addWidget(self.notice)
        lay.addWidget(head)

        # ── 搜尋、類型、排序 ──────────────────────────────────────────
        self.tools = QWidget()
        tools = QHBoxLayout(self.tools)
        tools.setContentsMargins(24, 4, 24, 10)
        tools.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋檔名或資料夾")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=250)
        self._search_timer.timeout.connect(self._apply_view)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        tools.addWidget(self.search, 1)
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部類型", "")
        for key, (name, _) in FILE_TYPES.items():
            self.type_combo.addItem(name, key)
        self.type_combo.addItem("其他", OTHER_TYPE)
        self.type_combo.currentIndexChanged.connect(lambda _: self._apply_view())
        tools.addWidget(self.type_combo)
        self.sort_combo = QComboBox()
        for key, name in SORTS.items():
            self.sort_combo.addItem(f"排序：{name}", key)
        self.sort_combo.currentIndexChanged.connect(lambda _: self._apply_view())
        tools.addWidget(self.sort_combo)
        self.count_label = label("", "faint")
        tools.addWidget(self.count_label)
        lay.addWidget(self.tools)

        # ── 清單／沒有結果 ────────────────────────────────────────────
        self.body = QStackedWidget()
        list_box = QWidget()
        list_lay = QVBoxLayout(list_box)
        list_lay.setContentsMargins(24, 0, 24, 12)
        self.view = ResultsView(self.model)
        self.view.toggle_requested.connect(self._toggle)
        self.view.open_requested.connect(self.open_file_requested.emit)
        self.view.menu_requested.connect(self._menu)
        self.view.collapsed.connect(self._on_collapsed)
        self.view.expanded.connect(self._on_expanded)
        list_lay.addWidget(self.view)
        self.no_match = label("", "muted")
        self.no_match.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_match.hide()
        list_lay.addWidget(self.no_match)
        self.body.addWidget(list_box)

        empty = QWidget()
        empty_lay = QVBoxLayout(empty)
        empty_lay.addStretch(2)
        self.empty_title = label("沒有找到重複的檔案", "title")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_title)
        self.empty_text = label("", "muted", wrap=True)
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_text)
        empty_lay.addStretch(3)
        self.body.addWidget(empty)
        lay.addWidget(self.body, 1)

        # ── 底部固定列 ────────────────────────────────────────────────
        bottom = QFrame()
        bottom.setObjectName("bottomBar")
        bottom_lay = QHBoxLayout(bottom)
        bottom_lay.setContentsMargins(24, 12, 24, 12)
        bottom_lay.setSpacing(10)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.summary = label("", "heading")
        self.summary_hint = label("", "faint")
        texts.addWidget(self.summary)
        texts.addWidget(self.summary_hint)
        bottom_lay.addLayout(texts, 1)
        bottom_lay.addWidget(button("自動選取規則…", slot=self.rules_requested.emit,
                                    tooltip="決定每一組要保留哪一份"))
        self.remove_button = button(self.remove_label, "danger", self.remove_requested.emit)
        bottom_lay.addWidget(self.remove_button)
        self.bottom = bottom
        lay.addWidget(bottom)

    # ── 內容 ──────────────────────────────────────────────────────────
    def set_results(self, groups: list[DuplicateGroup], info: ResultsInfo, rules: AutoRules,
                    sort_key: str = "wasted"):
        self.info = info
        self._collapsed.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.type_combo.blockSignals(True)
        self.type_combo.setCurrentIndex(0)
        self.type_combo.blockSignals(False)
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(max(self.sort_combo.findData(sort_key), 0))
        self.sort_combo.blockSignals(False)
        self.model.query, self.model.type_key, self.model.sort_key = "", "", self.sort_combo.currentData()
        self.model.set_groups(groups, rules)
        self._after_refresh()
        self._update_header()
        self._update_bottom()

    def sort_key(self) -> str:
        return self.sort_combo.currentData()

    def groups(self) -> list[DuplicateGroup]:
        return self.model.groups

    def set_remove_method(self, method: str):
        self.remove_label = "移到隔離資料夾" if method == "quarantine" else "移到資源回收筒"
        self._update_bottom()

    def apply_rules(self, rules: AutoRules):
        counts = self.model.apply_rules(rules)
        self._after_refresh()
        self._update_header()
        return counts

    def files_removed(self, removed: set[str]):
        self.model.remove_paths(removed)
        self._after_refresh()
        self._update_header()

    def set_font_metrics_changed(self):
        self.view.update_column_widths()

    # ── 更新畫面 ──────────────────────────────────────────────────────
    def _apply_view(self):
        self.model.set_view(self.search.text(), self.type_combo.currentData(), self.sort_combo.currentData())
        self._after_refresh()
        self._update_bottom()

    def _on_collapsed(self, index):
        if not self._restoring:
            self._collapsed.add(id(self.model.group_at(index)))

    def _on_expanded(self, index):
        if not self._restoring:
            self._collapsed.discard(id(self.model.group_at(index)))

    def _after_refresh(self):
        self._restoring = True
        try:
            self.view.expandAll()
            if self._collapsed:
                for row, g in enumerate(self.model.visible_groups):
                    if id(g) in self._collapsed:
                        self.view.setExpanded(self.model.index(row, 0), False)
        finally:
            self._restoring = False
        total, shown = len(self.model.groups), len(self.model.visible_groups)
        filtering = bool(self.model.query or self.model.type_key)
        self.count_label.setText(f"顯示 {shown:,} / {total:,} 組" if filtering else "")
        self.body.setCurrentIndex(0 if total else 1)
        no_match = bool(total and not shown)
        self.view.setVisible(not no_match)
        self.no_match.setVisible(no_match)
        if no_match:
            self.no_match.setText("沒有符合條件的重複檔案。換個關鍵字或類型試試。")

    def _update_header(self):
        groups = self.model.groups
        info = self.info
        wasted = sum(g.wasted_bytes for g in groups)
        self.title.setText(f"{len(groups):,} 組重複 · 最多可釋放 {human_size(wasted)}" if groups
                           else "掃描結果")
        parts = []
        if info:
            roots = info.roots
            if roots:
                parts.append(roots[0] if len(roots) == 1 else f"{roots[0]} 等 {len(roots)} 個資料夾")
            if info.scanned_at:
                parts.append(f"{format_time(info.scanned_at)} 掃描")
            if info.files_seen is not None:
                parts.append(f"檢查了 {info.files_seen:,} 個檔案")
            if info.elapsed is not None:
                parts.append(f"費時 {format_duration(info.elapsed)}")
            self.meta.setToolTip("\n".join(roots))
        self.meta.setText(" · ".join(parts))
        unreadable = info.unreadable if info else 0
        self.unreadable_button.setText(f"⚠ {unreadable:,} 個檔案或資料夾無法讀取" if unreadable else "")
        self.unreadable_button.setVisible(bool(unreadable))
        note = info.cache_note if info else ""
        self.notice.setText(f"雜湊快取：{note}" if note else "")
        self.notice.setVisible(bool(note))
        if not groups and info:
            where = info.roots[0] if len(info.roots) == 1 else "這些資料夾"
            seen = f"的 {info.files_seen:,} 個檔案" if info.files_seen else "裡的檔案"
            self.empty_text.setText(f"{where}{seen}內容都不一樣。")
        self.bottom.setVisible(bool(groups))
        self.tools.setVisible(bool(groups))

    def _update_bottom(self):
        count, size, hidden = self.model.totals()
        if count:
            self.summary.setText(f"將移除 {count:,} 個檔案，釋放 {human_size(size)}")
            hint = "每組都至少保留一份"
            if hidden:
                hint += f"；其中 {hidden:,} 個不在目前的搜尋結果中"
            self.summary_hint.setText(hint)
        else:
            self.summary.setText("目前沒有要移除的檔案")
            self.summary_hint.setText("點檔案前面的「保留」可以改成「移除」")
        self.remove_button.setText(self.remove_label + (f"（{count:,} 個）" if count else ""))
        self.remove_button.setEnabled(count > 0)
        set_role(self.summary, "heading")

    # ── 操作 ──────────────────────────────────────────────────────────
    def _toggle(self, fi: FileInfo, g: DuplicateGroup):
        result = self.model.toggle(fi, g)
        if result == "protected":
            self.message.emit("這個檔案在保護的資料夾，一律保留（可以在「自動選取規則」修改）", "info")
        elif result == "last":
            self.message.emit("每組至少要保留一份", "info")
        else:
            self.selection_edited.emit()

    def _menu(self, fi: FileInfo | None, g: DuplicateGroup, pos: QPoint):
        menu = QMenu(self)
        if fi is not None:
            menu.addAction("開啟檔案", lambda: self.open_file_requested.emit(fi))
            menu.addAction("在檔案總管中顯示", lambda: self.reveal_file_requested.emit(fi))
            menu.addSeparator()
            menu.addAction("只保留這一份", lambda: self._edit(self.model.keep_only, fi, g))
        menu.addAction("這組全部保留", lambda: self._edit(self.model.keep_all, g))
        menu.addAction("這組依規則重新選取", lambda: self._edit(self.model.reselect, g))
        if fi is not None:
            menu.addSeparator()
            menu.addAction("複製路徑", lambda: QApplication.clipboard().setText(str(fi.path)))
        menu.exec(pos)
        menu.deleteLater()

    def _edit(self, fn, *args):
        fn(*args)
        self.selection_edited.emit()


def reveal_in_explorer(path) -> str:
    """在檔案總管中選取這個檔案；回傳錯誤訊息（成功時為空字串）。"""
    path = os.path.normpath(str(path))
    if not os.path.exists(path):
        return "檔案已經不在了"
    try:
        subprocess.Popen(f'explorer /select,"{path}"')   # 不經過 shell：路徑中的 % 或 & 不會被解讀
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""


def open_file(path) -> str:
    """用預設程式開啟；回傳錯誤訊息（成功時為空字串）。"""
    try:
        os.startfile(os.path.normpath(str(path)))
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""
