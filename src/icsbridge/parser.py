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
import datetime
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


@dataclass
class _Observance:
    """One STANDARD or DAYLIGHT sub-component of a VTIMEZONE.

    dtstart is the local wall-clock time (y, m, d, hh, mm, ss) this
    observance's rule set starts from. rrule is None for a single
    one-off transition (the usual shape for the offset a zone had
    before it adopted DST rules); otherwise it holds the parsed
    year/month/weekday rule used to generate a transition per year.
    """

    dtstart: tuple[int, int, int, int, int, int]
    offset_from: int
    offset_to: int
    rrule: dict | None


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
) -> tuple[str, dict[str, tuple[str, _Pos]], str, list[_Pos]]:
    """Split one unfolded logical line into (name, params, raw_value, value_pos).

    raw_value is still escaped (backslash sequences intact); callers
    decide whether to unescape it, since not every property is TEXT.
    Each param maps to (value, position of the value's first character),
    so callers that reject a bad param value (e.g. an undefined TZID)
    can point at it instead of at the start of the line.
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

    params: dict[str, tuple[str, _Pos]] = {}
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
            pvalue_pos = pos[start] if start < n else pos[-1]
            i += 1  # skip closing quote
        else:
            start = i
            while i < n and text[i] not in (";", ":"):
                i += 1
            pvalue = text[start:i]
            pvalue_pos = pos[start] if start < n else pos[-1]
        params[pname] = (pvalue, pvalue_pos)

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


def _validate_utc_offset(
    prop_name: str, value: str, value_pos: list[_Pos], line_pos: list[_Pos]
) -> None:
    """Check a TZOFFSETFROM/TZOFFSETTO value against RFC 5545's utc-offset grammar.

    utc-offset = ("+" / "-") time-hour time-minute [time-second]
    e.g. "-0500", "+0530", "+013000".
    """

    def fail(msg: str, at: int = 0) -> None:
        p = value_pos[min(at, len(value_pos) - 1)] if value_pos else line_pos[-1]
        raise ICSParseError(f"{prop_name}: {msg}", p.line, p.column)

    if not value:
        fail("value is empty")
    if value[0] not in ("+", "-"):
        fail("utc-offset must start with '+' or '-'")

    digits = value[1:]
    if len(digits) not in (4, 6) or not digits.isdigit():
        fail("utc-offset must be +/-HHMM or +/-HHMMSS")

    hour = int(digits[0:2])
    minute = int(digits[2:4])
    if hour > 23:
        fail(f"offset hour {hour:02d} is not between 00 and 23", 1)
    if minute > 59:
        fail(f"offset minute {minute:02d} is not between 00 and 59", 3)
    if len(digits) == 6:
        second = int(digits[4:6])
        if second > 59:
            fail(f"offset second {second:02d} is not between 00 and 59", 5)


_WEEKDAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _parse_datetime_components(value: str) -> tuple[int, int, int, int, int, int]:
    """Split an already-validated DATE-TIME value into its components.

    Leap seconds (:60) are folded into :59; this tool converts wall-clock
    times between offsets, it doesn't model leap seconds.
    """
    date_part, _, time_part = value.partition("T")
    year = int(date_part[0:4])
    month = int(date_part[4:6])
    day = int(date_part[6:8])
    hour = int(time_part[0:2])
    minute = int(time_part[2:4])
    second = int(time_part[4:6])
    if second == 60:
        second = 59
    return year, month, day, hour, minute, second


def _offset_to_seconds(value: str) -> int:
    """Convert an already-validated utc-offset value (e.g. '-0500') to seconds."""
    sign = 1 if value[0] == "+" else -1
    digits = value[1:]
    hour = int(digits[0:2])
    minute = int(digits[2:4])
    second = int(digits[4:6]) if len(digits) == 6 else 0
    return sign * (hour * 3600 + minute * 60 + second)


def _to_epoch_seconds(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    return datetime.date(year, month, day).toordinal() * 86400 + hour * 3600 + minute * 60 + second


def _from_epoch_seconds(total: int) -> tuple[int, int, int, int, int, int]:
    ordinal, remainder = divmod(total, 86400)
    date = datetime.date.fromordinal(ordinal)
    hour, remainder = divmod(remainder, 3600)
    minute, second = divmod(remainder, 60)
    return date.year, date.month, date.day, hour, minute, second


def _parse_byday(value: str, pos: _Pos) -> tuple[int, int]:
    """Parse an RRULE BYDAY value like '1SU' or '-1SU' into (ordinal, weekday).

    weekday follows datetime.date.weekday() numbering (Monday=0..Sunday=6).
    A bare weekday code with no ordinal (e.g. just 'SU') isn't enough to
    place a single yearly transition, so that's rejected rather than guessed at.
    """
    s = value.strip()
    i = 0
    sign = 1
    if i < len(s) and s[i] in "+-":
        sign = -1 if s[i] == "-" else 1
        i += 1
    digit_start = i
    while i < len(s) and s[i].isdigit():
        i += 1
    if i == digit_start:
        raise ICSParseError(
            f"RRULE: BYDAY '{value}' needs an ordinal week number for VTIMEZONE use",
            pos.line,
            pos.column,
        )
    ordinal = sign * int(s[digit_start:i])
    code = s[i:]
    if code not in _WEEKDAY_CODES:
        raise ICSParseError(
            f"RRULE: BYDAY '{value}' has an unknown weekday code '{code}'", pos.line, pos.column
        )
    return ordinal, _WEEKDAY_CODES[code]


def _parse_vtimezone_rrule(
    raw_value: str, pos: _Pos, obs_dtstart: tuple[int, int, int, int, int, int]
) -> dict:
    """Parse the subset of RRULE that real VTIMEZONE producers actually use:
    FREQ=YEARLY, an optional BYMONTH, an optional BYDAY ordinal weekday, and
    an optional UNTIL bound. Anything else (COUNT, WEEKLY/MONTHLY/DAILY
    frequencies, multiple BYDAY values) isn't something we can turn into a
    yearly transition date, so it's rejected instead of silently misread.
    """
    fields: dict[str, str] = {}
    for part in raw_value.split(";"):
        if not part:
            continue
        key, sep, val = part.partition("=")
        if not sep:
            raise ICSParseError(f"RRULE: malformed rule part '{part}'", pos.line, pos.column)
        fields[key.upper()] = val

    if fields.get("FREQ") != "YEARLY":
        raise ICSParseError(
            "RRULE: only FREQ=YEARLY is understood for VTIMEZONE observances",
            pos.line,
            pos.column,
        )

    bymonth = fields.get("BYMONTH")
    month = int(bymonth) if bymonth else obs_dtstart[1]
    if not 1 <= month <= 12:
        raise ICSParseError(f"RRULE: BYMONTH {month} is not between 1 and 12", pos.line, pos.column)

    byday = fields.get("BYDAY")
    if byday:
        if "," in byday:
            raise ICSParseError(
                "RRULE: multiple BYDAY values are not supported for VTIMEZONE observances",
                pos.line,
                pos.column,
            )
        ordinal, weekday = _parse_byday(byday, pos)
    else:
        ordinal, weekday = None, None

    until = None
    if "UNTIL" in fields:
        u = fields["UNTIL"]
        if len(u) < 8 or not u[:8].isdigit():
            raise ICSParseError(f"RRULE: UNTIL '{u}' is not a valid date", pos.line, pos.column)
        until = (int(u[0:4]), int(u[4:6]), int(u[6:8]))

    return {"month": month, "ordinal": ordinal, "weekday": weekday, "until": until}


def _nth_weekday_of_month(year: int, month: int, weekday: int, ordinal: int) -> tuple[int, int, int]:
    """Find the date of the ordinal-th weekday in a month (ordinal < 0 counts from the end)."""
    days_in_month = calendar.monthrange(year, month)[1]
    if ordinal > 0:
        first_weekday = datetime.date(year, month, 1).weekday()
        offset = (weekday - first_weekday) % 7
        day = 1 + offset + (ordinal - 1) * 7
    else:
        last_weekday = datetime.date(year, month, days_in_month).weekday()
        offset = (last_weekday - weekday) % 7
        day = days_in_month - offset + (ordinal + 1) * 7
    day = min(max(day, 1), days_in_month)
    return year, month, day


def _observance_transition(
    obs: _Observance, year: int
) -> tuple[int, int, int, int, int, int] | None:
    """The local wall-clock instant this observance's rule transitions at in a
    given year, or None if the rule doesn't produce a transition that year.

    A non-recurring observance (rrule is None) is a single fixed instant -
    it doesn't belong to any particular year, so it's returned unconditionally
    and left to the caller to compare against the target instant. That's what
    makes a zone with just one STANDARD observance and no RRULE (common for
    zones with no DST) resolve correctly for dates decades after that instant.
    """
    _, _, _, hour, minute, second = obs.dtstart
    if obs.rrule is None:
        return obs.dtstart
    if year < obs.dtstart[0]:
        return None
    month = obs.rrule["month"]
    ordinal = obs.rrule["ordinal"]
    if ordinal is not None:
        y, m, d = _nth_weekday_of_month(year, month, obs.rrule["weekday"], ordinal)
    else:
        y, m, d = year, month, obs.dtstart[2]
    until = obs.rrule["until"]
    if until is not None and (y, m, d) > until:
        return None
    return (y, m, d, hour, minute, second)


def _resolve_offset_at(
    observances: list[_Observance], target: tuple[int, int, int, int, int, int]
) -> int:
    """The UTC offset (in seconds) in effect for a local wall-clock time.

    This is the offset given by the most recent transition at or before
    target, across every STANDARD/DAYLIGHT observance for the zone. If
    target predates every observance (e.g. an event scheduled before the
    zone's earliest recorded rule change), the earliest observance's
    starting offset applies.
    """
    year = target[0]
    candidates: list[tuple[tuple[int, int, int, int, int, int], int]] = []
    for obs in observances:
        for y in (year - 1, year):
            transition = _observance_transition(obs, y)
            if transition is not None and transition <= target:
                candidates.append((transition, obs.offset_to))
    if not candidates:
        earliest = min(observances, key=lambda o: o.dtstart)
        return earliest.offset_from
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


def _resolve_local_to_utc(value: str, observances: list[_Observance]) -> str:
    """Convert a local DTSTART/DTEND value to a 'Z'-suffixed UTC value, using
    the zone's VTIMEZONE observances to pick the offset that applies on that
    actual date. A DATE-only value (no time of day) has nothing to offset,
    so it passes through unchanged.
    """
    if "T" not in value:
        return value
    year, month, day, hour, minute, second = _parse_datetime_components(value)
    offset = _resolve_offset_at(observances, (year, month, day, hour, minute, second))
    epoch = _to_epoch_seconds(year, month, day, hour, minute, second) - offset
    uy, um, ud, uh, umin, us = _from_epoch_seconds(epoch)
    return f"{uy:04d}{um:02d}{ud:02d}T{uh:02d}{umin:02d}{us:02d}Z"


def _resolve_tzid_param(
    prop_name: str,
    params: dict[str, tuple[str, _Pos]],
    timezones: dict[str, list[_Observance]],
) -> str | None:
    """Look up a DTSTART/DTEND TZID param against the VTIMEZONEs seen so far.

    VTIMEZONE blocks must appear before any VEVENT that references them.
    RFC 5545 doesn't spell that ordering out as a MUST, but every real
    producer writes it that way, and this parser makes a single pass, so
    a TZID that shows up only later in the file is reported as undefined.
    """
    if "TZID" not in params:
        return None
    tzid, tz_pos = params["TZID"]
    if tzid not in timezones:
        raise ICSParseError(
            f"{prop_name}: TZID '{tzid}' is not defined by any VTIMEZONE in "
            "this file (VTIMEZONE blocks must come before the VEVENTs that use them)",
            tz_pos.line,
            tz_pos.column,
        )
    return tzid


def parse_calendar(text: str) -> list[Event]:
    content, positions = _unfold(text)
    logical_lines = _split_logical_lines(content, positions)

    events: list[Event] = []
    current: Event | None = None
    seen_vcalendar = False
    stack: list[str] = []
    last_line = 1

    # VTIMEZONE state. RFC 5545 requires a TZID and at least one
    # STANDARD/DAYLIGHT sub-component per VTIMEZONE; these track the block
    # currently being parsed, and each finished VTIMEZONE's observances are
    # kept so DTSTART/DTEND TZID references can be resolved to a real offset.
    timezones: dict[str, list[_Observance]] = {}
    pending_tzid: str | None = None
    pending_tzid_line = 0
    pending_observances: list[_Observance] = []
    pending_obs: dict | None = None
    pending_obs_line = 0

    for line_text, line_pos in logical_lines:
        if not line_text:
            continue
        last_line = line_pos[0].line
        name, params, raw_value, value_pos = _parse_property(line_text, line_pos)
        upper = name.upper()

        if upper == "BEGIN":
            block = raw_value.strip().upper()
            stack.append(block)
            if block == "VCALENDAR":
                seen_vcalendar = True
            elif block == "VEVENT":
                current = Event(line=line_pos[0].line)
            elif block == "VTIMEZONE":
                pending_tzid = None
                pending_tzid_line = line_pos[0].line
                pending_observances = []
            elif (
                block in ("STANDARD", "DAYLIGHT")
                and len(stack) >= 2
                and stack[-2] == "VTIMEZONE"
            ):
                pending_obs = {"dtstart": None, "offset_from": None, "offset_to": None}
                pending_obs_line = line_pos[0].line
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
            elif block == "VTIMEZONE":
                if pending_tzid is None:
                    raise ICSParseError(
                        "VTIMEZONE has no TZID property", pending_tzid_line, 1
                    )
                if not pending_observances:
                    raise ICSParseError(
                        "VTIMEZONE has no STANDARD or DAYLIGHT component",
                        pending_tzid_line,
                        1,
                    )
                if pending_tzid in timezones:
                    raise ICSParseError(
                        f"duplicate VTIMEZONE for TZID '{pending_tzid}'",
                        pending_tzid_line,
                        1,
                    )
                timezones[pending_tzid] = pending_observances
                pending_tzid = None
            elif block in ("STANDARD", "DAYLIGHT"):
                missing = [
                    prop_name
                    for key, prop_name in (
                        ("dtstart", "DTSTART"),
                        ("offset_from", "TZOFFSETFROM"),
                        ("offset_to", "TZOFFSETTO"),
                    )
                    if pending_obs is None or pending_obs.get(key) is None
                ]
                if missing:
                    raise ICSParseError(
                        f"{block} is missing required propert"
                        f"{'y' if len(missing) == 1 else 'ies'} {', '.join(missing)}",
                        pending_obs_line,
                        1,
                    )
                rrule = None
                if pending_obs.get("rrule_raw") is not None:
                    rrule = _parse_vtimezone_rrule(
                        pending_obs["rrule_raw"], pending_obs["rrule_pos"], pending_obs["dtstart"]
                    )
                pending_observances.append(
                    _Observance(
                        dtstart=pending_obs["dtstart"],
                        offset_from=pending_obs["offset_from"],
                        offset_to=pending_obs["offset_to"],
                        rrule=rrule,
                    )
                )
                pending_obs = None
            continue

        if current is None:
            if stack and stack[-1] == "VTIMEZONE" and upper == "TZID":
                if pending_tzid is not None:
                    p = line_pos[0]
                    raise ICSParseError(
                        "VTIMEZONE has more than one TZID property", p.line, p.column
                    )
                tzid = unescape_text(raw_value, value_pos).strip()
                if not tzid:
                    p = line_pos[0]
                    raise ICSParseError("TZID value is empty", p.line, p.column)
                pending_tzid = tzid
            elif stack and stack[-1] in ("STANDARD", "DAYLIGHT") and pending_obs is not None:
                if upper == "DTSTART":
                    _validate_date_time("DTSTART", raw_value, value_pos, line_pos)
                    if raw_value.endswith("Z"):
                        p = value_pos[-1] if value_pos else line_pos[-1]
                        raise ICSParseError(
                            f"{stack[-1]} DTSTART must be a local time, not UTC ('Z')",
                            p.line,
                            p.column,
                        )
                    if "T" not in raw_value:
                        p = value_pos[0] if value_pos else line_pos[0]
                        raise ICSParseError(
                            f"{stack[-1]} DTSTART must include a time of day", p.line, p.column
                        )
                    pending_obs["dtstart"] = _parse_datetime_components(raw_value)
                elif upper in ("TZOFFSETFROM", "TZOFFSETTO"):
                    _validate_utc_offset(upper, raw_value, value_pos, line_pos)
                    key = "offset_from" if upper == "TZOFFSETFROM" else "offset_to"
                    pending_obs[key] = _offset_to_seconds(raw_value)
                elif upper == "RRULE":
                    pending_obs["rrule_raw"] = raw_value
                    pending_obs["rrule_pos"] = value_pos[0] if value_pos else line_pos[0]
            continue  # calendar-level property (PRODID, VERSION, ...); not needed yet

        if upper == "UID":
            current.uid = unescape_text(raw_value, value_pos)
        elif upper == "SUMMARY":
            current.summary = unescape_text(raw_value, value_pos)
        elif upper == "DTSTART":
            tzid = _resolve_tzid_param("DTSTART", params, timezones)
            _validate_date_time("DTSTART", raw_value, value_pos, line_pos)
            if tzid and raw_value.endswith("Z"):
                p = value_pos[-1] if value_pos else line_pos[-1]
                raise ICSParseError(
                    "DTSTART: TZID param cannot be combined with a UTC ('Z') value",
                    p.line,
                    p.column,
                )
            current.start = (
                _resolve_local_to_utc(raw_value, timezones[tzid]) if tzid else raw_value
            )
        elif upper == "DTEND":
            tzid = _resolve_tzid_param("DTEND", params, timezones)
            _validate_date_time("DTEND", raw_value, value_pos, line_pos)
            if tzid and raw_value.endswith("Z"):
                p = value_pos[-1] if value_pos else line_pos[-1]
                raise ICSParseError(
                    "DTEND: TZID param cannot be combined with a UTC ('Z') value",
                    p.line,
                    p.column,
                )
            current.end = (
                _resolve_local_to_utc(raw_value, timezones[tzid]) if tzid else raw_value
            )
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
