import json
import smtplib
import ssl
from email import message_from_bytes, policy

import pytest

from email_merge_tool import (
    AccountRefusedError,
    DataError,
    FatalSendError,
    SMTPConfig,
    SMTPTransport,
    Template,
    TransportError,
    mail_merge,
    read_log,
    sent_addresses,
)

TPL = Template(subject="Hello {first_name}", text="Dear {first_name},\nWelcome.\n")
SENDER = "Study Team <team@example.org>"


def gmail():
    return SMTPTransport(SMTPConfig.from_service("gmail", "team@example.org", "app-pass"))


def test_dry_run_is_default_and_sends_nothing(people, fake_smtp):
    report = mail_merge(TPL, people, sender=SENDER)
    assert report.count("dry_run") == 4 and report.sent == 0
    assert fake_smtp.instances == []
    msg = report.messages[0]
    assert msg["Subject"] == "Hello Ann"
    assert msg["To"] == "ann@example.org"
    assert msg["Message-ID"].endswith("@example.org>") and msg["Date"]


def test_dry_run_writes_eml(people, tmp_path):
    mail_merge(TPL, people, sender=SENDER, output_dir=tmp_path / "out")
    files = sorted((tmp_path / "out").glob("*.eml"))
    assert len(files) == 4
    parsed = message_from_bytes(files[0].read_bytes(), policy=policy.default)
    assert parsed.get_content().startswith("Dear Ann")


def test_validation_happens_before_any_send(people, fake_smtp):
    rows = [*people, {"email": "not-an-address", "first_name": "X", "dept": "d", "id": "9"}]
    with pytest.raises(DataError) as info:
        mail_merge(TPL, rows, sender=SENDER, dry_run=False, transport=gmail())
    assert "row 5" in str(info.value) and "nothing was sent" in str(info.value)
    assert fake_smtp.instances == []  # never even connected


def test_missing_sender(people):
    with pytest.raises(DataError, match="no sender"):
        mail_merge(TPL, people)


def test_real_send_over_ssl_with_login(people, fake_smtp, tmp_path):
    log = tmp_path / "log.jsonl"
    report = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log)
    assert report.sent == 4
    (conn,) = fake_smtp.instances
    assert (conn.host, conn.port, conn.implicit_tls) == ("smtp.gmail.com", 465, True)
    assert conn.context.verify_mode == ssl.CERT_REQUIRED and conn.context.check_hostname
    assert conn.logged_in == ("team@example.org", "app-pass")
    assert [m["To"] for m in conn.sent] == [p["email"] for p in people]
    assert conn.closed
    records = list(read_log(log))
    assert [r["status"] for r in records] == ["sent"] * 4
    assert all(r["message_id"] for r in records)


def test_outlook_uses_starttls_on_587(fake_smtp, people):
    t = SMTPTransport(SMTPConfig.from_service("outlook", "me@outlook.com", "pw"))
    mail_merge(TPL, people[:1], sender="me@outlook.com", dry_run=False, transport=t)
    conn = fake_smtp.instances[0]
    assert (conn.host, conn.port, conn.implicit_tls, conn.tls) == ("smtp-mail.outlook.com", 587, False, True)
    assert conn.context.verify_mode == ssl.CERT_REQUIRED and conn.context.check_hostname


def test_resume_never_double_sends(people, fake_smtp, tmp_path):
    log = tmp_path / "log.jsonl"
    first = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log, limit=2)
    assert first.sent == 2 and first.skipped == 2
    second = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log, resume=True)
    assert second.sent == 2
    sent = [m["To"] for c in fake_smtp.instances for m in c.sent]
    assert sorted(sent) == sorted(p["email"] for p in people)  # each exactly once


def test_resume_requires_log(people):
    with pytest.raises(ValueError, match="log_path"):
        mail_merge(TPL, people, sender=SENDER, resume=True)


def test_exclude_for_reminder_wave(people, tmp_path):
    done = tmp_path / "responded.txt"
    done.write_text("BO@example.org\n", encoding="utf-8")
    report = mail_merge(TPL, people, sender=SENDER, exclude=done)
    assert [r.to for r in report.results if r.status == "skipped"] == ["bo@example.org"]


