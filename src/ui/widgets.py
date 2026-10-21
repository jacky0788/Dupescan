# 新介面共用的小元件：文字標籤、卡片、晶片選單、資料夾清單、提示框、浮動訊息、圖片預覽。
import os
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFileDialog, QFileIconProvider, QFrame, QHBoxLayout, QInputDialog, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .theme import set_role

IMAGE_PREVIEW_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
                      ".ico"}


# ── 基本元件 ─────────────────────────────────────────────────────────────
def label(text: str = "", role: str | None = None, wrap: bool = False,
          selectable: bool = False) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    if selectable:
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


def button(text: str, kind: str | None = None, slot=None, tooltip: str = "") -> QPushButton:
    """kind：primary／danger／link／icon／chip（對應 theme 的樣式）。"""
    btn = QPushButton(text)
    if kind:
        btn.setObjectName(kind)
    if slot is not None:
        btn.clicked.connect(lambda _checked=False, run=slot: run())   # 不把 checked 傳給 slot
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def card(margins: int = 16, spacing: int = 10) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("card", True)
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(spacing)
    return frame, lay


def divider() -> QFrame:
    line = QFrame()
    line.setProperty("divider", True)
    return line


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def is_inside(child: str, parent: str) -> bool:
    """child 是否就是 parent 或在它底下（不分大小寫）。"""
    c = os.path.normcase(os.path.normpath(child))
    p = os.path.normcase(os.path.normpath(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def folder_problem(path: str) -> str:
    """資料夾無法掃描的原因；可以掃描時回傳空字串。"""
    try:
        if not os.path.exists(path):
            return "找不到這個資料夾（已刪除、改名，或磁碟沒接上）"
        if not os.path.isdir(path):
            return "這不是資料夾"
        with os.scandir(path) as it:
            next(it, None)
    except PermissionError:
        return "沒有權限讀取這個資料夾"
    except OSError as exc:
        return f"無法讀取這個資料夾（{exc.strerror or exc}）"
    return ""


# ── 晶片選單 ─────────────────────────────────────────────────────────────
class ChipGroup(QWidget):
    """一排可切換的晶片。

    all_key：「全部」晶片的 key——選它會取消其他的；其他都沒選時自動回到「全部」。
    exclusive：單選（像選項按鈕）。
    """
    changed = pyqtSignal()

    def __init__(self, items: list[tuple[str, str]], all_key: str | None = None,
                 exclusive: bool = False, parent=None):
        super().__init__(parent)
        self.all_key = all_key
        self.exclusive = exclusive
        self.chips: dict[str, QPushButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for key, text in items:
            chip = button(text, "chip")
            chip.setCheckable(True)
            chip.clicked.connect(lambda checked, k=key: self._clicked(k, checked))
            lay.addWidget(chip)
            self.chips[key] = chip
        lay.addStretch()
        if all_key:
            self.chips[all_key].setChecked(True)
        elif exclusive and items:
            self.chips[items[0][0]].setChecked(True)

    def _clicked(self, key: str, checked: bool):
        if self.exclusive:
            for k, chip in self.chips.items():
                chip.setChecked(k == key)
        elif key == self.all_key:
            for k, chip in self.chips.items():
                chip.setChecked(k == key)
        else:
            if self.all_key:
                self.chips[self.all_key].setChecked(False)
                if not any(chip.isChecked() for chip in self.chips.values()):
                    self.chips[self.all_key].setChecked(True)
        self.changed.emit()

    def selected(self) -> list[str]:
        """選了哪些（不含「全部」；選「全部」時回傳空清單）。"""
        return [k for k, chip in self.chips.items() if chip.isChecked() and k != self.all_key]

    def current(self) -> str:
        """單選時目前選的 key。"""
        return next((k for k, chip in self.chips.items() if chip.isChecked()), "")

    def set_selected(self, keys):
        keys = [k for k in keys if k in self.chips and k != self.all_key]
        if self.exclusive:
            keys = keys[:1] or [next(iter(self.chips))]
        for k, chip in self.chips.items():
            chip.setChecked(k in keys)
        if self.all_key and not keys:
            self.chips[self.all_key].setChecked(True)


# ── 資料夾清單 ───────────────────────────────────────────────────────────
class _FolderRow(QFrame):
    removed = pyqtSignal(str)

    def __init__(self, path: str, is_name: bool, icon, parent=None):
        super().__init__(parent)
        self.path = path
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 0, 2)
        lay.setSpacing(10)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon.pixmap(QSize(18, 18)))
        icon_lbl.setFixedWidth(20)
        lay.addWidget(icon_lbl, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.text = label(f"名稱含「{path}」的資料夾" if is_name else path)
        self.text.setToolTip(path)
        self.text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.error = label("", "error", wrap=True)
        self.error.hide()
        texts.addWidget(self.text)
        texts.addWidget(self.error)
        lay.addLayout(texts, 1)
        remove = button("✕", "icon", lambda: self.removed.emit(self.path), "從清單移除")
        remove.setAccessibleName(f"移除 {path}")
        lay.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)

    def set_error(self, text: str):
        self.error.setText(f"⚠ {text}" if text else "")
        self.error.setVisible(bool(text))


class FolderListWidget(QWidget):
    """資料夾清單：「＋ 加入資料夾」挑選、拖放加入、每列 ✕ 移除，無法使用的資料夾在該列標紅。

    merge_nested：加入的資料夾在清單裡某個資料夾底下時不重複加入；包含清單裡的資料夾時取代它們。
    allow_names：另外可以輸入資料夾名稱（例如 Downloads），任何位置名稱符合的資料夾都算。
    """
    changed = pyqtSignal()
    message = pyqtSignal(str)          # 給使用者的說明（由視窗以浮動訊息顯示）

    def __init__(self, empty_text: str, add_text: str = "＋ 加入資料夾",
                 merge_nested: bool = False, allow_names: bool = False,
                 dialog_title: str = "選擇資料夾", parent=None):
        super().__init__(parent)
        self.merge_nested = merge_nested
        self.allow_names = allow_names
        self.dialog_title = dialog_title
        self._paths: list[str] = []
        self._errors: dict[str, str] = {}
        self._rows: dict[str, _FolderRow] = {}
        provider = QFileIconProvider()
        self._folder_icon = provider.icon(QFileIconProvider.IconType.Folder)
        self._name_icon = provider.icon(QFileIconProvider.IconType.Folder)
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.box, box_lay = card(margins=10, spacing=4)
        self._rows_lay = QVBoxLayout()
        self._rows_lay.setSpacing(2)
        box_lay.addLayout(self._rows_lay)
        self.empty = label(empty_text, "muted", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(56)
        box_lay.addWidget(self.empty)
        lay.addWidget(self.box)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.add_button = button(add_text, slot=self.browse)
        buttons.addWidget(self.add_button)
        if allow_names:
            buttons.addWidget(button("＋ 依名稱…", slot=self.ask_name,
                                     tooltip="例如 Downloads 或 _trash：任何位置名稱相同的資料夾都算"))
        buttons.addStretch()
        lay.addLayout(buttons)

    # ── 內容 ──────────────────────────────────────────────────────────
    def paths(self) -> list[str]:
        return list(self._paths)

    def set_paths(self, paths: list[str]):
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._paths = []
        self._errors = {}
        self.add_paths(paths, quiet=True)

    def add_paths(self, paths: list[str], quiet: bool = False) -> int:
        """加入資料夾，回傳實際加入的數量；有略過或合併時發出 message。"""
        notes, added = [], 0
        for raw in paths:
            raw = raw.strip().strip('"')
            if not raw:
                continue
            path = os.path.normpath(raw) if self._is_path(raw) else raw
            if any(same_path(path, p) if self._is_path(p) else p.casefold() == path.casefold()
                   for p in self._paths):
                notes.append(f"「{path}」已經在清單裡")
                continue
            if self.merge_nested and self._is_path(path):
                parent = next((p for p in self._paths if self._is_path(p) and is_inside(path, p)), None)
                if parent:
                    notes.append(f"「{path}」已經包含在「{parent}」裡")
                    continue
                children = [p for p in self._paths if self._is_path(p) and is_inside(p, path)]
                for child in children:
                    self._remove(child)
                if children:
                    notes.append(f"已用「{path}」取代清單裡 {len(children)} 個在它底下的資料夾")
            self._paths.append(path)
            row = _FolderRow(path, not self._is_path(path),
                             self._folder_icon if self._is_path(path) else self._name_icon)
            row.removed.connect(self.remove_path)
            self._rows[path] = row
            self._rows_lay.addWidget(row)
            added += 1
        self._update_empty()
        if notes and not quiet:
            self.message.emit("；".join(notes[:3]) + ("…" if len(notes) > 3 else ""))
        if added or notes:
            self.changed.emit()
        return added

    @staticmethod
    def _is_path(text: str) -> bool:
        return os.path.isabs(text) or text.startswith("\\\\")

    def _remove(self, path: str):
        if path in self._paths:
            self._paths.remove(path)
        self._errors.pop(path, None)
        row = self._rows.pop(path, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()

    def remove_path(self, path: str):
        self._remove(path)
        self._update_empty()
        self.changed.emit()

    def _update_empty(self):
        self.empty.setVisible(not self._paths)

    # ── 檢查 ──────────────────────────────────────────────────────────
    def check(self) -> dict[str, str]:
        """檢查每個資料夾能不能讀取，在有問題的那列標紅；回傳 {路徑: 原因}。"""
        self._errors = {p: problem for p in self._paths if self._is_path(p)
                        for problem in [folder_problem(p)] if problem}
        for path, row in self._rows.items():
            row.set_error(self._errors.get(path, ""))
        return dict(self._errors)

    def clear_errors(self):
        self._errors = {}
        for row in self._rows.values():
            row.set_error("")

    # ── 加入 ──────────────────────────────────────────────────────────
    def browse(self):
        start = self._paths[-1] if self._paths and self._is_path(self._paths[-1]) else ""
        path = QFileDialog.getExistingDirectory(self.window(), self.dialog_title, start)
        if path:
            self.add_paths([path])

    def ask_name(self):
        name, ok = QInputDialog.getText(
            self.window(), "依名稱加入",
            "資料夾名稱（例如 Downloads、_trash）\n任何位置名稱含有這段文字的資料夾都算：")
        name = name.strip().strip("\\/") if ok else ""
        if name:
            self.add_paths([name])

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        folders, others = [], 0
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local and os.path.isdir(local):
                folders.append(local)
            else:
                others += 1
        if folders:
            self.add_paths(folders)
        if others:
            self.message.emit("只能加入資料夾，拖進來的檔案已略過")
        event.acceptProposedAction()


# ── 提示框 ───────────────────────────────────────────────────────────────
class Banner(QFrame):
    """有底色的提示框：一段文字＋右側按鈕。kind：warning／info／error。"""

    def __init__(self, kind: str = "info", parent=None):
        super().__init__(parent)
        self.setProperty("banner", kind)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 12)
        lay.setSpacing(12)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = label("", "heading", wrap=True)
        self.text = label("", "muted", wrap=True)
        texts.addWidget(self.title)
        texts.addWidget(self.text)
        lay.addLayout(texts, 1)
        self._buttons = QHBoxLayout()
        self._buttons.setSpacing(8)
        lay.addLayout(self._buttons)

    def set_kind(self, kind: str):
        self.setProperty("banner", kind)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_text(self, title: str, text: str = ""):
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))

    def add_button(self, text: str, slot, kind: str | None = None) -> QPushButton:
        btn = button(text, kind, slot)
        self._buttons.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return btn


