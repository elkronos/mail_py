"""Property tests for the bytes SMTPTransport hands to smtplib."""

from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

pytest.importorskip("hypothesis")

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from email_merge_tool import SMTPConfig, SMTPTransport  # noqa: E402


class Capture:
    """The parts of an smtplib.SMTP connection that SMTPTransport.send uses."""

    def __init__(self, features):
        self.features = set(features)
        self.calls = []

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return name.lower() in self.features

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        self.calls.append((from_addr, list(to_addrs), msg, list(mail_options)))
        return {}


text = st.text(st.characters(exclude_categories=("Cs",), exclude_characters="\r\n"), max_size=30)
line = st.one_of(text, st.builds(str.__add__, st.sampled_from(["From ", ">From ", ".", ".."]), text))


@given(lines=st.lists(line, max_size=6), eight_bit=st.booleans())
def test_the_body_arrives_as_built(lines, eight_bit):
    message = EmailMessage()
    message["From"] = "Team <team@example.org>"
    message["To"] = "ann@example.org"
    message["Bcc"] = "archive@example.org"
    message["Subject"] = "Hello"
    message.set_content("\n".join(lines) + "\n")
    connection = Capture(["8bitmime"] if eight_bit else [])
    transport = SMTPTransport(SMTPConfig("smtp.example.org", 25, "none"))
    transport._smtp = connection
    transport.send(message)

    ((sender, recipients, wire, options),) = connection.calls
    assert (sender, recipients) == ("team@example.org", ["ann@example.org", "archive@example.org"])
    assert b"\n" not in wire.replace(b"\r\n", b"")  # CRLF line endings only
    assert b"Bcc" not in wire
    if not wire.isascii():
        assert eight_bit and options == ["BODY=8BITMIME"]
    received = message_from_bytes(wire, policy=policy.default)
    assert received.get_content().replace("\r\n", "\n") == message.get_content()
