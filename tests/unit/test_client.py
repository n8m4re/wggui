"""Keys and client logic without Qt or SSH."""

import base64

import pytest

from tests.keys import K1, PUB0
from wggui import client, keys
from wggui.client import ClientSpec
from wggui.parse import parse_conf

# RFC 7748, Section 6.1 (X25519): Alice
RFC_PRIVATE = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
RFC_PUBLIC = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")


def test_public_key_matches_rfc7748():
    priv = base64.b64encode(RFC_PRIVATE).decode()
    assert keys.public_key(priv) == base64.b64encode(RFC_PUBLIC).decode()


def test_generate_keypair_and_psk():
    pair = keys.generate_keypair()
    assert keys.public_key(pair.private) == pair.public
    assert len(base64.b64decode(pair.private)) == len(base64.b64decode(keys.generate_psk())) == 32
    assert pair.private not in repr(pair)  # never expose private key in repr or logs
    assert keys.generate_psk() != keys.generate_psk()


IFACE = ["10.98.0.1/24", "fd98::1/64"]
USED = ["10.98.0.2/32", "10.98.0.3/32", "192.0.2.0/24", "10.98.0.5/32", "fd98::2/128"]


def test_suggest_addresses_skips_used():
    assert client.suggest_addresses(IFACE, USED) == ["10.98.0.4/32", "fd98::3/128"]


def test_suggest_none_when_full():
    used = [f"10.98.0.{i}/32" for i in range(2, 255)]
    assert client.suggest_addresses(["10.98.0.1/24"], used) == []


def test_validate_addresses():
    assert client.validate_addresses(["10.98.0.9"], IFACE, USED) == ["10.98.0.9/32"]
    for bad, text in (("10.98.0.2", "vergeben"), ("10.98.0.1", "vergeben"), ("10.99.0.9", "nicht im Netz"),
                      ("10.98.0.0/30", "einzelne"), ("x", "keine gültige")):
        with pytest.raises(ValueError, match=text):
            client.validate_addresses([bad], IFACE, USED)
    with pytest.raises(ValueError, match="fehlt"):
        client.validate_addresses([], IFACE, USED)


@pytest.mark.parametrize("name", ["", "a\n[Peer]", "# x", "a=b", "x" * 41, "  "])
def test_validate_name_rejects(name):
    with pytest.raises(ValueError):
        client.validate_name(name)


def test_validate_endpoint_and_dns():
    assert client.validate_endpoint(" vpn.example.org:51820 ") == "vpn.example.org:51820"
    assert client.validate_endpoint("[2001:db8::1]:51820") == "[2001:db8::1]:51820"
    for bad in ("vpn.example.org", "vpn.example.org:0", "a b:1", "vpn:99999"):
        with pytest.raises(ValueError):
            client.validate_endpoint(bad)
    assert client.validate_dns("10.98.0.1, 2001:db8::53") == ["10.98.0.1", "2001:db8::53"]
    assert client.validate_dns("") == []
    with pytest.raises(ValueError):
        client.validate_dns("dns.example.org")


def test_default_endpoint():
    assert client.default_endpoint("root@vpn.example.org", 51820) == "vpn.example.org:51820"
    assert client.default_endpoint("vpn1", None) == "vpn1"


def test_server_block_parses_and_preview_hides_psk():
    psk = keys.generate_psk()
    block = client.server_peer_block("Handy Max", K1, ["10.98.0.6/32"], psk)
    conf = parse_conf("[Interface]\nAddress = 10.98.0.1/24\n\n" + block)
    [peer] = conf.peers
    assert (peer.name, peer.public_key, peer.allowed_ips, peer.has_psk) == ("Handy Max", K1, ["10.98.0.6/32"], True)
    preview = client.preview_peer_block("Handy Max", K1, ["10.98.0.6/32"], psk=True)
    assert psk not in preview and "PresharedKey = (neu" in preview


def test_client_config_split_and_full_tunnel():
    pair = keys.generate_keypair()
    spec = ClientSpec("handy", ["10.98.0.6/32"], "vpn.example.org:51820", full_tunnel=False, dns=["10.98.0.1"])
    text = client.client_config(spec, pair.private, PUB0, None, IFACE)
    conf = parse_conf(text)
    assert conf.addresses == ["10.98.0.6/32"] and ("DNS", "10.98.0.1") in conf.extra
    [peer] = conf.peers
    assert peer.public_key == PUB0 and peer.endpoint == "vpn.example.org:51820" and peer.keepalive == 25
    assert peer.allowed_ips == ["10.98.0.0/24", "fd98::/64"] and not peer.has_psk
    full = client.client_config(ClientSpec("handy", ["10.98.0.6/32"], "vpn.example.org:51820", full_tunnel=True,
                                           keepalive=False), pair.private, PUB0, keys.generate_psk(), IFACE)
    [peer] = parse_conf(full).peers
    assert peer.allowed_ips == ["0.0.0.0/0", "::/0"] and peer.has_psk and peer.keepalive is None
