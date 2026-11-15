"""Host dialog: select or generate an SSH key; main window: show its public key."""

import shutil

import pytest

from tests.gui import test_peer_flow as flow
from wggui.ui.dialogs import HostDialog, KeyDialog
from wggui.ui.main_window import _default_runner

db = flow.db
window = flow.window


def test_dialog_default_is_system(qtbot):
    dlg = HostDialog()
    qtbot.addWidget(dlg)
    assert dlg.key_system.isChecked() and not dlg.key_path.isEnabled()
    dlg.name.setText("srv-a")
    dlg.target.setText("root@vpn.example.org")
    assert dlg.values()[4] is None and dlg.ok.isEnabled()


def test_dialog_own_file_is_checked(qtbot, tmp_path):
    key = tmp_path / "id_test"
    key.write_text("nicht gelesen\n")
    key.chmod(0o600)
    dlg = HostDialog()
    qtbot.addWidget(dlg)
    dlg.name.setText("srv-a")
    dlg.target.setText("root@vpn.example.org")
    dlg.key_file.setChecked(True)
    assert not dlg.ok.isEnabled() and "fehlt" in dlg.error.text()
    dlg.ask_key_file = lambda: str(key)
    dlg.key_choose.click()
    assert dlg.ok.isEnabled() and dlg.values()[4] == str(key)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen fehlt")
def test_dialog_generates_pair(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("WGGUI_SSH_DIR", str(tmp_path / "keys"))
    dlg = HostDialog()
    qtbot.addWidget(dlg)
    shown = []
    dlg.show_public_key = lambda name, path, text: shown.append((name, path, text))
    dlg.key_file.setChecked(True)
    dlg.key_new.click()
    assert "Namen" in dlg.error.text() and not shown  # no filename without a name
    dlg.name.setText("srv-a")
    dlg.target.setText("root@vpn.example.org")
    dlg.key_new.click()
    [(name, path, text)] = shown
    assert path == str(tmp_path / "keys" / "srv-a_ed25519") and text.startswith("ssh-ed25519 ")
    assert dlg.values()[4] == path and dlg.ok.isEnabled()


def test_runner_and_pubkey_action(qtbot, window, db, tmp_path):
    key = tmp_path / "srv-a_ed25519"
    key.write_text("nicht gelesen\n")
    key.chmod(0o600)
    (tmp_path / "srv-a_ed25519.pub").write_text("ssh-ed25519 AAAATEST wggui-srv-a\n")
    host = db.host(window.hid)
    window.select(window.hid)
    assert not window.act_pubkey.isEnabled()
    db.update_host(host.id, host.name, host.ssh_target, host.port, host.sudo, str(key))
    window.reload_tree()
    window.select(window.hid)
    assert window.act_pubkey.isEnabled() and str(key) in window.info.text()
    assert "-i" in _default_runner(db.host(window.hid)).base_command()
    shown = []
    window.show_public_key = lambda h, text: shown.append(text)
    window.show_host_key()
    assert shown == ["ssh-ed25519 AAAATEST wggui-srv-a"]


def test_key_dialog_copies(qtbot):
    from PySide6.QtGui import QGuiApplication

    dlg = KeyDialog("srv-a", "root@vpn.example.org", "/tmp/k/srv-a_ed25519", "ssh-ed25519 AAAATEST c")
    qtbot.addWidget(dlg)
    dlg.copy()
    assert QGuiApplication.clipboard().text() == "ssh-ed25519 AAAATEST c"
