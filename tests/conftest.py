import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.keys import PUB0
from wggui.discover import REDACT_CONF, REDACTED
from wggui.remote import FakeRunner

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# never write test settings to the real ~/.config
os.environ["WGGUI_SETTINGS"] = os.path.join(tempfile.mkdtemp(prefix="wggui-test-"), "settings.ini")

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def redact_conf(text: str) -> str:
    """Run the real REDACT_CONF host script locally for FakeRunner responses."""
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
        f.write(text)
    try:
        return subprocess.run(["sh", "-c", REDACT_CONF, "sh", f.name], capture_output=True, text=True,
                              check=True).stdout
    finally:
        os.unlink(f.name)


def redact_dump(text: str) -> str:
    out = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) == 5:
            f[1] = REDACTED
        elif len(f) == 9 and f[2] != "(none)":
            f[2] = REDACTED
        out.append("\t".join(f))
    return "\n".join(out) + "\n"


def debian_runner(conf: str | None = None, dump: str | None = None) -> FakeRunner:
    """Simulate a Debian host with wg0; responses are already redacted."""
    conf = redact_conf(conf if conf is not None else fixture_text("wg0.conf"))
    dump = redact_dump(dump if dump is not None else fixture_text("wg-show-all-dump.txt"))
    return (
        FakeRunner(label="fake-debian")
        .on(r"os-release", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
        .on(r"^id -u", "0\n")
        .on(r"command -v", "wg\nwg-quick\nsystemctl\n")
        .on(r"basename", "wg0\n")
        .on(r"wg pubkey", PUB0 + "\n")
        .on(r"presharedkey.*/etc/wireguard/wg0\.conf", conf)
        .on(r"wg show all dump", dump)
        .on(r"^systemctl list-units", "wg-quick@wg0.service loaded active exited WireGuard via wg-quick(8) for wg0\n")
    )


@pytest.fixture
def runner() -> FakeRunner:
    return debian_runner()
