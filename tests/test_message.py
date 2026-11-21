"""The bytes a message goes out as: headers, MIME structure and encodings."""

import copy
import pickle
import smtplib
from email import message_from_bytes, policy

import pytest

from email_merge_tool import DataError, Template, build_message, mail_merge
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
