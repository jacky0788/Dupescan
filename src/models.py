from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileInfo:
    path: Path
    size: int          # bytes
    hash_full: str = ""
    mtime: float = 0.0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def folder(self) -> str:
        return str(self.path.parent)


@dataclass
class DuplicateGroup:
    hash_value: str
    size: int
    files: list[FileInfo] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        return self.size * (len(self.files) - 1)
