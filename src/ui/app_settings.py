# 使用者設定：%APPDATA%\DupeScan\DupeScan.ini（測試用環境變數 DUPESCAN_SETTINGS 指到別處）。
#
# 每個值讀取時都檢查內容，讀不懂（檔案被改壞、舊版本留下的格式）就改用預設值，並在 notice
# 留一句話給介面顯示；寫不進去時（權限、磁碟滿）也一樣只提示，不讓程式出錯。
# 第一次使用新版時，從舊版設定搬過「要掃描的資料夾」等幾個值，使用者不必重新加入。
import json
import os

from PyQt6.QtCore import QByteArray, QSettings

from ..scan_options import ScanOptions, parse_exts
from ..selection import AutoRules, split_dirs
from .theme import DEFAULT_FONT, FONT_SIZES

SORTS = {"wasted": "可釋放最多", "count": "份數最多", "name": "檔名"}
METHODS = {"recycle": "資源回收筒", "quarantine": "隔離資料夾"}


def _open_qsettings() -> QSettings:
    path = os.environ.get("DUPESCAN_SETTINGS")
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "DupeScan", "DupeScan")


class AppSettings:
    def __init__(self, qsettings: QSettings | None = None):
        self._s = qsettings if qsettings is not None else _open_qsettings()
        self.notice = ""            # 讀寫設定時遇到的狀況（給介面顯示一次）
        self._broken: list[str] = []
        if not self._s.contains("scan/options"):
            self._migrate_old()

    @property
    def path(self) -> str:
        return self._s.fileName()

    # ── 基本讀寫 ──────────────────────────────────────────────────────
    def _json(self, key: str):
        raw = self._s.value(key)
        if raw is None or raw == "":
            return None
        try:
            return json.loads(raw if isinstance(raw, str) else str(raw))
        except (TypeError, ValueError):
            self._mark_broken(key)
            return None

    def _choice(self, key: str, choices, default: str) -> str:
        raw = self._s.value(key)
        if raw is None:
            return default
        if raw in choices:
            return raw
        self._mark_broken(key)
        return default

    def _bool(self, key: str, default: bool) -> bool:
        raw = self._s.value(key)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1"):
            return True
        if str(raw).lower() in ("false", "0"):
            return False
        self._mark_broken(key)
        return default

    def _mark_broken(self, key: str):
        if key not in self._broken:
            self._broken.append(key)
            self.notice = "設定檔裡有些設定讀不懂，已改回預設值。"
        self._s.remove(key)

    def _set(self, key: str, value):
        self._s.setValue(key, value)

    def sync(self):
        """寫入磁碟；寫不進去時留下提示（回傳是否成功）。"""
        self._s.sync()
        if self._s.status() != QSettings.Status.NoError:
            self.notice = "無法寫入設定檔，這次的設定不會保存。"
            return False
        return True

    # ── 各項設定 ──────────────────────────────────────────────────────
    @property
    def scan_options(self) -> ScanOptions:
        data = self._json("scan/options")
        return ScanOptions.from_dict(data) if isinstance(data, dict) else ScanOptions()

    @scan_options.setter
    def scan_options(self, options: ScanOptions):
        self._set("scan/options", json.dumps(options.to_dict(), ensure_ascii=False))

    @property
    def auto_rules(self) -> AutoRules:
        return AutoRules.from_dict(self._json("select/rules"))

    @auto_rules.setter
    def auto_rules(self, rules: AutoRules):
        self._set("select/rules", json.dumps(rules.to_dict(), ensure_ascii=False))

    @property
    def text_size(self) -> str:
        return self._choice("view/text_size", FONT_SIZES, DEFAULT_FONT)

    @text_size.setter
    def text_size(self, key: str):
        self._set("view/text_size", key if key in FONT_SIZES else DEFAULT_FONT)

    @property
    def sort(self) -> str:
        return self._choice("view/result_sort", SORTS, "wasted")

    @sort.setter
    def sort(self, key: str):
        self._set("view/result_sort", key if key in SORTS else "wasted")

    @property
    def remove_method(self) -> str:
        return self._choice("remove/method", METHODS, "recycle")

    @remove_method.setter
    def remove_method(self, method: str):
        self._set("remove/method", method if method in METHODS else "recycle")

    @property
    def full_verify(self) -> bool:
        return self._bool("remove/full_verify", False)

    @full_verify.setter
    def full_verify(self, value: bool):
        self._set("remove/full_verify", bool(value))

    @property
    def geometry(self) -> QByteArray | None:
        raw = self._s.value("window/geometry")
        return raw if isinstance(raw, QByteArray) and not raw.isEmpty() else None

    @geometry.setter
    def geometry(self, value: QByteArray):
        self._set("window/geometry", value)

    # ── 舊版設定 ──────────────────────────────────────────────────────
    def _migrate_old(self):
        """從 0.9 版的設定搬過資料夾清單、隱藏檔、副檔名與選取規則用的資料夾。"""
        s = self._s

        def text(key: str) -> str:
            value = s.value(key)
            return value if isinstance(value, str) else ""

        roots = [p for p in split_dirs(text("scan/paths")) if os.path.isabs(p)]
        if not roots:
            return
        options = ScanOptions(
            roots=roots,
            custom_exts=parse_exts(text("scan/include_exts")),
            exclude_dirs=[p for p in split_dirs(text("scan/exclude_dirs")) if os.path.isabs(p)],
            include_hidden=self._bool("scan/include_hidden", False))
        self.scan_options = options
        if not s.contains("select/rules"):
            self.auto_rules = AutoRules(protected_dirs=split_dirs(text("select/reference_dirs")),
                                        remove_dirs=split_dirs(text("select/remove_dirs")))
