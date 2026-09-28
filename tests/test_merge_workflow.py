import json
import smtplib
from email import message_from_bytes, policy

import pytest

from email_merge_tool import (
    DataError,
    FatalSendError,
    SMTPConfig,
    SMTPTransport,
    Template,
    mail_merge,
    read_log,
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
    assert (conn.host, conn.port, conn.tls) == ("smtp.gmail.com", 465, True)
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
    assert (conn.host, conn.port, conn.tls) == ("smtp-mail.outlook.com", 587, True)


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
