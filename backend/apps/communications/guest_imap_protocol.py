"""IMAP protocol helpers for guest inbox polling (UIDVALIDITY/UIDNEXT, safe UID search)."""

from __future__ import annotations

import re
from typing import Any

_STATUS_RE = re.compile(rb"(UIDVALIDITY|UIDNEXT)\s+(\d+)")


def parse_status_numbers(data: Any) -> dict[str, int]:
    """Parse ``STATUS`` data into ``{"UIDVALIDITY": n, "UIDNEXT": n}`` (missing keys omitted)."""
    blob = b" ".join(x for x in (data or []) if isinstance(x, bytes))
    return {k.decode(): int(v) for k, v in _STATUS_RE.findall(blob)}


def mailbox_status(client: Any, mailbox: str = "INBOX") -> dict[str, int]:
    """UIDVALIDITY and UIDNEXT of ``mailbox`` (STATUS does not select it). {} on any failure."""
    try:
        typ, data = client.status(mailbox, "(UIDVALIDITY UIDNEXT)")
    except Exception:
        return {}
    if typ != "OK":
        return {}
    return parse_status_numbers(data)


def search_new_uids(client: Any, *, after_uid: int, uidnext: int) -> list[int]:
    """UIDs strictly greater than ``after_uid`` that exist in the selected mailbox.

    Never uses an open-ended ``n:*`` range. IMAP defines ``n:*`` to include the *highest*
    UID even when that is smaller than ``n`` (``UID 5000:*`` on a mailbox whose max UID is
    1128 returns ``[1128]``), which would re-deliver an already processed message — or,
    after a UIDVALIDITY reset, make a stale cursor look "caught up" while new mail with
    lower UIDs is skipped. The range is therefore bounded by ``UIDNEXT-1`` and the reply
    is filtered again, so a server that ignores the bounds cannot leak old UIDs in.

    Raises ``RuntimeError`` if the server rejects the search.
    """
    low, high = after_uid + 1, uidnext - 1
    if low > high:
        return []  # nothing newer than the cursor can exist
    typ, data = client.uid("SEARCH", None, f"UID {low}:{high}")
    if typ != "OK":
        raise RuntimeError("IMAP UID SEARCH failed")
    found = data[0].split() if data and data[0] else []
    return sorted({int(x) for x in found if low <= int(x) <= high})
