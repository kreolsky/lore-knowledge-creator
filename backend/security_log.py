"""Centralized security event logger for auth and sensitive operations."""

import logging

security_logger = logging.getLogger("lore.security")


def log_security(event: str, *, user_id: str | None = None, **kwargs: object) -> None:
    """Log a security-relevant event with structured context.

    H-7: Pass extra dict to logger so log aggregators can index structured fields.
    """
    extra = {"security_event": event, "user_id": user_id, **kwargs}
    detail = " ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    security_logger.info("%s | user=%s %s", event, user_id or "anon", detail, extra=extra)
