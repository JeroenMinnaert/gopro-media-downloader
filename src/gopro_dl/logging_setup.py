"""Human console logging plus a structured JSON-lines run log."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from rich.logging import RichHandler

LOGGER_NAME = "gopro_dl"


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def setup_logging(log_dir: Path, quiet: bool = False, verbose: bool = False) -> Path:
    """Configure logging; returns the path of this run's JSONL log."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"run-{stamp}.jsonl"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonLinesFormatter())
    logger.addHandler(file_handler)

    console = RichHandler(rich_tracebacks=True, show_path=False, show_time=False)
    console.setLevel(logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO))
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    return log_path


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(level: int, event: str, **fields) -> None:
    """Log with structured fields that land in the JSONL file."""
    get_logger().log(level, event, extra={"extra_fields": fields})
