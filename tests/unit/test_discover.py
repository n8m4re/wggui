import re
import subprocess

import pytest

from tests.conftest import FIXTURES, debian_runner, fixture_text
from tests.keys import K9, PRIVATE, PSK, PUB0, SECRETS
from wggui.discover import (
    DUMP_AWK,
    PUBKEY,
    REDACT_CONF,
    REDACTED,
    discover,
    parse_os_release,
    read_conf_redacted,
    read_os_release,
)
from wggui.remote import FakeRunner


def _sh(script: str, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(["sh", "-c", script, "sh", *args], input=stdin, capture_output=True, text=True,
                          check=True).stdout


def test_redact_conf_script_really_redacts():
    """Run the real host sed script locally against the fixture."""
    out = _sh(REDACT_CONF, str(FIXTURES / "wg0.conf"))
    for secret in SECRETS:
        assert secret not in out
    assert out.count(REDACTED) == 2
    assert "PostUp = iptables" in out  # rest unchanged


def test_redact_conf_case_insensitive(tmp_path):
    f = tmp_path / "x.conf"
    f.write_text(f"[Interface]\n  privatekey={PRIVATE}\n")
    assert PRIVATE not in _sh(REDACT_CONF, str(f))


def test_dump_awk_really_redacts():
    out = _sh(DUMP_AWK, stdin=fixture_text("wg-show-all-dump.txt"))
    for secret in SECRETS:
        assert secret not in out
    assert PUB0 in out and K9 in out
    assert out.count(REDACTED) == 2  # interface key and one PSK; “(none)” remains


def test_pubkey_script_extracts_only_private_key_line():
    # replace “wg pubkey” with “cat” to inspect piped input locally
    out = _sh(PUBKEY.replace("wg pubkey", "cat"), str(FIXTURES / "wg0.conf"))
    assert out.strip() == PRIVATE


def test_discover(runner):
    state = discover(runner)
    assert state.info.os_name == "Debian GNU/Linux 13 (trixie)"
    assert state.info.is_root and state.info.wg_installed
    assert list(state.confs) == ["wg0"]
    assert state.public_keys["wg0"] == PUB0
    assert len(state.runtime["wg0"].peers) == 3
    assert state.active_units == ["wg-quick@wg0.service"]
    assert state.warnings == []


def test_discover_is_read_only(runner):
    discover(runner)
    forbidden = re.compile(r"\b(rm|mv|cp|tee|dd|chmod|chown|mkdir|truncate|ip link|systemd-run)\b|"
                           r"wg-quick (up|down|save)|\bwg (set|setconf|syncconf|addconf)\b|"
                           r"systemctl (start|stop|restart|enable|disable)|"
                           r">(?!/dev/null|&1)")  # redirect only to /dev/null
    for call in runner.calls:
        assert call.stdin is None
        joined = " ".join(call.argv)
        assert not forbidden.search(joined), joined


def test_discover_not_root():
    r = (FakeRunner().on(r"os-release", "NAME=Debian\n").on(r"^id -u", "1000\n").on(r"command -v", ""))
    state = discover(r)
    assert not state.info.is_root
    assert state.confs == {}
    assert "Kein root-Zugriff" in state.warnings[0]


def test_discover_warnings():
    r = debian_runner(conf="[Interface]\nSaveConfig = true\n", dump="")
    state = discover(r)
    assert any("SaveConfig" in w for w in state.warnings)
    assert any("läuft nicht" in w for w in state.warnings)


def test_discover_skips_bad_names_and_broken_conf():
    r = debian_runner(conf="kaputt\n").on(r"basename", "wg0\nbad;name\n")
    r.rules.insert(0, (re.compile("basename"), "wg0\nbad;name\n", 0, ""))
    state = discover(r)
    assert any("bad;name" in w for w in state.warnings)
    assert any("wg0.conf nicht lesbar" in w for w in state.warnings)


def test_read_conf_rejects_bad_interface_name(runner):
    with pytest.raises(ValueError):
        read_conf_redacted(runner, "../etc/shadow")


def test_parse_os_release():
    assert parse_os_release('NAME="Debian GNU/Linux"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n') \
        == "Debian GNU/Linux 13 (trixie)"
    assert parse_os_release("NAME=Foo\n") == "Foo"
    assert parse_os_release("") == "unbekannt"


def test_redact_conf_commented_out_keys(tmp_path):
    """Commented-out old keys must not leave the host either."""
    f = tmp_path / "x.conf"
    f.write_text(f"[Interface]\n#PrivateKey = {PRIVATE}\n  ##  presharedkey={PSK}\n# Name = laptop\n")
    out = _sh(REDACT_CONF, str(f))
    assert PRIVATE not in out and PSK not in out
    assert out.count(REDACTED) == 2 and "# Name = laptop" in out


def test_pubkey_ignores_commented_key(tmp_path):
    f = tmp_path / "x.conf"
    f.write_text(f"[Interface]\n# PrivateKey = {PSK}\nPrivateKey = {PRIVATE} # aktiv\n")
    assert _sh(PUBKEY.replace("wg pubkey", "cat"), str(f)).strip() == PRIVATE


@pytest.mark.parametrize(("text", "family"), [
    ('ID=debian\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"', "debian"),
    ('ID=ubuntu\nID_LIKE=debian', "debian"),
    ('ID=fedora', "fedora"),
    ('ID="rocky"\nID_LIKE="rhel centos fedora"', "rhel"),
    ('ID=almalinux\nID_LIKE="rhel centos fedora"', "rhel"),
    ('ID=arch', "arch"),
    ('ID=manjaro\nID_LIKE=arch', "arch"),
    ('ID=alpine', "alpine"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"', "suse"),
    ('ID=exotic', None),
])
def test_os_family(text, family):
    assert read_os_release(text).family == family


def test_missing_wg_gives_install_hint():
    r = (FakeRunner().on("os-release", "ID=alpine\nPRETTY_NAME=Alpine\n").on("^id -u", "0\n")
         .on("command -v", "").on("basename", ""))
    state = discover(r)
    assert any("apk add -U wireguard-tools" in w for w in state.warnings)
    assert not any(c.argv[0] == "systemctl" for c in r.calls)  # without systemd, do not call systemctl


def test_runtime_parse_error_is_warning(runner):
    runner.rules.insert(0, (re.compile("wg show all dump"), "kaputt\n", 0, ""))
    state = discover(runner)
    assert state.runtime == {}
    assert any("wg show" in w for w in state.warnings)
