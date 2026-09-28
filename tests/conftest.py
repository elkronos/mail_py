import smtplib

import pytest

# tests/test_merge.py tests the pre-0.2 API (main/merge.py), which could not be
# imported. It is kept only until the legacy files are deleted; see docs/review.md.
collect_ignore = ["test_merge.py"]


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what was sent."""

    instances: list["FakeSMTP"] = []
    # Scripted failures: list of exceptions raised by successive send_message calls.
    failures: list[Exception] = []
    login_error: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.sent = []
        self.logged_in = None
        self.tls = context is not None
        self.closed = False
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
            if error is not None:
                raise error
        self.sent.append(msg)
        return {}

    def quit(self):
        self.closed = True


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
