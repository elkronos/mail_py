"""email_merge_tool: careful, reproducible mail merge for Python.

Quick start::

    from email_merge_tool import mail_merge, SMTPConfig, SMTPTransport

    report = mail_merge("invite.txt", "people.csv", sender="Lab <lab@example.org>")  # dry run
    print(report.messages[0])

The package installs a :class:`logging.NullHandler`; configure logging in your
application if you want its messages.
"""

import logging

__version__ = "0.2.0"

from .data import DataError, Issue, is_valid_address, load_recipients, validate_recipients  # noqa: E402
from .experiment import Assignment, assign_variants, write_assignments  # noqa: E402
from .merge import MergeReport, Result, mail_merge  # noqa: E402
from .message import build_message  # noqa: E402
from .sendlog import read_log, sent_addresses  # noqa: E402
from .templates import RenderedMessage, Template, TemplateError, fill, html_to_text, load_template  # noqa: E402
from .transport import (  # noqa: E402
    PRESETS,
    DryRunTransport,
    FatalSendError,
    PermanentSendError,
    SMTPConfig,
    SMTPTransport,
)

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "__version__",
    "mail_merge",
    "MergeReport",
    "Result",
    "Template",
    "RenderedMessage",
    "TemplateError",
    "load_template",
    "fill",
    "html_to_text",
    "load_recipients",
    "validate_recipients",
    "is_valid_address",
    "DataError",
    "Issue",
    "assign_variants",
    "write_assignments",
    "Assignment",
    "build_message",
    "read_log",
    "sent_addresses",
    "PRESETS",
    "SMTPConfig",
    "SMTPTransport",
    "DryRunTransport",
    "PermanentSendError",
    "FatalSendError",
]
