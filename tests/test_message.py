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
from email_merge_tool.message import needs_smtputf8, wire_bytes
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
        ("<https://example.org/u?t=" + "a" * 1000 + ">", "at most 980 characters"),
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
