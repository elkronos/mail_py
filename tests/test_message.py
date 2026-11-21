"""The bytes a message goes out as: headers, MIME structure and encodings."""

import copy
import pickle
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import pytest

from email_merge_tool import (
    DataError,
    DryRunTransport,
    FatalSendError,
    SMTPConfig,
    SMTPTransport,
    Template,
    build_message,
    mail_merge,
)
from email_merge_tool.cli import main
from email_merge_tool.message import attachment_name, message_id, needs_smtputf8, wire_bytes
from email_merge_tool.templates import RenderedMessage

SENDER = "Study Team <team@example.org>"
TPL = Template(subject="Hello {first_name}", text="Dear {first_name},\nWelcome.\n")


class _Capture(smtplib.SMTP):
    """smtplib's own send_message, stopped at the point where it hands the bytes to the server."""

    def __init__(self):
        super().__init__()  # no host: nothing connects
        self.wire = b""

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, opt):
        return True

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        self.wire = msg
        return {}


def smtp_bytes(message):
    capture = _Capture()
    capture.send_message(message)
    return capture.wire


def header_lines(wire, name):
    """The physical lines of one header, folding included."""
    lines = wire.split(b"\r\n")
    start = next(i for i, line in enumerate(lines) if line.lower().startswith(name.lower().encode() + b":"))
    end = start + 1
    while lines[end][:1] in (b" ", b"\t"):
        end += 1
    return lines[start:end]


def rendered(**headers):
    return RenderedMessage(to="ann@example.org", subject="Hi", text="Hello\n", headers=headers)


def test_long_list_unsubscribe_url_is_written_verbatim():
    url = "<https://example.org/unsubscribe?id=7&token=" + "0123456789abcdef" * 9 + ">"
    assert 150 <= len(url) <= 200
    message = build_message(rendered(), sender=SENDER, list_unsubscribe=url, one_click_unsubscribe=True)
    wire = smtp_bytes(message)
    assert b"=?" not in wire
    assert header_lines(wire, "List-Unsubscribe") == [b"List-Unsubscribe: " + url.encode()]
    assert header_lines(wire, "List-Unsubscribe-Post") == [b"List-Unsubscribe-Post: List-Unsubscribe=One-Click"]
    parsed = message_from_bytes(wire, policy=policy.default)
    assert parsed["List-Unsubscribe"] == url


def test_list_unsubscribe_folds_only_between_uris(people):
    https = "<https://example.org/u?id={id}&t=" + "f" * 60 + ">"
    report = mail_merge(TPL, people, sender=SENDER, list_unsubscribe=f"<mailto:unsub@example.org>,{https}")
    wire = smtp_bytes(report.messages[0])
    assert header_lines(wire, "List-Unsubscribe") == [
        b"List-Unsubscribe: <mailto:unsub@example.org>,",
        b" " + https.replace("{id}", "1").encode(),
    ]
    parsed = message_from_bytes(wire, policy=policy.default)
    assert parsed["List-Unsubscribe"] == "<mailto:unsub@example.org>, " + https.replace("{id}", "1")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("<https://example.org/désinscription>", "must be ASCII"),
        ("https://example.org/u?id=1", "one or more <URI>"),
        ("<https://example.org/u?id=a b>", "one or more <URI>"),
        ("<https://example.org/u?t=" + "a" * 1000 + ">", "980 characters a header line allows"),
    ],
)
def test_bad_list_unsubscribe_values_are_refused(value, error):
    with pytest.raises(ValueError, match=error):
        build_message(rendered(), sender=SENDER, list_unsubscribe=value)


def test_non_ascii_unsubscribe_value_fails_its_row(people):
    rows = [dict(p, token="tök" if p["id"] == "3" else "ok") for p in people]
    with pytest.raises(DataError, match="row 3: List-Unsubscribe must be ASCII"):
        mail_merge(TPL, rows, sender=SENDER, list_unsubscribe="<https://example.org/u?t={token}>")


