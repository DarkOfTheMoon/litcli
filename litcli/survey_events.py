"""The survey trace format: one JSON object per line, plus a markdown view of it.

``trace.jsonl`` is two things at once — the loop's checkpoint (resume rebuilds the frontier from
the last ``hop_end``) and the record the user reads afterwards. Both want the same shape: one
event per line, ``event`` first so a line is classifiable without the rest, everything else
event-specific and additive. A new field is backward compatible; a new event name is not, so
the vocabulary lives here and an unknown name is refused at write time.

:func:`render_md` turns the trace into one table per hop. The row carries the download verdict
because "admitted but no copy" is exactly what a survey must show, not hide.
"""
from __future__ import annotations

import json
from collections.abc import Iterable

from .errors import BadInput

EVENTS = ("start", "candidate", "admit", "download", "hop_end", "stop")

#: Columns of one hop table, in order.
COLUMNS = ("score", "year", "key", "reason", "title")


def line(event: str, **fields) -> str:
    """One trace line; ``event`` is always the first key."""
    if event not in EVENTS:
        raise BadInput(f"unknown survey event {event!r}; known: {', '.join(EVENTS)}")
    return json.dumps({"event": event, **fields}, ensure_ascii=False)


def _cell(value) -> str:
    """A table cell: missing is ``-``, a pipe is escaped, newlines flatten."""
    text = "-" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_md(events: Iterable[dict]) -> str:
    """Render a trace as hop tables with every candidate's verdict and download state."""
    out: list[str] = []
    rows: list[dict] = []
    downloads: dict[str, str] = {}

    def flush(hop) -> None:
        if not rows:
            return
        out.append(f"### Hop {hop}\n")
        out.append("| " + " | ".join(COLUMNS) + " |")
        out.append("|" + "---|" * len(COLUMNS))
        for row in rows:
            status = downloads.get(str(row.get("key")))
            reason = row.get("reason")
            if status and status != "ok":
                reason = f"{reason} (download: {status})"
            cells = [row.get(col) if col != "reason" else reason for col in COLUMNS]
            out.append("| " + " | ".join(_cell(v) for v in cells) + " |")
        out.append("")
        rows.clear()

    hop = None
    for event in events:
        kind = event.get("event")
        if kind == "hop_end":
            flush(event.get("hop", hop))
            hop = None
        elif kind == "candidate":
            hop = event.get("hop", hop)
            rows.append(event)
        elif kind == "download":
            downloads[str(event.get("key"))] = str(event.get("status", ""))
    flush(hop)
    return "\n".join(out).rstrip("\n") + "\n" if out else ""