def test_transient_error_is_retried_then_permanent_fails_one(people, fake_smtp, tmp_path):
    fake_smtp.failures = [
        smtplib.SMTPServerDisconnected("gone"),  # ann: retried after reconnecting...
        None,  # ...and the retry succeeds
        smtplib.SMTPDataError(550, b"mailbox unavailable"),  # permanent for this recipient
    ]
    report = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=tmp_path / "l")
    assert [r.status for r in report.results] == ["sent", "failed", "sent", "sent"]
    assert "550" in report.results[1].detail
    assert len(fake_smtp.instances) == 2  # reconnected once
    assert fake_smtp.sleeps == [2.0]  # one backoff, before the retry


def test_auth_failure_is_fatal_and_clear(people, fake_smtp):
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with pytest.raises(FatalSendError, match="app password"):
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail())


def test_send_requires_transport(people):
    with pytest.raises(ValueError, match="transport"):
        mail_merge(TPL, people, sender=SENDER, dry_run=False)


def test_experiment_assignment_and_log(people, fake_smtp, tmp_path):
    a = Template(subject="Survey", text="Please take our survey.", name="control")
    b = Template(subject="Survey for {first_name}", text="Dear {first_name}, please take our survey.", name="personal")
    log = tmp_path / "log.jsonl"
    out = tmp_path / "assign.csv"
    report = mail_merge(
        [a, b],
        people,
        sender=SENDER,
        seed=11,
        strata="dept",
        assignments_path=out,
        dry_run=False,
        transport=gmail(),
        log_path=log,
    )
    by_variant = {r.to: r.variant for r in report.results}
    assert sorted(by_variant.values()) == ["control", "control", "personal", "personal"]
    # stratified: one of each arm per department
    for dept in ("bio", "chem"):
        arms = {by_variant[p["email"]] for p in people if p["dept"] == dept}
        assert arms == {"control", "personal"}
    assert out.exists()
    assert {json.loads(line)["variant"] for line in log.read_text().splitlines()} == {"control", "personal"}
    for msg in fake_smtp.instances[0].sent:
        personal = by_variant[msg["To"]] == "personal"
        assert (msg["Subject"] != "Survey") is personal


def test_several_templates_need_seed(people):
    a = Template(subject="a", text="a", name="a")
    b = Template(subject="b", text="b", name="b")
    with pytest.raises(ValueError, match="seed"):
        mail_merge([a, b], people, sender=SENDER)


def test_variant_field(people):
    a = Template(subject="a", text="a", name="a")
    b = Template(subject="b", text="b", name="b")
    rows = [dict(p, arm="a" if i % 2 else "b") for i, p in enumerate(people)]
    report = mail_merge({"a": a, "b": b}, rows, sender=SENDER, variant_field="arm")
    assert [m["Subject"] for m in report.messages] == ["b", "a", "b", "a"]
    rows[0]["arm"] = "c"
    with pytest.raises(DataError, match="'c' is not one of"):
        mail_merge({"a": a, "b": b}, rows, sender=SENDER, variant_field="arm")


def test_list_unsubscribe_headers(people):
    report = mail_merge(
        TPL,
        people,
        sender=SENDER,
        list_unsubscribe="<https://example.org/u?id={id}>, <mailto:unsub@example.org>",
        one_click_unsubscribe=True,
    )
    msg = report.messages[2]
    assert msg["List-Unsubscribe"] == "<https://example.org/u?id=3>, <mailto:unsub@example.org>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_one_click_needs_https(people):
    with pytest.raises(DataError, match="https"):
        mail_merge(TPL, people, sender=SENDER, list_unsubscribe="<mailto:u@example.org>", one_click_unsubscribe=True)


def test_html_message_is_multipart_alternative(people):
    t = Template(subject="Hi", text="Hi {first_name}", html="<p>Hi {first_name}</p>")
    msg = mail_merge(t, people, sender=SENDER).messages[0]
    assert msg.get_content_type() == "multipart/alternative"
    assert msg.get_body(("html",)).get_content().strip() == "<p>Hi Ann</p>"
    assert msg.get_body(("plain",)).get_content().strip() == "Hi Ann"


