import pytest

from tests.conftest import fixture_text
from tests.keys import K1, K2, K3, K4, K9, PUB0, SECRETS, fake_key
from wggui.parse import is_valid_key, parse_conf, parse_dump


def test_interface_fields():
    conf = parse_conf(fixture_text("wg0.conf"))
    assert conf.addresses == ["10.98.0.1/24"]
    assert conf.listen_port == 51820
    assert conf.has_private_key
    assert conf.extra == [("PostUp", "iptables -A FORWARD -i %i -j ACCEPT"), ("MTU", "1420")]


def test_peer_names_from_comments():
    peers = {p.public_key: p for p in parse_conf(fixture_text("wg0.conf")).peers}
    assert peers[K1].name == "laptop"  # “# laptop” before [Peer]
    assert peers[K2].name == "phone"  # PiVPN “### begin phone ###”
    assert peers[K3].name == "office"  # “# Name = office” inside the block
    assert peers[K4].name is None  # “### end phone ###” is not a name


def test_peer_fields():
    peers = {p.public_key: p for p in parse_conf(fixture_text("wg0.conf")).peers}
    assert peers[K2].allowed_ips == ["10.98.0.3/32", "192.0.2.0/24"]
    assert peers[K2].has_psk and not peers[K1].has_psk
    assert peers[K3].endpoint == "office.example.org:51820"
    assert peers[K3].keepalive == 25
    assert peers[K4].allowed_ips == ["10.98.0.5/32"]  # inline comment removed; keys are case-insensitive


def test_no_secret_values_kept():
    text = repr(parse_conf(fixture_text("wg0.conf")))
    for secret in SECRETS:
        assert secret not in text


def test_wg_easy_style_name():
    conf = parse_conf("[Interface]\nAddress = 10.98.0.1/24\n\n"
                      "# Client: tablet (0f8e7d6c-1234-5678-9abc-def012345678)\n"
                      f"[Peer]\nPublicKey = {K1}\nAllowedIPs = 10.98.0.7/32\n")
    assert conf.peers[0].name == "tablet"


def test_comment_after_keys_belongs_to_next_peer():
    conf = parse_conf(f"[Interface]\n[Peer]\nPublicKey = {K1}\n# zweiter\n[Peer]\nPublicKey = {K2}\n")
    assert [p.name for p in conf.peers] == [None, "zweiter"]


@pytest.mark.parametrize("text, message", [
    ("[Peer]\nAllowedIPs = 10.98.0.2/32\n", "ohne PublicKey"),
    ("[Interface]\n[Peer]\nPublicKey = kaputt\n", "ungültiger PublicKey"),
    ("[Interface]\nListenPort = abc\n", "keine Zahl"),
    ("Address = 10.98.0.1/24\n", "vor [Interface]"),
    ("[Interface]\nnur text\n", "Schlüssel = Wert"),
    ("[Foo]\n", "unbekannter Abschnitt"),
])
def test_errors(text, message):
    with pytest.raises(ValueError, match=message.replace("[", r"\[")):
        parse_conf(text)


def test_save_config_flag():
    assert parse_conf("[Interface]\nSaveConfig = true\n").save_config


def test_is_valid_key():
    assert is_valid_key(fake_key(7))
    assert not is_valid_key("(redacted)")
    assert not is_valid_key("AAAA")


def test_dump():
    rt = parse_dump(fixture_text("wg-show-all-dump.txt"))["wg0"]
    assert rt.public_key == PUB0 and rt.listen_port == 51820
    assert rt.peers[K1].endpoint == "203.0.113.5:40000"
    assert rt.peers[K1].latest_handshake == 1800000000
    assert (rt.peers[K1].rx_bytes, rt.peers[K1].tx_bytes) == (1024, 2048)
    assert rt.peers[K2].allowed_ips == ["10.98.0.3/32", "192.0.2.0/24"]
    assert rt.peers[K2].endpoint is None
    assert rt.peers[K9].keepalive == 25


def test_dump_rejects_unknown_lines():
    with pytest.raises(ValueError):
        parse_dump("a\tb\tc\n")


def test_commented_setting_is_not_a_name():
    text = f"[Interface]\nPrivateKey = x\n\n# AllowedIPs = 0.0.0.0/0\n[Peer]\nPublicKey = {K1}\n"
    assert parse_conf(text).peers[0].name is None