def test_messages_with_verbatim_headers_copy_and_pickle():
    message = build_message(rendered(), sender=SENDER, list_unsubscribe="<https://example.org/u?id=1>")
    for clone in (copy.deepcopy(message), pickle.loads(pickle.dumps(message))):
        assert clone["List-Unsubscribe"] == "<https://example.org/u?id=1>"
        assert smtp_bytes(clone) == smtp_bytes(message)


def test_preflight_serializes_every_message(people, fake_smtp, tmp_path):
    # This display name folds into a bare line break on Python 3.10 to 3.12,
    # which ends the header block early; 3.13 folds it correctly.
    name = "x" * 80 + " aaa" + "x" * 240
    header = policy.default.header_factory("Reply-To", f"{name} <r@example.org>")
    folded = header.fold(policy=policy.default.clone(linesep="\r\n")).removesuffix("\r\n")
    broken = re.search(r"\r\n[^ \t]", folded) is not None
    tpl = Template(subject="s", text="t", headers={"Reply-To": "{reply}"})
    rows = [dict(p, reply="r@example.org") for p in people]
    rows[1]["reply"] = f"{name} <r@example.org>"
    transport = SMTPTransport(SMTPConfig("smtp.example.org", 587, username="u", password="p"))
    if broken:
        with pytest.raises(DataError, match="row 2: the message cannot be written: Reply-To"):
            mail_merge(tpl, rows, sender=SENDER, dry_run=False, transport=transport, log_path=tmp_path / "log")
        assert fake_smtp.instances == []
    else:
        report = mail_merge(tpl, rows, sender=SENDER, dry_run=False, transport=transport, log_path=tmp_path / "log")
        assert report.sent == 4
        parsed = message_from_bytes(smtp_bytes(fake_smtp.instances[0].sent[1]), policy=policy.default)
        assert parsed["Reply-To"] == f"{name} <r@example.org>"


def test_wire_bytes_matches_smtplib_and_keeps_from_lines():
    message = build_message(
        RenderedMessage(to="ann@example.org", subject="Hi", text="From the team:\nhello\n", headers={"Bcc": "b@x.org"}),
        sender=SENDER,
    )
    wire = wire_bytes(message)
    assert b"\r\nFrom the team:\r\n" in wire and b">From" not in wire
    assert b"Bcc" not in wire and b"Bcc: b@x.org\r\n" in wire_bytes(message, keep_bcc=True)
    assert wire.replace(b"\r\nFrom the team", b"\r\n>From the team") == smtp_bytes(message)


def test_internationalized_domain_is_written_as_utf8(tmp_path):
    message = EmailMessage()
    message["From"] = "Ann <ann@münchen.de>"
    message["To"] = "zoe@example.org"
    message["Subject"] = "Grüße"
    message.set_content("x\n")
    assert needs_smtputf8(message)
    transport = DryRunTransport(tmp_path)
    transport.open()
    transport.send(message)
    (eml,) = tmp_path.glob("*.eml")
    data = eml.read_bytes()
    assert "From: Ann <ann@münchen.de>\r\n".encode() in data and "Subject: Grüße\r\n".encode() in data
    assert data == wire_bytes(message, keep_bcc=True)


def body_parts(message):
    return [part for part in message.walk() if part.get_content_maintype() == "text"]


@pytest.mark.parametrize(
    ("text", "encoding"),
    [
        ("Dear Ann,\nshort lines only.\n", "7bit"),
        ("Dear Ann,\n" + "=" * 30 + "\nJUNE NEWSLETTER\n" + "=" * 79 + "\n" + "word " * 50 + "\n", "quoted-printable"),
        ("NUL \0 is not 7bit data\n", "quoted-printable"),
        ("Liebe Frau Müller,\nvielen Dank für Ihre Teilnahme. Wir melden uns nächste Woche.\n", "quoted-printable"),
        ("你好，欢迎参加我们的研究。请在周五之前回复。\n" * 3, "base64"),
    ],
)
def test_transfer_encoding_is_chosen_explicitly(text, encoding):
    html = f"<p>{text}</p>"
    wire = wire_bytes(build_message(RenderedMessage("ann@example.org", "Hi", text, html=html), sender=SENDER))
    assert wire.isascii()  # never 8bit, so no 8BITMIME is needed
    parts = body_parts(message_from_bytes(wire, policy=policy.default))
    assert [p["Content-Transfer-Encoding"] for p in parts] == [encoding, encoding]
    assert [p.get_content().replace("\r\n", "\n") for p in parts] == [text, html + "\n"]
    body = wire.split(b"\r\n\r\n", 1)[1]
    assert max(len(line) for line in body.split(b"\r\n")) <= 76  # RFC 2045 section 6.7


