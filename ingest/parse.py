"""Reconstruct PTR transaction rows from PDF word bounding boxes.

The Clerk's PTR PDFs cannot be parsed from linear text. A single logical
transaction wraps over several visual lines, and a wrapped line routinely
carries text belonging to two different columns (an asset-name remainder at
x0~104 next to an amount remainder at x0~446). Rows also wrap across page
boundaries. So every word is assigned to a column by its x0 and rows are
assembled from those columns, never from `extract_text()` output.

Small-caps labels ("Filing Status:", "Description:") are drawn with a font
whose cmap maps every non-initial glyph to NUL, so they extract as
'F\\x00\\x00\\x00\\x00\\x00 S\\x00\\x00\\x00\\x00\\x00:'. The surviving initial plus
the run length identifies the label unambiguously; see _SMALLCAPS.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

from ingest.models import RawRow

log = logging.getLogger(__name__)

COLUMNS = (
    "id",
    "owner",
    "asset",
    "transaction_type",
    "transaction_date",
    "notification_date",
    "amount",
    "cap_gains",
)

# Fallback column left edges, measured off the Clerk's PTR template. Overridden
# per page whenever the column header row is found.
DEFAULT_COLUMN_STARTS: dict[str, float] = {
    "id": 21.0,
    "owner": 60.0,
    "asset": 100.0,
    "transaction_type": 256.0,
    "transaction_date": 321.0,
    "notification_date": 377.0,
    "amount": 442.0,
    "cap_gains": 520.0,
}

# The column header block wraps over three bold lines ("ID Owner Asset ...",
# "Type Date Gains >", "$200?"). All three must be recognised, or the trailing
# ones get appended to whatever row is still open across a page break.
_HEADER_VOCAB = {
    "ID", "Owner", "Asset", "Transaction", "Date", "Notification",
    "Amount", "Cap.", "Type", "Gains", ">", "$200?",
}

# Header word -> column it labels. 'Date' appears twice and is resolved by x0.
_HEADER_LABELS = {
    "ID": "id",
    "Owner": "owner",
    "Asset": "asset",
    "Transaction": "transaction_type",
    "Notification": "notification_date",
    "Amount": "amount",
    "Cap.": "cap_gains",
}

# (initial, total glyph count) -> the label actually printed.
_SMALLCAPS = {
    ("F", 6): "Filing",
    ("S", 7): "Status:",
    ("D", 12): "Description:",
    ("C", 9): "Comments:",
    ("S", 10): "Subholding",
    ("O", 3): "Of:",
    ("L", 9): "Location:",
}

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
ASSET_TYPE_RE = re.compile(r"\[([A-Z0-9]{1,6})\]")
LINE_TOLERANCE = 3.0
COMMENT_MAX_SIZE = 8.9  # comment rows render at 8.55pt, data rows at 9.00pt
SECTION_HEADER_MIN_SIZE = 11.0


class ParseError(Exception):
    """The filing could not be parsed into rows at all."""


def _decode_smallcaps(text: str) -> str:
    """Recover a small-caps label whose glyphs extracted as NUL."""
    if "\x00" not in text:
        return text
    known = _SMALLCAPS.get((text[0], len(text)))
    if known:
        return known
    # Unknown label: keep whatever glyphs survived rather than inventing text.
    return text.replace("\x00", "")


def _line_text(words: list[dict]) -> str:
    return " ".join(_decode_smallcaps(w["text"]) for w in sorted(words, key=lambda w: w["x0"]))


def _column_starts(header_words: list[dict]) -> dict[str, float]:
    """Derive column left edges from a header row, falling back to the template."""
    starts = dict(DEFAULT_COLUMN_STARTS)
    dates = []
    for w in header_words:
        label = _HEADER_LABELS.get(w["text"])
        if label:
            starts[label] = w["x0"]
        elif w["text"] == "Date":
            dates.append(w["x0"])
    # Two 'Date' headers: transaction date then notification date, left to right.
    for x0 in sorted(dates):
        if x0 < starts["notification_date"] - 20:
            starts["transaction_date"] = x0
        else:
            starts["notification_date"] = x0
    return starts


def _bounds(starts: dict[str, float]) -> list[tuple[str, float, float]]:
    """Half-open [lo, hi) x-ranges per column, ordered left to right."""
    ordered = sorted(COLUMNS, key=lambda c: starts[c])
    out = []
    for i, col in enumerate(ordered):
        lo = starts[col] - 4.0
        hi = (starts[ordered[i + 1]] - 4.0) if i + 1 < len(ordered) else float("inf")
        out.append((col, lo, hi))
    out[0] = (out[0][0], float("-inf"), out[0][2])
    return out


def _cells(words: list[dict], bounds: list[tuple[str, float, float]]) -> dict[str, str]:
    """Bucket a line's words into columns by x0 (all columns are left-aligned)."""
    buckets: dict[str, list[dict]] = {c: [] for c in COLUMNS}
    for w in words:
        for col, lo, hi in bounds:
            if lo <= w["x0"] < hi:
                buckets[col].append(w)
                break
    return {c: _line_text(ws) for c, ws in buckets.items()}


