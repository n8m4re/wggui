"""Start test containers and provide one SshRunner per target (Debian, Ubuntu with sudo, Fedora, Alpine).

- Keys: disposable ed25519 keys in tests/integration/_tmp/ (gitignored), without SSH agent.
- known_hosts: separate file in _tmp/, `accept-new` here only, never in the app.
- `-F /dev/null`: ignore personal ssh config.
- WGGUI_IT_KEEP=1 leaves containers running after tests.
- WGGUI_IT_DISTROS=debian,alpine limits the targets.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from wggui.remote import SshRunner

HERE = Path(__file__).parent
TMP = HERE / "_tmp"
COMPOSE = ["docker", "compose", "-f", str(HERE / "compose.yaml")]


@dataclass(frozen=True)
class Target:
    name: str
    port: int
    user: str
    sudo: bool
    family: str
    os_prefix: str


TARGETS = {
    t.name: t
    for t in (
        Target("debian", 22211, "root", False, "debian", "Debian GNU/Linux 13"),
        Target("ubuntu", 22212, "admin", True, "debian", "Ubuntu 24.04"),
        Target("fedora", 22213, "root", False, "fedora", "Fedora Linux 44"),
        Target("alpine", 22214, "root", False, "alpine", "Alpine Linux"),
        Target("alma9", 22215, "root", False, "rhel", "AlmaLinux 9"),     # RHEL 9 compatible
        Target("alma10", 22216, "admin", True, "rhel", "AlmaLinux 10"),  # RHEL 10 compatible, sudo
    )
}
SELECTED = [n for n in os.environ.get("WGGUI_IT_DISTROS", ",".join(TARGETS)).split(",") if n in TARGETS]


def ssh_options() -> list[str]:
    return ["-F", "/dev/null", "-i", str(TMP / "id_test"), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={TMP / 'known_hosts'}", "-o", "StrictHostKeyChecking=accept-new"]


def make_runner(target: Target, *, sudo: bool | None = None) -> SshRunner:
    return SshRunner(f"{target.user}@127.0.0.1", target.port, sudo=target.sudo if sudo is None else sudo,
                     extra_options=ssh_options(), timeout=30)


@pytest.fixture(scope="session")
def stack():
    TMP.mkdir(exist_ok=True)
    key = TMP / "id_test"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wggui-it", "-f", str(key)], check=True)
    (TMP / "known_hosts").unlink(missing_ok=True)  # new containers have new host keys
    proc = subprocess.run([*COMPOSE, "up", "-d", "--build", "--force-recreate", *SELECTED], capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"docker compose up fehlgeschlagen:\n{proc.stderr[-3000:]}")
    yield
    if os.environ.get("WGGUI_IT_KEEP") != "1":
        subprocess.run([*COMPOSE, "down", "-v", "--remove-orphans"], check=False, capture_output=True)


@pytest.fixture(scope="session", params=SELECTED)
def distro(request, stack) -> Target:
    target = TARGETS[request.param]
    runner = make_runner(target, sudo=False)
    deadline = time.monotonic() + 30
    while not runner.run(["true"], check=False).ok:
        if time.monotonic() > deadline:
            raise RuntimeError(f"sshd im Container {target.name} nicht erreichbar")
        time.sleep(0.5)
    return target


@pytest.fixture(scope="session")
def target(distro) -> SshRunner:
    return make_runner(distro)
