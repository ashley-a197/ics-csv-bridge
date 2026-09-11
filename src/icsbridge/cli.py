"""Command line entry point: convert between .ics and .csv calendar files.

    icsbridge to-csv events.ics -o events.csv
    icsbridge to-ics events.csv -o events.ics
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

from .parser import ICSParseError, escape_text, fold_line, parse_calendar

FIELDS = ["uid", "summary", "start", "end", "location", "description"]
REQUIRED_CSV_FIELDS = ("uid", "summary", "start")


class CsvFormatError(Exception):
    pass


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _write_text(path: str, text: str) -> None:
    if path == "-":
        sys.stdout.write(text)
        return
    Path(path).write_text(text, encoding="utf-8")


def ics_to_csv(text: str) -> str:
    events = parse_calendar(text)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    writer.writeheader()
    for event in events:
        writer.writerow(
            {
                "uid": event.uid,
                "summary": event.summary,
                "start": event.start,
                "end": event.end,
                "location": event.location,
                "description": event.description,
            }
        )
    return buffer.getvalue()


def csv_to_ics(text: str, prodid: str = "-//ics-csv-bridge//EN") -> str:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvFormatError("CSV file has no header row")
    missing = [f for f in REQUIRED_CSV_FIELDS if f not in reader.fieldnames]
    if missing:
        raise CsvFormatError(
            f"CSV header is missing required column(s): {', '.join(missing)}"
        )

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", fold_line(f"PRODID:{prodid}")]
    for row_number, row in enumerate(reader, start=2):  # header is row 1
        uid = (row.get("uid") or "").strip()
        summary = (row.get("summary") or "").strip()
        start = (row.get("start") or "").strip()
        if not uid:
            raise CsvFormatError(f"row {row_number}: 'uid' is required but empty")
        if not start:
            raise CsvFormatError(f"row {row_number}: 'start' is required but empty")

        lines.append("BEGIN:VEVENT")
        lines.append(fold_line(f"UID:{escape_text(uid)}"))
        lines.append(fold_line(f"SUMMARY:{escape_text(summary)}"))
        lines.append(fold_line(f"DTSTART:{start}"))

        end = (row.get("end") or "").strip()
        if end:
            lines.append(fold_line(f"DTEND:{end}"))
        location = (row.get("location") or "").strip()
        if location:
            lines.append(fold_line(f"LOCATION:{escape_text(location)}"))
        description = (row.get("description") or "").strip()
        if description:
            lines.append(fold_line(f"DESCRIPTION:{escape_text(description)}"))

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="icsbridge",
        description="Convert calendar events between iCalendar (.ics) and CSV.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    to_csv = sub.add_parser("to-csv", help="convert an .ics file to .csv")
    to_csv.add_argument("input", help="path to .ics file, or '-' for stdin")
    to_csv.add_argument("-o", "--output", default="-", help="output path, or '-' for stdout")

    to_ics = sub.add_parser("to-ics", help="convert a .csv file to .ics")
    to_ics.add_argument("input", help="path to .csv file, or '-' for stdin")
    to_ics.add_argument("-o", "--output", default="-", help="output path, or '-' for stdout")

    args = parser.parse_args(argv)
    source_name = args.input if args.input != "-" else "<stdin>"

    try:
        text = _read_text(args.input)
        if args.command == "to-csv":
            result = ics_to_csv(text)
        else:
            result = csv_to_ics(text)
    except ICSParseError as exc:
        print(f"{source_name}:{exc.line}:{exc.column}: {exc.message}", file=sys.stderr)
        return 1
    except CsvFormatError as exc:
        print(f"{source_name}: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"{source_name}: no such file", file=sys.stderr)
        return 1

    _write_text(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
