"""Run real host write scripts locally against a temporary directory with fake `wg`/`wg-quick`.
Rollback and confirmation run for real (setsid, sleep)."""

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import fixture_text
from tests.keys import K1, SECRETS
from wggui import apply
from wggui.apply import APPLY, BUILD_APPEND, CONFIRM, PREPARE, ROLLBACK, WritePlan
from wggui.client import server_peer_block
from wggui.keys import generate_psk
from wggui.remote import CommandError, FakeRunner

pytestmark = pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid fehlt")

# wg: “show <if>” runs if $FAKE_RUNNING=1; “syncconf <if> <file>” copies to $FAKE_LOG/synced
FAKE_WG = """#!/bin/sh
case "$1" in
  show) [ "${FAKE_RUNNING:-0}" = 1 ] ;;
  syncconf) [ "${FAKE_SYNC_FAIL:-0}" = 1 ] && exit 1; cp "$3" "$FAKE_LOG/synced" ;;
  *) exit 2 ;;
esac
"""
# wg-quick strip <file>: remove wg-quick options (PostUp/MTU); $FAKE_STRIP_EMPTY=1 returns nothing
FAKE_WG_QUICK = """#!/bin/sh
[ "$1" = strip ] || exit 2
[ "${FAKE_STRIP_EMPTY:-0}" = 1 ] && exit 0
grep -v -e '^PostUp' -e '^MTU' "$2"
"""
BLOCK = server_peer_block("handy", K1, ["10.98.0.6/32"], None)


@pytest.fixture
def host(tmp_path, monkeypatch):
    conf_dir = tmp_path / "wireguard"
    conf_dir.mkdir()
    (conf_dir / "wg0.conf").write_text(fixture_text("wg0.conf"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("wg", FAKE_WG), ("wg-quick", FAKE_WG_QUICK)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log_dir))
    return conf_dir, log_dir


def _sh(script, *args, stdin=None, check=True):
    return subprocess.run(["sh", "-c", script, "sh", *args], input=stdin, capture_output=True, text=True,
                          check=check)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _apply(conf_dir, seconds="30", tag="20261115-120000", expected=None, block=BLOCK, check=True):
    conf = conf_dir / "wg0.conf"
    return _sh(APPLY, "wg0", str(conf_dir), expected or _hash(conf), tag, seconds, ROLLBACK, BUILD_APPEND,
               stdin=block, check=check)


def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_prepare_reports_hash_and_tools(host, monkeypatch):
    conf_dir, _ = host
    out = _sh(PREPARE, "wg0", str(conf_dir)).stdout
    assert f"sha256 {_hash(conf_dir / 'wg0.conf')}" in out
    assert "running no" in out and "setsid yes" in out and "wgquick yes" in out
    monkeypatch.setenv("FAKE_RUNNING", "1")
    assert "running yes" in _sh(PREPARE, "wg0", str(conf_dir)).stdout
    assert _sh(PREPARE, "wg9", str(conf_dir), check=False).returncode == 2


def test_apply_file_only_then_confirm(host):
    conf_dir, log_dir = host
    original = (conf_dir / "wg0.conf").read_text()
    out = _apply(conf_dir, seconds="1").stdout
    assert "file-only" in out
    text = (conf_dir / "wg0.conf").read_text()
    assert text.startswith(original) and text.endswith(BLOCK)
    backup = conf_dir / "wg0.conf.wggui-20261115-120000"
    assert backup.read_text() == original
    assert oct(backup.stat().st_mode & 0o777) == oct((conf_dir / "wg0.conf").stat().st_mode & 0o777)
    assert not (log_dir / "synced").exists()
    assert _sh(CONFIRM, "wg0", str(conf_dir), "20261115-120000").stdout.strip() == "confirmed"
    time.sleep(1.5)  # timer expired but did nothing
    assert (conf_dir / "wg0.conf").read_text() == text
    assert not list(conf_dir.glob(".wggui-*")) and not (conf_dir / "wg0.conf.wggui-new").exists()


def test_rollback_without_confirm(host, monkeypatch):
    conf_dir, log_dir = host
    monkeypatch.setenv("FAKE_RUNNING", "1")
    original = (conf_dir / "wg0.conf").read_text()
    assert "synced" in _apply(conf_dir, seconds="1").stdout
    assert BLOCK in (log_dir / "synced").read_text()
    assert _wait_for(lambda: (conf_dir / "wg0.conf").read_text() == original), "Rollback lief nicht"
    assert _wait_for(lambda: BLOCK not in (log_dir / "synced").read_text()), "Laufzeit nicht zurückgesetzt"
    assert _wait_for(lambda: not list(conf_dir.glob(".wggui-*"))), "Markierung/Temp-Datei bleibt liegen"
    confirm = _sh(CONFIRM, "wg0", str(conf_dir), "20261115-120000", check=False)
    assert confirm.returncode == 5 and "Rollback" in confirm.stderr


def test_synced_config_is_stripped_and_temp_removed(host, monkeypatch):
    conf_dir, log_dir = host
    monkeypatch.setenv("FAKE_RUNNING", "1")
    _apply(conf_dir)
    synced = (log_dir / "synced").read_text()
    assert "PostUp" not in synced and BLOCK in synced
    assert not list(conf_dir.glob(".wggui-strip.*"))
    _sh(CONFIRM, "wg0", str(conf_dir), "20261115-120000")


