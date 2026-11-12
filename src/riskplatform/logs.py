"""One logging setup for every command, and an optional log file for a run.

Every command calls setup_logging() once at start. Records go to the console (stderr) as

    2026-11-12T16:13:12Z INFO riskplatform.quality.run Run ... PUBLISHED ...

Timestamps are UTC, like run ids, so a log line and a run folder can be matched. The level comes
from the RISKPLATFORM_LOG_LEVEL environment variable (DEBUG, INFO, WARNING, ERROR; default INFO),
so it can change per environment without editing code.

log_to_file(path) also writes every record to a file while a block runs (the pipeline keeps one
per run under data/logs/), and closes the file afterwards: Windows keeps an open file locked.
"""

import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LEVEL_ENV = "RISKPLATFORM_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CONSOLE_HANDLER = "riskplatform-console"  # name of our console handler, so it is added once
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class _Formatter(logging.Formatter):
    converter = time.gmtime  # UTC, matching the "Z" in DATE_FORMAT

    def format(self, record: logging.LogRecord) -> str:
        # A module run with `python -m` logs as "__main__"; show its real name instead.
        if record.name == "__main__":
            spec = getattr(sys.modules["__main__"], "__spec__", None)
            if spec is not None:
                record.name = spec.name
        return super().format(record)


def _formatter() -> logging.Formatter:
    return _Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def log_level() -> str:
    """The level named by RISKPLATFORM_LOG_LEVEL (default INFO); an unknown name is an error."""
    level = os.environ.get(LEVEL_ENV, DEFAULT_LEVEL).strip().upper()
    if level not in LEVELS:
        raise ValueError(f"{LEVEL_ENV}={level!r} is not one of {', '.join(LEVELS)}")
    return level


def setup_logging() -> None:
    """Send records at the configured level and above to the console. Safe to call twice."""
    root = logging.getLogger()
    root.setLevel(log_level())
    if not any(handler.get_name() == CONSOLE_HANDLER for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.set_name(CONSOLE_HANDLER)
        handler.setFormatter(_formatter())
        root.addHandler(handler)


@contextmanager
def log_to_file(path: str | Path) -> Iterator[Path]:
    """While the block runs, also write every record to path (its folder is created)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_formatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield path
    finally:
        root.removeHandler(handler)
        handler.close()
