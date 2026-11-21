"""A custom transport that sends through an HTTP email API instead of SMTP.

The endpoint, the bearer token and the JSON body are placeholders: adapt them to
your provider (Amazon SES, Postmark, Mailgun and others accept the raw message).
What matters is how each outcome maps to the transport contract:

* 2xx: delivered, so ``send`` returns None.
* 401 or 403: the key is wrong, so nothing else can be sent: FatalSendError.
* 429, or 5xx and connection errors before the request went out: retried with
  backoff; a 429 that persists stops the run (AccountRefusedError).
* Other 4xx: this message was refused: PermanentSendError.
* The request went out but the answer did not come back: UncertainSendError,
  so the message is never sent twice.

Use it like SMTPTransport::

    transport = HTTPTransport("https://api.example.com/v1/send", os.environ["MAIL_API_KEY"])
    mail_merge("invite.txt", "people.csv", sender="lab@example.org",
               transport=transport, dry_run=False, log_path="invite.jsonl")
"""

import base64
import json
import time
import urllib.error
import urllib.request

from email_merge_tool import AccountRefusedError, FatalSendError, PermanentSendError, UncertainSendError, wire_bytes


class HTTPTransport:
    def __init__(self, url, api_key, *, retries=3, backoff=2.0, timeout=30.0):
        if not url.startswith(("https://", "http://")):  # urlopen would also read file: URLs
            raise ValueError(f"not an http(s) URL: {url!r}")
        self.url, self.api_key = url, api_key
        self.retries, self.backoff, self.timeout = retries, backoff, timeout

    def open(self):
        pass  # nothing to connect; a provider with a "check the key" call could use it here

    def close(self):
        pass

    def send(self, message):
        body = json.dumps({"raw": base64.b64encode(wire_bytes(message)).decode("ascii")}).encode("ascii")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")  # noqa: S310
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff**attempt)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    response.read()
                return None
            except urllib.error.HTTPError as exc:  # the API answered with an error status
                reply = exc.read().decode("utf-8", "replace")
                if exc.code in (401, 403):
                    raise FatalSendError(f"the API refused the key ({exc.code} {reply})") from exc
                if exc.code != 429 and exc.code < 500:
                    raise PermanentSendError(f"the API refused the message ({exc.code} {reply})") from exc
                error = f"{exc.code} {reply}"
            except urllib.error.URLError as exc:  # the request did not go out, so trying again is safe
                error = str(exc.reason)
            except OSError as exc:  # it went out, and the answer was lost: the API may have sent it
                raise UncertainSendError(f"no answer from the API ({exc}); not sent again") from exc
        if error.startswith("429"):
            raise AccountRefusedError(f"the API kept refusing with {error}; continue later with resume")
        raise FatalSendError(f"the API failed {self.retries + 1} times; the last error: {error}")
