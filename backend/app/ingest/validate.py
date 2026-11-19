"""Validate a CSV file against its manifest, row by row.

Produces typed Python values ready to load, plus a list of problems a steward can act on,
e.g. "row 112, column unit_price: 'abc' is not a decimal". Row numbers match what a
spreadsheet shows: the header is row 1, the first data row is row 2.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.ingest.manifest import Column, Manifest

MAX_ROWS = 200_000
MAX_REPORTED_ISSUES = 50  # the total is always reported; only the first N are listed
TRUE, FALSE = {"true", "t", "yes", "1"}, {"false", "f", "no", "0"}


@dataclass(frozen=True)
class Issue:
    row: int | None  # None for file-level problems
    column: str | None
    message: str

    def __str__(self) -> str:
        where = []
        if self.row is not None:
            where.append(f"row {self.row}")
        if self.column:
            where.append(f"column {self.column}")
        return f"{', '.join(where)}: {self.message}" if where else self.message


@dataclass
class ValidationResult:
    rows: list[tuple] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    issue_count: int = 0

    @property
    def ok(self) -> bool:
        return self.issue_count == 0

    def add(self, issue: Issue) -> None:
        self.issue_count += 1
        if len(self.issues) < MAX_REPORTED_ISSUES:
            self.issues.append(issue)


def _convert(col: Column, raw: str) -> object:
    """Convert one CSV cell to a typed value, or raise ValueError with a readable message."""
    match col.type:
        case "string":
            if col.allowed_values is not None and raw not in col.allowed_values:
                raise ValueError(f"{raw!r} is not one of {col.allowed_values}")
            return raw
        case "integer":
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{raw!r} is not a whole number") from None
        case "decimal":
            try:
                value = Decimal(raw)
            except InvalidOperation:
                raise ValueError(f"{raw!r} is not a decimal number") from None
            if not value.is_finite():
                raise ValueError(f"{raw!r} is not a finite number")
            return value
        case "boolean":
            low = raw.lower()
            if low in TRUE:
                return True
            if low in FALSE:
                return False
            raise ValueError(f"{raw!r} is not true/false")
        case "date":
            try:
                return date.fromisoformat(raw)
            except ValueError:
                raise ValueError(f"{raw!r} is not a date (YYYY-MM-DD)") from None
        case "timestamp":
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                raise ValueError(f"{raw!r} is not a timestamp (YYYY-MM-DDTHH:MM:SS)") from None
    raise AssertionError(col.type)


def validate_csv(manifest: Manifest, content: bytes) -> ValidationResult:
    result = ValidationResult()
    try:
        text = content.decode("utf-8-sig")  # tolerate the BOM that Excel adds
    except UnicodeDecodeError:
        result.add(Issue(None, None, "file is not UTF-8 text"))
        return result

    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    expected = [c.name for c in manifest.columns]
    if header != expected:
        missing = [c for c in expected if c not in (header or [])]
        extra = [c for c in (header or []) if c not in expected]
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        if not detail:
            detail.append("columns are in a different order than the manifest")
        result.add(Issue(1, None, "header does not match the manifest: " + "; ".join(detail)))
        return result

    pk_index = [expected.index(k) for k in manifest.primary_key]
    seen_keys: dict[tuple, int] = {}
    for row_number, raw_row in enumerate(reader, start=2):
        if row_number - 1 > MAX_ROWS:
            result.add(Issue(None, None, f"file has more than {MAX_ROWS} rows"))
            return result
        if len(raw_row) != len(expected):
            result.add(
                Issue(row_number, None, f"expected {len(expected)} values, found {len(raw_row)}")
            )
            continue
        typed: list[object] = []
        row_ok = True
        for col, raw in zip(manifest.columns, raw_row, strict=True):
            if raw == "":
                if not col.nullable:
                    result.add(Issue(row_number, col.name, "value is required"))
                    row_ok = False
                typed.append(None)
                continue
            try:
                typed.append(_convert(col, raw))
            except ValueError as e:
                result.add(Issue(row_number, col.name, str(e)))
                row_ok = False
                typed.append(None)
        if row_ok:
            key = tuple(typed[i] for i in pk_index)
            if key in seen_keys:
                result.add(
                    Issue(
                        row_number,
                        ",".join(manifest.primary_key),
                        f"duplicate key {key} (first seen on row {seen_keys[key]})",
                    )
                )
                continue
            seen_keys[key] = row_number
            result.rows.append(tuple(typed))
    if not result.rows and result.ok:
        result.add(Issue(None, None, "file has no data rows"))
    return result
