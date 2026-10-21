# 「掃描中」畫面：三個步驟、進度、剩餘時間；只有「暫停」與「停止」兩個按鈕。
# 掃描出錯時改顯示錯誤說明與「重試（從中斷處繼續）」「回到開始」。
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

from ..formatting import format_eta
from ..scan_options import ScanOptions
from ..scanner import STEP_NAMES, TOTAL_STEPS
from .scan_controller import Progress
from .theme import set_role
from .widgets import Banner, button, card, label


class StepIndicator(QWidget):
    """① 列出檔案 ─ ② 快速比對 ─ ③ 比對檔案內容（目前的步驟亮起，完成的打勾）。"""
    MARKS = "①②③"

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.labels = []
        for step in range(1, TOTAL_STEPS + 1):
            if step > 1:
                lay.addWidget(label("—", "faint"))
            lbl = label(f"{self.MARKS[step - 1]} {STEP_NAMES[step]}", "faint")
            self.labels.append(lbl)
            lay.addWidget(lbl)
        lay.addStretch()

    def set_step(self, current: int):
        for step, lbl in enumerate(self.labels, 1):
            if step < current:
                lbl.setText(f"✓ {STEP_NAMES[step]}")
                set_role(lbl, "success")
            elif step == current:
                lbl.setText(f"{self.MARKS[step - 1]} {STEP_NAMES[step]}")
                set_role(lbl, "heading")
            else:
                lbl.setText(f"{self.MARKS[step - 1]} {STEP_NAMES[step]}")
                set_role(lbl, "faint")


class ScanPage(QWidget):
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    home_requested = pyqtSignal()
    details_requested = pyqtSignal()        # 錯誤的詳細內容

    def __init__(self, parent=None):
        super().__init__(parent)
        center = QHBoxLayout(self)
        center.setContentsMargins(24, 24, 24, 24)
        column = QWidget()
        column.setMaximumWidth(760)
        center.addWidget(column)
        lay = QVBoxLayout(column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addStretch(2)

        self.title = label("正在掃描", "title", wrap=True)
        lay.addWidget(self.title)
        lay.addSpacing(4)
        self.subtitle = label("", "muted", wrap=True)
        lay.addWidget(self.subtitle)
        lay.addSpacing(24)

        # ── 進度 ──────────────────────────────────────────────────────
        self.progress_box, box = card(margins=20, spacing=12)
        self.steps = StepIndicator()
        box.addWidget(self.steps)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(12)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        bar_row.addWidget(self.bar, 1)
        self.percent = label("", "muted")
        self.percent.setMinimumWidth(44)
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar_row.addWidget(self.percent)
        box.addLayout(bar_row)
        self.detail = label("準備中…", wrap=True)
        box.addWidget(self.detail)
        info = QHBoxLayout()
        self.eta = label("", "muted")
        self.disk = label("", "faint")
        info.addWidget(self.eta)
        info.addStretch()
        info.addWidget(self.disk)
        box.addLayout(info)
        lay.addWidget(self.progress_box)
        lay.addSpacing(20)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()
        self.pause_button = button("暫停", slot=self._toggle_pause)
        self.pause_button.setMinimumWidth(110)
        self.stop_button = button("停止", slot=self.stop_requested.emit)
        self.stop_button.setMinimumWidth(110)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch()
        lay.addLayout(buttons)
        lay.addSpacing(16)
        self.hint = label("直接關閉 DupeScan 也沒關係，下次開啟時可以從中斷的地方繼續。", "faint", wrap=True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)

        # ── 出錯 ──────────────────────────────────────────────────────
        self.error_banner = Banner("error")
        self.error_banner.add_button("詳細內容", self.details_requested.emit)
        self.error_banner.add_button("回到開始", self.home_requested.emit)
        self.retry_button = self.error_banner.add_button("重試（從中斷處繼續）",
                                                         self.retry_requested.emit, "primary")
        self.error_banner.hide()
        lay.addWidget(self.error_banner)
        lay.addStretch(3)
        self._paused = False
        self._where = ""

    # ── 狀態 ──────────────────────────────────────────────────────────
    def start(self, options: ScanOptions, resumed: bool = False):
        """新的一次掃描（或繼續掃描）開始。"""
        self.title.setText("繼續掃描" if resumed else "正在掃描")
        self._where = options.roots_summary()
        self.subtitle.setText(self._where + (" · 已經比對過的檔案會直接沿用" if resumed else ""))
        self.subtitle.setToolTip("\n".join(options.roots))
        self.steps.set_step(1)
        self.bar.setRange(0, 0)
        self.percent.setText("")
        self.detail.setText("準備中…")
        self.eta.setText("")
        self.disk.setText("")
        self._set_paused(False)
        self.progress_box.show()
        self.pause_button.show()
        self.stop_button.show()
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.stop_button.setText("停止")
        self.hint.show()
        self.error_banner.hide()

    def update_progress(self, p: Progress):
        self.steps.set_step(p.step)
        fraction = p.fraction
        if fraction is None:
            self.bar.setRange(0, 0)                    # 第 1 步：不知道總數，顯示忙碌動畫
            self.percent.setText("")
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(fraction * 1000))
            self.percent.setText(f"{int(fraction * 100)}%")
        self.detail.setText(p.message.replace("...", "…"))
        if not self._paused:
            self.eta.setText("" if p.step == 1 else f"剩餘時間：{format_eta(p.eta)}")

    def set_disk(self, summary: str):
        self.disk.setText(f"磁碟：{summary}")

    def set_paused(self, paused: bool):
        self._set_paused(paused)

    def _set_paused(self, paused: bool):
        self._paused = paused
        self.pause_button.setText("繼續" if paused else "暫停")
        self.pause_button.setObjectName("primary" if paused else "")
        self.pause_button.style().unpolish(self.pause_button)
        self.pause_button.style().polish(self.pause_button)
        if paused:
            self.eta.setText("已暫停（不會讀取磁碟）")

    def set_stopping(self):
        self.detail.setText("正在停止…（已比對過的部分都會保留）")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.stop_button.setText("停止中…")

    def show_error(self, message: str):
        self.title.setText("掃描沒有完成")
        self.subtitle.setText(self._where)
        self.progress_box.hide()
        self.pause_button.hide()
        self.stop_button.hide()
        self.hint.hide()
        self.error_banner.set_text("掃描時發生錯誤",
                                   f"{message}\n\n已經比對過的部分都保留了，重試會從中斷的地方繼續。")
        self.error_banner.show()

    def _toggle_pause(self):
        (self.resume_requested if self._paused else self.pause_requested).emit()
