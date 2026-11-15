from wggui import fmt


def test_bytes():
    assert fmt.bytes_h(512) == "512 B"
    assert fmt.bytes_h(2048) == "2.0 KiB"
    assert fmt.bytes_h(5 * 1024**3) == "5.0 GiB"


def test_handshake():
    now = 1_000_000
    assert fmt.handshake_h(0, now) == "nie"
    assert fmt.handshake_h(now - 30, now) == "vor 30 s"
    assert fmt.handshake_h(now - 300, now) == "vor 5 min"
    assert fmt.handshake_h(now - 7200, now) == "vor 2 h"
    assert fmt.handshake_h(now - 3 * 86400, now) == "vor 3 d"


def test_is_active():
    assert fmt.is_active(990, 1000)
    assert not fmt.is_active(0, 1000)
    assert not fmt.is_active(1000 - fmt.ACTIVE_SECONDS, 1000)


def test_short_key():
    assert fmt.short_key(None) == "–"
    assert fmt.short_key("ABCDEFGHIJ") == "ABCDEFGH…"