def test_non_ascii_subject_survives_folding_on_every_version():
    # Python 3.10 to 3.12 fold this subject as "...for invitationlab..." when left to the standard library.
    subject = "the lab survey 🎉 the a 🎉 for invitation lab 你好 open lab open Zoë 你好"
    wire = wire_bytes(build_message(RenderedMessage("ann@example.org", subject, "x"), sender=SENDER))
    assert header_lines(wire, "Subject") == [
        b"Subject: =?utf-8?b?dGhlIGxhYiBzdXJ2ZXkg8J+OiSB0aGUgYSDwn46JIGZvciBpbnZp?=",
        b" =?utf-8?b?dGF0aW9uIGxhYiDkvaDlpb0gb3BlbiBsYWIgb3BlbiBab8OrIOS9oOWlvQ==?=",
    ]
    assert message_from_bytes(wire, policy=policy.default)["Subject"] == subject


def test_long_subjects_round_trip_within_the_line_limit():
    for subject in [
        "Grüße " * 40,
        "😀" * 100,
        "open the a 😀😀 Zoë invitation for open Zoë lab meeting " * 3,
        "ascii " * 30,
    ]:
        message = build_message(RenderedMessage("ann@example.org", subject, "x"), sender=SENDER)
        assert message["Subject"] == subject
        wire = wire_bytes(message)
        assert all(len(line) <= 76 for line in header_lines(wire, "Subject"))
        assert message_from_bytes(wire, policy=policy.default)["Subject"] == subject


def attach(tmp_path, name, data=b"%PDF-1.4"):
    path = tmp_path / name
    path.write_bytes(data)
    message = build_message(rendered(), sender=SENDER, attachments=[path])
    wire = wire_bytes(message)
    (part,) = message_from_bytes(wire, policy=policy.default).iter_attachments()
    return wire, part


def test_eml_and_text_attachments(tmp_path):
    forwarded = b"From: a@example.org\r\nSubject: old\r\n\r\nbody\r\n"
    _, part = attach(tmp_path, "forward.eml", forwarded)
    assert part.get_content_type() == "application/octet-stream"  # message/rfc822 may not be base64
    assert part.get_content() == forwarded
    wire, part = attach(tmp_path, "list.csv", "name\nZoë\n".encode())
    assert b'Content-Type: text/csv; charset="utf-8"; name="list.csv"' in wire
    assert part.get_content() == "name\nZoë\n"
    wire, part = attach(tmp_path, "legacy.txt", "Zoë\n".encode("latin-1"))
    assert b"charset" not in wire.split(b"legacy.txt")[0].rsplit(b"Content-Type", 1)[1]
    assert part.get_payload(decode=True) == "Zoë\n".encode("latin-1")


