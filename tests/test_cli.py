import pytest

from email_merge_tool.cli import main


@pytest.fixture
def files(tmp_path):
    (tmp_path / "invite.txt").write_text("Subject: Hi {first_name}\n\nDear {first_name}\n", encoding="utf-8")
    (tmp_path / "people.csv").write_text("email,first_name\nann@example.org,Ann\nbo@example.org,Bo\n", encoding="utf-8")
    return tmp_path


def test_dry_run_prints_preview(files, capsys, fake_smtp):
    code = main(["-t", str(files / "invite.txt"), "-d", str(files / "people.csv"), "--from", "t@example.org"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Subject: Hi Ann" in out and "Dear Ann" in out and "Nothing was sent" in out
    assert fake_smtp.instances == []


def test_send_with_env_password(files, capsys, fake_smtp, monkeypatch):
    monkeypatch.setenv("EMAIL_MERGE_PASSWORD", "app-pass")
    code = main(
        [
            "-t",
            str(files / "invite.txt"),
            "-d",
            str(files / "people.csv"),
            "--from",
            "t@gmail.com",
            "--service",
            "gmail",
            "--send",
            "--yes",
            "--log",
            str(files / "log.jsonl"),
        ]
    )
    assert code == 0
    assert fake_smtp.instances[0].logged_in == ("t@gmail.com", "app-pass")
    assert len(fake_smtp.instances[0].sent) == 2
    assert "sent: 2" in capsys.readouterr().out


def test_send_confirmation_can_abort(files, fake_smtp, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    code = main(
        [
            "-t",
            str(files / "invite.txt"),
            "-d",
            str(files / "people.csv"),
            "--from",
            "t@example.org",
            "--host",
            "localhost",
            "--send",
            "--log",
            str(files / "log.jsonl"),
        ]
    )
    assert code == 1 and fake_smtp.instances == []


def test_validation_error_exit_code(files, capsys):
    (files / "people.csv").write_text("mail,first_name\nann@example.org,Ann\n", encoding="utf-8")
    code = main(["-t", str(files / "invite.txt"), "-d", str(files / "people.csv"), "--from", "t@example.org"])
    assert code == 2
    assert "address column 'email' not found" in capsys.readouterr().err


def test_experiment_cli(files, capsys):
    (files / "b.txt").write_text("Subject: Hello\n\nHello\n", encoding="utf-8")
    code = main(
        [
            "-t",
            str(files / "invite.txt"),
            "-t",
            str(files / "b.txt"),
            "-d",
            str(files / "people.csv"),
            "--from",
            "t@example.org",
            "--seed",
            "1",
            "--assignments",
            str(files / "a.csv"),
        ]
    )
    assert code == 0
    assert "Assignment: b=1, invite=1" in capsys.readouterr().out
    assert (files / "a.csv").exists()


def test_no_auth_relay_does_not_prompt(files, fake_smtp, monkeypatch):
    monkeypatch.delenv("EMAIL_MERGE_PASSWORD", raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("prompted for a password"))
    args = ["-t", str(files / "invite.txt"), "-d", str(files / "people.csv"), "--from", "t@example.org"]
    code = main([*args, "--host", "relay.example.org", "--port", "25", "--no-auth", "--send", "--yes", "--no-log"])
    assert code == 0
    conn = fake_smtp.instances[0]
    assert (conn.host, conn.port, conn.logged_in) == ("relay.example.org", 25, None)


def test_send_without_server_fails_before_anything(files, fake_smtp, capsys):
    with pytest.raises(SystemExit) as info:
        main(["-t", str(files / "invite.txt"), "-d", str(files / "people.csv"), "--from", "t@x.org", "--send"])
    assert info.value.code == 2
    assert "--send needs --service or --host" in capsys.readouterr().err
    assert fake_smtp.instances == []


def _send_args(files, *extra):
    return [
        "-t",
        str(files / "invite.txt"),
        "-d",
        str(files / "people.csv"),
        "--from",
        "t@example.org",
        "--host",
        "relay.example.org",
        "--no-auth",
        "--send",
        "--yes",
        *extra,
    ]


def test_send_requires_a_log(files, fake_smtp, capsys):
    with pytest.raises(SystemExit) as info:
        main(_send_args(files))
    assert info.value.code == 2
    assert "--send needs --log FILE" in capsys.readouterr().err
    assert fake_smtp.instances == []
    with pytest.raises(SystemExit):
        main(_send_args(files, "--log", str(files / "l.jsonl"), "--no-log"))


def test_repeating_a_send_without_resume_sends_nothing(files, fake_smtp, capsys):
    log = str(files / "sent.jsonl")
    assert main(_send_args(files, "--log", log)) == 0
    assert len(fake_smtp.instances[0].sent) == 2
    capsys.readouterr()

    assert main(_send_args(files, "--log", log)) == 2  # the same command again
    assert "already records 2 sent address(es)" in capsys.readouterr().err
    assert len(fake_smtp.instances) == 1  # never reconnected

    assert main(_send_args(files, "--log", log, "--resume")) == 0
    assert sum(len(c.sent) for c in fake_smtp.instances) == 2  # nobody got a second copy
