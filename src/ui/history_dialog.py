# 操作紀錄視窗：列出每次移除的結果；移到隔離資料夾的檔案可以在這裡一鍵還原。
import os
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .. import file_ops
from ..formatting import human_size
from ..logger import logger


class HistoryDialog(QDialog):
    """操作紀錄：資源回收筒的請到 Windows 資源回收筒還原，隔離資料夾的可在此還原。"""

    COLUMNS = ["時間", "資源回收筒", "隔離資料夾", "失敗", "容量", "隔離檔案狀態"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("操作紀錄")
        self.resize(820, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        hint = QLabel(
            "DupeScan 不會永久刪除檔案。送到資源回收筒的檔案請從 Windows 資源回收筒還原；"
            "移到隔離資料夾（各磁碟根目錄的 .dupescan_quarantine）的檔案可以在這裡一鍵還原。"
            "確定不需要之後，隔離資料夾請自行刪除。")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_restore = QPushButton("↩  還原隔離的檔案")
        self.btn_restore.setObjectName("primary")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_open = QPushButton("開啟隔離資料夾")
        self.btn_open.clicked.connect(self._open_folder)
        close = QPushButton("關閉")
        close.clicked.connect(self.accept)
        row.addWidget(self.btn_restore)
        row.addWidget(self.btn_open)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)

        self._entries: list[dict] = []
        self.reload()

    def reload(self):
        self._entries = file_ops.read_journal()
        self.table.setRowCount(len(self._entries))
        for r, e in enumerate(self._entries):
            restored = total = 0
            for m in e.get("manifests", []):
                a, b = file_ops.manifest_status(Path(m))
                restored += a
                total += b
            status = "—" if not e.get("manifests") else (
                "全部已還原" if total and restored == total else f"{restored}/{total} 已還原")
            cells = [e.get("time", "").replace("T", " "), str(e.get("recycled", 0)),
                     str(e.get("quarantined", 0)), str(e.get("failed", 0)),
                     human_size(e.get("bytes", 0)), status]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, item)
        self._update_buttons()

    def _selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        return self._entries[rows[0].row()] if rows else None

    def _update_buttons(self):
        e = self._selected()
        has_quarantine = bool(e and e.get("manifests"))
        self.btn_restore.setEnabled(has_quarantine)
        self.btn_open.setEnabled(has_quarantine)

    def _restore(self):
        e = self._selected()
        if not e:
            return
        results = []
        for m in e.get("manifests", []):
            if Path(m).exists():
                results += file_ops.restore_batch(Path(m))
        ok = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        for r in ok:
            logger.info(f"  還原: {r.path}")
        for r in bad:
            logger.warning(f"  無法還原: {r.path}  原因: {r.note}")
        logger.info(f"還原隔離檔案 — 成功 {len(ok)} 個，失敗 {len(bad)} 個")
        msg = f"已還原 {len(ok)} 個檔案。"
        if bad:
            msg += f"\n\n{len(bad)} 個沒有還原：\n" + "\n".join(
                f"{r.path}（{r.note}）" for r in bad[:15])
        QMessageBox.information(self, "還原結果", msg)
        self.reload()

    def _open_folder(self):
        e = self._selected()
        for m in (e or {}).get("manifests", []):
            folder = Path(m).parent
            if folder.exists():
                os.startfile(str(folder))
                return
        QMessageBox.information(self, "提示", "找不到這批檔案的隔離資料夾（可能已被刪除）。")
