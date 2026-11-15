"""List and prune old backups on all distros (BusyBox, dash, bash)."""

import pytest

from wggui import apply

from .test_write import fresh  # noqa: F401 – Fixture

pytestmark = pytest.mark.integration

TAGS = [f"2026111{i}-120000" for i in range(7)]


def test_list_and_prune_on_host(fresh):  # noqa: F811
    for tag in TAGS:
        fresh.run(["sh", "-c", 'cp -p /etc/wireguard/wg0.conf "/etc/wireguard/wg0.conf.wggui-$1"', "sh", tag])
    assert apply.list_backups(fresh, "wg0") == TAGS
    old = apply.backups_to_prune(TAGS)
    assert old == TAGS[:2]
    # keep backups used by an active rollback
    fresh.run(["sh", "-c", 'echo "/etc/wireguard/wg0.conf.wggui-$1" > /etc/wireguard/.wggui-pending-wg0-test',
               "sh", TAGS[0]])
    assert apply.prune_backups(fresh, "wg0", old) == [TAGS[1]]
    assert apply.list_backups(fresh, "wg0") == [TAGS[0], *TAGS[2:]]
    fresh.run(["rm", "-f", "/etc/wireguard/.wggui-pending-wg0-test"])
