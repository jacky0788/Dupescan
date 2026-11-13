# 對話框：自動選取規則、移除確認、整個資料夾移除、設定、無法讀取的檔案、純文字清單、關於。
import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QMessageBox, QPlainTextEdit,
    QRadioButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .. import __version__
from ..delete_plan import KEEP, OK, PROTECTED, REFERENCE_NOTE, SKIP, DeletePlan
from ..folder_dupes import conflict
from ..formatting import human_size
from ..selection import FALLBACKS, AutoRules
from .app_settings import AppSettings
from .theme import DANGER_2, FONT_SIZES, SUCCESS, TEXT_3, WARNING, icon
from .widgets import Banner, ChipGroup, FolderListWidget, button, label


def open_folder(path) -> str:
    """用檔案總管開啟資料夾；回傳錯誤訊息（成功時為空字串）。"""
    try:
        os.makedirs(path, exist_ok=True)
        os.startfile(os.path.normpath(str(path)))
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""


class Dialog(QDialog):
    """共用版面：標題、內文、右下角按鈕列。"""

    def __init__(self, title: str, parent=None, width: int = 560):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(24, 20, 24, 18)
        self.lay.setSpacing(10)

    def add_heading(self, text: str, sub: str = ""):
        self.lay.addWidget(label(text, "heading", wrap=True))
        if sub:
            self.lay.addWidget(label(sub, "muted", wrap=True))

    def add_section(self, text: str, hint: str = ""):
        self.lay.addSpacing(6)
        self.lay.addWidget(label(text, "section"))
        if hint:
            self.lay.addWidget(label(hint, "faint", wrap=True))

    def add_buttons(self, *buttons, left=()):
        row = QHBoxLayout()
        row.setSpacing(8)
        for b in left:
            row.addWidget(b)
        row.addStretch()
        for b in buttons:
            row.addWidget(b)
        self.lay.addSpacing(8)
        self.lay.addLayout(row)
        return row


# ── 自動選取規則 ─────────────────────────────────────────────────────────
class RulesDialog(Dialog):
    def __init__(self, rules: AutoRules, has_results: bool = True, parent=None):
        super().__init__("自動選取規則", parent, 640)
        self.add_heading("每一組重複要保留哪一份？",
                         "依下面的順序決定，前面的條件分不出來才看下一個；沒被保留的標成「移除」。")

        self.add_section("1　保護的資料夾", "裡面的檔案一律保留，永遠不會被移除（例如整理好的相簿、備份）。")
        self.protected = FolderListWidget("沒有保護的資料夾", add_text="＋ 加入資料夾",
                                          dialog_title="選擇要保護的資料夾")
        self.protected.set_paths(rules.protected_dirs)
        self.lay.addWidget(self.protected)

        self.add_section("2　優先移除這些資料夾裡的副本",
                         "例如 Downloads、暫存資料夾。可以加入資料夾，或依名稱加入（任何位置名稱相同的都算）。")
        self.remove = FolderListWidget("沒有優先移除的資料夾", add_text="＋ 加入資料夾", allow_names=True,
                                       dialog_title="選擇要優先移除副本的資料夾")
        self.remove.set_paths(rules.remove_dirs)
        self.lay.addWidget(self.remove)

        self.lay.addSpacing(6)
        self.copy_names = QCheckBox("3　保留原始檔名，移除像副本的檔案")
        self.copy_names.setChecked(rules.copy_names)
        self.lay.addWidget(self.copy_names)
        self.lay.addWidget(label("例如「photo (1).jpg」「photo - 複製.jpg」「Copy of photo.jpg」會優先移除，"
                                 "保留「photo.jpg」。", "faint", wrap=True))

        self.add_section("4　其他情況保留")
        self.fallback = ChipGroup([(key, text) for key, (_, text) in FALLBACKS.items()], exclusive=True)
        self.fallback.set_selected([rules.fallback])
        self.lay.addWidget(self.fallback)

        if has_results:
            self.lay.addSpacing(4)
            self.lay.addWidget(label("套用後會重新選取所有群組，手動調整過的會被取代。", "warning", wrap=True))
        cancel = button("取消", slot=self.reject)
        apply = button("套用", "primary", self.accept)
        reset = button("改回預設", slot=self._reset)
        self.add_buttons(cancel, apply, left=[reset])
        apply.setDefault(True)

    def _reset(self):
        default = AutoRules()
        self.protected.set_paths(default.protected_dirs)
        self.remove.set_paths(default.remove_dirs)
        self.copy_names.setChecked(default.copy_names)
        self.fallback.set_selected([default.fallback])

    def rules(self) -> AutoRules:
        return AutoRules(copy_names=self.copy_names.isChecked(),
                         protected_dirs=self.protected.paths(), remove_dirs=self.remove.paths(),
                         fallback=self.fallback.current() or "oldest")


