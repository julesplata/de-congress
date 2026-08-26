"""Pydantic schemas for the ingestion pipeline."""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SOURCE_ID = "house_clerk"

PTR_FILING_TYPE = "P"

Owner = Literal["SP", "DC", "JT", "self", "unknown"]
TransactionType = Literal["Purchase", "Sale (Full)", "Sale (Partial)", "Exchange"]

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FilingIndexEntry(BaseModel):
    """One <Member> row from the year's XML index."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    year: int
    filing_type: str
    filing_date: str
    prefix: str = ""
    last: str = ""
    first: str = ""
    suffix: str = ""
    state_dst: str = ""

    @field_validator("filing_date")
    @classmethod
    def _iso(cls, v: str) -> str:
        if not _ISO_DATE.match(v):
            raise ValueError(f"filing_date must be ISO YYYY-MM-DD, got {v!r}")
        return v

    @property
    def is_ptr(self) -> bool:
        return self.filing_type == PTR_FILING_TYPE

    @property
    def filer_name(self) -> str:
        parts = [self.prefix, self.first, self.last, self.suffix]
        return " ".join(p.strip() for p in parts if p and p.strip())

    @property
    def pdf_url(self) -> str:
        return (
            "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/"
            f"{self.year}/{self.doc_id}.pdf"
        )


class RawRow(BaseModel):
    """A transaction row as literally reconstructed from PDF word boxes.

    Every field is the verbatim text found in the corresponding column, before
    any normalization. `None` means the column was blank on the filing.
    """

    model_config = ConfigDict(frozen=True)

    row_index: int
    owner: str | None = None
    asset_name: str
    asset_type: str | None = None
    transaction_type: str | None = None
    transaction_date: str | None = None
    notification_date: str | None = None
    amount: str | None = None
    comment: str | None = None
    page: int = 0


class Transaction(BaseModel):
    """The output data contract. One record per transaction in trades.json."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    source_id: Literal["house_clerk"] = SOURCE_ID
    doc_id: str
    doc_url: str
    filer_name: str
    transaction_date: str
    filing_date: str
    owner: Owner
    ticker: str | None
    asset_name: str
    asset_type: str | None
    transaction_type: TransactionType
    amount_range_low: int
    amount_range_high: int
    amount_range_label: str
    days_to_file: int
    is_late: bool
    comment: str | None
    row_index: int
    # Set by the cross-filing duplicate pass in run.py; see normalize.flag_amendments.
    possible_amendment: bool = False

    @field_validator("transaction_date", "filing_date")
    @classmethod
    def _iso(cls, v: str) -> str:
        if not _ISO_DATE.match(v):
            raise ValueError(f"date must be ISO YYYY-MM-DD, got {v!r}")
        dt.date.fromisoformat(v)
        return v

    @field_validator("asset_name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("asset_name must not be empty")
        return v


class FilingRecord(BaseModel):
    """One record per parsed filing, with row-count reconciliation."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    doc_url: str
    filer_name: str
    filing_date: str
    year: int
    parsed_row_count: int
    stated_row_count: int | None
    reconciled: bool
    rows_dropped: int
    parse_error: str | None = None


class ValidationDrop(BaseModel):
    """A row that failed pydantic validation and was dropped from output."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    row_index: int
    reason: str


class RunReport(BaseModel):
    """Run metrics written to data/report.json."""

    model_config = ConfigDict(frozen=True)

    year: int
    started_at: str
    duration_seconds: float
    filings_seen: int
    filings_parsed: int
    filings_skipped_cached: int
    filings_failed: int
    rows_produced: int
    rows_dropped_validation: int
    ticker_resolution_rate: float
    tickers_resolved: int
    unreconciled_filings: list[str]
    validation_drops: list[ValidationDrop]
    parse_errors: list[FilingRecord]
