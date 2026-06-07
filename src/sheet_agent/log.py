"""Structured-ish logging to stdout (captured by journald under systemd)."""
from __future__ import annotations

import logging
import os
import sys


def setup(level: str | None = None) -> logging.Logger:
    """Build the `sheet_agent` logger. The level comes from the `LOG_LEVEL` env var
    (e.g. DEBUG/INFO/WARNING) so the daemon stays 12-factor; an explicit `level`
    argument overrides it, and an unknown value falls back to INFO."""
    level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    logger = logging.getLogger("sheet_agent")
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = setup()
