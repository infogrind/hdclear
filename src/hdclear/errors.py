"""A single exception type for user-facing failures.

Anything caught at the CLI boundary and printed as ``Error: ...`` must be a
HdclearError -- a condition the user caused or can fix (wrong device, not
root, missing tool, disk refused by a safety check). Everything else is a bug
and should crash with a full traceback.
"""

from __future__ import annotations


class HdclearError(Exception):
    """A user-facing error -- not a bug."""