def _group_lines(words: list[dict]) -> list[list[dict]]:
    """Cluster words into visual lines by their top coordinate."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and w["top"] - lines[-1][0]["top"] <= LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


def _count_date_anchors(words: list[dict]) -> int:
    """Count transaction-date words on a page by a flat, stateless scan.

    Deliberately shares nothing with row assembly - no line grouping, no
    header detection, no continuation state - so that a bug in any of those
    shows up as a reconciliation mismatch instead of passing silently.
    """
    starts = DEFAULT_COLUMN_STARTS
    lo = starts["transaction_date"] - 4.0
    hi = starts["notification_date"] - 4.0
    return sum(1 for w in words if lo <= w["x0"] < hi and DATE_RE.match(w["text"]))


def _is_header_line(words: list[dict]) -> bool:
    """True for any of the bold column-header lines."""
    return all(
        w["text"] in _HEADER_VOCAB and "Bold" in w.get("fontname", "") for w in words
    )


def _defines_columns(words: list[dict]) -> bool:
    texts = {w["text"] for w in words}
    return "Owner" in texts and "Asset" in texts and "Amount" in texts


def _ends_table(words: list[dict]) -> bool:
    """True at the asset-type footnote or the next 12pt section heading."""
    first = min(words, key=lambda w: w["x0"])
    if first["text"].startswith("*"):
        return True
    return any(w.get("size", 0) >= SECTION_HEADER_MIN_SIZE for w in words)


def _append(existing: str | None, addition: str) -> str | None:
    addition = addition.strip()
    if not addition:
        return existing
    return f"{existing} {addition}".strip() if existing else addition


def _split_asset_type(asset: str) -> tuple[str, str | None]:
    """Pull the trailing [XX] asset-type token out of the asset cell."""
    match = ASSET_TYPE_RE.search(asset)
    if not match:
        return asset.strip(), None
    cleaned = ASSET_TYPE_RE.sub("", asset)
    return re.sub(r"\s{2,}", " ", cleaned).strip(), match.group(1)


class _RowBuilder:
    def __init__(self, row_index: int, page: int, cells: dict[str, str]) -> None:
        self.row_index = row_index
        self.page = page
        self.owner = cells["owner"].strip() or None
        self.asset = cells["asset"].strip()
        self.transaction_type = cells["transaction_type"].strip() or None
        self.transaction_date = cells["transaction_date"].strip() or None
        self.notification_date = cells["notification_date"].strip() or None
        self.amount = cells["amount"].strip() or None
        self.comment: str | None = None

    def extend(self, cells: dict[str, str]) -> None:
        self.asset = _append(self.asset, cells["asset"]) or ""
        self.transaction_type = _append(self.transaction_type, cells["transaction_type"])
        self.amount = _append(self.amount, cells["amount"])
        self.owner = self.owner or (cells["owner"].strip() or None)
        self.transaction_date = self.transaction_date or (cells["transaction_date"].strip() or None)
        self.notification_date = self.notification_date or (
            cells["notification_date"].strip() or None
        )

    def add_comment(self, text: str) -> None:
        text = text.strip()
        if text:
            self.comment = f"{self.comment} | {text}" if self.comment else text

    def build(self) -> RawRow:
        asset_name, asset_type = _split_asset_type(self.asset)
        return RawRow(
            row_index=self.row_index,
            owner=self.owner,
            asset_name=asset_name,
            asset_type=asset_type,
            transaction_type=self.transaction_type,
            transaction_date=self.transaction_date,
            notification_date=self.notification_date,
            amount=self.amount,
            comment=self.comment,
            page=self.page,
        )


def parse_ptr(pdf_path: Path) -> tuple[list[RawRow], int]:
    """Parse a PTR PDF into raw rows plus an independent row-anchor count.

    Returns ``(rows, anchor_count)``. ``anchor_count`` is the number of words
    matching a date that land in the Transaction Date column across the whole
    document, counted by a flat scan that shares no line-grouping or
    continuation logic with row assembly. It is the reconciliation signal:
    the Clerk's PTRs state no transaction total anywhere in the document, so
    this cross-check against a second, independent pass is what catches row
    assembly going wrong. Divergence means the parse is suspect.

    Raises ParseError when the PDF carries no text layer at all (scanned paper
    filings), which the caller records rather than treating as a crash.
    """
    rows: list[RawRow] = []
    current: _RowBuilder | None = None
    anchor_count = 0
    saw_any_word = False

    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages):
            words = page.extract_words(
                use_text_flow=False, keep_blank_chars=False, extra_attrs=["size", "fontname"]
            )
            if words:
                saw_any_word = True
            starts = dict(DEFAULT_COLUMN_STARTS)
            in_table = False
            anchor_count += _count_date_anchors(words)

            for line in _group_lines(words):
                if _is_header_line(line):
                    if _defines_columns(line):
                        starts = _column_starts(line)
                    in_table = True
                    continue
                if not in_table:
                    continue
                if _ends_table(line):
                    in_table = False
                    continue

                bounds = _bounds(starts)
                cells = _cells(line, bounds)

                if min(w.get("size", 9.0) for w in line) < COMMENT_MAX_SIZE:
                    if current is not None:
                        # Comment blocks are indented under the asset column but
                        # run the full width of the table, so take the whole line
                        # rather than bucketing it into columns.
                        current.add_comment(_line_text(line))
                    continue

                if DATE_RE.match(cells["transaction_date"].strip()):
                    if current is not None:
                        rows.append(current.build())
                    current = _RowBuilder(len(rows), page_no, cells)
                elif current is not None:
                    current.extend(cells)

    if current is not None:
        rows.append(current.build())

    if not saw_any_word:
        raise ParseError("no text layer (scanned paper filing); OCR not available")

    return rows, anchor_count
