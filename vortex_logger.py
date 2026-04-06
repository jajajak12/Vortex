"""
Central logging config untuk Vortex.

Usage:
    from vortex_logger import get_logger
    log = get_logger(__name__)
    log.info("...")
    log.warning("...")
    log.error("...")

Output format: HH:MM:SS [LEVEL] message
Ditulis ke stdout (ditangkap oleh redirect > /tmp/scanner.log).
"""

import logging
import sys


_LOG_FORMAT  = "%(asctime)s [%(levelname)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def _configure():
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root = logging.getLogger("vortex")
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str = "vortex") -> logging.Logger:
    _configure()
    # Pastikan child logger selalu di bawah "vortex" namespace
    if not name.startswith("vortex"):
        name = f"vortex.{name}"
    return logging.getLogger(name)
