"""Visual review (Codex, 2026-11-15): shorter errors, expandable workflow, narrow table,
compact toolbar."""

from PySide6.QtWidgets import QToolBar, QToolButton

from tests.gui import test_peer_flow as flow
from wggui import apply
from wggui.ui.dialogs import PlanDialog
from wggui.ui.main_window import KEY_COLUMN, PEER_COLUMNS, short_error

db = flow.db
window = flow.window


def test_short_error_prefers_hint():
    msg = ("Befehl fehlgeschlagen (255): sh -c 'cat /etc/os-release'\nssh: connect to host x port 22: "
           "Connection timed out\n→ Keine Antwort: Host aus, Firewall oder falsches Netz (VPN?)")
    assert short_error(msg) == ("Keine Antwort: Host aus, Firewall oder falsches Netz (VPN?). "
                                "(ssh: connect to host x port 22: Connection timed out)")
    rollback = ("Geschrieben, aber nicht bestätigt: Befehl fehlgeschlagen (5): sh -c '…'\nKeine offene Änderung\n"
                "→ srv-a spielt nach 120 s das Backup /etc/wireguard/wg0.conf.wggui-x zurück.")
    text = short_error(rollback)
    assert text.startswith("Geschrieben, aber nicht bestätigt. srv-a spielt nach 120 s") and "sh -c" not in text
    assert short_error("Unerwartet") == "Unerwartet"


def test_plan_steps_are_collapsible(qtbot):
    plan = apply.WritePlan("srv-a", "wg0", "/etc/wireguard", "+[Peer]\n", "x", "c" * 64, True, "20261115-120000",
                           action="phone ändern")
    dlg = PlanDialog(plan)
    qtbot.addWidget(dlg)
    dlg.show()
    assert not dlg.steps.isVisible()
    toggle = dlg.findChild(QToolButton)
    toggle.click()
    assert dlg.steps.isVisible() and "Backup" in dlg.steps.text()


def test_narrow_table_and_toolbar(window):
    window.select(window.hid, "wg0")
    assert window.table.isColumnHidden(KEY_COLUMN) and window.table.isColumnHidden(PEER_COLUMNS.index("Empfangen"))
    window.act_col_key.setChecked(True)
    assert not window.table.isColumnHidden(KEY_COLUMN)
    texts = [a.iconText() for a in window.findChildren(QToolBar)[0].actions() if not a.isSeparator()]
    assert "Host bearbeiten" not in texts and "Verbindung testen" not in texts and "Abgleichen" in texts
