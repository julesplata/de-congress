"""CLI orchestrator: fetch -> parse -> normalize -> resolve -> write JSON."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

from ingest import fetch
from ingest.models import FilingIndexEntry, FilingRecord, RunReport, Transaction, ValidationDrop
from ingest.normalize import deduplicate, flag_amendments, normalize_filing, sort_key
from ingest.parse import ParseError, parse_ptr
from ingest.resolve import SymbolTable, load_symbols, resolve_ticker

log = logging.getLogger("ingest")

DATA_DIR = Path("data")
TRADES_FILE = "trades.json"
FILINGS_FILE = "filings.json"
REPORT_FILE = "report.json"


def _write_json(path: Path, payload: object) -> None:
    """Write deterministic, newline-terminated JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON; starting from empty", path)
        return default


def process_filing(
    entry: FilingIndexEntry,
    pdf: Path,
    symbols: SymbolTable,
) -> tuple[FilingRecord, list[Transaction], list[ValidationDrop]]:
    """Parse, normalize and resolve one filing."""
    try:
        rows, anchors = parse_ptr(pdf)
    except ParseError as exc:
        record = FilingRecord(
            doc_id=entry.doc_id,
            doc_url=entry.pdf_url,
            filer_name=entry.filer_name,
            filing_date=entry.filing_date,
            year=entry.year,
            parsed_row_count=0,
            stated_row_count=None,
            reconciled=False,
            rows_dropped=0,
            parse_error=str(exc),
        )
        return record, [], []

    kept, dropped = normalize_filing(rows, entry)
    kept = [
        txn.model_copy(update={"ticker": resolve_ticker(txn.asset_name, symbols)})
        for txn in kept
    ]
    record = FilingRecord(
        doc_id=entry.doc_id,
        doc_url=entry.pdf_url,
        filer_name=entry.filer_name,
        filing_date=entry.filing_date,
        year=entry.year,
        parsed_row_count=len(rows),
        stated_row_count=anchors,
        reconciled=len(rows) == anchors,
        rows_dropped=len(dropped),
        parse_error=None,
    )
    return record, kept, dropped


def run(
    year: int,
    data_dir: Path = DATA_DIR,
    cache_dir: Path = fetch.CACHE_DIR,
    force: bool = False,
    client=None,
) -> RunReport:
    """Ingest one calendar year, merging into whatever is already in data/."""
    started = time.time()
    started_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    symbols = load_symbols()
    trades_path = data_dir / TRADES_FILE
    filings_path = data_dir / FILINGS_FILE

    # Existing output is the incremental state: a doc_id already in
    # filings.json is not re-parsed, and its rows are carried straight over.
    known_filings: dict[str, dict] = {
        rec["doc_id"]: rec for rec in _read_json(filings_path, [])
    }
    carried: dict[str, list[dict]] = {}
    for raw in _read_json(trades_path, []):
        carried.setdefault(raw["doc_id"], []).append(raw)

    zip_path = fetch.fetch_year_zip(year, cache_dir=cache_dir, client=client, force=force)
    entries = fetch.ptr_entries(fetch.parse_index(zip_path))
    log.info("year %d: %d PTR filings in index", year, len(entries))

    records: dict[str, FilingRecord] = {
        doc_id: FilingRecord(**rec) for doc_id, rec in known_filings.items()
    }
    # Keyed by doc_id so replacing one filing's rows stays O(1); at the full
    # 2016-to-present scale a flat list would make the merge quadratic.
    by_doc: dict[str, list[Transaction]] = {}
    for doc_id, raws in carried.items():
        for raw in raws:
            try:
                by_doc.setdefault(doc_id, []).append(Transaction(**raw))
            except Exception:
                log.warning("discarding unreadable cached row in %s", doc_id)

    parsed = skipped = failed = 0
    drops: list[ValidationDrop] = []

    for entry in entries:
        if not force and entry.doc_id in records:
            skipped += 1
            continue

        try:
            pdf = fetch.fetch_pdf(entry, cache_dir=cache_dir, client=client)
        except Exception as exc:
            # Transient: not recorded, so the next run retries it.
            log.warning("download failed for %s: %s", entry.doc_id, exc)
            failed += 1
            continue

        record, kept, dropped = process_filing(entry, pdf, symbols)
        if record.parse_error:
            failed += 1
        else:
            parsed += 1
        records[entry.doc_id] = record
        by_doc[entry.doc_id] = kept
        drops.extend(dropped)

    transactions = [t for rows in by_doc.values() for t in rows]
    transactions = flag_amendments(deduplicate(transactions))
    transactions.sort(key=sort_key)

    resolved = sum(1 for t in transactions if t.ticker)
    rate = round(resolved / len(transactions), 4) if transactions else 0.0
    unreconciled = sorted(
        r.doc_id for r in records.values() if not r.reconciled and not r.parse_error
    )

    _write_json(trades_path, [t.model_dump() for t in transactions])
    _write_json(
        filings_path,
        [records[k].model_dump() for k in sorted(records)],
    )

    report = RunReport(
        year=year,
        started_at=started_at,
        duration_seconds=round(time.time() - started, 2),
        filings_seen=len(entries),
        filings_parsed=parsed,
        filings_skipped_cached=skipped,
        filings_failed=failed,
        rows_produced=len(transactions),
        rows_dropped_validation=len(drops),
        ticker_resolution_rate=rate,
        tickers_resolved=resolved,
        unreconciled_filings=unreconciled,
        validation_drops=drops,
        parse_errors=sorted(
            (r for r in records.values() if r.parse_error), key=lambda r: r.doc_id
        ),
    )
    _write_json(data_dir / REPORT_FILE, report.model_dump())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.run",
        description="Ingest House Clerk Periodic Transaction Reports for one year.",
    )
    parser.add_argument("--year", type=int, required=True, help="calendar year to ingest")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download the index and re-parse every filing, ignoring cached results",
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--cache-dir", type=Path, default=fetch.CACHE_DIR)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    with fetch.open_client() as client:
        report = run(
            year=args.year,
            data_dir=args.data_dir,
            cache_dir=args.cache_dir,
            force=args.force,
            client=client,
        )

    log.info(
        "year %d: %d seen, %d parsed, %d cached, %d failed, %d rows, "
        "%d dropped, ticker rate %.1f%%, %.1fs",
        report.year,
        report.filings_seen,
        report.filings_parsed,
        report.filings_skipped_cached,
        report.filings_failed,
        report.rows_produced,
        report.rows_dropped_validation,
        report.ticker_resolution_rate * 100,
        report.duration_seconds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
