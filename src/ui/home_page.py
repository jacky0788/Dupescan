# 「開始」畫面：選資料夾、選檔案類型與大小，按「開始掃描」。
# 上次沒有完成的掃描在最上面提示可以繼續；上次的結果在最下面可以直接開啟。
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget,
)

from ..formatting import format_time, human_size
from ..scan_job import ScanJob
from ..scan_options import FILE_TYPES, SIZE_PRESETS, ScanOptions, parse_exts, size_label
from .theme import icon
from .widgets import Banner, ChipGroup, FolderListWidget, button, card, label

CUSTOM = "custom"
SIZE_TEXT = {0: "不限（空檔案除外）", 100 * 1024: "100 KB 以上（建議）"}


class HomePage(QWidget):
    start_requested = pyqtSignal(object)      # ScanOptions
    resume_requested = pyqtSignal()
    discard_requested = pyqtSignal()
    open_last_requested = pyqtSignal()
    options_changed = pyqtSignal()
    message = pyqtSignal(str, str)            # (文字, 種類) → 浮動訊息

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        center = QHBoxLayout(body)
        center.setContentsMargins(24, 24, 24, 32)
        column = QWidget()
        column.setMaximumWidth(820)
        center.addWidget(column)
        lay = QVBoxLayout(column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── 上次沒有完成的掃描 ─────────────────────────────────────────
        self.resume_banner = Banner("warning")
        self.resume_banner.add_button("捨棄", self.discard_requested.emit)
        self.resume_button = self.resume_banner.add_button("繼續掃描", self.resume_requested.emit,
                                                           "primary")
        self.resume_banner.hide()
        lay.addWidget(self.resume_banner)
        lay.addSpacing(20)

        lay.addWidget(label("找出重複的檔案", "title"))
        lay.addSpacing(4)
        lay.addWidget(label("DupeScan 會比對檔案內容，找出完全相同的檔案。掃描只會讀取，不會更動任何檔案。",
                            "muted", wrap=True))
        lay.addSpacing(24)

        # ── 資料夾 ────────────────────────────────────────────────────
        lay.addWidget(label("要掃描的資料夾", "section"))
        lay.addSpacing(8)
        self.folders = FolderListWidget("把資料夾拖到這裡，或按「加入資料夾」", merge_nested=True,
                                        dialog_title="選擇要掃描的資料夾")
        self.folders.changed.connect(self._folders_changed)
        self.folders.message.connect(lambda text: self.message.emit(text, "info"))
        lay.addWidget(self.folders)
        lay.addSpacing(20)

        # ── 檔案類型與大小 ────────────────────────────────────────────
        lay.addWidget(label("要比對的檔案", "section"))
        lay.addSpacing(8)
        types = [("all", "全部")] + [(k, name) for k, (name, _) in FILE_TYPES.items()] + [(CUSTOM, "自訂…")]
        self.types = ChipGroup(types, all_key="all")
        self.types.changed.connect(self._types_changed)
        lay.addWidget(self.types)
        self.custom_row = QWidget()
        custom = QHBoxLayout(self.custom_row)
        custom.setContentsMargins(0, 8, 0, 0)
        custom.setSpacing(8)
        custom.addWidget(label("副檔名"))
        self.custom_exts = QLineEdit()
        self.custom_exts.setPlaceholderText("例如 psd, blend, 3mf（以逗號分隔）")
        self.custom_exts.setClearButtonEnabled(True)
        self.custom_exts.textChanged.connect(self._custom_changed)
        custom.addWidget(self.custom_exts, 1)
        self.custom_row.hide()
        lay.addWidget(self.custom_row)
        self.custom_error = label("", "error")
        self.custom_error.hide()
        lay.addWidget(self.custom_error)
        lay.addSpacing(12)

        size_row = QHBoxLayout()
        size_row.setSpacing(10)
        size_row.addWidget(label("檔案大小"))
        self.min_size = QComboBox()
        for value, text in SIZE_PRESETS:
            self.min_size.addItem(SIZE_TEXT.get(value, f"{text} 以上"), value)
        self.min_size.currentIndexChanged.connect(lambda _: self.options_changed.emit())
        size_row.addWidget(self.min_size)
        size_row.addWidget(label("太小的檔案通常不值得清理，略過可以快很多", "faint"))
        size_row.addStretch()
        lay.addLayout(size_row)
        lay.addSpacing(18)

        # ── 進階 ──────────────────────────────────────────────────────
        self.advanced_toggle = button("進階選項", "expander", self._toggle_advanced)
        self.advanced_toggle.setIcon(icon("chevron-right"))
        lay.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced, adv = card(margins=16, spacing=10)
        adv.addWidget(label("不要掃描這些資料夾", "section"))
        self.excluded = FolderListWidget("沒有排除的資料夾", add_text="＋ 加入要排除的資料夾",
                                         merge_nested=True, dialog_title="選擇不要掃描的資料夾")
        self.excluded.changed.connect(self.options_changed.emit)
        self.excluded.message.connect(lambda text: self.message.emit(text, "info"))
        adv.addWidget(self.excluded)
        self.skip_system = QCheckBox("略過系統與程式資料夾")
        self.skip_system.setChecked(True)
        self.skip_system.toggled.connect(lambda _: self.options_changed.emit())
        adv.addWidget(self.skip_system)
        adv.addWidget(label("Windows、Program Files、AppData、.git、node_modules 等。"
                            "這些地方的重複檔案通常是程式需要的，刪了可能讓程式壞掉。", "faint", wrap=True))
        self.include_hidden = QCheckBox("包含隱藏的檔案與資料夾")
        self.include_hidden.toggled.connect(lambda _: self.options_changed.emit())
        adv.addWidget(self.include_hidden)
        self.advanced.hide()
        lay.addSpacing(6)
        lay.addWidget(self.advanced)
        lay.addSpacing(24)

        # ── 開始 ──────────────────────────────────────────────────────
        start_row = QHBoxLayout()
        self.start_hint = label("", "muted")
        start_row.addWidget(self.start_hint, 1)
        self.start_button = button("開始掃描", "primary", self._start)
        self.start_button.setProperty("big", True)
        self.start_button.setMinimumWidth(180)
        self.start_button.setMinimumHeight(44)
        start_row.addWidget(self.start_button)
        lay.addLayout(start_row)
        lay.addSpacing(28)

        # ── 上次的結果 ────────────────────────────────────────────────
        self.last_box, last = card(margins=14, spacing=2)
        last_row = QHBoxLayout()
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(label("上次的結果", "section"))
        self.last_text = label("", "muted", wrap=True)
        texts.addWidget(self.last_text)
        last_row.addLayout(texts, 1)
        last_row.addWidget(button("開啟", slot=self.open_last_requested.emit), 0,
                           Qt.AlignmentFlag.AlignVCenter)
        last.addLayout(last_row)
        self.last_box.hide()
        lay.addWidget(self.last_box)
        lay.addStretch(1)

        self._folders_changed()

    # ── 設定 ⇄ 畫面 ──────────────────────────────────────────────────
    def set_options(self, options: ScanOptions):
        self.folders.set_paths(options.roots)
        selected = list(options.types) + ([CUSTOM] if options.custom_exts else [])
        self.types.set_selected(selected)
        self.custom_exts.setText(", ".join(options.custom_exts))
        index = self.min_size.findData(options.min_size)
        if index < 0:                                     # 不在選單裡的大小（舊設定）：加一個選項
            self.min_size.addItem(f"{size_label(options.min_size)} 以上", options.min_size)
            index = self.min_size.count() - 1
        self.min_size.setCurrentIndex(index)
        self.excluded.set_paths(options.exclude_dirs)
        self.skip_system.setChecked(options.skip_system)
        self.include_hidden.setChecked(options.include_hidden)
        if options.exclude_dirs or not options.skip_system or options.include_hidden:
            self._toggle_advanced(True)
        self._types_changed(emit=False)
        self._folders_changed(emit=False)

    def options(self) -> ScanOptions:
        selected = self.types.selected()
        custom = parse_exts(self.custom_exts.text()) if CUSTOM in selected else []
        return ScanOptions(roots=self.folders.paths(),
                           types=[t for t in selected if t in FILE_TYPES],
                           custom_exts=custom,
                           min_size=int(self.min_size.currentData() or 0),
                           exclude_dirs=self.excluded.paths(),
                           skip_system=self.skip_system.isChecked(),
                           include_hidden=self.include_hidden.isChecked())

    # ── 未完成的掃描與上次結果 ────────────────────────────────────────
    def set_job(self, job: ScanJob | None):
        if job is None:
            self.resume_banner.hide()
            return
        where = job.options.roots_summary()
        when = format_time(job.updated_at)
        if job.state == "failed":
            self.resume_banner.set_kind("error")
            title = "上次的掃描因為錯誤而中斷"
            detail = f"{where} · {job.progress_text()} · {when}" + (f"\n錯誤：{job.error}" if job.error else "")
        else:
            self.resume_banner.set_kind("warning")
            title = "上次的掃描沒有完成"
            detail = f"{where} · {job.progress_text()} · {when}"
        self.resume_banner.set_text(title, detail + "\n繼續掃描時，已經比對過的檔案不必再讀一次。")
        self.resume_banner.show()

    def set_last_summary(self, summary: dict | None):
        if not summary:
            self.last_box.hide()
            return
        roots = [r for r in summary.get("roots") or [] if isinstance(r, str)]
        where = roots[0] if len(roots) == 1 else (f"{roots[0]} 等 {len(roots)} 個資料夾" if roots else "")
        groups = summary.get("groups", 0)
        parts = [where, format_time(summary.get("scanned_at")),
                 f"{groups:,} 組重複" if groups else "沒有重複的檔案"]
        if groups and isinstance(summary.get("wasted"), int):
            parts.append(f"最多可釋放 {human_size(summary['wasted'])}")
        self.last_text.setText(" · ".join(p for p in parts if p))
        self.last_box.show()

    # ── 互動 ──────────────────────────────────────────────────────────
    def _toggle_advanced(self, show: bool | None = None):
        if show is None:
            show = self.advanced.isHidden()
        self.advanced.setVisible(show)
        self.advanced_toggle.setIcon(icon("chevron-down" if show else "chevron-right"))

    def _folders_changed(self, emit: bool = True):
        count = len(self.folders.paths())
        self.start_button.setEnabled(count > 0)
        self.start_hint.setText("先加入要掃描的資料夾" if not count else "")
        if emit:
            self.options_changed.emit()

    def _types_changed(self, emit: bool = True):
        custom = CUSTOM in self.types.selected()
        self.custom_row.setVisible(custom)
        if custom and emit:
            self.custom_exts.setFocus()
        self.custom_error.hide()
        if emit:
            self.options_changed.emit()

    def _custom_changed(self):
        self.custom_error.hide()
        self.options_changed.emit()

    def _start(self):
        options = self.options()
        if CUSTOM in self.types.selected() and not options.custom_exts:
            self.custom_error.setText("請輸入要比對的副檔名，例如 psd, blend")
            self.custom_error.show()
            self.custom_exts.setFocus()
            return
        problems = self.folders.check()
        if problems:
            usable = len(options.roots) - len(problems)
            if usable:
                self.message.emit(f"有 {len(problems)} 個資料夾無法掃描（已標成紅色），"
                                  "請從清單移除或確認磁碟有接上", "warning")
            else:
                self.message.emit("清單裡的資料夾都無法掃描，請確認路徑或磁碟", "error")
            return
        self.start_requested.emit(options)