def test_non_ascii_round_trip(people):
    rows = [{"email": "zoe@example.org", "first_name": "Zoë 李"}]
    msg = mail_merge(TPL, rows, sender="Équipe <team@example.org>").messages[0]
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert parsed["Subject"] == "Hello Zoë 李"
    assert "Équipe" in parsed["From"]


def test_config_never_leaks_password():
    assert "secret" not in repr(SMTPConfig("h", 587, username="u", password="secret"))
    with pytest.raises(ValueError, match="unencrypted"):
        SMTPConfig("h", 25, "none", "u", "secret")
    with pytest.raises(ValueError, match="unknown service"):
        SMTPConfig.from_service("yahoo")


def test_attachments_shared_and_per_recipient(people, tmp_path):
    shared = tmp_path / "flyer.pdf"
    shared.write_bytes(b"%PDF-1.4 flyer")
    for p in people:
        (tmp_path / f"cert_{p['id']}.txt").write_text(f"certificate {p['first_name']}", encoding="utf-8")
    rows = [dict(p, cert=str(tmp_path / "cert_{id}.txt")) for p in people]
    report = mail_merge(TPL, rows, sender=SENDER, attachments=[shared], attachment_field="cert")
    parts = list(report.messages[1].iter_attachments())
    assert [(a.get_filename(), a.get_content_type()) for a in parts] == [
        ("flyer.pdf", "application/pdf"),
        ("cert_2.txt", "text/plain"),
    ]
    assert parts[1].get_content() == "certificate Bo"


def test_missing_attachment_blocks_sending(people, tmp_path):
    with pytest.raises(DataError, match="attachment not found"):
        mail_merge(TPL, people, sender=SENDER, attachments=[tmp_path / "nope.pdf"])
    rows = [dict(p, cert="missing_{id}.pdf") for p in people]
    with pytest.raises(DataError, match="row 1: attachment not found: missing_1.pdf"):
        mail_merge(TPL, rows, sender=SENDER, attachment_field="cert")


def test_invalid_sender_and_header_addresses(people):
    with pytest.raises(DataError, match="invalid sender"):
        mail_merge(TPL, people, sender="team at example.org")
    cc = Template(subject="s", text="t", headers={"Cc": "{advisor}"})
    rows = [dict(p, advisor="Prof. Lee <lee@example.org>, dean@example.org") for p in people]
    cc_header = mail_merge(cc, rows, sender=SENDER).messages[0]["Cc"]
    assert [a.addr_spec for a in cc_header.addresses] == ["lee@example.org", "dean@example.org"]
    rows[2]["advisor"] = "lee-at-example.org"
    with pytest.raises(DataError, match="row 3: invalid address in Cc"):
        mail_merge(cc, rows, sender=SENDER)


def test_missing_exclude_file_is_a_validation_error(people, tmp_path):
    with pytest.raises(DataError, match="exclude list: address list not found"):
        mail_merge(TPL, people, sender=SENDER, exclude=tmp_path / "responded.txt")


def test_address_with_trailing_text_is_rejected_not_rerouted(people):
    rows = [*people, dict(people[0], email="ann@example.org extra")]
    with pytest.raises(DataError, match="row 5: invalid email address 'ann@example.org extra'"):
        mail_merge(TPL, rows, sender=SENDER)


def test_exclusion_matches_display_name_and_case_variants(people):
    rows = [dict(people[0], email="Dr. Ann Smith <ANN@example.org>"), *people[1:]]
    report = mail_merge(TPL, rows, sender=SENDER, exclude=["ann@example.org"])
    assert [r.status for r in report.results] == ["skipped", "dry_run", "dry_run", "dry_run"]


def test_exclusion_that_matches_nobody_warns(people):
    report = mail_merge(TPL, people, sender=SENDER, exclude=["zed@example.org"])
    assert any("none of its 1 address(es)" in i.message for i in report.issues)


def test_reusing_a_log_without_resume_is_refused(people, fake_smtp, tmp_path):
    log = tmp_path / "log.jsonl"
    mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log)
    with pytest.raises(DataError, match="already records 4 sent or uncertain address"):
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log)
    with pytest.raises(DataError, match="already records"):  # the dry-run preview fails the same way
        mail_merge(TPL, people, sender=SENDER, log_path=log)
    assert sum(len(c.sent) for c in fake_smtp.instances) == 4


