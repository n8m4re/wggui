import pytest

from wggui.remote import CommandError, FakeRunner, SshRunner


def test_base_command(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    cmd = SshRunner("root@vpn.example.org", 2222).base_command()
    assert cmd[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert f"ControlPath={tmp_path}/wggui-%C" in cmd
    assert cmd[-2:] == ["--", "root@vpn.example.org"]
    assert cmd[cmd.index("-p") + 1] == "2222"


def test_no_control_master_without_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert not any("ControlMaster" in c for c in SshRunner("vpn").base_command())


@pytest.mark.parametrize("target", ["-oProxyCommand=evil", "root@host;id", "a b", "", "host$(id)"])
def test_rejects_bad_target(target):
    with pytest.raises(ValueError):
        SshRunner(target)


def test_rejects_bad_port():
    with pytest.raises(ValueError):
        SshRunner("vpn", 70000)


def test_run_quotes_argv(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class P:
            returncode, stdout, stderr = 0, "ok", ""
        return P()

    monkeypatch.setattr("wggui.remote.subprocess.run", fake_run)
    res = SshRunner("vpn").run(["sh", "-c", 'echo "$1"', "sh", "a b; rm -rf /"])
    assert res.ok and res.stdout == "ok"
    # one quoted command string; values are not interpreted by the remote shell
    assert seen["cmd"][-1] == "sh -c 'echo \"$1\"' sh 'a b; rm -rf /'"


def test_fake_runner():
    r = FakeRunner().on(r"^echo", "hi\n")
    assert r.run(["echo", "x"]).stdout == "hi\n"
    with pytest.raises(CommandError):
        r.run(["unknown"])
    assert r.run(["unknown"], check=False).returncode == 127
    assert [c.argv for c in r.calls][0] == ["echo", "x"]


def test_sudo_prefix():
    assert SshRunner("vpn", sudo=True).remote_argv(["wg", "show"]) == ["sudo", "-n", "--", "wg", "show"]
    assert SshRunner("vpn").remote_argv(["wg", "show"]) == ["wg", "show"]


def test_error_hint_in_message():
    r = FakeRunner().on("true", returncode=255, stderr="root@vpn: Permission denied (publickey).")
    with pytest.raises(CommandError) as exc:
        r.run(["true"])
    assert "→ Anmeldung abgelehnt" in str(exc.value)


def test_explain_unknown_is_none():
    from wggui.remote import explain

    assert explain("irgendwas") is None
    assert "NOPASSWD" in explain("sudo: a password is required")


def test_commands_are_logged(caplog):
    caplog.set_level("INFO", logger="wggui")
    r = FakeRunner(label="srv-x").on("^true", stdout="geheim-stdout").on("^false", returncode=1, stderr="kaputt")
    r.run(["true"])
    r.run(["false"], check=False)
    text = caplog.text
    assert "srv-x: true → 0" in text and "srv-x: false → 1" in text and "kaputt" in text
    assert "geheim-stdout" not in text  # never log stdout
