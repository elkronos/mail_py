"""The bytes a message goes out as: headers, MIME structure and encodings."""

import copy
import pickle
import re
import smtplib
from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from email_merge_tool import (
    DataError,
    DryRunTransport,
    SMTPConfig,
    SMTPTransport,
    Template,
    build_message,
    mail_merge,
)
from email_merge_tool.message import attachment_name, needs_smtputf8, wire_bytes
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
