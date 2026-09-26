"""日志：控制台输出 + 轮转文件（数据目录 logs/，见 settings.data_dir）。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from typefast.settings import data_subdir

LOGGER_NAME = "typefast"


def setup(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    log_dir = data_subdir("logs")
    fh = RotatingFileHandler(log_dir / "typefast.log", maxBytes=2000000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
