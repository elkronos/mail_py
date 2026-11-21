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

from .data import (  # noqa: E402
    DataError,
    Issue,
    is_valid_address,
    load_address_list,
    load_recipients,
    normalize_address,
    validate_recipients,
)
from .experiment import Assignment, assign_variants, write_assignments  # noqa: E402
from .merge import MergeReport, Result, mail_merge  # noqa: E402
from .message import build_message, wire_bytes  # noqa: E402
from .sendlog import bounced_addresses, read_log, sent_addresses  # noqa: E402
from .templates import (  # noqa: E402
    RenderedMessage,
    Template,
    TemplateError,
    fill,
    header_name,
    html_to_text,
    load_template,
)
from .transport import (  # noqa: E402
    PRESETS,
    AccountRefusedError,
    DryRunTransport,
    FatalSendError,
    PermanentSendError,
    SMTPConfig,
    SMTPTransport,
    TransportError,
    UncertainSendError,
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
    "header_name",
    "load_recipients",
    "validate_recipients",
    "is_valid_address",
    "load_address_list",
    "normalize_address",
    "DataError",
    "Issue",
    "assign_variants",
    "write_assignments",
    "Assignment",
    "build_message",
    "wire_bytes",
    "read_log",
    "sent_addresses",
    "bounced_addresses",
    "PRESETS",
    "SMTPConfig",
    "SMTPTransport",
    "DryRunTransport",
    "PermanentSendError",
    "UncertainSendError",
    "FatalSendError",
    "AccountRefusedError",
    "TransportError",
]