def test_resume_with_a_missing_or_empty_log(people, tmp_path):
    with pytest.raises(DataError, match="does not exist, so resuming would skip nobody"):
        mail_merge(TPL, people, sender=SENDER, log_path=tmp_path / "typo.jsonl", resume=True)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    report = mail_merge(TPL, people, sender=SENDER, log_path=empty, resume=True)
    assert any("records no sent messages" in i.message for i in report.issues)


def test_disconnect_after_data_is_not_retried(people, fake_smtp, tmp_path):
    log = tmp_path / "log.jsonl"
    fake_smtp.failures = [("after_data", smtplib.SMTPServerDisconnected("reply lost"))]
    report = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log)
    assert [r.status for r in report.results] == ["uncertain", "sent", "sent", "sent"]
    delivered = [m["To"] for c in fake_smtp.instances for m in c.sent]
    assert delivered.count("ann@example.org") == 1
    assert [r["status"] for r in read_log(log)][0] == "uncertain"
    assert "uncertain: 1" in report.summary()

    again = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log, resume=True)
    assert again.skipped == 4
    assert [m["To"] for c in fake_smtp.instances for m in c.sent].count("ann@example.org") == 1


class _Server:
    """A minimal SMTP server on a real socket that misbehaves on request.

    ``script`` holds one action per DATA command, used in order: None replies
    normally; "rcpt" closes at RCPT; "data_cmd" closes after the DATA command;
    "payload" closes partway through the message; "data" queues the message
    and closes without a reply; "reply25" queues it and sends a cut-off "25";
    a number queues it and waits that many seconds before replying 250.
    """

    def __init__(self, script):
        import socket
        import threading

        self.script = list(script)
        self.messages = []
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, conn.makefile("rb") as reader:
                conn.sendall(b"220 test\r\n")
                self._session(conn, reader)

    def _session(self, conn, reader):
        import time

        while line := reader.readline():
            verb = line[:4].upper()
            if verb == b"RCPT" and self.script and self.script[0] == "rcpt":
                self.script.pop(0)
                return
            if verb == b"DATA":
                action = self.script.pop(0) if self.script else None
                if action == "data_cmd":
                    return
                conn.sendall(b"354 go\r\n")
                if action == "payload":
                    reader.readline()
                    return
                self.messages.append(b"".join(iter(reader.readline, b".\r\n")))
                if action == "data":
                    return
                if action == "reply25":
                    conn.sendall(b"25")
                    return
                if isinstance(action, (int, float)):
                    time.sleep(action)
                conn.sendall(b"250 queued\r\n")
            elif verb == b"QUIT":
                conn.sendall(b"221 bye\r\n")
                return
            else:
                conn.sendall(b"250 ok\r\n")

    def recipients(self):
        return [next(x for x in m.split(b"\r\n") if x.startswith(b"To:"))[4:].decode() for m in self.messages]


