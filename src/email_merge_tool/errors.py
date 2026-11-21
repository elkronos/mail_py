"""The base class of the exceptions this package raises."""

from __future__ import annotations

__all__ = ["EmailMergeError"]


class EmailMergeError(Exception):
    """Base class of every exception this package defines.

    Catch it to handle any of them: :class:`~email_merge_tool.DataError` and
    :class:`~email_merge_tool.TemplateError` (which are also ``ValueError``),
    and the send errors (:class:`~email_merge_tool.PermanentSendError`,
    :class:`~email_merge_tool.FatalSendError` and their subclasses).
    """
