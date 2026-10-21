# 掃描範圍：要掃哪些資料夾、比對哪些檔案。「開始」畫面、掃描引擎與續掃紀錄共用。
#
#   檔案類型   — 全部，或圖片／影片／音樂／文件／壓縮檔／AI 模型（可複選），另可加自訂副檔名
#   略過小檔案 — 小於門檻的檔案不比對（預設 100 KB；0＝不略過，但空檔案一律不算）
#   排除資料夾 — 使用者挑選的完整路徑，連同底下所有子資料夾
#   略過系統與程式資料夾（預設開）— Windows、Program Files、ProgramData、AppData、.git、
#                node_modules 等由系統或程式管理的地方，裡面的重複通常不該動
#   包含隱藏檔（預設關）— 「.」開頭或帶 Windows 隱藏／系統屬性的檔案與資料夾
import os
from dataclasses import dataclass, field

FILE_TYPES: dict[str, tuple[str, frozenset[str]]] = {
    "image": ("圖片", frozenset({
        "jpg", "jpeg", "jfif", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "heif", "avif",
        "psd", "raw", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "svg", "ico", "jxl"})),
    "video": ("影片", frozenset({
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "ts", "m2ts",
        "mts", "3gp", "vob", "rmvb", "ogv"})),
    "audio": ("音樂", frozenset({
        "mp3", "flac", "wav", "aac", "m4a", "ogg", "opus", "wma", "ape", "alac", "aiff", "aif",
        "dsf", "dff", "mka", "tta", "wv"})),
    "document": ("文件", frozenset({
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "txt",
        "md", "csv", "epub", "mobi", "azw3", "djvu", "cbz", "cbr"})),
    "archive": ("壓縮檔", frozenset({
        "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "cab", "zipmod"})),
    "model": ("AI 模型", frozenset({
        "safetensors", "ckpt", "pt", "pth", "gguf", "ggml", "onnx"})),
}

KB, MB = 1024, 1024 * 1024
SIZE_PRESETS: list[tuple[int, str]] = [
    (0, "不略過"), (100 * KB, "100 KB"), (1 * MB, "1 MB"), (10 * MB, "10 MB"), (100 * MB, "100 MB"),
]
DEFAULT_MIN_SIZE = 100 * KB

# 「略過系統與程式資料夾」：任何位置的這些資料夾名稱（不分大小寫）……
SYSTEM_DIR_NAMES = frozenset({
    "appdata", ".git", ".svn", ".hg", "node_modules", "__pycache__", "site-packages", ".venv",
    "steamapps", "windowsapps",
})
# ……以及這些系統路徑（由環境變數取得，沒有設定的略過）
_SYSTEM_PATH_VARS = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")


def system_paths() -> frozenset[str]:
    paths = set()
    for var in _SYSTEM_PATH_VARS:
        value = os.environ.get(var)
        if value:
            paths.add(os.path.normcase(os.path.abspath(value)))
    return frozenset(paths)


def parse_exts(text: str) -> list[str]:
    """「psd, .AI  raw」→ ["psd", "ai", "raw"]：以逗號、分號或空白分隔，去掉點、轉小寫、去重複。"""
    parts = text.replace(";", ",").replace("，", ",").replace(" ", ",").split(",")
    return list(dict.fromkeys(p.strip().lstrip(".").lower() for p in parts if p.strip().lstrip(".")))


def file_type_of(name: str) -> str | None:
    """檔名屬於哪個檔案類型（"image" 等）；不屬於任何類型時回傳 None。"""
    ext = os.path.splitext(name)[1][1:].lower()
    for key, (_, exts) in FILE_TYPES.items():
        if ext in exts:
            return key
    return None


def size_label(size: int) -> str:
    for value, label in SIZE_PRESETS:
        if value == size:
            return label
    return f"{size:,} B"


@dataclass
class ScanOptions:
    roots: list[str] = field(default_factory=list)          # 要掃描的資料夾（完整路徑）
    types: list[str] = field(default_factory=list)          # 檔案類型；空＝全部
    custom_exts: list[str] = field(default_factory=list)    # 另外要比對的副檔名（不含點、小寫）
    min_size: int = DEFAULT_MIN_SIZE                        # 小於此大小的檔案不比對
    exclude_dirs: list[str] = field(default_factory=list)   # 排除的資料夾（完整路徑）
    skip_system: bool = True                                # 略過系統與程式資料夾
    include_hidden: bool = False                            # 包含隱藏／系統屬性的檔案與資料夾

    def extensions(self) -> frozenset[str] | None:
        """要比對的副檔名；None 表示所有檔案。"""
        if not self.types and not self.custom_exts:
            return None
        exts = {e.lower().lstrip(".") for e in self.custom_exts if e}
        for t in self.types:
            if t in FILE_TYPES:
                exts |= FILE_TYPES[t][1]
        return frozenset(exts)

    def excluded_names(self) -> frozenset[str]:
        return SYSTEM_DIR_NAMES if self.skip_system else frozenset()

    def excluded_paths(self) -> frozenset[str]:
        paths = {os.path.normcase(os.path.abspath(d)) for d in self.exclude_dirs if d}
        if self.skip_system:
            paths |= system_paths()
        return frozenset(paths)

    # ── 存取（續掃紀錄、設定檔）──────────────────────────────────────
    def to_dict(self) -> dict:
        return {"roots": list(self.roots), "types": list(self.types),
                "custom_exts": list(self.custom_exts), "min_size": self.min_size,
                "exclude_dirs": list(self.exclude_dirs), "skip_system": self.skip_system,
                "include_hidden": self.include_hidden}

    @classmethod
    def from_dict(cls, data) -> "ScanOptions":
        """讀回設定；缺少或型別不對的欄位用預設值（檔案被改壞也不會出錯）。"""
        if not isinstance(data, dict):
            return cls()

        def strings(key):
            value = data.get(key)
            return [s for s in value if isinstance(s, str) and s] if isinstance(value, list) else []

        min_size = data.get("min_size")
        return cls(
            roots=strings("roots"),
            types=[t for t in strings("types") if t in FILE_TYPES],
            custom_exts=parse_exts(",".join(strings("custom_exts"))),
            min_size=min_size if isinstance(min_size, int) and min_size >= 0 else DEFAULT_MIN_SIZE,
            exclude_dirs=strings("exclude_dirs"),
            skip_system=data.get("skip_system") is not False,
            include_hidden=data.get("include_hidden") is True,
        )

    # ── 顯示 ──────────────────────────────────────────────────────────
    def roots_summary(self) -> str:
        """「D:\\Downloads」或「D:\\Downloads 等 3 個資料夾」。"""
        if not self.roots:
            return "（沒有資料夾）"
        return self.roots[0] if len(self.roots) == 1 else f"{self.roots[0]} 等 {len(self.roots)} 個資料夾"

    def describe(self) -> str:
        """寫進 log 的完整設定。"""
        types = "、".join(FILE_TYPES[t][0] for t in self.types if t in FILE_TYPES) or "全部"
        parts = [f"路徑: {'; '.join(self.roots)}", f"類型: {types}"]
        if self.custom_exts:
            parts.append(f"其他副檔名: {', '.join(self.custom_exts)}")
        parts.append(f"略過小於: {size_label(self.min_size)}")
        if self.exclude_dirs:
            parts.append(f"排除: {'; '.join(self.exclude_dirs)}")
        parts.append("略過系統資料夾" if self.skip_system else "包含系統資料夾")
        parts.append("包含隱藏檔" if self.include_hidden else "略過隱藏檔")
        return "，".join(parts)
