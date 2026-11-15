"""Per-host SSH keys: system default, optional file path, and key pair generation."""

import shutil
import stat

import pytest

from wggui import sshkeys
from wggui.db import Database
from wggui.remote import SshRunner

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen fehlt")


def test_runner_uses_identity_only_when_set():
    assert "-i" not in SshRunner("root@vpn.example.org").base_command()  # system: agent or ssh config
    cmd = SshRunner("root@vpn.example.org", identity_file="/tmp/x/id_test").base_command()
    i = cmd.index("-i")
    assert cmd[i + 1] == "/tmp/x/id_test" and "IdentitiesOnly=yes" in cmd
    assert cmd.index("--") > i  # options before target


@needs_keygen
def test_generate_and_check(tmp_path, monkeypatch):
    monkeypatch.setenv("WGGUI_SSH_DIR", str(tmp_path / "keys"))
    path = sshkeys.suggest_path("srv-a")
    public = sshkeys.generate(path, "wggui-srv-a")
    assert public.startswith("ssh-ed25519 ") and public.endswith(" wggui-srv-a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert sshkeys.check_identity(str(path)) is None
    assert sshkeys.public_key_text(str(path)) == public
    with pytest.raises(ValueError, match="gibt es schon"):
        sshkeys.generate(path, "wggui-srv-a")  # never overwrite
    path.chmod(0o644)
    assert "chmod 600" in sshkeys.check_identity(str(path))


def test_check_identity_errors(tmp_path):
    assert "fehlt" in sshkeys.check_identity("")
    assert "vollständigen Pfad" in sshkeys.check_identity("id_test")
    assert "nicht gefunden" in sshkeys.check_identity(str(tmp_path / "nix"))
    assert "ohne .pub" in sshkeys.check_identity(str(tmp_path / "x.pub"))
    with pytest.raises(ValueError):
        sshkeys.generate(tmp_path / "k", "kommentar mit leerzeichen")


def test_db_stores_only_path(tmp_path):
    d = Database(tmp_path / "x.db")
    hid = d.add_host("srv-a", "vpn", identity_file="/home/user/keys/srv-a_ed25519")
    assert d.host(hid).identity_file == "/home/user/keys/srv-a_ed25519"
    d.update_host(hid, "srv-a", "vpn", None, identity_file=None)
    assert d.host(hid).identity_file is None
    d.close()
