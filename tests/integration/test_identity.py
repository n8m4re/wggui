"""Use a host-specific identity_file against real containers, without an SSH agent."""

import pytest

from wggui.discover import check_connection
from wggui.remote import SshRunner

from .conftest import TMP

pytestmark = pytest.mark.integration


def test_identity_file_without_agent(distro):
    opts = ["-F", "/dev/null", "-o", "IdentityAgent=none", "-o", f"UserKnownHostsFile={TMP / 'known_hosts'}",
            "-o", "StrictHostKeyChecking=accept-new"]
    runner = SshRunner(f"{distro.user}@127.0.0.1", distro.port, sudo=distro.sudo, extra_options=opts, timeout=30,
                       identity_file=str(TMP / "id_test"))
    info = check_connection(runner)
    assert info.is_root and info.wg_installed
