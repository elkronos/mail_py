import smtplib

import pytest

# tests/test_merge.py tests the pre-0.2 API (main/merge.py), which could not be
# imported. It is kept only until the legacy files are deleted; see docs/review.md.
collect_ignore = ["test_merge.py"]


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what was sent."""

    instances: list["FakeSMTP"] = []
    # Scripted outcomes of successive send_message calls: None succeeds, an
    # exception is raised before DATA, ("payload", exc) raises exc while the
    # message is being written, and ("after_data", exc) delivers the message
    # and then raises exc, as when the server's final reply is lost.
    failures: list = []
    login_error: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.sent = []
        self.logged_in = None
        self.tls = context is not None
        self.closed = False
        self.payload_error = None
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
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


@pytest.fixture(autouse=True)
def no_hostname_lookup(monkeypatch):
    # smtplib names the local host for EHLO with socket.getfqdn(), a DNS
    # lookup that takes tens of seconds on some CI runners (macOS).
    monkeypatch.setattr("socket.getfqdn", lambda name="": "localhost")


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.failures = []
    FakeSMTP.login_error = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return FakeSMTP


@pytest.fixture
def people():
    return [
        {"email": "ann@example.org", "first_name": "Ann", "dept": "bio", "id": "1"},
        {"email": "bo@example.org", "first_name": "Bo", "dept": "bio", "id": "2"},
        {"email": "cy@example.org", "first_name": "Cy", "dept": "chem", "id": "3"},
        {"email": "di@example.org", "first_name": "Di", "dept": "chem", "id": "4"},
    ]
