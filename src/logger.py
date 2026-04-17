# 日誌模組：每次程式啟動在 logs/ 建立一個帶時間戳記的新 log 檔案。
# 只記錄重要操作（掃描開始/結束、刪除），不記錄每個掃描到的檔案。

import logging
from datetime import datetime
from pathlib import Path

# log 檔放在專案根目錄的 logs/ 子目錄（已加入 .gitignore）
LOG_DIR = Path(__file__).parent.parent / "logs"


def _setup() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)

    # 用啟動時間戳記產生不重複的檔名，方便事後追查特定執行紀錄
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"dupescan_{stamp}.log"

    logger = logging.getLogger("dupescan")
    logger.setLevel(logging.DEBUG)

    # 避免重複加入 handler（模組被 import 多次時會觸發）
    if not logger.handlers:
        fh  = logging.FileHandler(log_file, encoding="utf-8")
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    # 將本次 log 路徑掛在 logger 上，UI 若需要顯示可直接讀取
    logger.log_file = str(log_file)   # type: ignore[attr-defined]
    return logger


# 模組層級單例，其他模組直接 from src.logger import logger 使用
logger = _setup()
