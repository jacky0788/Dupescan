"""
DupeScan - Logger
Creates a new timestamped log file per session in logs/.
Only important actions are logged (not every file scanned).
"""

import logging
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"


def _setup() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"dupescan_{stamp}.log"

    logger = logging.getLogger("dupescan")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        fh  = logging.FileHandler(log_file, encoding="utf-8")
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    # Attach log_file path for display in UI if needed
    logger.log_file = str(log_file)   # type: ignore[attr-defined]
    return logger


logger = _setup()
