import pytest

from email_merge_tool.transport import _account_refusal, _mailbox_problem


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
