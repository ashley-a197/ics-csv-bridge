"""Hand-rolled parser for RFC 5545 iCalendar (.ics) content.

The stdlib has nothing for this format, and the whole point of this
project is precise error locations, so the parser is written from
scratch instead of leaning on a permissive line-by-line split. Every
character we consume is tracked back to its (line, column) in the
original file, so a malformed escape or a missing ':' points at the
exact spot that caused it, not just "somewhere in this file".
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass


class ICSParseError(Exception):
    """Malformed ICS input. line/column are 1-based, like a compiler error."""

    def __init__(self, message: str, line: int, column: int):
        self.message = message
        self.line = line
        self.column = column
        super().__init__(f"line {line}, column {column}: {message}")


@dataclass
class Event:
    uid: str = ""
    summary: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    description: str = ""
    line: int = 0  # line of the BEGIN:VEVENT that opened this event


@dataclass
class _Pos:
    line: int
    column: int


def _unfold(text: str) -> tuple[str, list[_Pos]]:
    """Undo RFC 5545 line folding.

    Folding rule: a writer may split a long line by inserting CRLF
    (or bare LF) immediately followed by a single space or tab. That
    CRLF-plus-whitespace is not data, it is removed on read. This
    returns the unfolded content plus a parallel list mapping every
    character in it back to its original (line, column), so later
    errors can report a real position instead of a position in the
    unfolded copy.
    """
    out_chars: list[str] = []
    positions: list[_Pos] = []

    line = 1
    column = 1
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
            if i + 2 < n and text[i + 2] in (" ", "\t"):
                i += 3
                line += 1
                column = 2
                continue
            out_chars.append("\n")
            positions.append(_Pos(line, column))
            i += 2
            line += 1
            column = 1
            continue
        if ch == "\n":
            if i + 1 < n and text[i + 1] in (" ", "\t"):
                i += 2
                line += 1
                column = 2
                continue
            out_chars.append("\n")
            positions.append(_Pos(line, column))
            i += 1
            line += 1
            column = 1
            continue
        out_chars.append(ch)
        positions.append(_Pos(line, column))
        i += 1
        column += 1

    return "".join(out_chars), positions


def _split_logical_lines(
    content: str, positions: list[_Pos]
) -> list[tuple[str, list[_Pos]]]:
    lines: list[tuple[str, list[_Pos]]] = []
    start = 0
    for i, ch in enumerate(content):
        if ch == "\n":
            lines.append((content[start:i], positions[start:i]))
            start = i + 1
    if start < len(content):
        lines.append((content[start:], positions[start:]))
    return lines


def _parse_property(
    text: str, pos: list[_Pos]
) -> tuple[str, dict[str, str], str, list[_Pos]]:
    """Split one unfolded logical line into (name, params, raw_value, value_pos).

    raw_value is still escaped (backslash sequences intact); callers
    decide whether to unescape it, since not every property is TEXT.
    """
    n = len(text)
    i = 0
    while i < n and text[i] not in (";", ":"):
        i += 1
    if i == 0:
        p = pos[0]
        raise ICSParseError("empty property name", p.line, p.column)
    if i == n:
        p = pos[-1]
        raise ICSParseError("property line has no ':' separator", p.line, p.column + 1)
    name = text[:i]

    params: dict[str, str] = {}
    while i < n and text[i] == ";":
        i += 1
        start = i
        while i < n and text[i] not in ("=", ":", ";"):
            i += 1
        if i >= n or text[i] != "=":
            p = pos[start] if start < n else pos[-1]
            raise ICSParseError("expected '=' in parameter", p.line, p.column)
        pname = text[start:i]
        i += 1  # skip '='
        if i < n and text[i] == '"':
            i += 1
            start = i
            while i < n and text[i] != '"':
                i += 1
            if i >= n:
                p = pos[start - 1]
                raise ICSParseError("unterminated quoted parameter value", p.line, p.column)
            pvalue = text[start:i]
            i += 1  # skip closing quote
        else:
            start = i
            while i < n and text[i] not in (";", ":"):
                i += 1
            pvalue = text[start:i]
        params[pname] = pvalue

    if i >= n or text[i] != ":":
        p = pos[i] if i < n else pos[-1]
        raise ICSParseError("expected ':' before property value", p.line, p.column)
    i += 1  # skip ':'
    raw_value = text[i:]
    value_pos = pos[i:]
    return name, params, raw_value, value_pos


def unescape_text(value: str, pos: list[_Pos]) -> str:
    """Undo RFC 5545 TEXT escaping (\\, \\;, \\,, \\n)."""
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\":
            if i + 1 >= n:
                p = pos[i]
                raise ICSParseError(
                    "trailing backslash with nothing to escape", p.line, p.column
                )
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt == "\\":
                out.append("\\")
            elif nxt == ";":
                out.append(";")
            elif nxt == ",":
                out.append(",")
            else:
                p = pos[i]
                raise ICSParseError(f"unknown escape sequence '\\{nxt}'", p.line, p.column)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def escape_text(value: str) -> str:
    """Apply RFC 5545 TEXT escaping, for writing values back out."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_line(line: str, limit: int = 75) -> str:
    """Fold one unfolded content line to RFC 5545's 75-octet-per-line limit.

    The limit is on octets, not characters, so this works in UTF-8 bytes
    and backs off a split point that would fall inside a multi-byte
    character. Continuation lines start with a single space, which
    itself counts against the 75, so each one after the first can only
    hold 74 octets of content.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= limit:
        return line

    chunks: list[str] = []
    start = 0
    n = len(encoded)
    chunk_limit = limit
    while start < n:
        end = min(start + chunk_limit, n)
        while end > start and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
        chunk_limit = limit - 1  # leave room for the leading space
    return "\r\n ".join(chunks)


def _validate_date_time(
    prop_name: str, value: str, value_pos: list[_Pos], line_pos: list[_Pos]
) -> None:
    """Check a DTSTART/DTEND value against RFC 5545's DATE / DATE-TIME grammar.

    DATE is YYYYMMDD; DATE-TIME is DATE "T" HHMMSS, optionally suffixed
    with "Z" for UTC. This only confirms the value names a real calendar
    date (and, for DATE-TIME, a real time of day) - it doesn't resolve a
    TZID against a VTIMEZONE, that's a separate piece of work.
    """

    def fail(msg: str, at: int) -> None:
        p = value_pos[min(at, len(value_pos) - 1)] if value_pos else line_pos[-1]
        raise ICSParseError(f"{prop_name}: {msg}", p.line, p.column)

    if not value:
        fail("value is empty", 0)

    is_utc = value.endswith("Z")
    body = value[:-1] if is_utc else value

    if "T" in body:
        date_part, _, time_part = body.partition("T")
    else:
        date_part, time_part = body, None
        if is_utc:
            fail("'Z' suffix needs a time-of-day (DATE-TIME), not a bare DATE", 0)

    if len(date_part) != 8 or not date_part.isdigit():
        fail("date must be 8 digits (YYYYMMDD)", 0)

    year = int(date_part[0:4])
    month = int(date_part[4:6])
    day = int(date_part[6:8])
    if not 1 <= month <= 12:
        fail(f"month {month:02d} is not between 01 and 12", 4)
    try:
        days_in_month = calendar.monthrange(year, month)[1]
    except ValueError:
        fail(f"year {year:04d} is out of range (must be 0001-9999)", 0)
    if not 1 <= day <= days_in_month:
        fail(f"day {day:02d} is not valid for {year:04d}-{month:02d}", 6)

    if time_part is not None:
        if len(time_part) != 6 or not time_part.isdigit():
            fail("time-of-day must be 6 digits (HHMMSS)", 9)
        hour = int(time_part[0:2])
        minute = int(time_part[2:4])
        second = int(time_part[4:6])
        if not 0 <= hour <= 23:
            fail(f"hour {hour:02d} is not between 00 and 23", 9)
        if not 0 <= minute <= 59:
            fail(f"minute {minute:02d} is not between 00 and 59", 11)
        if not 0 <= second <= 60:  # 60 is allowed, for leap seconds
            fail(f"second {second:02d} is not between 00 and 60", 13)


def parse_calendar(text: str) -> list[Event]:
    content, positions = _unfold(text)
    logical_lines = _split_logical_lines(content, positions)

    events: list[Event] = []
    current: Event | None = None
    seen_vcalendar = False
    stack: list[str] = []
    last_line = 1

    for line_text, line_pos in logical_lines:
        if not line_text:
            continue
        last_line = line_pos[0].line
        name, _params, raw_value, value_pos = _parse_property(line_text, line_pos)
        upper = name.upper()

        if upper == "BEGIN":
            block = raw_value.strip().upper()
            stack.append(block)
            if block == "VCALENDAR":
                seen_vcalendar = True
            elif block == "VEVENT":
                current = Event(line=line_pos[0].line)
            continue

        if upper == "END":
            block = raw_value.strip().upper()
            if not stack or stack[-1] != block:
                p = line_pos[0]
                expected = stack[-1] if stack else "nothing (no block is open)"
                raise ICSParseError(
                    f"unmatched END:{block}, expected END:{expected}", p.line, p.column
                )
            stack.pop()
            if block == "VEVENT" and current is not None:
                events.append(current)
                current = None
            continue

        if current is None:
            continue  # calendar-level property (PRODID, VERSION, ...); not needed yet

        if upper == "UID":
            current.uid = unescape_text(raw_value, value_pos)
        elif upper == "SUMMARY":
            current.summary = unescape_text(raw_value, value_pos)
        elif upper == "DTSTART":
            _validate_date_time("DTSTART", raw_value, value_pos, line_pos)
            current.start = raw_value
        elif upper == "DTEND":
            _validate_date_time("DTEND", raw_value, value_pos, line_pos)
            current.end = raw_value
        elif upper == "LOCATION":
            current.location = unescape_text(raw_value, value_pos)
        elif upper == "DESCRIPTION":
            current.description = unescape_text(raw_value, value_pos)

    if not seen_vcalendar:
        raise ICSParseError("missing BEGIN:VCALENDAR", 1, 1)
    if stack:
        raise ICSParseError(
            f"unclosed BEGIN:{stack[-1]}, reached end of file", last_line, 1
        )

    return events