def test_real_smtplib_retries_before_data_but_not_after(people, tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    # Consumed in order: ann's first try is cut at RCPT and her retry succeeds,
    # bo's reply is lost after his data, cy succeeds.
    server = _Server(script=["rcpt", None, "data", None])
    transport = SMTPTransport(SMTPConfig("127.0.0.1", server.port, "none"))
    report = mail_merge(TPL, people[:3], sender=SENDER, dry_run=False, transport=transport, log_path=tmp_path / "l")
    server.sock.close()
    assert [r.status for r in report.results] == ["sent", "uncertain", "sent"]
    assert server.recipients() == ["ann@example.org", "bo@example.org", "cy@example.org"]


@pytest.mark.parametrize(
    ("action", "status", "copies"),
    [
        ("data_cmd", "sent", 1),  # dropped before 354: the server has nothing, so retry
        ("data", "uncertain", 1),  # queued, reply lost: never retry
        ("reply25", "uncertain", 1),  # queued, reply cut short: never retry
    ],
)
def test_real_smtplib_each_failure_point(people, tmp_path, monkeypatch, action, status, copies):
    monkeypatch.setattr("time.sleep", lambda s: None)
    server = _Server(script=[action])
    transport = SMTPTransport(SMTPConfig("127.0.0.1", server.port, "none"))
    report = mail_merge(TPL, people[:1], sender=SENDER, dry_run=False, transport=transport, log_path=tmp_path / "l")
    server.sock.close()
    assert [r.status for r in report.results] == [status]
    assert server.recipients().count("ann@example.org") == copies


def test_real_smtplib_waits_longer_for_the_reply_to_the_message(people, tmp_path):
    server = _Server(script=[1.5, None])  # the reply comes after the 0.5 s socket timeout
    transport = SMTPTransport(SMTPConfig("127.0.0.1", server.port, "none", timeout=0.5), data_timeout=5)
    report = mail_merge(TPL, people[:2], sender=SENDER, dry_run=False, transport=transport, log_path=tmp_path / "l")
    server.sock.close()
    assert [r.status for r in report.results] == ["sent", "sent"]


def test_timeout_and_methods_are_restored_after_a_send(people):
    server = _Server(script=[None])
    transport = SMTPTransport(SMTPConfig("127.0.0.1", server.port, "none", timeout=0.5), data_timeout=5)
    transport.open()
    message = mail_merge(TPL, people[:1], sender=SENDER).messages[0]
    transport.send(message)
    assert transport._smtp.sock.gettimeout() == 0.5
    assert "data" not in vars(transport._smtp) and "send" not in vars(transport._smtp)
    transport.close()
    server.sock.close()


def test_garbled_reply_after_data_is_uncertain_but_4xx_is_retried(people, fake_smtp, tmp_path):
    fake_smtp.failures = [
        ("after_data", smtplib.SMTPDataError(-1, b"garbled")),  # ann: unclear, never retried
        ("after_data", smtplib.SMTPDataError(451, b"try later")),  # bo: refused for now, retried
        None,
    ]
    report = mail_merge(TPL, people[:2], sender=SENDER, dry_run=False, transport=gmail(), log_path=tmp_path / "l")
    assert [r.status for r in report.results] == ["uncertain", "sent"]
    delivered = [m["To"] for c in fake_smtp.instances for m in c.sent]
    assert delivered.count("ann@example.org") == 1 and delivered.count("bo@example.org") == 1


def test_ctrl_c_while_awaiting_the_reply_is_logged_as_uncertain(people, fake_smtp, tmp_path):
    log = tmp_path / "log.jsonl"
    fake_smtp.failures = [None, ("after_data", KeyboardInterrupt())]
    with pytest.raises(KeyboardInterrupt):
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log)
    assert [(r["to"], r["status"]) for r in read_log(log)] == [
        ("ann@example.org", "sent"),
        ("bo@example.org", "uncertain"),
    ]
    again = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log, resume=True)
    assert [r.status for r in again.results] == ["skipped", "skipped", "sent", "sent"]


def test_header_that_cannot_be_written_fails_validation(people):
    cc = Template(subject="s", text="t", headers={"Cc": "{advisor}"})
    rows = [dict(p, advisor="Lee <lee@example.org>") for p in people]
    rows[1]["advisor"] = "\u00a0boss@example.org"  # a no-break space before the address
    with pytest.raises(DataError, match="row 2: invalid address in Cc"):
        mail_merge(cc, rows, sender=SENDER)


def test_drop_while_writing_the_message_is_retried(people, fake_smtp, tmp_path):
    fake_smtp.failures = [("payload", smtplib.SMTPServerDisconnected("reset")), None]
    report = mail_merge(TPL, people[:1], sender=SENDER, dry_run=False, transport=gmail(), log_path=tmp_path / "l")
    assert [r.status for r in report.results] == ["sent"]
    assert [m["To"] for c in fake_smtp.instances for m in c.sent] == ["ann@example.org"]


def test_resume_with_another_mailings_log_is_refused(people, fake_smtp, tmp_path):
    other = tmp_path / "other.jsonl"
    other.write_text(json.dumps({"to": "zed@example.org", "status": "sent"}) + "\n", encoding="utf-8")
    with pytest.raises(DataError, match="none of them is in this recipient data"):
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=other, resume=True)
    assert fake_smtp.instances == []


