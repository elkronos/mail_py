import pytest

from email_merge_tool.transport import _account_refusal, _bounced, _enhanced_status, _mailbox_problem


@pytest.mark.parametrize(
    ("code", "text", "account"),
    [
        (550, "5.4.5 Daily user sending limit exceeded.", True),
        (554, "5.2.0 STOREDRV.Submission.Exception:SubmissionQuotaExceededException", True),
        (530, "5.7.0 Authentication Required", True),
        (535, "5.7.8 Username and Password not accepted", True),
        (451, "4.7.1 Rate limit exceeded", True),
        (550, "5.7.1 Daily sending quota exceeded", True),
        (421, "4.7.0 Too many messages from this account", True),
        (451, "4.7.1 Greylisted, please try again later", False),
        (550, "5.7.1 Relaying denied", False),
        (554, "5.7.1 Rejected: inaccurate or separate headers", False),  # "rate" inside other words
        (550, "5.1.1 No such user", False),
        (452, "4.2.2 Mailbox over quota", False),  # the recipient's mailbox, not the account
        (552, "5.3.4 Message size exceeds fixed limit", False),
        (250, "2.0.0 OK quota", False),
    ],
)
def test_account_level_replies(code, text, account):
    assert (_account_refusal(code, text) is not None) is account


@pytest.mark.parametrize(
    ("text", "mailbox"),
    [
        ("5.1.1 No such user", True),
        ("4.2.1 Mailbox busy", True),
        ("5.2.2 Mailbox full", True),
        ("5.1.7 Bad sender address", False),
        ("5.7.1 Message rejected", False),
        ("No such user here", False),
        ("", False),
    ],
)
def test_mailbox_problems(text, mailbox):
    assert _mailbox_problem(text) is mailbox


@pytest.mark.parametrize(
    ("code", "text", "bounce"),
    [
        (550, "5.1.1 The email account that you tried to reach does not exist.", True),  # Gmail
        (550, "5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup", True),  # Microsoft
        (553, "5.1.3 Bad recipient address syntax", True),
        (550, "5.2.1 The email account that you tried to reach is disabled.", True),
        (550, "User unknown", True),
        (550, "Recipient address rejected: unknown user", True),
        (550, "no such user here", True),
        (552, "5.2.2 Mailbox full", False),  # exists, may take mail later
        (550, "5.1.7 Bad sender address", False),  # about the sender
        (550, "5.7.1 Message rejected as spam", False),
        (550, "Requested action not taken: mailbox unavailable", False),  # also used for policy refusals
        (553, "sorry, that domain isn't in my list of allowed rcpthosts", False),
        (450, "4.1.1 Recipient address verification in progress", False),  # temporary
        (550, "4.1.1 inconsistent", False),
        (None, "5.1.1 no such user", False),
    ],
)
def test_bounces(code, text, bounce):
    assert _bounced(code, text) is bounce


def test_enhanced_status():
    assert _enhanced_status("5.1.10 RESOLVER.ADR.RecipientNotFound") == "5.1.10"
    assert _enhanced_status("  4.7.1 Greylisted") == "4.7.1"
    assert _enhanced_status("User unknown") is None
    assert _enhanced_status("") is None
