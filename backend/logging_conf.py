"""Logging configuration — file + console handlers.

RotatingFileHandler writes all levels to /logs/lore.log (10MB × 5 files).
Extractor loggers are forced to DEBUG for detailed pipeline tracing.
"""
import logging
import os
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from typing import Any

# INFRA WIRING, not configuration: the log directory is the compose mount
# (/logs in every stack) — no env leg.
LOG_DIR = "/logs"
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


#: True inside `http_clients.isolated_client` — a request line logged there
#: shows the host only.
hide_url_path: ContextVar[bool] = ContextVar("hide_url_path", default=False)


class _StripQueryFilter(logging.Filter):
    """Rewrite a request line's URL arg to drop its query string, and its path
    where the path is not ours to log.

    # INVARIANT(security): no httpx log line carries a URL query; no line of an
    # isolated_client exchange or of httpx2 (the MCP SDK client) carries a path.
    # Why: a personal key in query mode (SYSTEM: user-secrets) rides in the
    # URL, and httpx logs every request URL at INFO — the key would land in
    # /logs/lore.log, breaking "a user secret never enters a log line". A
    # service whose secret is its address carries that secret in the path.

    # WHY the URL is recognised by `copy_with`: httpx2 is a transitive package
    # this project does not import.
    """

    def __init__(self, *, hide_path: bool = False) -> None:
        super().__init__()
        self._hide_path = hide_path

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            hide = self._hide_path or hide_url_path.get()
            record.args = tuple(
                _loggable_url(a, hide) if hasattr(a, "copy_with") else a
                for a in args
            )
        return True


def _loggable_url(url: Any, hide_path: bool) -> Any:
    # WHY a ready string: copy_with(path="/[path hidden]") percent-encodes the
    # space.
    if hide_path:
        return str(url.copy_with(path="/", query=None, fragment=None)) + "[path hidden]"
    return url.copy_with(query=None) if url.query else url


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
    logging.getLogger("httpx2").addFilter(_StripQueryFilter(hide_path=True))

    for name in ("extractor", "extractor.utils", "extractor.nodes",
                 "extractor.runner", "extractor.flow"):
        logging.getLogger(name).setLevel(logging.DEBUG)
