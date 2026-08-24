"""Rotating file + stderr logger, shared across the agent."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from src.config import LOG_DIR

_configured = False


def get_logger(name: str = "pvr") -> logging.Logger:
    global _configured
    log = logging.getLogger(name)
    if _configured:
        return log

    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    fh = RotatingFileHandler(LOG_DIR / "pvr.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)

    _configured = True
    return log
