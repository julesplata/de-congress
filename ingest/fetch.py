"""Download and cache House Clerk disclosure artifacts; parse the XML index."""

from __future__ import annotations

import io
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
from dateutil import parser as date_parser

from ingest.models import FilingIndexEntry

log = logging.getLogger(__name__)

ZIP_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

CACHE_DIR = Path(".cache")
USER_AGENT = "congressional-trading-ingest/0.1 (+https://github.com/)"
TIMEOUT = httpx.Timeout(60.0, connect=30.0)

# The year ZIP is cumulative and republished continuously. Re-running the
# pipeline within this window reuses the cached copy so an immediate re-run
# downloads nothing; a weekly CI run always exceeds it and picks up new filings.
INDEX_MAX_AGE_HOURS = 12.0


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ), True


def _get(url: str, client: httpx.Client | None, retries: int = 3) -> bytes:
    c, owned = _client(client)
    try:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = c.get(url)
                resp.raise_for_status()
                return resp.content
            except (httpx.HTTPError, httpx.StreamError) as exc:
                last = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                    raise
                if attempt < retries - 1:
                    time.sleep(2**attempt)
        assert last is not None
        raise last
    finally:
        if owned:
            c.close()


def to_iso(value: str) -> str:
    """Parse a Clerk-formatted date (M/D/YYYY) into ISO YYYY-MM-DD."""
    return date_parser.parse(value.strip(), dayfirst=False).date().isoformat()


def year_zip_path(year: int, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"{year}FD.zip"


def pdf_path(year: int, doc_id: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / "ptr-pdfs" / str(year) / f"{doc_id}.pdf"


def fetch_year_zip(
    year: int,
    cache_dir: Path = CACHE_DIR,
    client: httpx.Client | None = None,
    force: bool = False,
    max_age_hours: float = INDEX_MAX_AGE_HOURS,
) -> Path:
    """Return a path to the year's cumulative FD ZIP, downloading only if stale."""
    dest = year_zip_path(year, cache_dir)
    if dest.exists() and not force:
        age_hours = (time.time() - dest.stat().st_mtime) / 3600.0
        if age_hours < max_age_hours:
            log.info("index cache hit %s (age %.1fh)", dest, age_hours)
            return dest
        log.info("index cache stale %s (age %.1fh), refreshing", dest, age_hours)

    url = ZIP_URL.format(year=year)
    log.info("downloading %s", url)
    payload = _get(url, client)
    if not payload.startswith(b"PK"):
        raise ValueError(f"{url} did not return a ZIP archive")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(payload)
    tmp.replace(dest)
    return dest


def parse_index(zip_path: Path) -> list[FilingIndexEntry]:
    """Parse the XML index inside a year ZIP into index entries.

    Entries are returned sorted by (doc_id) so downstream ordering is stable
    regardless of the order the Clerk happens to emit them in.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not names:
            raise ValueError(f"no XML index inside {zip_path}")
        raw = zf.read(names[0])

    # The Clerk ships the index as UTF-8 with a BOM.
    root = ET.fromstring(raw.decode("utf-8-sig"))

    entries: list[FilingIndexEntry] = []
    skipped = 0
    for member in root.findall("Member"):

        def txt(tag: str) -> str:
            return (member.findtext(tag) or "").strip()

        doc_id = txt("DocID")
        filing_date = txt("FilingDate")
        year = txt("Year")
        if not doc_id or not filing_date or not year:
            # Observed only on withdrawal ("W") rows, which carry no FilingDate
            # and are never PTRs.
            skipped += 1
            continue
        entries.append(
            FilingIndexEntry(
                doc_id=doc_id,
                year=int(year),
                filing_type=txt("FilingType"),
                filing_date=to_iso(filing_date),
                prefix=txt("Prefix"),
                last=txt("Last"),
                first=txt("First"),
                suffix=txt("Suffix"),
                state_dst=txt("StateDst"),
            )
        )
    if skipped:
        log.info("skipped %d index rows missing DocID/FilingDate/Year", skipped)
    entries.sort(key=lambda e: (e.doc_id, e.filing_date))
    return entries


def ptr_entries(entries: list[FilingIndexEntry]) -> list[FilingIndexEntry]:
    """Filter an index down to Periodic Transaction Reports."""
    return [e for e in entries if e.is_ptr]


def fetch_pdf(
    entry: FilingIndexEntry,
    cache_dir: Path = CACHE_DIR,
    client: httpx.Client | None = None,
) -> Path:
    """Return a path to the filing's PDF. Never re-downloads a cached file."""
    dest = pdf_path(entry.year, entry.doc_id, cache_dir)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = PDF_URL.format(year=entry.year, doc_id=entry.doc_id)
    log.info("downloading %s", url)
    payload = _get(url, client)
    if not payload.startswith(b"%PDF"):
        raise ValueError(f"{url} did not return a PDF")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.part")
    tmp.write_bytes(payload)
    tmp.replace(dest)
    return dest


def open_client() -> httpx.Client:
    """A connection-pooled client callers should reuse across many fetches."""
    return httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