# ── 移除確認 ─────────────────────────────────────────────────────────────
METHOD_TEXT = {
    "recycle": ("移到資源回收筒", "檔案會送到 Windows 資源回收筒，需要時可以從那裡還原。"),
    "quarantine": ("移到隔離資料夾", "檔案會搬到同一顆磁碟根目錄的 .dupescan_quarantine 資料夾，"
                                    "可以在「⋯ → 操作紀錄與還原」一鍵還原。"),
}


class RemoveDialog(Dialog):
    """移除前確認：一句話摘要＋需要注意的事項，「查看清單」展開每個檔案的處理方式。"""

    def __init__(self, plan: DeletePlan, method: str, full_verify: bool, parent=None):
        action, explain = METHOD_TEXT.get(method, METHOD_TEXT["recycle"])
        super().__init__(action, parent, 600)
        self.plan = plan
        self.action = action
        self.summary = label("", "heading", wrap=True)
        self.lay.addWidget(self.summary)
        self.detail = label("", "muted", wrap=True)
        self.lay.addWidget(self.detail)
        self.lay.addWidget(label(explain + ("移除前會再完整比對一次內容（設定中可以關閉）。" if full_verify else ""),
                                 "muted", wrap=True))

        skipped = plan.entries_with(SKIP)
        protected = plan.entries_with(PROTECTED)
        whole = plan.whole_groups
        self.allow_protected = QCheckBox("仍要移除這些檔案")
        if skipped or protected or whole:
            box = Banner("warning")
            lines = []
            if skipped:
                lines.append(f"{len(skipped):,} 個檔案在掃描後有變動或已經不在了，不會移除。")
            if whole:
                lines.append(f"{len(whole):,} 組的每一份都被標成移除，這幾組不會處理。")
            if protected:
                system = sum(1 for e in protected if e.note != REFERENCE_NOTE)
                where = "系統或程式資料夾（Windows、Program Files、AppData…）" if system else "保護的資料夾"
                lines.append(f"{len(protected):,} 個檔案在{where}，預設不移除。")
            box.set_text("請注意", "\n".join(lines))
            if protected:
                box.add_widget(self.allow_protected)
            self.lay.addWidget(box)
        self.allow_protected.toggled.connect(self._refresh)

        self.list_toggle = button("查看清單", "expander", self._toggle_list)
        self.list_toggle.setIcon(icon("chevron-right"))
        self.lay.addWidget(self.list_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["處理", "檔名", "資料夾", "說明"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setMinimumHeight(260)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 120)
        self.tree.setColumnWidth(1, 200)
        self.tree.hide()
        self.lay.addWidget(self.tree, 1)

        self.cancel_button = button("取消", slot=self.reject)
        self.ok_button = button(action, "danger", self.accept)
        self.add_buttons(self.cancel_button, self.ok_button)
        self.cancel_button.setDefault(True)
        self._refresh()

    def removable(self):
        return self.plan.removable(allow_whole_group=False, allow_protected=self.allow_protected.isChecked())

    def selected(self) -> list[tuple]:
        """[(FileInfo, 保留的那份的路徑)]"""
        return [(e.file, pg.reference) for pg, e in self.removable()]

    def _refresh(self):
        items = self.removable()
        count = len(items)
        size = sum(e.file.size for _, e in items)
        if count:
            self.summary.setText(f"要把 {count:,} 個檔案{self.action}嗎？")
            self.detail.setText(f"會釋放 {human_size(size)}。每組都至少保留一份。")
        else:
            self.summary.setText("沒有可以移除的檔案")
            self.detail.setText("要移除的檔案都已經變動、不在了，或位在預設不移除的地方。")
        self.ok_button.setText(f"{self.action}（{count:,} 個）" if count else self.action)
        self.ok_button.setEnabled(count > 0)
        if not self.tree.isHidden():
            self._fill_tree()

    def _toggle_list(self):
        show = self.tree.isHidden()
        self.tree.setVisible(show)
        self.list_toggle.setIcon(icon("chevron-down" if show else "chevron-right"))
        if show:
            self._fill_tree()
            self.resize(max(self.width(), 900), max(self.height(), 640))

    def _fill_tree(self):
        self.tree.clear()
        removing = {id(e) for _, e in self.removable()}
        bold = QFont(self.tree.font())
        bold.setBold(True)
        tops = []
        for pg in self.plan.groups:
            top = QTreeWidgetItem([f"{pg.group.files[0].name}　{len(pg.group.files)} 份 · 每份 "
                                   f"{human_size(pg.group.size)}"])
            top.setFont(0, bold)
            for e in pg.entries:
                if id(e) in removing:
                    action, color = "移除", DANGER_2
                elif e.status == KEEP:
                    action, color = "保留", SUCCESS
                elif e.status in (OK, PROTECTED):
                    action, color = "不移除", WARNING
                else:
                    action, color = "略過", TEXT_3
                child = QTreeWidgetItem(top, [action, e.file.name, e.file.folder, e.note])
                child.setForeground(0, _brush(color))
                child.setToolTip(2, str(e.file.path))
            tops.append(top)
        self.tree.addTopLevelItems(tops)
        for top in tops:
            top.setFirstColumnSpanned(True)
        self.tree.expandAll()


def _brush(color: str) -> QBrush:
    return QBrush(QColor(color))


# ── 整個資料夾移除 ───────────────────────────────────────────────────────
class FolderRemoveDialog(Dialog):
    """整個都是重複的資料夾：勾選要移除的，連同空的子資料夾一起移走。

    勾選時檢查能不能一起移除（兩個資料夾互為副本、或一個包含另一個時不行）。
    """

    def __init__(self, folders: list, method: str, full_verify: bool, preselect: str = "",
                 not_found_reason: str = "", parent=None):
        action, explain = METHOD_TEXT.get(method, METHOD_TEXT["recycle"])
        super().__init__("重複的資料夾", parent, 760)
        self.folders = folders
        self.action = action
        self._updating = False
        if folders:
            self.add_heading(f"{len(folders):,} 個資料夾整個都是重複的",
                             "這些資料夾裡的每個檔案，在資料夾外面都還有一份一模一樣的，"
                             "整個移走不會少任何內容。勾選要移除的資料夾。")
        else:
            self.add_heading("沒有整個重複的資料夾",
                             "要每個檔案在資料夾外面都有一份一樣的，資料夾才能整個移除。")
        if not_found_reason:
            self.lay.addWidget(label(f"這個資料夾不行：{not_found_reason}", "warning", wrap=True))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["資料夾", "檔案", "大小", "另一份在"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setMinimumHeight(240)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 90)
        self.items: list[QTreeWidgetItem] = []
        for f in folders:
            files = f"{len(f.files):,} 個" + (f"＋{len(f.extra)} 小檔" if f.extra else "")
            item = QTreeWidgetItem([f.path, files, human_size(f.size), f.elsewhere_text()])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            tip = [f.path, f"{len(f.files):,} 個重複的檔案" + (f"、{f.subfolders:,} 個子資料夾" if f.subfolders else "")]
            if f.extra:
                tip.append(f"另有 {len(f.extra):,} 個系統小檔或空檔案（Thumbs.db、desktop.ini…）會一起移除")
            tip.append("")
            tip.append("另一份在：")
            tip += [f"  {where}（{n:,} 個）" for where, n in f.elsewhere[:10]]
            if len(f.elsewhere) > 10:
                tip.append(f"  …等 {len(f.elsewhere):,} 個資料夾")
            item.setToolTip(0, "\n".join(tip))
            item.setToolTip(3, "\n".join(where for where, _ in f.elsewhere[:10]))
            self.items.append(item)
        self.tree.addTopLevelItems(self.items)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(lambda item, _: open_folder_existing(item.text(0)))
        self.tree.setVisible(bool(folders))
        self.lay.addWidget(self.tree, 1)

        self.conflict = label("", "warning", wrap=True)
        self.conflict.hide()
        self.lay.addWidget(self.conflict)
        if folders:
            self.lay.addWidget(label("雙擊可以在檔案總管開啟資料夾。" + explain
                                     + ("移除前會再完整比對一次內容（設定中可以關閉）。" if full_verify
                                        else "移除前會再確認一次檔案沒有變動。"), "muted", wrap=True))

        self.select_all = button("全選", slot=self._select_all)
        self.select_all.setVisible(len(folders) > 1)
        self.cancel_button = button("取消" if folders else "關閉", slot=self.reject)
        self.ok_button = button(action, "danger", self.accept)
        self.ok_button.setVisible(bool(folders))
        self.add_buttons(self.cancel_button, self.ok_button, left=[self.select_all])
        self.cancel_button.setDefault(True)

        if preselect:
            key = os.path.normcase(os.path.normpath(preselect))
            for item, f in zip(self.items, folders):
                if f.key == key:
                    item.setCheckState(0, Qt.CheckState.Checked)
        self._refresh()
        self.resize(max(self.width(), 900), 520 if folders else 220)

    def selected(self) -> list:
        return [f for item, f in zip(self.items, self.folders) if item.checkState(0) == Qt.CheckState.Checked]

    def _item_changed(self, item: QTreeWidgetItem, column: int):
        if self._updating or column != 0:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            folder = self.folders[self.items.index(item)]
            reason = conflict([f for f in self.selected() if f is not folder], folder)
            if reason:
                self._updating = True
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self._updating = False
                self.conflict.setText(f"「{folder.name}」不能一起移除：{reason}")
                self.conflict.show()
                return
        self.conflict.hide()
        self._refresh()

    def _select_all(self):
        """依可釋放空間由大到小，能一起移除的都勾起來。"""
        self._updating = True
        chosen = []
        for item, f in sorted(zip(self.items, self.folders), key=lambda t: -t[1].size):
            ok = not conflict(chosen, f)
            item.setCheckState(0, Qt.CheckState.Checked if ok else Qt.CheckState.Unchecked)
            if ok:
                chosen.append(f)
        self._updating = False
        skipped = len(self.folders) - len(chosen)
        self.conflict.setText(f"有 {skipped:,} 個資料夾和其他勾選的互為副本或互相包含，沒有勾選。" if skipped else "")
        self.conflict.setVisible(bool(skipped))
        self._refresh()

    def _refresh(self):
        chosen = self.selected()
        count = len(chosen)
        self.ok_button.setText(f"{self.action}（{count:,} 個資料夾，{human_size(sum(f.size for f in chosen))}）"
                               if count else self.action)
        self.ok_button.setEnabled(count > 0)


def open_folder_existing(path) -> str:
    """開啟已經存在的資料夾（不像 open_folder 會自動建立）；回傳錯誤訊息。"""
    if not os.path.isdir(path):
        return "資料夾已經不在了"
    try:
        os.startfile(os.path.normpath(str(path)))
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""


# ── 設定 ─────────────────────────────────────────────────────────────────
class SettingsDialog(Dialog):
    def __init__(self, settings: AppSettings, cache_size: int, scanning: bool, parent=None):
        super().__init__("設定", parent, 560)
        self.cache_cleared = False
        self.add_section("文字大小")
        self.text_size = ChipGroup([(k, name) for k, (name, _) in FONT_SIZES.items()], exclusive=True)
        self.text_size.set_selected([settings.text_size])
        self.lay.addWidget(self.text_size)

        self.add_section("移除檔案的方式")
        self.recycle = QRadioButton("移到資源回收筒（建議）")
        self.quarantine = QRadioButton("移到隔離資料夾")
        (self.quarantine if settings.remove_method == "quarantine" else self.recycle).setChecked(True)
        self.lay.addWidget(self.recycle)
        self.lay.addWidget(label("可以從 Windows 資源回收筒還原。網路磁碟、隨身碟等沒有資源回收筒的位置，"
                                 "會自動改用隔離資料夾。", "faint", wrap=True))
        self.lay.addWidget(self.quarantine)
        self.lay.addWidget(label("搬到同一顆磁碟根目錄的 .dupescan_quarantine，可在「操作紀錄與還原」一鍵還原；"
                                 "確定不需要後再自行刪除。", "faint", wrap=True))
        self.full_verify = QCheckBox("移除前完整比對內容（比較慢，但最保險）")
        self.full_verify.setChecked(settings.full_verify)
        self.lay.addWidget(self.full_verify)

        self.add_section("雜湊快取", "記住已經比對過的檔案，再次掃描同樣的資料夾時不必重新讀取。")
        cache_row = QHBoxLayout()
        self.cache_label = label(f"目前大小：{human_size(cache_size)}", "muted")
        cache_row.addWidget(self.cache_label)
        cache_row.addStretch()
        self.clear_cache_button = button("清除快取", slot=self._clear_cache)
        self.clear_cache_button.setEnabled(not scanning and cache_size > 0)
        if scanning:
            self.clear_cache_button.setToolTip("掃描進行中，不能清除")
        cache_row.addWidget(self.clear_cache_button)
        self.lay.addLayout(cache_row)

        self.add_section("資料與紀錄")
        folders = QHBoxLayout()
        self.data_button = button("開啟資料資料夾", tooltip="操作紀錄、快取、上次的結果、報表")
        self.log_button = button("開啟 log 資料夾", tooltip="每次執行的紀錄與當機紀錄")
        folders.addWidget(self.data_button)
        folders.addWidget(self.log_button)
        folders.addStretch()
        self.lay.addLayout(folders)
        save = button("儲存", "primary", self.accept)
        self.add_buttons(button("取消", slot=self.reject), save)
        save.setDefault(True)

    def _clear_cache(self):
        answer = QMessageBox.question(self, "清除快取",
                                      "清除後，下次掃描會重新讀取所有檔案（比較慢）。要清除嗎？")
        if answer == QMessageBox.StandardButton.Yes:
            self.cache_cleared = True
            self.cache_label.setText("清除後：0 B（按「儲存」或關閉後生效）")
            self.clear_cache_button.setEnabled(False)

    def values(self) -> tuple[str, str, bool]:
        """(文字大小, 移除方式, 移除前完整比對)"""
        return (self.text_size.current() or "normal",
                "quarantine" if self.quarantine.isChecked() else "recycle",
                self.full_verify.isChecked())


# ── 文字清單 ─────────────────────────────────────────────────────────────
class TextDialog(Dialog):
    """一段說明＋可以複製的文字清單（無法讀取的檔案、移除失敗的檔案…）。"""

    def __init__(self, title: str, heading: str, text: str, body: str, parent=None):
        super().__init__(title, parent, 640)
        self.add_heading(heading, text)
        self.text = QPlainTextEdit(body)
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setMinimumHeight(260)
        self.lay.addWidget(self.text, 1)
        copy = button("複製", slot=lambda: QApplication.clipboard().setText(body))
        close = button("關閉", "primary", self.accept)
        self.add_buttons(close, left=[copy])
        self.resize(720, 460)


def unreadable_dialog(samples: list[str], total: int, parent=None) -> TextDialog:
    more = f"（只列出前 {len(samples):,} 筆）" if total > len(samples) else ""
    return TextDialog("無法讀取的檔案", f"{total:,} 個檔案或資料夾無法讀取",
                      "可能是沒有權限、被其他程式鎖定，或路徑太長。這些檔案沒有被比對，"
                      f"也不會被移除。{more}", "\n".join(samples), parent)


def about(parent=None):
    QMessageBox.about(parent, "關於 DupeScan",
                      f"<b>DupeScan {__version__}</b><br><br>"
                      "比對檔案內容，找出完全相同的重複檔案。<br>"
                      "移除的檔案會送到資源回收筒或隔離資料夾，不會永久刪除。")