def test_resume_reads_logs_from_version_0_2_0(people, fake_smtp, tmp_path):
    # 0.2.0 accepted "ann@example.org extra" and mailed ann@example.org.
    log = tmp_path / "old.jsonl"
    log.write_text(json.dumps({"to": "ann@example.org extra", "status": "sent"}) + "\n", encoding="utf-8")
    report = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log, resume=True)
    assert [r.status for r in report.results] == ["skipped", "sent", "sent", "sent"]


def test_two_runs_cannot_share_a_log(people, fake_smtp, tmp_path):
    from email_merge_tool.sendlog import SendLog

    log = tmp_path / "log.jsonl"
    with SendLog(log) as other_run:
        other_run.lock()
        with pytest.raises(DataError, match="in use by another run"):
            mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log)
    assert fake_smtp.instances == []
    assert mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log).sent == 4


def test_torn_last_log_line_does_not_swallow_the_next_record(people, fake_smtp, tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text('{"to": "bo@example.org", "status": "sent"}\n{"to": "cy@exa', encoding="utf-8")  # crash mid-line
    mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=log, resume=True, limit=1)
    # ann is the first record written after the torn line; it must stay readable.
    assert sent_addresses(log) == {"ann@example.org", "bo@example.org"}


def test_dry_run_does_not_create_the_log(people, tmp_path):
    mail_merge(TPL, people, sender=SENDER, log_path=tmp_path / "log.jsonl")
    assert not (tmp_path / "log.jsonl").exists()


def test_limit_counts_uncertain_messages(people, fake_smtp, tmp_path):
    fake_smtp.failures = [("after_data", smtplib.SMTPServerDisconnected("reply lost"))]
    report = mail_merge(
        TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=tmp_path / "l.jsonl", limit=2
    )
    assert [r.status for r in report.results] == ["uncertain", "sent", "skipped", "skipped"]


def test_empty_exclusion_list_warns(people, tmp_path):
    empty = tmp_path / "optout.txt"
    empty.write_text("# nobody yet\n", encoding="utf-8")
    report = mail_merge(TPL, people, sender=SENDER, exclude=empty)
    assert any("holds no addresses" in str(i) for i in report.issues)


def test_a_transport_that_refuses_the_to_address_fails_the_row(people, tmp_path):
    class PartlyRefusing:
        def open(self):
            pass

        def send(self, message):
            if message["To"] == "bo@example.org":
                return {"Bo@Example.org": (554, b"5.7.1 policy"), "x@example.org": (550, b"no")}
            return {"lee@example.org": (550, b"5.1.1 no such user")}

        def close(self):
            pass

    log = tmp_path / "log.jsonl"
    report = mail_merge(TPL, people[:2], sender=SENDER, dry_run=False, transport=PartlyRefusing(), log_path=log)
    assert [(r.status, r.detail) for r in report.results] == [
        ("sent", "some recipients refused: lee@example.org (550 5.1.1 no such user)"),
        (
            "failed",
            "recipient refused: Bo@Example.org (554 5.7.1 policy); the other recipients were sent the message",
        ),
    ]
    assert sent_addresses(log) == {"ann@example.org"}


def test_max_consecutive_failures_must_not_be_negative(people):
    with pytest.raises(ValueError, match="max_consecutive_failures"):
        mail_merge(TPL, people, sender=SENDER, max_consecutive_failures=-1)


def test_failures_in_a_row_stop_with_the_fake(people, fake_smtp, tmp_path):
    fake_smtp.failures = [smtplib.SMTPDataError(554, b"5.7.1 rejected")] * 4
    with pytest.raises(FatalSendError, match="stopped after 2 failed messages in a row"):
        mail_merge(
            TPL,
            people,
            sender=SENDER,
            dry_run=False,
            transport=gmail(),
            log_path=tmp_path / "l",
            max_consecutive_failures=2,
        )
    assert [r["status"] for r in read_log(tmp_path / "l")] == ["failed", "failed"]


