"""Turn raw PDF rows into validated Transaction records."""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import defaultdict

from pydantic import ValidationError

from ingest.models import FilingIndexEntry, RawRow, Transaction, ValidationDrop

log = logging.getLogger(__name__)

LATE_FILING_DAYS = 45

_OWNERS = {"SP": "SP", "DC": "DC", "JT": "JT"}

_TRANSACTION_TYPES = {
    "p": "Purchase",
    "s": "Sale (Full)",
    "s (partial)": "Sale (Partial)",
    "e": "Exchange",
}

_MONEY = r"\$?\s*([\d,]+(?:\.\d+)?)"
_RANGE_RE = re.compile(rf"^{_MONEY}\s*[-‐-―]\s*{_MONEY}$")
_EXACT_RE = re.compile(rf"^{_MONEY}$")
_OVER_RE = re.compile(rf"(?:over|greater than)\s*{_MONEY}", re.I)
_PLUS_RE = re.compile(rf"^{_MONEY}\s*\+$")
_OR_LESS_RE = re.compile(rf"^{_MONEY}\s*or\s*less$", re.I)
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


class NormalizeError(ValueError):
    """A raw row could not be normalized into the data contract."""


def _money(text: str) -> int:
    return int(round(float(text.replace(",", ""))))


def parse_amount(label: str) -> tuple[int, int]:
    """Map an amount cell to (low, high) in whole dollars.

    House PTRs use fixed brackets, but three other shapes occur in practice:
    an open-ended spouse/dependent-child bracket ("Spouse/DC Over
    $1,000,000"), an open-ended top bracket ("$50,000,000 +"), and an exact
    figure a filer typed instead of a bracket ("$2,722.50"). Open-ended
    brackets have no stated ceiling, and the contract types both bounds as
    int, so they get high == low; `amount_range_label` keeps the verbatim text
    and is the authoritative value for those rows.
    """
    text = " ".join(label.split())
    if not text:
        raise NormalizeError("empty amount")

    match = _RANGE_RE.match(text)
    if match:
        return _money(match.group(1)), _money(match.group(2))

    match = _OR_LESS_RE.match(text)
    if match:
        return 0, _money(match.group(1))

    match = _PLUS_RE.match(text)
    if match:
        low = _money(match.group(1))
        return low, low

    match = _OVER_RE.search(text)
    if match:
        low = _money(match.group(1)) + 1
        return low, low

    match = _EXACT_RE.match(text)
    if match:
        value = _money(match.group(1))
        return value, value

    raise NormalizeError(f"unrecognized amount {label!r}")


def parse_us_date(text: str) -> str:
    match = _US_DATE_RE.match(text.strip())
    if not match:
        raise NormalizeError(f"unrecognized date {text!r}")
    month, day, year = (int(g) for g in match.groups())
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError as exc:
        raise NormalizeError(f"invalid date {text!r}: {exc}") from exc


def normalize_owner(raw: str | None) -> str:
    """Blank owner means the filer's own holding; anything unexpected is unknown."""
    if raw is None or not raw.strip():
        return "self"
    return _OWNERS.get(raw.strip().upper(), "unknown")


def normalize_transaction_type(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise NormalizeError("missing transaction type")
    key = " ".join(raw.split()).lower()
    if key not in _TRANSACTION_TYPES:
        raise NormalizeError(f"unrecognized transaction type {raw!r}")
    return _TRANSACTION_TYPES[key]


def normalize_row(row: RawRow, entry: FilingIndexEntry) -> Transaction:
    """Build one validated Transaction. Raises on anything that cannot be trusted."""
    if not row.transaction_date:
        raise NormalizeError("missing transaction date")
    transaction_date = parse_us_date(row.transaction_date)
    low, high = parse_amount(row.amount or "")

    filed = dt.date.fromisoformat(entry.filing_date)
    traded = dt.date.fromisoformat(transaction_date)
    days_to_file = (filed - traded).days

    try:
        return Transaction(
            id=f"house_{entry.doc_id}_t{row.row_index}",
            doc_id=entry.doc_id,
            doc_url=entry.pdf_url,
            filer_name=entry.filer_name,
            transaction_date=transaction_date,
            filing_date=entry.filing_date,
            owner=normalize_owner(row.owner),
            ticker=None,
            asset_name=row.asset_name,
            asset_type=row.asset_type,
            transaction_type=normalize_transaction_type(row.transaction_type),
            amount_range_low=low,
            amount_range_high=high,
            amount_range_label=" ".join((row.amount or "").split()),
            days_to_file=days_to_file,
            is_late=days_to_file > LATE_FILING_DAYS,
            comment=row.comment,
            row_index=row.row_index,
        )
    except ValidationError as exc:
        raise NormalizeError(str(exc)) from exc


def normalize_filing(
    rows: list[RawRow], entry: FilingIndexEntry
) -> tuple[list[Transaction], list[ValidationDrop]]:
    """Normalize every row of one filing, dropping (never coercing) bad rows."""
    kept: list[Transaction] = []
    dropped: list[ValidationDrop] = []
    for row in rows:
        try:
            kept.append(normalize_row(row, entry))
        except (NormalizeError, ValidationError) as exc:
            reason = " ".join(str(exc).split())[:300]
            log.warning("dropped %s row %d: %s", entry.doc_id, row.row_index, reason)
            dropped.append(
                ValidationDrop(doc_id=entry.doc_id, row_index=row.row_index, reason=reason)
            )
    return kept, dropped


def deduplicate(transactions: list[Transaction]) -> list[Transaction]:
    """Collapse exact re-parses of the same (doc_id, row_index)."""
    seen: dict[tuple[str, int], Transaction] = {}
    for txn in transactions:
        seen.setdefault((txn.doc_id, txn.row_index), txn)
    return list(seen.values())


def flag_amendments(transactions: list[Transaction]) -> list[Transaction]:
    """Mark transactions that recur across different filings.

    Filings get amended and refiled and the source provides no transaction ID,
    so the same trade can legitimately appear under two doc_ids. Both records
    are kept; the flag tells a consumer they may be the same underlying trade.
    """
    groups: dict[tuple[str, str, str | None, str], set[str]] = defaultdict(set)
    for txn in transactions:
        key = (txn.filer_name, txn.transaction_date, txn.ticker, txn.amount_range_label)
        groups[key].add(txn.doc_id)

    out: list[Transaction] = []
    for txn in transactions:
        key = (txn.filer_name, txn.transaction_date, txn.ticker, txn.amount_range_label)
        flag = len(groups[key]) > 1
        out.append(txn.model_copy(update={"possible_amendment": flag}) if flag else txn)
    return out


def sort_key(txn: Transaction) -> tuple:
    """Stable output ordering, independent of filesystem or network order."""
    return (txn.doc_id, txn.row_index)
