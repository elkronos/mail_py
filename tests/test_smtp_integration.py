"""Sending through a real SMTP server (aiosmtpd) on 127.0.0.1.

The fakes in conftest.py stand in for smtplib itself; these tests run the real
smtplib against a real server, so refused recipients, reply codes, TLS and
the bytes on the wire are what a provider would see.
"""

import asyncio
import socket
import ssl
from dataclasses import dataclass
from email import message_from_bytes, policy

import pytest

pytest.importorskip("aiosmtpd")

from aiosmtpd.controller import Controller  # noqa: E402

from email_merge_tool import (  # noqa: E402
    AccountRefusedError,
    FatalSendError,
    PermanentSendError,
    SMTPConfig,
    SMTPTransport,
    Template,
    mail_merge,
    read_log,
    sent_addresses,
)

TPL = Template(subject="Hello {first_name}", text="Dear {first_name},\nWelcome.\n")
SENDER = "Study Team <team@example.org>"


@dataclass
class Delivery:
    """One message the server accepted."""

    mail_from: str
    rcpt_tos: list[str]
    mail_options: list[str]
    content: bytes  # the DATA as transmitted, after dot-unstuffing
    tls: bool

    @property
    def message(self):
        return message_from_bytes(self.content, policy=policy.default)


class Handler:
    """Records every command and replies as scripted.

    ``mail`` and ``data`` hold replies for successive MAIL and DATA commands,
    and ``rcpt`` maps an address to replies for its successive RCPT commands.
    Each is used in order; ``None`` or an exhausted list accepts. A reply that
    starts with ``421`` closes the connection after it is sent. The DATA reply
    ``"drop"`` keeps the message and closes the connection without replying.
    """

    def __init__(self):
        self.mail: list[str | None] = []
        self.rcpt: dict[str, list[str | None]] = {}
        self.data: list[str | None] = []
        self.commands: list[tuple[str, str]] = []
        self.delivered: list[Delivery] = []
        self.port = 0

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        self.commands.append(("MAIL", address))
        if reply := _next(self.mail):
            return _reply(server, reply)
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        self.commands.append(("RCPT", address))
        if reply := _next(self.rcpt.get(address.lower(), [])):
            return _reply(server, reply)
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.commands.append(("DATA", ",".join(envelope.rcpt_tos)))
        reply = _next(self.data)
        if reply is None or reply == "drop":
            tls = bool(session.ssl) or server.transport.get_extra_info("sslcontext") is not None
            delivered = Delivery(
                envelope.mail_from,
                list(envelope.rcpt_tos),
                list(envelope.mail_options),
                envelope.original_content,
                tls,
            )
            self.delivered.append(delivered)
        if reply == "drop":
            server.transport.abort()
            return "250 never sent"
        return _reply(server, reply) if reply else "250 OK queued"

    def count(self, verb):
        return sum(c[0] == verb for c in self.commands)

    def received_by(self, address):
        return [d for d in self.delivered if address in d.rcpt_tos]


def _next(replies):
    return replies.pop(0) if replies else None


def _reply(server, reply):
    if reply.startswith("421"):
        asyncio.get_running_loop().call_soon(server.transport.close)  # after the reply is written
    return reply


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def serve():
    """Start an aiosmtpd server; keyword arguments go to aiosmtpd's SMTP class."""
    controllers = []

    def start(ssl_context=None, **smtp_options):
        handler = Handler()
        handler.port = _free_port()
        controller = Controller(
            handler, hostname="127.0.0.1", port=handler.port, ssl_context=ssl_context, **smtp_options
        )
        controller.start()
        controllers.append(controller)
        return handler

    yield start
    for controller in controllers:
        controller.stop()


@pytest.fixture
def server(serve):
    return serve()


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr("time.sleep", calls.append)
    return calls


def plain(handler, **options):
    """A transport for the test server, without TLS or login."""
    return SMTPTransport(SMTPConfig("127.0.0.1", handler.port, "none"), **options)


def send(handler, rows, template=TPL, tmp_path=None, **options):
    log = (tmp_path / "log.jsonl") if tmp_path is not None else None
    return mail_merge(template, rows, sender=SENDER, dry_run=False, transport=plain(handler), log_path=log, **options)


def test_plain_delivery(server, people, sleeps):
    report = send(server, people[:2])
    assert [r.status for r in report.results] == ["sent", "sent"]
    first = server.delivered[0]
    assert (first.mail_from, first.rcpt_tos) == ("team@example.org", ["ann@example.org"])
    assert first.message["Subject"] == "Hello Ann"
    assert first.message.get_content().splitlines() == ["Dear Ann,", "Welcome."]
    assert server.count("MAIL") == 2 and sleeps == []