@pytest.mark.parametrize(
    ("suffix", "ctype"),
    [(".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), (".ics", "text/calendar")],
)
def test_attachment_types_do_not_depend_on_the_machine(tmp_path, suffix, ctype):
    assert attach(tmp_path, "file" + suffix)[1].get_content_type() == ctype


def test_long_ascii_attachment_name_is_one_plain_parameter(tmp_path):
    name = "Quarterly_report_for_the_Department_of_Environmental_and_Occupational_Health_Sciences_2026_final.pdf"
    wire, part = attach(tmp_path, name)
    assert header_lines(wire, "Content-Disposition") == [
        b"Content-Disposition: attachment;",
        f' filename="{name}"'.encode(),
    ]
    assert f'Content-Type: application/pdf;\r\n name="{name}"\r\n'.encode() in wire
    assert b"filename*" not in wire
    assert part.get_filename() == name


def test_non_ascii_attachment_name_has_both_forms(tmp_path):
    name = "Résumé – 2026.pdf"
    wire, part = attach(tmp_path, name)
    assert b"filename*=utf-8''R%C3%A9sum%C3%A9%20%E2%80%93%202026.pdf" in wire
    assert b'name="=?utf-8?b?UsOpc3Vtw6kg4oCTIDIwMjYucGRm?="' in wire
    assert part.get_filename() == name


def test_attachment_name_cannot_inject_a_header(tmp_path):
    name = "=?utf-8?q?=0D=0ABcc:_e@evil.org?=.pdf"  # the stdlib decodes this into a line break
    wire, part = attach(tmp_path, name)
    assert b"\r\nBcc" not in wire
    assert part.get_filename() == name
    assert attachment_name("bell\x07and​space.txt") == "bell_and_space.txt"


def test_duplicate_attachment_names_warn(people, tmp_path):
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "report.pdf").write_bytes(b"%PDF")
    shared = mail_merge(TPL, people, sender=SENDER, attachments=[tmp_path / "a/report.pdf", tmp_path / "b/report.pdf"])
    assert [str(i) for i in shared.issues] == [
        "warning: attachments: two attachments are named 'report.pdf'; "
        "mail clients rename one of them or save one over the other"
    ]
    rows = [dict(p, cert=str(tmp_path / ("b" if p["id"] == "2" else "a") / "Report.PDF")) for p in people]
    for folder in ("a", "b"):
        (tmp_path / folder / "report.pdf").rename(tmp_path / folder / "Report.PDF")
    (tmp_path / "report.pdf").write_bytes(b"%PDF")
    per_row = mail_merge(TPL, rows, sender=SENDER, attachments=[tmp_path / "report.pdf"], attachment_field="cert")
    assert [(i.row, i.severity) for i in per_row.issues] == [
        (1, "warning"),
        (2, "warning"),
        (3, "warning"),
        (4, "warning"),
    ]
    assert "'Report.PDF'" in per_row.issues[0].message


def test_from_must_be_exactly_one_mailbox(people, fake_smtp, tmp_path):
    tpl = Template(subject="s", text="t", headers={"From": "{boss}"})
    rows = [dict(p, boss="Lab <lab@example.org>") for p in people]
    rows[1]["boss"] = ""  # used to go out with an empty From and MAIL FROM:<>
    rows[2]["boss"] = "a@example.org, b@example.org"  # several mailboxes need a Sender header
    transport = SMTPTransport(SMTPConfig("smtp.example.org", 587, username="u", password="p"))
    with pytest.raises(DataError) as info:
        mail_merge(tpl, rows, dry_run=False, transport=transport, log_path=tmp_path / "log")
    assert [str(i) for i in info.value.issues] == [
        "error: row 2: no From address: the template's From is empty here and no sender was given",
        "error: row 3: From must be exactly one valid address, got 'a@example.org, b@example.org' "
        "(more than one address)",
    ]
    assert fake_smtp.instances == []


def test_template_from_that_replaces_sender_is_reported(people):
    tpl = Template(subject="s", text="t", headers={"From": "Dean <dean@example.org>"}, name="memo")
    report = mail_merge(tpl, people, sender=SENDER)
    assert [str(i) for i in report.issues] == [
        "warning: template 'memo': its From: 'Dean <dean@example.org>' replaces sender='Study Team <team@example.org>'"
    ]
    assert report.messages[0]["From"] == "Dean <dean@example.org>"
    assert mail_merge(Template(subject="s", text="t", headers={"From": SENDER}), people, sender=SENDER).issues == []


def test_blank_subject_fails_its_row(people):
    tpl = Template(subject="{topic}", text="t")
    rows = [dict(p, topic="News") for p in people]
    rows[3]["topic"] = "  "
    with pytest.raises(DataError, match="row 4: the Subject is empty once the placeholders are filled in"):
        mail_merge(tpl, rows, sender=SENDER)


def test_message_id_is_random_and_uses_the_from_domain(monkeypatch):
    monkeypatch.setattr("socket.getfqdn", lambda name="": pytest.fail("Message-ID must not use the host name"))
    first = build_message(rendered(), sender="Team <team@Example.ORG>")["Message-ID"]
    second = build_message(rendered(), sender="Team <team@Example.ORG>")["Message-ID"]
    assert re.fullmatch(r"<[0-9a-f]{32}@example\.org>", first) and first != second
    assert re.fullmatch(r"<[0-9a-f]{32}@xn--mnchen-3ya\.de>", message_id("münchen.de"))
    with pytest.raises(ValueError, match="IDNA"):
        message_id("x" * 64 + ".de")


def test_date_is_the_send_time_in_utc(people, monkeypatch):
    clock = [datetime(2026, 11, 13, 4, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr("email_merge_tool.message._now", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)))

    class Recorder:
        def __init__(self):
            self.dates = []

        def open(self):
            pass

        def close(self):
            pass

        def send(self, message):
            self.dates.append((clock[0], str(message["Date"])))
            return {}

    transport = Recorder()
    mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport, min_interval=3600)
    # The fourth message leaves three hours after the messages were built.
    assert [sent - parsedate_to_datetime(date) for sent, date in transport.dates] == [timedelta(0)] * 4
    assert all(date.endswith(" +0000") for _, date in transport.dates)
    assert transport.dates[3][0] - transport.dates[0][0] == timedelta(hours=3)


