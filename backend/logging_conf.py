"""Logging configuration — file + console handlers.

RotatingFileHandler writes all levels to /logs/lore.log (10MB × 5 files).
Extractor loggers are forced to DEBUG for detailed pipeline tracing.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.environ.get("LOG_DIR", "/logs")
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


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

    for name in ("extractor", "extractor.utils", "extractor.nodes",
                 "extractor.runner", "extractor.flow"):
        logging.getLogger(name).setLevel(logging.DEBUG)
