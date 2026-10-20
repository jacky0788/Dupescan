# 日誌模組：程式啟動時由 main.py 呼叫 setup_logging()，才在 logs/ 建立帶時間戳記的 log 檔。
# 只記錄重要操作（掃描開始/結束、刪除），不記錄每個掃描到的檔案。
# 單純 import 不會建立檔案，測試或其他程式引用掃描引擎時不會在 logs/ 留下空檔。

import logging
from datetime import datetime
from pathlib import Path

# log 檔放在專案根目錄的 logs/ 子目錄（已加入 .gitignore）
LOG_DIR = Path(__file__).parent.parent / "logs"

# 模組層級單例，其他模組直接 from src.logger import logger 使用；
# 尚未 setup_logging() 前只掛 NullHandler，訊息直接丟棄
logger = logging.getLogger("dupescan")
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.NullHandler())
logger.log_file = ""   # type: ignore[attr-defined]


def setup_logging(log_dir: Path = LOG_DIR) -> str:
    """建立本次執行的 log 檔並掛上 handler，回傳 log 檔路徑；重複呼叫不會重複建立。"""
    if logger.log_file:   # type: ignore[attr-defined]
        return logger.log_file   # type: ignore[attr-defined]
    log_dir.mkdir(exist_ok=True)

    # 用啟動時間戳記產生不重複的檔名，方便事後追查特定執行紀錄
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"dupescan_{stamp}.log"

    fh  = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    # 將本次 log 路徑掛在 logger 上，UI 若需要顯示可直接讀取
    logger.log_file = str(log_file)   # type: ignore[attr-defined]
    return logger.log_file   # type: ignore[attr-defined]