def test_bcc_is_delivered_but_not_transmitted(server, people, sleeps):
    tpl = Template(subject="s", text="t", headers={"Cc": "lee@example.org", "Bcc": "archive@example.org"})
    report = send(server, people[:1], template=tpl)
    assert report.sent == 1
    (delivery,) = server.delivered
    assert sorted(delivery.rcpt_tos) == ["ann@example.org", "archive@example.org", "lee@example.org"]
    assert b"archive@example.org" not in delivery.content
    assert delivery.message["Cc"] == "lee@example.org" and delivery.message["Bcc"] is None


def test_data_4xx_is_retried(server, people, sleeps, tmp_path):
    server.data = ["451 4.3.0 Try again later"]
    report = send(server, people[:1], tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["sent"]
    assert len(server.delivered) == 1 and sleeps == [2.0]


def test_421_closes_the_connection_and_the_retry_reconnects(server, people, sleeps, tmp_path):
    server.mail = ["421 4.3.2 Service shutting down"]
    report = send(server, people[:2], tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["sent", "sent"]
    assert [d.rcpt_tos for d in server.delivered] == [["ann@example.org"], ["bo@example.org"]]
    assert sleeps == [2.0]


def test_drop_after_data_is_uncertain_and_not_retried(server, people, sleeps, tmp_path):
    server.data = ["drop"]
    report = send(server, people[:2], tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["uncertain", "sent"]
    assert len(server.received_by("ann@example.org")) == 1
    assert [r["status"] for r in read_log(tmp_path / "log.jsonl")] == ["uncertain", "sent"]


def test_message_over_the_size_limit_fails_alone(serve, people, sleeps, tmp_path):
    server = serve(data_size_limit=2000)
    big = Template(subject="s", text="{note}")
    rows = [dict(people[0], note="x" * 3000), dict(people[1], note="short")]
    report = send(server, rows, template=big, tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["failed", "sent"]
    assert "552" in report.results[0].detail


def test_international_address_needs_smtputf8(serve, people, sleeps):
    from email.message import EmailMessage

    def message():
        msg = EmailMessage()
        msg["From"] = SENDER
        msg["To"] = "José <josé@example.org>"
        msg["Subject"] = "Hola"
        msg.set_content("Hola")
        return msg

    without = serve(enable_SMTPUTF8=False)
    transport = plain(without)
    with transport, pytest.raises(PermanentSendError, match="SMTPUTF8"):
        transport.send(message())
    assert without.count("MAIL") == 0

    with_utf8 = serve()  # aiosmtpd offers SMTPUTF8 by default
    with plain(with_utf8) as transport:
        transport.send(message())
    (delivery,) = with_utf8.delivered
    assert delivery.rcpt_tos == ["josé@example.org"]
    assert "SMTPUTF8" in delivery.mail_options


def test_starttls_not_offered_stops_the_run(server, people):
    transport = SMTPTransport(SMTPConfig("127.0.0.1", server.port, "starttls"))
    with pytest.raises(FatalSendError, match="STARTTLS"):
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport)
    assert server.count("MAIL") == 0


@pytest.fixture
def ca():
    trustme = pytest.importorskip("trustme")
    return trustme.CA()


def _server_context(ca):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1").configure_cert(context)
    return context


def _trust(monkeypatch, ca):
    create = ssl.create_default_context

    def trusting(*args, **kwargs):
        context = create(*args, **kwargs)
        ca.configure_trust(context)
        return context

    monkeypatch.setattr(ssl, "create_default_context", trusting)


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_tls_certificate_is_verified(serve, people, monkeypatch, ca, security):
    context = _server_context(ca)
    server = serve(tls_context=context) if security == "starttls" else serve(ssl_context=context)
    transport = SMTPTransport(SMTPConfig("127.0.0.1", server.port, security))
    with pytest.raises(FatalSendError, match="certificate"):  # the test CA is not trusted yet
        mail_merge(TPL, people[:1], sender=SENDER, dry_run=False, transport=transport)
    assert server.delivered == []

    _trust(monkeypatch, ca)
    report = mail_merge(TPL, people[:1], sender=SENDER, dry_run=False, transport=transport)
    assert report.sent == 1 and server.delivered[0].tls


def test_from_lines_reach_the_recipient_unchanged(server, people, sleeps, tmp_path):
    line = "From the whole study team, thanks!"
    tpl = Template(subject="s", text=f"Dear {{first_name}},\n{line}\n.Dotted\n", html=f"<p>Hi</p>\n{line}<br>\n")
    preview = mail_merge(tpl, people[:1], sender=SENDER, output_dir=tmp_path / "eml")
    assert preview.count("dry_run") == 1
    send(server, people[:1], template=tpl)
    wire = server.delivered[0].content
    assert b">From" not in wire
    assert wire.count(f"\r\n{line}".encode()) == 2  # the text and the HTML part
    assert b"\r\n.Dotted\r\n" in wire  # smtplib's dot-stuffing is undone by the server
    eml = next((tmp_path / "eml").glob("*.eml")).read_bytes()
    assert eml.count(f"\n{line}".encode()) == 2  # what the preview showed is what was sent


def test_8bit_text_is_declared_or_encoded_for_7bit_servers(serve, sleeps):
    rows = [{"email": "zoe@example.org", "first_name": "Zoë"}]
    eight = serve()
    send(eight, rows)
    (delivery,) = eight.delivered
    assert "BODY=8BITMIME" in delivery.mail_options
    assert "Dear Zoë,".encode() in delivery.content

    seven = serve(decode_data=True, enable_SMTPUTF8=False)  # no 8BITMIME; refuses 8-bit data
    report = send(seven, rows)
    assert report.sent == 1
    (delivery,) = seven.delivered
    assert delivery.content.isascii() and not any(o.startswith("BODY") for o in delivery.mail_options)
    assert delivery.message.get_content().splitlines() == ["Dear Zoë,", "Welcome."]


ARCHIVED = Template(subject="Hello {first_name}", text="Dear {first_name}", headers={"Cc": "archive@example.org"})


def test_refused_to_with_an_accepted_cc_is_failed_and_resumable(server, people, sleeps, tmp_path):
    server.rcpt["ann@example.org"] = ["550 5.1.1 No such user"]
    report = send(server, people[:2], template=ARCHIVED, tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["failed", "sent"]
    assert report.failed == 1
    assert "ann@example.org (550 5.1.1 No such user)" in report.results[0].detail
    assert [d.rcpt_tos for d in server.delivered] == [
        ["archive@example.org"],
        ["bo@example.org", "archive@example.org"],
    ]
    assert sent_addresses(tmp_path / "log.jsonl") == {"bo@example.org"}

    again = send(server, people[:2], template=ARCHIVED, tmp_path=tmp_path, resume=True)
    assert [r.status for r in again.results] == ["sent", "skipped"]
    assert len(server.received_by("ann@example.org")) == 1


def test_temporary_refusal_of_to_is_retried_for_that_address_only(server, people, sleeps, tmp_path):
    server.rcpt["ann@example.org"] = ["451 4.7.1 Greylisted, try again"]
    report = send(server, people[:1], template=ARCHIVED, tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["sent"]
    assert [d.rcpt_tos for d in server.delivered] == [["archive@example.org"], ["ann@example.org"]]
    assert sleeps == [2.0]


def test_refused_cc_alone_still_counts_as_sent(server, people, sleeps, tmp_path):
    server.rcpt["archive@example.org"] = ["550 5.1.1 No such user"]
    report = send(server, people[:1], template=ARCHIVED, tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["sent"]
    assert report.results[0].detail == "some recipients refused: archive@example.org (550 5.1.1 No such user)"
    assert [d.rcpt_tos for d in server.delivered] == [["ann@example.org"]]


def test_rcpt_4xx_is_retried(server, people, sleeps, tmp_path):
    server.rcpt["ann@example.org"] = ["451 4.7.1 Greylisted, try again", "452 4.5.3 Too many recipients"]
    report = send(server, people[:2], tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["sent", "sent"]
    assert server.commands.count(("RCPT", "ann@example.org")) == 3
    assert sleeps == [2.0, 4.0]


def test_rcpt_421_reconnects_and_retries(server, people, sleeps, tmp_path):
    server.rcpt["ann@example.org"] = ["421 4.3.2 Shutting down"]
    report = send(server, people[:2], tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["sent", "sent"]
    assert sleeps == [2.0]  # one backoff for ann; bo needs none


def test_rcpt_4xx_that_persists_fails_after_the_retries(server, people, sleeps, tmp_path):
    server.rcpt["ann@example.org"] = ["450 4.2.1 Mailbox busy"] * 4
    report = send(server, people[:2], tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["failed", "sent"]
    assert (
        report.results[0].detail
        == "gave up after 3 retries: recipient refused: ann@example.org (450 4.2.1 Mailbox busy)"
    )
    assert sleeps == [2.0, 4.0, 8.0]


def test_rcpt_5xx_fails_at_once(server, people, sleeps, tmp_path):
    server.rcpt["ann@example.org"] = ["550 5.1.1 No such user"]
    report = send(server, people[:2], tmp_path=tmp_path)
    assert [r.status for r in report.results] == ["failed", "sent"]
    assert report.results[0].detail == "recipient refused: ann@example.org (550 5.1.1 No such user)"
    assert server.commands.count(("RCPT", "ann@example.org")) == 1 and sleeps == []


def rows(n):
    return [{"email": f"r{i}@example.org", "first_name": f"R{i}"} for i in range(1, n + 1)]


def logged(tmp_path):
    return [(r["to"], r["status"]) for r in read_log(tmp_path / "log.jsonl")]


def test_daily_quota_stops_the_run_and_resume_continues(server, people, sleeps, tmp_path):
    server.mail = [None, None, "550 5.4.5 Daily user sending limit exceeded."]
    with pytest.raises(AccountRefusedError, match="5.4.5 Daily user sending limit") as info:
        send(server, people, tmp_path=tmp_path)
    assert (info.value.smtp_code, info.value.smtp_error) == (550, "5.4.5 Daily user sending limit exceeded.")
    assert server.count("MAIL") == 3  # nothing after the refusal
    assert logged(tmp_path) == [("ann@example.org", "sent"), ("bo@example.org", "sent"), ("cy@example.org", "failed")]

    again = send(server, people, tmp_path=tmp_path, resume=True)  # the next day
    assert [r.status for r in again.results] == ["skipped", "skipped", "sent", "sent"]
    assert sleeps == []


@pytest.mark.parametrize(
    ("stage", "reply"),
    [
        ("data", "554 5.2.0 STOREDRV.Submission.Exception:SubmissionQuotaExceededException; Cannot submit message."),
        ("mail", "530 5.7.0 Authentication required"),
        ("mail", "553 5.7.1 Sender address rejected: not owned by user team@example.org"),
        ("rcpt", "450 4.7.1 Rate limit exceeded, try again later"),
        ("data", "451 4.7.0 Too many messages, slow down"),
        ("mail", "421 4.7.0 Sending rate limited, closing connection"),
    ],
)
def test_account_level_replies_stop_at_once(server, people, sleeps, tmp_path, stage, reply):
    if stage == "rcpt":
        server.rcpt["ann@example.org"] = [reply]
    else:
        setattr(server, stage, [reply])
    with pytest.raises(AccountRefusedError, match=reply[:3]):
        send(server, people, tmp_path=tmp_path)
    assert logged(tmp_path) == [("ann@example.org", "failed")]
    assert server.delivered == [] and sleeps == []  # not retried, and nobody else tried


def test_failures_in_a_row_stop_the_run(server, sleeps, tmp_path):
    server.data = ["554 5.7.1 Message rejected"] * 10
    with pytest.raises(FatalSendError, match="stopped after 3 failed messages in a row") as info:
        send(server, rows(6), tmp_path=tmp_path, max_consecutive_failures=3)
    assert not isinstance(info.value, AccountRefusedError)
    assert "554 5.7.1 Message rejected" in str(info.value)
    assert server.count("DATA") == 3
    assert [status for _, status in logged(tmp_path)] == ["failed"] * 3


def test_the_default_stop_is_five_failures(server, sleeps):
    server.data = ["554 5.7.1 Message rejected"] * 10
    with pytest.raises(FatalSendError, match="stopped after 5 failed"):
        send(server, rows(7))
    assert server.count("DATA") == 5


def test_zero_never_stops_and_failures_are_spaced(server, sleeps):
    server.data = ["554 5.7.1 Message rejected"] * 6
    report = send(server, rows(6), max_consecutive_failures=0, min_interval=3)
    assert report.failed == 6
    assert sleeps == [3] * 5  # min_interval applies between failed attempts too


def test_unknown_addresses_do_not_count_and_a_sent_message_resets(server, sleeps):
    for i in (1, 2, 3):
        server.rcpt[f"r{i}@example.org"] = ["550 5.1.1 No such user"]
    server.data = ["554 5.7.1 Message rejected", None, "554 5.7.1 Message rejected", None]
    report = send(server, rows(7), max_consecutive_failures=2)
    assert [r.status for r in report.results] == ["failed"] * 4 + ["sent", "failed", "sent"]
