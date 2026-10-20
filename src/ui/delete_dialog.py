# 移除前預覽視窗與背景移除 worker。
#
# DeletePlanDialog：依群組列出「保留／移除／略過／受保護」、總釋放容量與需要注意的項目，
#   可選處理方式（資源回收筒／隔離資料夾）、搜尋、匯出 CSV；整組全刪與受保護資料夾（含參考
#   資料夾）預設不移除，要另外勾選確認。
# RemovalWorker：在背景執行緒（可選擇先完整比對內容）再呼叫 file_ops.remove_files，不卡畫面。
import csv
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QRadioButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .. import file_ops
from ..delete_plan import KEEP, OK, PROTECTED, REFERENCE_NOTE, SKIP, DeletePlan
from ..file_ops import OpResult
from ..scanner import _full_hash
from .theme import DANGER, FAINT, GREEN, ORANGE, SUBTEXT, TEXT

STATUS_TEXT  = {KEEP: "保留", OK: "移除", SKIP: "略過", PROTECTED: "受保護"}
STATUS_COLOR = {KEEP: GREEN, OK: DANGER, SKIP: FAINT, PROTECTED: ORANGE}
_ROLE_ENTRY = Qt.ItemDataRole.UserRole


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class DeletePlanDialog(QDialog):
    """移除前預覽：確認後由呼叫端取 selected()、method()、full_verify()。"""

    def __init__(self, plan: DeletePlan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("確認移除")
        self.resize(1040, 700)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"font-size:16px;font-weight:700;color:{TEXT};")
        lay.addWidget(self.summary)

        # ── 處理方式 ─────────────────────────────────────────────────
        mode = QHBoxLayout()
        mode.addWidget(QLabel("處理方式："))
        self.rb_recycle = QRadioButton("資源回收筒（可從回收筒還原）")
        self.rb_quarantine = QRadioButton("隔離資料夾（同一顆磁碟內搬移，可在「操作紀錄」一鍵還原）")
        self.rb_recycle.setChecked(True)
        mode.addWidget(self.rb_recycle)
        mode.addWidget(self.rb_quarantine)
        mode.addStretch()
        lay.addLayout(mode)
        self.cb_verify = QCheckBox("移除前完整比對內容（會重新讀完每個檔案，較慢但最保險）")
        lay.addWidget(self.cb_verify)

        # ── 需要注意 ─────────────────────────────────────────────────
        warn = QFrame()
        warn.setObjectName("warnbox")
        wl = QVBoxLayout(warn)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.setSpacing(6)
        n_whole = len(plan.whole_groups)
        n_protected = len(plan.entries_with(PROTECTED))
        n_reference = sum(1 for e in plan.entries_with(PROTECTED) if e.note == REFERENCE_NOTE)
        n_skipped = len(plan.entries_with(SKIP))
        self.cb_whole = QCheckBox("仍要移除這些群組的所有副本")
        self.cb_protected = QCheckBox("仍要移除受保護資料夾內的檔案" if not n_reference
                                      else "仍要移除受保護／參考資料夾內的檔案")
        if n_whole:
            wl.addLayout(self._warn_row(
                f"⛔  {n_whole} 個群組的每一份都被勾選，移除後一份都不剩 → 預設不移除",
                self.cb_whole))
        if n_protected:
            where = []
            if n_protected > n_reference:
                where.append("受保護資料夾（Windows、Program Files、AppData、.git、Ollama 模型等"
                             "程式管理的位置）")
            if n_reference:
                where.append("你設定的參考資料夾")
            wl.addLayout(self._warn_row(
                f"🔒  {n_protected} 個檔案位於{'或'.join(where)} → 預設不移除", self.cb_protected))
        if n_skipped:
            wl.addLayout(self._warn_row(
                f"⚠  {n_skipped} 個檔案在掃描後有變動、已不存在，或找不到可比對的保留副本 → "
                f"自動略過，不會移除", None))
        warn.setVisible(bool(n_whole or n_protected or n_skipped))
        lay.addWidget(warn)

        # ── 清單 ─────────────────────────────────────────────────────
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋檔名或路徑…")
        self._search_timer = QTimer(self, singleShot=True, interval=250)
        self._search_timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        lay.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["動作", "名稱", "資料夾", "大小", "說明"])
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 110)
        self.tree.setColumnWidth(1, 240)
        self.tree.setColumnWidth(3, 90)
        self.tree.setColumnWidth(4, 220)
        self._populate()
        lay.addWidget(self.tree, 1)

        # ── 按鈕 ─────────────────────────────────────────────────────
        row = QHBoxLayout()
        export = QPushButton("匯出 CSV")
        export.clicked.connect(self._export_csv)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton()
        self.btn_ok.setObjectName("danger")
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(export)
        row.addStretch()
        row.addWidget(cancel)
        row.addWidget(self.btn_ok)
        lay.addLayout(row)

        self.cb_whole.toggled.connect(self._refresh)
        self.cb_protected.toggled.connect(self._refresh)
        self._refresh()

    @staticmethod
    def _warn_row(text: str, checkbox: QCheckBox | None) -> QHBoxLayout:
        row = QHBoxLayout()
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{ORANGE};")
        row.addWidget(lbl, 1)
        if checkbox is not None:
            row.addWidget(checkbox)
        return row

    # ── 清單內容 ─────────────────────────────────────────────────────
    def _populate(self):
        bold = QFont()
        bold.setBold(True)
        items = []
        for pg in self.plan.groups:
            top = QTreeWidgetItem([
                f"群組 #{pg.number} · {len(pg.group.files)} 份 · 每份 {_size(pg.group.size)}"
                + ("　⛔ 每一份都被勾選" if pg.whole_group else "")])
            top.setFont(0, bold)
            for e in pg.entries:
                child = QTreeWidgetItem(top, ["", e.file.name, e.file.folder,
                                              _size(e.file.size), e.note])
                child.setData(0, _ROLE_ENTRY, (pg, e))
                child.setToolTip(2, str(e.file.path))
                child.setForeground(2, QColor(SUBTEXT))
                child.setForeground(4, QColor(SUBTEXT))
            items.append(top)
        self.tree.addTopLevelItems(items)
        for top in items:
            top.setFirstColumnSpanned(True)
        self.tree.expandAll()

    def _refresh(self):
        """依「仍要移除」的勾選更新每列的動作文字、摘要與確認按鈕。"""
        allow_whole, allow_protected = self.cb_whole.isChecked(), self.cb_protected.isChecked()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                pg, e = child.data(0, _ROLE_ENTRY)
                status = e.status
                if status in (OK, PROTECTED) and pg.whole_group and not allow_whole:
                    text, color = "不移除", FAINT
                elif status == PROTECTED and allow_protected:
                    text, color = "移除", DANGER
                else:
                    text, color = STATUS_TEXT[status], STATUS_COLOR[status]
                child.setText(0, text)
                child.setForeground(0, QColor(color))

        items = self.removable()
        n = len(items)
        freed = sum(e.file.size for _, e in items)
        kept = len(self.plan.entries_with(KEEP))
        self.summary.setText(f"將移除 {n:,} 個檔案，釋放 {_size(freed)}；保留 {kept:,} 份。")
        self.btn_ok.setText(f"確認移除 {n:,} 個" if n else "沒有可移除的檔案")
        self.btn_ok.setEnabled(n > 0)

    def _apply_search(self):
        text = self.search.text().strip().casefold()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            hit = not text or any(text in str(top.child(j).data(0, _ROLE_ENTRY)[1].file.path).casefold()
                                  for j in range(top.childCount()))
            top.setHidden(not hit)

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "匯出移除清單", "dupescan_移除清單.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        allow_whole, allow_protected = self.cb_whole.isChecked(), self.cb_protected.isChecked()
        removing = {id(e) for _, e in self.removable()}
        with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM：Excel 直接開不亂碼
            w = csv.writer(f)
            w.writerow(["群組", "動作", "路徑", "大小(bytes)", "說明"])
            for pg in self.plan.groups:
                for e in pg.entries:
                    action = "移除" if id(e) in removing else (
                        "保留" if e.status == KEEP else f"不移除（{STATUS_TEXT[e.status]}）")
                    w.writerow([pg.number, action, str(e.file.path), e.file.size, e.note])
        QMessageBox.information(self, "已匯出", f"已匯出到：\n{path}")

    # ── 給呼叫端的結果 ───────────────────────────────────────────────
    def removable(self):
        return self.plan.removable(self.cb_whole.isChecked(), self.cb_protected.isChecked())

    def selected(self) -> list[tuple]:
        """[(FileInfo, 掃描時的完整 hash, 比對基準路徑)]"""
        return [(e.file, pg.group.hash_value, pg.reference) for pg, e in self.removable()]

    def method(self) -> str:
        return "quarantine" if self.rb_quarantine.isChecked() else "recycle"

    def set_method(self, method: str):
        (self.rb_quarantine if method == "quarantine" else self.rb_recycle).setChecked(True)

    def full_verify(self) -> bool:
        return self.cb_verify.isChecked()


