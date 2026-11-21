import smtplib

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
    assert "already records 2 sent or uncertain address(es)" in capsys.readouterr().err
    assert len(fake_smtp.instances) == 1  # never reconnected

    assert main(_send_args(files, "--log", log, "--resume")) == 0
    assert sum(len(c.sent) for c in fake_smtp.instances) == 2  # nobody got a second copy


def test_resume_needs_a_log_and_the_preview_shows_it(files, fake_smtp, capsys):
    with pytest.raises(SystemExit):
        main(_send_args(files, "--no-log", "--resume"))
    assert "--resume needs --log FILE" in capsys.readouterr().err
    log = files / "sent.jsonl"
    assert main(_send_args(files, "--log", str(log))) == 0
    assert f"Send log: {log.resolve()} (new file)" in capsys.readouterr().out
    assert main(_send_args(files, "--log", str(log), "--resume")) == 0
    assert "(2 address(es) recorded as sent or uncertain)" in capsys.readouterr().out


def test_failures_in_a_row_stop_the_run_with_resume_advice(files, fake_smtp, capsys):
    fake_smtp.failures = [smtplib.SMTPDataError(554, b"5.7.1 rejected")] * 2
    log = str(files / "sent.jsonl")
    assert main(_send_args(files, "--log", log, "--max-failures", "2")) == 2
    err = capsys.readouterr().err
    assert "stopped after 2 failed messages in a row" in err
    assert "Re-run with --resume (and the same --log) to continue." in err

    fake_smtp.failures = [smtplib.SMTPDataError(554, b"5.7.1 rejected")] * 2
    assert main(_send_args(files, "--log", log, "--resume", "--max-failures", "0")) == 1  # failed, not stopped
    with pytest.raises(SystemExit):
        main(_send_args(files, "--log", log, "--max-failures", "-1"))


def test_security_none_defaults_to_port_25(files, fake_smtp):
    args = ["-t", str(files / "invite.txt"), "-d", str(files / "people.csv"), "--from", "t@example.org"]
    assert (
        main([*args, "--host", "relay.example.org", "--security", "none", "--no-auth", "--send", "--yes", "--no-log"])
        == 0
    )
    assert [(c.port, c.tls) for c in fake_smtp.instances] == [(25, False)]


def test_starttls_not_offered_explains_and_closes_the_connection(files, fake_smtp, capsys):
    fake_smtp.features.discard("starttls")
    assert main(_send_args(files, "--no-log")) == 2
    err = capsys.readouterr().err
    assert "relay.example.org:587 does not offer STARTTLS" in err and "--security none" in err
    assert fake_smtp.instances[0].closed


def test_a_stop_mid_run_prints_what_was_done(files, fake_smtp, capsys):
    (files / "people.csv").write_text(
        "email,first_name\nann@example.org,Ann\nbo@example.org,Bo\ncy@example.org,Cy\n", encoding="utf-8"
    )
    fake_smtp.failures = [None, smtplib.SMTPSenderRefused(550, b"5.4.5 Daily user sending limit exceeded", "t")]
    assert main(_send_args(files, "--log", str(files / "sent.jsonl"))) == 2
    err = capsys.readouterr().err
    assert "Stopped after 2 of 3 recipient(s): sent: 1, failed: 1." in err
    assert err.index("Stopped after") < err.index("Re-run with --resume")


def test_bounces_exit_1_and_resume_skips_them_unless_asked(files, fake_smtp, capsys):
    log = str(files / "sent.jsonl")
    fake_smtp.failures = [smtplib.SMTPRecipientsRefused({"ann@example.org": (550, b"5.1.1 No such user")})]
    assert main(_send_args(files, "--log", log)) == 1
    captured = capsys.readouterr()
    assert "bounced  ann@example.org (recipient refused: ann@example.org (550 5.1.1 No such user))" in captured.out
    assert "Done: sent: 1, bounced: 1." in captured.out
    assert "1 address(es) bounced" in captured.err

    assert main(_send_args(files, "--log", log, "--resume")) == 0
    assert "(1 address(es) recorded as sent or uncertain, 1 as bounced)" in capsys.readouterr().out
    assert sum(len(c.sent) for c in fake_smtp.instances) == 1  # nobody was mailed again

    assert main(_send_args(files, "--log", log, "--resume", "--retry-bounced")) == 0
    assert [m["To"] for c in fake_smtp.instances for m in c.sent] == ["bo@example.org", "ann@example.org"]
    with pytest.raises(SystemExit):
        main(_send_args(files, "--log", log, "--retry-bounced"))
    assert "--retry-bounced needs --resume" in capsys.readouterr().err


def test_max_size_limits_messages(files, fake_smtp, capsys):
    (files / "big.bin").write_bytes(bytes(400_000))
    args = ["-t", str(files / "invite.txt"), "-d", str(files / "people.csv"), "--from", "t@example.org"]
    assert main([*args, "--attach", str(files / "big.bin"), "--max-size", "0.3"]) == 2
    err = capsys.readouterr().err
    assert "row 1: the message is 0.5 MB as sent, over the 0.3 MB limit" in err and "big.bin (0.4 MB)" in err
    assert main([*args, "--attach", str(files / "big.bin"), "--max-size", "0"]) == 0
    with pytest.raises(SystemExit):
        main([*args, "--max-size", "-1"])
