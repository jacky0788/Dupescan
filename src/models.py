# 核心資料結構：掃描引擎與 UI 共用這兩個 dataclass 傳遞結果
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileInfo:
    """單一檔案的基本資訊，由 Scanner Pass 1 建立，Pass 3 填入 hash_full。"""
    path:      Path         # 檔案絕對路徑
    size:      int          # 檔案大小（bytes）
    hash_full: str  = ""    # 完整 Hash（Pass 3 後填入，空字串表示尚未計算）
    mtime:     float = 0.0  # 修改時間（Unix timestamp）

    @property
    def name(self) -> str:
        """檔案名稱（不含目錄）。"""
        return self.path.name

    @property
    def folder(self) -> str:
        """所在目錄的字串路徑，供 UI 顯示用。"""
        return str(self.path.parent)


@dataclass
class DuplicateGroup:
    """一組 Hash 相同的重複檔案。size 為單一檔案大小，files 含所有副本。"""
    hash_value: str
    size:       int
    files:      list[FileInfo] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        """可釋放的空間 = 每個檔案大小 × (副本數 - 1)，保留一份後可刪除的總量。"""
        return self.size * (len(self.files) - 1)
