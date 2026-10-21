"""Published-date extraction (BLUEPRINT.md §9). Metadata-only for now —
a Mesh fallback (reading the first ~2KB of a page when metadata has no
date, returning a `DateExtraction` schema per the structured-output
invariant, §1.2) is a known, deliberately deferred addition: nothing in
Phase 2 depends on it yet, and adding it later is a small, isolated
change. Until then, an unknown published date is stored as None, which is
an honest "unknown," not a guess."""

from __future__ import annotations

import datetime as dt
from email.utils import parsedate_to_datetime


def parse_published_date(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed
    except (TypeError, ValueError):
        return None