def test_starttls_on_port_465_times_out_with_a_hint(monkeypatch):
    def hang(host, port, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(smtplib, "SMTP", hang)
    transport = SMTPTransport(SMTPConfig("smtp.example.org", 465, "starttls"))
    with pytest.raises(FatalSendError, match=r"could not connect to smtp.example.org:465 \(timed out\); port 465"):
        transport.open()


def test_a_refused_login_on_reconnect_stops_at_once(people, fake_smtp, tmp_path):
    def password_changed(result):
        fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    fake_smtp.failures = [None, smtplib.SMTPServerDisconnected("gone")]
    with pytest.raises(FatalSendError, match="login to smtp.gmail.com failed") as info:
        mail_merge(
            TPL,
            people,
            sender=SENDER,
            dry_run=False,
            transport=gmail(),
            log_path=tmp_path / "l",
            progress=password_changed,
        )
    assert fake_smtp.sleeps == [2.0]  # the dropped connection is retried once; the refused login is not
    assert [r.status for r in info.value.report.results] == ["sent", "failed"]


class Scripted:
    """A custom transport whose send() runs the next step of a script: None delivers, an exception is raised."""

    def __init__(self, *steps, open_error=None, close_error=None):
        self.steps = list(steps)
        self.open_error, self.close_error = open_error, close_error
        self.sent = []
        self.closed = False

    def open(self):
        if self.open_error:
            raise self.open_error

    def send(self, message):
        step = self.steps.pop(0) if self.steps else None
        if step is not None:
            raise step
        self.sent.append(message["To"])

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def test_an_unexpected_transport_error_stops_with_the_report(people, tmp_path):
    log = tmp_path / "log.jsonl"
    transport = Scripted(None, RuntimeError("HTTP 500 from the provider"))
    with pytest.raises(TransportError, match="RuntimeError: HTTP 500 from the provider") as info:
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport, log_path=log)
    assert isinstance(info.value, FatalSendError) and isinstance(info.value.__cause__, RuntimeError)
    assert [(r.to, r.status) for r in info.value.report.results] == [
        ("ann@example.org", "sent"),
        ("bo@example.org", "uncertain"),  # the provider may have taken it before failing
    ]
    assert [r["status"] for r in read_log(log)] == ["sent", "uncertain"]
    assert transport.closed

    again = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=Scripted(), log_path=log, resume=True)
    assert [r.status for r in again.results] == ["skipped", "skipped", "sent", "sent"]


def test_a_transport_that_cannot_open_stops_before_sending(people, tmp_path):
    transport = Scripted(open_error=ConnectionError("no route to the API"))
    with pytest.raises(TransportError, match="could not open: ConnectionError: no route to the API") as info:
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport, log_path=tmp_path / "l")
    assert info.value.report.results == [] and transport.sent == []


def test_a_failing_close_after_the_run_keeps_the_report(people):
    transport = Scripted(close_error=OSError("flush failed"))
    with pytest.raises(TransportError, match="closing the transport failed after the run") as info:
        mail_merge(TPL, people[:2], sender=SENDER, dry_run=False, transport=transport)
    assert info.value.report.sent == 2


def test_a_failing_close_does_not_hide_the_error_that_stopped_the_run(people, tmp_path):
    refusal = AccountRefusedError("quota", smtp_code=550, smtp_error="5.4.5 Daily limit")
    transport = Scripted(None, refusal, close_error=OSError("flush failed"))
    with pytest.raises(AccountRefusedError) as info:
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport, log_path=tmp_path / "l")
    assert info.value.report.summary() == "sent: 1, failed: 1"


def test_sending_without_a_log_warns(people, fake_smtp, tmp_path):
    def warned(report):
        return [str(i) for i in report.issues if "sending without a log" in i.message]

    assert warned(mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail())) == [
        "warning: send log: sending without a log: a run that stops part-way cannot be resumed, and only the "
        "report (exc.report on an error) says who was sent the message; pass log_path="
    ]
    assert not warned(mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=gmail(), log_path=tmp_path / "l"))
    assert not warned(mail_merge(TPL, people, sender=SENDER))


