import smtplib

import pytest

# tests/test_merge.py tests the pre-0.2 API (main/merge.py), which could not be
# imported. It is kept only until the legacy files are deleted; see docs/review.md.
collect_ignore = ["test_merge.py"]


class FakeSMTP:
    """Stands in for smtplib.SMTP and records what was sent.

    The constructor has smtplib.SMTP's signature, which takes no TLS context.
    ``features`` names the extensions the fake server offers; ``starttls()``
    and ``login()`` fail as smtplib's do when theirs is missing.
    """

    instances: list["FakeSMTP"] = []
    # Scripted outcomes of successive send_message calls: None succeeds, an
    # exception is raised before DATA, ("payload", exc) raises exc while the
    # message is being written, and ("after_data", exc) delivers the message
    # and then raises exc, as when the server's final reply is lost.
    failures: list = []
    login_error: Exception | None = None
    features: set[str] = set()
    sleeps: list[float] = []

    def __init__(self, host="", port=0, local_hostname=None, timeout=30.0, source_address=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.context = None
        self.implicit_tls = False
        self.sent = []
        self.logged_in = None
        self.tls = False
        self.closed = False
        self.payload_error = None
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        if "starttls" not in FakeSMTP.features:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        self.tls, self.context = True, context

    def login(self, user, password):
        if "auth" not in FakeSMTP.features:
            raise smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server.")
        if FakeSMTP.login_error:
            raise FakeSMTP.login_error
        self.logged_in = (user, password)

    def send_message(self, msg):
        if FakeSMTP.failures:
            error = FakeSMTP.failures.pop(0)  # None means "this call succeeds"
            if isinstance(error, tuple) and error[0] == "payload":
                self.payload_error = error[1]
                self.data(msg)  # raises from the second write
            if isinstance(error, tuple):
                self.data(msg)  # like smtplib: goes through the instance's data()
                exc = error[1]
                rejected = isinstance(exc, smtplib.SMTPResponseException) and 400 <= exc.smtp_code < 600
                if not rejected:  # the server queued it, but its reply never arrived
                    self.sent.append(msg)
                raise exc
            if error is not None:
                raise error
        self.data(msg)
        self.sent.append(msg)
        return {}

    def data(self, msg):
        # smtplib's data() writes the DATA command, then the message.
        self.send(b"DATA\r\n")
        self.send(b"message\r\n.\r\n")
        return (250, b"queued")

    def send(self, payload):
        if payload != b"DATA\r\n" and self.payload_error is not None:
            error, self.payload_error = self.payload_error, None
            raise error

    def close(self):
        self.closed = True

    def quit(self):
        self.closed = True


class FakeSMTPSSL(FakeSMTP):
    """Stands in for smtplib.SMTP_SSL, whose constructor takes the TLS context."""

    def __init__(self, host="", port=0, local_hostname=None, *, timeout=30.0, source_address=None, context=None):
        super().__init__(host, port, local_hostname, timeout, source_address)
        self.context, self.implicit_tls, self.tls = context, True, True


@pytest.fixture(autouse=True)
def no_hostname_lookup(monkeypatch):
    # smtplib names the local host for EHLO with socket.getfqdn(), a DNS
    # lookup that takes tens of seconds on some CI runners (macOS). The name
    # needs a dot, or smtplib falls back to a second lookup.
    monkeypatch.setattr("socket.getfqdn", lambda name="": "localhost.localdomain")


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.failures = []
    FakeSMTP.login_error = None
    FakeSMTP.features = {"starttls", "auth", "8bitmime", "size"}
    FakeSMTP.sleeps = []  # the delays the code under test asked for, in seconds
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr("time.sleep", FakeSMTP.sleeps.append)
    return FakeSMTP


@pytest.fixture
def people():
    return [
        {"email": "ann@example.org", "first_name": "Ann", "dept": "bio", "id": "1"},
        {"email": "bo@example.org", "first_name": "Bo", "dept": "bio", "id": "2"},
        {"email": "cy@example.org", "first_name": "Cy", "dept": "chem", "id": "3"},
        {"email": "di@example.org", "first_name": "Di", "dept": "chem", "id": "4"},
    ]
