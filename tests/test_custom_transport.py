"""The transport contract, end to end, with the HTTP transport from examples/ and a local HTTP server."""

import base64
import importlib.util
import json
import socket
import threading
from email import message_from_bytes, policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from email_merge_tool import (
    AccountRefusedError,
    FatalSendError,
    Template,
    mail_merge,
    read_log,
    sent_addresses,
)

TPL = Template(
    subject="Hello {first_name}", text="Dear {first_name},\nWelcome.\n", headers={"Bcc": "archive@example.org"}
)
SENDER = "Study Team <team@example.org>"

_spec = importlib.util.spec_from_file_location(
    "http_transport", Path(__file__).parent.parent / "examples" / "http_transport.py"
)
http_transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(http_transport)


class API:
    """A stand-in email API: it records each message and answers as scripted per ``To`` address.

    ``replies[address]`` lists (status, body) answers for successive requests, or
    "drop" to read the request and close the connection without answering.
    An exhausted list answers 202.
    """

    def __init__(self):
        self.replies = {}
        self.received = []  # (Authorization header, message bytes)
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                raw = base64.b64decode(body["raw"])
                to = message_from_bytes(raw, policy=policy.default)["To"]
                script = api.replies.get(str(to), [])
                reply = script.pop(0) if script else (202, '{"id": "queued"}')
                if reply == "drop":
                    api.received.append((self.headers["Authorization"], raw))
                    self.close_connection = True
                    return
                status, text = reply
                if status < 300:
                    api.received.append((self.headers["Authorization"], raw))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(text)))
                self.end_headers()
                self.wfile.write(text.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/send"
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()

    def delivered_to(self):
        return [str(message_from_bytes(raw, policy=policy.default)["To"]) for _, raw in self.received]


@pytest.fixture
def api():
    server = API()
    yield server
    server.server.shutdown()
    server.server.server_close()


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr("time.sleep", calls.append)
    return calls


def transport(api):
    return http_transport.HTTPTransport(api.url, "key-123")


def test_each_outcome_of_the_contract(api, people, sleeps, tmp_path):
    api.replies = {
        "bo@example.org": [(422, '{"error": "inactive recipient"}')],
        "cy@example.org": [(429, '{"error": "slow down"}'), (503, "busy")],
        "di@example.org": ["drop"],
    }
    log = tmp_path / "log.jsonl"
    report = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport(api), log_path=log)
    assert [r.status for r in report.results] == ["sent", "failed", "sent", "uncertain"]
    assert report.results[1].detail == 'the API refused the message (422 {"error": "inactive recipient"})'
    assert report.results[0].detail == ""  # the API's JSON answer is not a list of refusals
    assert sleeps == [2.0, 4.0]  # cy was throttled, then the API was busy
    assert api.delivered_to() == ["ann@example.org", "cy@example.org", "di@example.org"]
    auth, raw = api.received[0]
    assert auth == "Bearer key-123"
    assert b"archive@example.org" not in raw and raw.count(b"\r\n") > 3  # the SMTP bytes, without Bcc
    assert [r["status"] for r in read_log(log)] == ["sent", "failed", "sent", "uncertain"]

    again = mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport(api), log_path=log, resume=True)
    assert [r.status for r in again.results] == ["skipped", "sent", "skipped", "skipped"]
    assert api.delivered_to().count("di@example.org") == 1  # the lost answer is never sent twice


def test_a_refused_key_stops_the_run(api, people, sleeps, tmp_path):
    api.replies = {"ann@example.org": [(401, '{"error": "bad key"}')]}
    with pytest.raises(FatalSendError, match="the API refused the key") as info:
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport(api), log_path=tmp_path / "l")
    assert [(r.to, r.status) for r in info.value.report.results] == [("ann@example.org", "failed")]
    assert api.received == [] and sleeps == []


def test_throttling_that_persists_stops_the_run(api, people, sleeps, tmp_path):
    api.replies = {"bo@example.org": [(429, "slow down")] * 4}
    with pytest.raises(AccountRefusedError, match="kept refusing with 429") as info:
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=transport(api), log_path=tmp_path / "l")
    assert info.value.report.summary() == "sent: 1, failed: 1"
    assert sleeps == [2.0, 4.0, 8.0]
    assert sent_addresses(tmp_path / "l") == {"ann@example.org"}


def test_an_unreachable_api_is_retried_then_stops(people, sleeps):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    unreachable = http_transport.HTTPTransport(f"http://127.0.0.1:{port}/v1/send", "key")
    with pytest.raises(FatalSendError, match="the API failed 4 times") as info:
        mail_merge(TPL, people, sender=SENDER, dry_run=False, transport=unreachable)
    assert [r.status for r in info.value.report.results] == ["failed"]
    assert sleeps == [2.0, 4.0, 8.0]


def test_a_dry_run_does_not_use_the_transport(api, people):
    report = mail_merge(TPL, people, sender=SENDER, transport=transport(api))
    assert report.count("dry_run") == 4 and api.received == []
    assert [str(i) for i in report.issues if i.where == "transport"] == [
        "warning: transport: HTTPTransport is not used: this is a dry run (dry_run=True is the default), "
        "so nothing is sent; pass dry_run=False to send"
    ]