def test_a_transport_reports_bounces_with_the_reply(people, tmp_path):
    from email_merge_tool import PermanentSendError

    class Provider:
        def open(self):
            pass

        def send(self, message):
            if message["To"] == "ann@example.org":
                raise PermanentSendError("hard bounce", smtp_code=550, smtp_error="5.1.1 mailbox does not exist")
            if message["To"] == "bo@example.org":
                return {"bo@example.org": (550, "5.2.1 mailbox disabled")}  # text may be str

        def close(self):
            pass

    log = tmp_path / "log.jsonl"
    report = mail_merge(TPL, people[:3], sender=SENDER, dry_run=False, transport=Provider(), log_path=log)
    assert [(r.status, r.smtp_code, r.enhanced_status) for r in report.results] == [
        ("bounced", 550, "5.1.1"),
        ("bounced", 550, "5.2.1"),
        ("sent", None, None),
    ]
    assert report.bounced == 2 and report.failed == 0
    assert sent_addresses(log) == {"cy@example.org"}
    from email_merge_tool import bounced_addresses

    assert bounced_addresses(log) == {"ann@example.org", "bo@example.org"}


def test_retry_bounced_needs_resume(people, tmp_path):
    with pytest.raises(ValueError, match="retry_bounced=True needs resume=True"):
        mail_merge(TPL, people, sender=SENDER, log_path=tmp_path / "l", retry_bounced=True)


def test_a_dry_run_resume_shows_bounced_addresses_as_skipped(people, tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text(
        json.dumps({"to": "ann@example.org", "status": "sent"})
        + "\n"
        + json.dumps({"to": "Bo@Example.org", "status": "bounced", "smtp_code": 550, "enhanced_status": "5.1.1"})
        + "\n",
        encoding="utf-8",
    )
    report = mail_merge(TPL, people, sender=SENDER, log_path=log, resume=True)
    assert [r.status for r in report.results] == ["skipped", "skipped", "dry_run", "dry_run"]
    again = mail_merge(TPL, people, sender=SENDER, log_path=log, resume=True, retry_bounced=True)
    assert [r.status for r in again.results] == ["skipped", "dry_run", "dry_run", "dry_run"]


def test_only_refusal_entries_count_as_refused(people):
    class Provider:
        def open(self):
            pass

        def send(self, message):
            if message["To"] == "ann@example.org":
                return {"id": "prov-123", "status": "queued"}  # a provider's receipt
            if message["To"] == "bo@example.org":
                return "prov-456"
            return {"MessageId": "x", "lee@example.org": (550, b"5.1.1 no such user")}

        def close(self):
            pass

    report = mail_merge(TPL, people[:3], sender=SENDER, dry_run=False, transport=Provider())
    assert [(r.status, r.detail) for r in report.results] == [
        ("sent", ""),
        ("sent", ""),
        ("sent", "some recipients refused: lee@example.org (550 5.1.1 no such user)"),
    ]


def test_a_dry_run_transport_makes_a_dry_run(people, tmp_path):
    from email_merge_tool import DryRunTransport

    log = tmp_path / "log.jsonl"
    outbox = DryRunTransport(tmp_path / "outbox")
    report = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=outbox, log_path=log)
    assert report.count("dry_run") == 4 and report.sent == 0
    assert not log.exists()  # nothing was sent, so nothing is logged
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 4 and len(outbox.messages) == 4

    preview = DryRunTransport()
    report = mail_merge(TPL, people, sender=SENDER, transport=preview)  # used in the default dry run too
    assert len(preview.messages) == 4 and not any(i.where == "transport" for i in report.issues)
    with pytest.raises(ValueError, match="output_dir= or a DryRunTransport, not both"):
        mail_merge(TPL, people, sender=SENDER, transport=DryRunTransport(), output_dir=tmp_path / "x")


def test_the_transport_contract_is_exported():
    from email_merge_tool import DryRunTransport, EmailMergeError, PermanentSendError, TemplateError, Transport

    assert isinstance(gmail(), Transport) and isinstance(DryRunTransport(), Transport)
    assert not isinstance(object(), Transport)
    for error in (DataError("x"), TemplateError("x"), PermanentSendError("x"), FatalSendError("x")):
        assert isinstance(error, EmailMergeError)
    assert isinstance(DataError("x"), ValueError) and isinstance(TemplateError("x"), ValueError)
    assert issubclass(TransportError, FatalSendError) and issubclass(AccountRefusedError, EmailMergeError)
