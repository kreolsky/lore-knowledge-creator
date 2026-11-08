"""Logging configuration — file + console handlers.

RotatingFileHandler writes all levels to /logs/lore.log (10MB × 5 files).
Extractor loggers are forced to DEBUG for detailed pipeline tracing.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

import httpx

# INFRA WIRING, not configuration (plan component-wiring-not-settings): the
# log directory is the compose mount (/logs in every stack) — no env leg.
LOG_DIR = "/logs"
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class _StripQueryFilter(logging.Filter):
    """Rewrite httpx's request-line URL arg to drop its query string.

    # INVARIANT(security): no httpx log line carries a URL query.
    # Why: a personal key in query mode (SYSTEM: user-secrets) rides in the
    # URL, and httpx logs every request URL at INFO — the key would land in
    # /logs/lore.log, breaking "a user secret never enters a log line".
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                a.copy_with(query=None) if isinstance(a, httpx.URL) and a.query else a
                for a in args
            )
        return True


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_dir = LOG_DIR
    os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "lore.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("httpx").addFilter(_StripQueryFilter())

    for name in ("extractor", "extractor.utils", "extractor.nodes",
                 "extractor.runner", "extractor.flow"):
        logging.getLogger(name).setLevel(logging.DEBUG)