def test_dry_run_replaces_only_its_own_earlier_files(people, tmp_path):
    out = tmp_path / "outbox"
    out.mkdir()
    keep = {"notes.txt": b"mine", "draft.eml": b"mine too", "00009_someone@example.org.eml": b"written by hand"}
    for name, data in keep.items():
        (out / name).write_bytes(data)
    mail_merge(TPL, people, sender=SENDER, output_dir=out)
    assert len(list(out.glob("0000?_*@example.org.eml"))) == 4 + 1
    mail_merge(TPL, people[:2], sender=SENDER, output_dir=out)  # e.g. a reminder after two people replied
    written = sorted(p.name for p in out.iterdir() if p.name not in keep and p.name != DryRunTransport.MANIFEST)
    assert written == ["00001_ann@example.org.eml", "00002_bo@example.org.eml"]
    assert {name: (out / name).read_bytes() for name in keep} == keep


def test_dry_run_file_names_are_short_ascii(tmp_path):
    local = "a" * 64
    long_address = f"{local}@{'b' * 63}.{'c' * 63}.{'d' * 50}.org"
    rows = [{"email": long_address}, {"email": "É" * 120 + " <z@example.org>"}]
    mail_merge(Template(subject="s", text="t"), rows, sender=SENDER, output_dir=tmp_path)
    names = sorted(p.name for p in tmp_path.glob("*.eml"))
    assert names[1] == "00002_z@example.org.eml"
    assert names[0].startswith("00001_" + local) and len(names[0]) == 6 + 80 + 4
    assert names[0] != "00001_" + long_address[:80] + ".eml"


def test_dry_run_write_errors_stop_the_run_cleanly(people, tmp_path, capsys):
    blocked = tmp_path / "out"
    blocked.mkdir()
    (blocked / "00002_bo@example.org.eml").mkdir()  # a directory where the file should go
    with pytest.raises(FatalSendError, match="cannot write .*00002_bo@example.org.eml"):
        mail_merge(TPL, people, sender=SENDER, output_dir=blocked)
    afile = tmp_path / "file"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(FatalSendError, match="cannot write the dry-run messages to"):
        mail_merge(TPL, people, sender=SENDER, output_dir=afile)
    template = tmp_path / "t.txt"
    template.write_text("Subject: Hi\n\nHello\n", encoding="utf-8")
    data = tmp_path / "people.csv"
    data.write_text("email\nann@example.org\n", encoding="utf-8")
    assert main(["-t", str(template), "-d", str(data), "--from", SENDER, "--output-dir", str(afile)]) == 2
    assert "error: cannot write the dry-run messages to" in capsys.readouterr().err