def test_changed_file_is_refused(host):
    conf_dir, _ = host
    result = _apply(conf_dir, expected="0" * 64, check=False)
    assert result.returncode == 3 and "geändert" in result.stderr
    assert BLOCK not in (conf_dir / "wg0.conf").read_text()
    assert not list(conf_dir.glob("wg0.conf.wggui-*")) and not list(conf_dir.glob(".wggui-*"))


@pytest.mark.parametrize("env", ["FAKE_SYNC_FAIL", "FAKE_STRIP_EMPTY"])
def test_failed_sync_restores(host, monkeypatch, env):
    """Restore the old file if syncconf fails or strip returns nothing (which would remove all peers)."""
    conf_dir, log_dir = host
    monkeypatch.setenv("FAKE_RUNNING", "1")
    monkeypatch.setenv(env, "1")
    original = (conf_dir / "wg0.conf").read_text()
    result = _apply(conf_dir, check=False)
    assert result.returncode == 4 and "wiederhergestellt" in result.stderr
    assert (conf_dir / "wg0.conf").read_text() == original
    assert not list(conf_dir.glob(".wggui-*"))
    if env == "FAKE_STRIP_EMPTY":
        assert not (log_dir / "synced").exists()  # never apply an empty config


def test_python_api_sends_secret_only_via_stdin():
    psk = generate_psk()
    block = server_peer_block("handy", K1, ["10.98.0.6/32"], psk)
    plan = WritePlan("srv-a", "wg0", "/etc/wireguard", "preview", block, "a" * 64, True, "20261115-120000")
    runner = FakeRunner().on(r"sha256sum", "synced\nbackup /etc/wireguard/wg0.conf.wggui-20261115-120000\n")
    assert apply.apply(runner, plan) == "synced"
    [call] = runner.calls
    assert call.stdin == block
    assert all(psk not in arg for arg in call.argv)
    assert psk not in repr(plan) and block not in repr(plan)
    for secret in SECRETS:
        assert all(secret not in arg for arg in call.argv)


def test_prepare_and_check_plan():
    runner = FakeRunner().on(r"sha256sum", f"sha256 {'b' * 64}\nrunning yes\nsetsid yes\nwgquick no\n")
    prepared = apply.prepare(runner, "wg0")
    assert prepared == apply.Prepared("b" * 64, True, True, False)
    plan = WritePlan("srv-a", "wg0", "/etc/wireguard", "p", "b", "b" * 64, True, apply.make_tag())
    with pytest.raises(ValueError, match="wg-quick"):
        apply.check_plan(plan, prepared)
    with pytest.raises(ValueError, match="setsid"):
        apply.check_plan(plan, apply.Prepared("b" * 64, False, False, True))
    apply.check_plan(plan, apply.Prepared("b" * 64, False, True, False))
    with pytest.raises(ValueError):
        apply.prepare(FakeRunner().on(r"sha256sum", "kaputt\n"), "wg0")
    with pytest.raises(ValueError):
        apply.prepare(runner, "wg0; rm -rf /")


def test_confirm_failure_raises():
    plan = WritePlan("srv-a", "wg0", "/etc/wireguard", "p", "b", "b" * 64, True, "20261115-120000")
    with pytest.raises(CommandError):
        apply.confirm(FakeRunner().on(r"pending", "", 5, "Keine offene Änderung"), plan)


def test_ssh_over_interface():
    assert apply.ssh_over_interface(["10.98.0.1"], ["10.98.0.1/24"])
    assert not apply.ssh_over_interface(["203.0.113.7"], ["10.98.0.1/24"])
    assert not apply.ssh_over_interface([], ["10.98.0.1/24"])


def test_steps_mention_backup_and_rollback():
    plan = WritePlan("srv-a", "wg0", "/etc/wireguard", "p", "b", "b" * 64, False, "20261115-120000")
    steps = "\n".join(plan.steps())
    assert "wg0.conf.wggui-20261115-120000" in steps and "120 s" in steps and "nächsten Start" in steps
    assert "Peer-Block anhängen" in steps


def test_list_and_prune_backups(tmp_path):
    from wggui.apply import LIST_BACKUPS, PRUNE_BACKUPS, backups_to_prune

    d = tmp_path
    (d / "wg0.conf").write_text("x")
    tags = [f"2026111{i}-120000" for i in range(8)]
    for t in tags:
        (d / f"wg0.conf.wggui-{t}").write_text(t)
    (d / "wg0.conf.wggui-new").write_text("kein Backup")  # not a backup
    listed = _sh(LIST_BACKUPS, "wg0", str(d)).stdout.split()
    assert listed == tags
    old = backups_to_prune(listed, keep=5)
    assert old == tags[:3]
    (d / ".wggui-pending-wg0-x").write_text(str(d / f"wg0.conf.wggui-{tags[0]}") + "\n")  # rollback in progress
    out = _sh(PRUNE_BACKUPS, "wg0", str(d), *old).stdout.split("\n")
    assert f"kept {tags[0]}" in out and f"removed {tags[1]}" in out
    left = sorted(p.name for p in d.glob("wg0.conf.wggui-2*"))
    assert left == [f"wg0.conf.wggui-{t}" for t in [tags[0], *tags[3:]]]
    assert backups_to_prune(tags[:4], keep=5) == []


def test_prune_python_api_validates_tags():
    runner = FakeRunner().on(r"removed", "removed 20261110-120000\n")
    assert apply.prune_backups(runner, "wg0", ["20261110-120000", "../etc"]) == ["20261110-120000"]
    assert "../etc" not in " ".join(runner.calls[0].argv)
