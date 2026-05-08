"""Logging configuration.

Imported once at app startup; submodules thereafter just call
`logging.getLogger("pp_runsheet")` to get the configured logger.

Logs go to a rotating file in DATA_DIR (`app.log`, 512 KB × 3) and, when
running on a TTY, also to stdout. Inside a PyInstaller .app/.exe there's
no TTY so only the file handler is active."""

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE


def setup_logging() -> logging.Logger:
    """Idempotent — calling twice doesn't double the handlers."""
    logger = logging.getLogger("pp_runsheet")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


# Configure on import so any module that does
# `import logging; log = logging.getLogger("pp_runsheet")` after this gets
# the configured logger automatically.
log = setup_logging()