class RemovalWorker(QObject):
    """在背景執行緒移除檔案；full_verify 時先重算完整 hash，與掃描結果不符的一律略過。"""
    progress = pyqtSignal(str, int, int)   # (階段, 已處理, 總數)
    finished = pyqtSignal(list)            # list[OpResult]
    error    = pyqtSignal(str)
    ended    = pyqtSignal()

    def __init__(self, items: list[tuple], method: str, hwnd: int | None, full_verify: bool):
        super().__init__()
        self._items = items
        self._method = method
        self._hwnd = hwnd
        self._full_verify = full_verify

    def run(self):
        try:
            results: list[OpResult] = []
            todo = self._items
            if self._full_verify:
                todo, skipped = self._verify(todo)
                results += skipped
            results += file_ops.remove_files(
                [(fi.path, fi.size) for fi, _, _ in todo], self._method, self._hwnd,
                progress=lambda done, total: self.progress.emit("移除中", done, total))
            self.finished.emit(results)
        except Exception as exc:          # 不讓例外吞掉 ended，畫面才不會卡在進度視窗
            self.error.emit(str(exc))
        finally:
            self.ended.emit()

    def _verify(self, items):
        def digest(path):
            try:
                return _full_hash(Path(path))
            except OSError:
                return None

        ok, skipped, reference_ok = [], [], {}
        for i, (fi, expected, reference) in enumerate(items):
            self.progress.emit("比對內容中", i, len(items))
            if reference and reference not in reference_ok:
                reference_ok[reference] = digest(reference) == expected
            if reference and not reference_ok[reference]:
                skipped.append(OpResult(str(fi.path), False, "verify",
                                        note="保留的那份內容已和掃描時不同，已略過"))
            elif digest(fi.path) != expected:
                skipped.append(OpResult(str(fi.path), False, "verify",
                                        note="內容和掃描結果不同，已略過"))
            else:
                ok.append((fi, expected, reference))
        return ok, skipped
