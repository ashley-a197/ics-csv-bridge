# ics-csv-bridge

Convert calendar events between iCalendar (`.ics`) and CSV.

The usual reason you want this: someone hands you a `.ics` export from
Google Calendar or Outlook and you want to review or bulk-edit the
events in a spreadsheet, then hand back a valid `.ics`. Every general
"ics library" out there is really a full RFC 5545 implementation
(timezones, recurrence, alarms, the works). This is the opposite: a
small, dependency-free tool that handles the common case (events with
a summary, start/end time, location, description) and, when it hits
something it can't parse, tells you exactly where in the file the
problem is instead of a generic "invalid ICS" exception.

No third-party dependencies. Standard library only.

## Install

```sh
pip install -e .
```

This gives you an `icsbridge` command. You can also run it without
installing via `python -m icsbridge.cli`.

## Usage

Convert an `.ics` file to CSV:

```sh
icsbridge to-csv events.ics -o events.csv
```

```csv
uid,summary,start,end,location,description
1@example.com,Standup,20260302T090000Z,20260302T091500Z,,Daily sync
2@example.com,Dentist,20260305T140000Z,20260305T150000Z,123 Main St,
```

Edit the CSV in a spreadsheet, then convert it back:

```sh
icsbridge to-ics events.csv -o events.ics
```

Both directions read stdin and write stdout when you pass `-`, so you
can pipe:

```sh
curl -s https://example.com/calendar.ics | icsbridge to-csv - | cut -d, -f1,2
```

The CSV columns are `uid,summary,start,end,location,description`.
`uid`, `summary`, and `start` are required; `end`, `location`, and
`description` may be left blank. `start` and `end` are passed through
as raw iCalendar date-time values (e.g. `20260302T090000Z`) rather
than reformatted, since the round trip needs to preserve whatever
precision and timezone form the original had.

## Error messages

Malformed input gets a compiler-style `file:line:column: message`
instead of a stack trace. For example, given a `.ics` file with an
unterminated escape sequence on line 7:

```
events.ics:7:34: trailing backslash with nothing to escape
```

or a block that was never closed:

```
events.ics:22:1: unclosed BEGIN:VEVENT, reached end of file
```

or a `DTSTART`/`DTEND` that isn't a real calendar date:

```
events.ics:9:9: DTSTART: day 31 is not valid for 2026-04
```

or a `DTSTART;TZID=...` that names a timezone with no matching
`VTIMEZONE` block:

```
events.ics:14:23: DTSTART: TZID 'Europe/Paris' is not defined by any VTIMEZONE in this file (VTIMEZONE blocks must come before the VEVENTs that use them)
```

The parser tracks the position of every character through RFC 5545's
line-folding (long lines split across multiple physical lines with a
leading space) so the reported line and column point at the actual
spot in the original file, not an offset into some internal unfolded
buffer.

## Current limitations

This is an early skeleton. It handles the fields listed above and
detects structural errors (bad property syntax, mismatched
`BEGIN`/`END`, bad escapes, malformed `DTSTART`/`DTEND` values).

`VTIMEZONE` blocks are checked, not just skipped: each one needs a
`TZID` and at least one `STANDARD`/`DAYLIGHT` sub-component with a
valid UTC offset, and a `DTSTART`/`DTEND` with a `TZID` param has to
reference one that was actually declared. `VTIMEZONE` blocks must
come before the events that reference them, since parsing is a single
pass over the file.

A `DTSTART`/`DTEND` with a `TZID` param is resolved against that
zone's actual offset for that specific date rather than passed
through as local time: the parser walks the zone's `STANDARD`/
`DAYLIGHT` observances (including a `YEARLY` `RRULE` with `BYMONTH`/
`BYDAY`, e.g. "the last Sunday in October", the shape real `.ics`
exporters use for DST rules) to find which offset was in effect, and
the event's start/end come out as an absolute UTC value. `RRULE`
frequencies other than `YEARLY`, and `COUNT`, aren't understood and
are rejected rather than silently mishandled.

It does not yet:

- understand `RRULE` on `VEVENT` (recurring events) or `VALARM`

See the commit history for what's been added since this was written.

## License

MIT, see `LICENSE`.
