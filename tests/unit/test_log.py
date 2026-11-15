import logging
import stat
import sys

from tests.keys import K1, PRIVATE, PSK
from wggui.log import LOGGER, MASK, default_log_path, install_excepthook, mask, setup_logging


def test_mask_replaces_every_key():
    text = f"PrivateKey = {PRIVATE}\npeer {K1} psk={PSK}"
    out = mask(text)
    assert PRIVATE not in out and PSK not in out and K1 not in out
    assert out.count(MASK) == 3


def test_mask_keeps_normal_text():
    text = "vpn1: sh -c 'wg show all dump' → 0 (0.12 s) 10.98.0.1/24 example.org"
    assert mask(text) == text
    # not a key: 44 characters within a longer Base64 block
    assert mask("A" * 60 + "=") == "A" * 60 + "="


def test_default_log_path(monkeypatch, tmp_path):
    monkeypatch.delenv("WGGUI_LOG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_log_path() == tmp_path / "wireguard-gui" / "wggui.log"
    monkeypatch.setenv("WGGUI_LOG", "/x/y.log")
    assert str(default_log_path()) == "/x/y.log"


def test_setup_logging_writes_masked_file(tmp_path):
    logger = logging.getLogger(LOGGER)
    try:
        path = setup_logging(tmp_path / "sub" / "wggui.log", "DEBUG")
        logging.getLogger("wggui.test").error("Fehler mit %s", PRIVATE)
        for handler in logger.handlers:
            handler.flush()
        content = path.read_text()
        assert "Fehler mit " + MASK in content and PRIVATE not in content
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        setup_logging(path)  # second call does not duplicate handlers
        assert len(logger.handlers) == 2
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)


def test_excepthook_logs_traceback(caplog, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restore after test
    install_excepthook()
    try:
        raise RuntimeError("kaputt")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    record = caplog.records[-1]
    assert record.levelno == logging.CRITICAL and record.exc_info[1].args == ("kaputt",)
