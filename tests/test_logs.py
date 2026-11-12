"""Shared logging: the level from the environment, one console handler, a closed log file."""

import logging
import re
import sys
import types

import pytest

from riskplatform.logs import (
    CONSOLE_HANDLER,
    LEVEL_ENV,
    _formatter,
    log_level,
    log_to_file,
    setup_logging,
)


@pytest.fixture
def clean_root():
    """Put the root logger back as it was, so these tests leave no handler behind."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    for handler in root.handlers:
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)


def test_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv(LEVEL_ENV, raising=False)
    assert log_level() == "INFO"


def test_level_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(LEVEL_ENV, " debug ")
    assert log_level() == "DEBUG"


def test_unknown_level_is_an_error(monkeypatch):
    monkeypatch.setenv(LEVEL_ENV, "LOUD")
    with pytest.raises(ValueError, match=LEVEL_ENV):
        log_level()


def test_setup_twice_adds_one_console_handler(clean_root, monkeypatch):
    monkeypatch.setenv(LEVEL_ENV, "WARNING")
    setup_logging()
    setup_logging()
    ours = [h for h in clean_root.handlers if h.get_name() == CONSOLE_HANDLER]
    assert len(ours) == 1
    assert clean_root.level == logging.WARNING


def test_log_file_gets_utc_records_and_is_closed(clean_root, tmp_path, monkeypatch):
    monkeypatch.setenv(LEVEL_ENV, "INFO")
    setup_logging()
    path = tmp_path / "logs" / "run.log"  # the folder does not exist yet
    before = list(clean_root.handlers)  # pytest has file handlers of its own, so compare
    with log_to_file(path):
        logging.getLogger("riskplatform.example").info("hello %s", "file")
    logging.getLogger("riskplatform.example").info("after the block")

    text = path.read_text(encoding="utf-8")
    assert re.search(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO riskplatform.example hello file$",
                     text, flags=re.MULTILINE)  # fmt: skip
    assert "after the block" not in text
    assert clean_root.handlers == before  # the file handler was removed
    path.unlink()  # possible only because the file was closed (Windows locks open files)


def test_module_run_with_python_m_logs_under_its_real_name(monkeypatch):
    """`python -m riskplatform.pipeline` logs as __main__; the formatter restores the name."""
    started_as = types.SimpleNamespace(__spec__=types.SimpleNamespace(name="riskplatform.pipeline"))
    monkeypatch.setitem(sys.modules, "__main__", started_as)
    record = logging.LogRecord("__main__", logging.INFO, __file__, 1, "hello", None, None)
    assert " INFO riskplatform.pipeline hello" in _formatter().format(record)