# ── 浮動訊息 ─────────────────────────────────────────────────────────────
class Toast(QFrame):
    """視窗底部的短暫訊息（取代狀態列），幾秒後自動消失；可附一個動作按鈕。"""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("toast")
        self._host = host
        self.bottom_offset = 24
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 10, 10)
        lay.setSpacing(12)
        self.text = label("", wrap=True)
        self.text.setMaximumWidth(560)
        lay.addWidget(self.text, 1)
        self.action = button("", "link")
        self.action.hide()
        lay.addWidget(self.action)
        close = button("✕", "icon", self.hide, "關閉")
        lay.addWidget(close)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self.hide)
        self._slot = None
        self.action.clicked.connect(self._run_action)
        host.installEventFilter(self)
        self.hide()

    def show_message(self, text: str, kind: str = "info", action: tuple[str, object] | None = None,
                     timeout: int | None = None):
        """kind：info／success／warning／error（改變文字顏色）。timeout 毫秒；0＝不自動消失。"""
        self.text.setText(text)
        set_role(self.text, {"info": "", "success": "success", "warning": "warning",
                             "error": "error"}.get(kind, ""))
        if action:
            self.action.setText(action[0])
            self._slot = action[1]
            self.action.show()
        else:
            self._slot = None
            self.action.hide()
        if timeout is None:
            timeout = 4000 + 40 * len(text) + (3000 if action or kind in ("warning", "error") else 0)
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        if timeout:
            self._timer.start(timeout)
        else:
            self._timer.stop()

    def _run_action(self):
        slot = self._slot
        self.hide()
        if slot:
            slot()

    def _place(self):
        self.adjustSize()
        width = min(self.sizeHint().width(), self._host.width() - 32)
        self.resize(width, self.sizeHint().height())
        x = (self._host.width() - width) // 2
        y = self._host.height() - self.height() - self.bottom_offset
        self.move(max(x, 16), max(y, 16))

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False


# ── 圖片預覽 ─────────────────────────────────────────────────────────────
class ImagePreview(QLabel):
    """滑鼠停在圖片上時顯示的縮圖（不搶焦點、不出現在工作列）。"""
    MAX_SIDE = 260

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#22262e;border:1px solid #3a404b;border-radius:10px;padding:6px;")
        self._path = ""

    @staticmethod
    def supports(path: Path) -> bool:
        return path.suffix.lower() in IMAGE_PREVIEW_EXTS

    def show_for(self, path: Path, global_pos: QPoint):
        key = str(path)
        if key != self._path:
            self._path = key
            pix = QPixmap(key)
            if pix.isNull():
                self.hide()
                return
            pix = pix.scaled(self.MAX_SIDE, self.MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            self.setPixmap(pix)
            self.resize(pix.width() + 14, pix.height() + 14)
        self.move(global_pos + QPoint(18, 18))
        self.show()

    def reset(self):
        self._path = ""
        self.hide()

