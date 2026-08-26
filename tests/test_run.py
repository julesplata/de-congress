"""End-to-end orchestrator tests, run entirely offline against the fixtures."""

import io
import json
import shutil
import zipfile

import pytest

from conftest import FIXTURES
from ingest import fetch
from ingest.run import main, run

DOCS = ["20034401", "20034585", "20033695", "9115808"]

INDEX = """﻿<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
{members}
</FinancialDisclosure>
"""

MEMBER = """  <Member><Prefix>Hon.</Prefix><Last>{last}</Last><First>{first}</First><Suffix />
    <FilingType>P</FilingType><StateDst>XX01</StateDst><Year>2026</Year>
    <FilingDate>{filed}</FilingDate><DocID>{doc_id}</DocID></Member>"""

NAMES = {
    "20034401": ("Latta", "Robert E.", "4/24/2026"),
    "20034585": ("Gottheimer", "Josh", "5/19/2026"),
    "20033695": ("Matsui", "Doris O.", "1/13/2026"),
    "9115808": ("Rogers", "Harold Dallas", "2/9/2026"),
}


class OfflineClient:
    """Serves only the year ZIP; any PDF request is a test failure."""

    def __init__(self, zip_bytes):
        self.zip_bytes = zip_bytes
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        if url.endswith("FD.zip"):
            return _Response(self.zip_bytes)
        raise AssertionError(f"unexpected PDF download: {url}")


class _Response:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


@pytest.fixture
def workspace(tmp_path):
    cache = tmp_path / "cache"
    data = tmp_path / "data"
    pdf_dir = cache / "ptr-pdfs" / "2026"
    pdf_dir.mkdir(parents=True)
    for doc_id in DOCS:
        shutil.copy(FIXTURES / f"{doc_id}.pdf", pdf_dir / f"{doc_id}.pdf")

    members = "\n".join(
        MEMBER.format(
            doc_id=d, last=NAMES[d][0], first=NAMES[d][1], filed=NAMES[d][2]
        )
        for d in DOCS
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2026FD.xml", INDEX.format(members=members).encode("utf-8"))
    zip_bytes = buf.getvalue()
    fetch.year_zip_path(2026, cache).write_bytes(zip_bytes)
    return cache, data, OfflineClient(zip_bytes)


def load(data, name):
    return json.loads((data / name).read_text())


def test_full_run_writes_all_three_files(workspace):
    cache, data, client = workspace
    report = run(2026, data_dir=data, cache_dir=cache, client=client)

    assert (data / "trades.json").exists()
    assert (data / "filings.json").exists()
    assert (data / "report.json").exists()

    assert report.filings_seen == 4
    assert report.filings_parsed == 3
    assert report.filings_failed == 1  # the scanned filing
    assert report.filings_skipped_cached == 0
    assert report.rows_produced == 31  # 1 + 26 + 4
    assert report.rows_dropped_validation == 0
    assert report.unreconciled_filings == []
    assert client.requests == []  # everything served from cache


def test_trades_match_the_data_contract(workspace):
    cache, data, client = workspace
    run(2026, data_dir=data, cache_dir=cache, client=client)
    trades = load(data, "trades.json")

    expected_fields = {
        "id", "source_id", "doc_id", "doc_url", "filer_name", "transaction_date",
        "filing_date", "owner", "ticker", "asset_name", "asset_type",
        "transaction_type", "amount_range_low", "amount_range_high",
        "amount_range_label", "days_to_file", "is_late", "comment", "row_index",
        "possible_amendment",
    }
    for row in trades:
        assert set(row) == expected_fields
        assert row["source_id"] == "house_clerk"
        assert row["id"] == f"house_{row['doc_id']}_t{row['row_index']}"
        assert row["owner"] in {"SP", "DC", "JT", "self", "unknown"}
        assert row["transaction_type"] in {
            "Purchase", "Sale (Full)", "Sale (Partial)", "Exchange"
        }
        assert row["is_late"] == (row["days_to_file"] > 45)
        assert row["amount_range_low"] <= row["amount_range_high"]

    latta = next(t for t in trades if t["doc_id"] == "20034401")
    assert latta == {
        "id": "house_20034401_t0",
        "source_id": "house_clerk",
        "doc_id": "20034401",
        "doc_url": (
            "https://disclosures-clerk.house.gov/public_disc/"
            "ptr-pdfs/2026/20034401.pdf"
        ),
        "filer_name": "Hon. Robert E. Latta",
        "transaction_date": "2026-04-20",
        "filing_date": "2026-04-24",
        "owner": "SP",
        "ticker": "FMAO",
        "asset_name": "Farmers & Merchants Bancorp, Inc. (FMAO)",
        "asset_type": "ST",
        "transaction_type": "Purchase",
        "amount_range_low": 1001,
        "amount_range_high": 15000,
        "amount_range_label": "$1,001 - $15,000",
        "days_to_file": 4,
        "is_late": False,
        "comment": (
            "Filing Status: New | Description: dividend reinvestment "
            "| Comments: dividend reinvestment"
        ),
        "row_index": 0,
        "possible_amendment": False,
    }


def test_filings_record_reconciliation(workspace):
    cache, data, client = workspace
    run(2026, data_dir=data, cache_dir=cache, client=client)
    filings = {f["doc_id"]: f for f in load(data, "filings.json")}

    assert len(filings) == 4
    assert filings["20034585"]["parsed_row_count"] == 26
    assert filings["20034585"]["stated_row_count"] == 26
    assert filings["20034585"]["reconciled"] is True

    scanned = filings["9115808"]
    assert scanned["parsed_row_count"] == 0
    assert scanned["reconciled"] is False
    assert "no text layer" in scanned["parse_error"]


def test_rerun_is_byte_identical_and_downloads_nothing(workspace):
    cache, data, client = workspace
    run(2026, data_dir=data, cache_dir=cache, client=client)
    first_trades = (data / "trades.json").read_bytes()
    first_filings = (data / "filings.json").read_bytes()

    report = run(2026, data_dir=data, cache_dir=cache, client=client)

    assert (data / "trades.json").read_bytes() == first_trades
    assert (data / "filings.json").read_bytes() == first_filings
    assert report.filings_skipped_cached == 4
    assert report.filings_parsed == 0
    assert client.requests == []


def test_force_reparses_every_filing(workspace):
    cache, data, client = workspace
    run(2026, data_dir=data, cache_dir=cache, client=client)
    first_trades = (data / "trades.json").read_bytes()

    report = run(2026, data_dir=data, cache_dir=cache, client=client, force=True)

    assert report.filings_parsed == 3
    assert report.filings_skipped_cached == 0
    assert (data / "trades.json").read_bytes() == first_trades
    # force refreshes the index, but still never re-downloads a cached PDF.
    assert all(u.endswith("FD.zip") for u in client.requests)


def test_report_metrics(workspace):
    cache, data, client = workspace
    run(2026, data_dir=data, cache_dir=cache, client=client)
    report = load(data, "report.json")

    for key in [
        "filings_seen", "filings_parsed", "filings_skipped_cached", "rows_produced",
        "rows_dropped_validation", "ticker_resolution_rate", "duration_seconds",
    ]:
        assert key in report
    assert 0.0 <= report["ticker_resolution_rate"] <= 1.0
    assert report["duration_seconds"] >= 0
    assert len(report["parse_errors"]) == 1


def test_amendment_flag_across_filings(workspace):
    """The same trade filed under two doc_ids keeps both rows, flagged."""
    cache, data, client = workspace
    run(2026, data_dir=data, cache_dir=cache, client=client)
    trades = load(data, "trades.json")

    # 20034585 reports two identical AMD purchases on different dates and two
    # Goldman Sachs trades; none share a doc_id, so nothing is flagged here.
    assert not any(t["possible_amendment"] for t in trades)

    # Simulate that filing being amended and refiled under a new doc_id.
    original = next(t for t in trades if t["ticker"] == "TSCO")
    duplicate = dict(original, doc_id="20099999", id="house_20099999_t0", row_index=0)
    (data / "trades.json").write_text(json.dumps(trades + [duplicate], indent=2) + "\n")
    filings = load(data, "filings.json")
    filings.append({**filings[0], "doc_id": "20099999"})
    (data / "filings.json").write_text(json.dumps(filings, indent=2) + "\n")

    run(2026, data_dir=data, cache_dir=cache, client=client)
    after = load(data, "trades.json")
    flagged = [t for t in after if t["possible_amendment"]]
    assert len(flagged) == 2  # both kept, neither dropped
    assert {t["doc_id"] for t in flagged} == {original["doc_id"], "20099999"}


def test_amendment_key_over_flags_rather_than_dropping(workspace):
    """Rows sharing the spec'd key are all flagged, even if distinct trades.

    The key is (filer_name, transaction_date, ticker, amount_range_label), so
    four different Treasury notes bought on one day - all with ticker None and
    the same bracket - collide. Flagging is deliberately the safe direction:
    the field is named `possible_amendment` and nothing is ever dropped.
    """
    cache, data, client = workspace
    run(2026, data_dir=data, cache_dir=cache, client=client)
    trades = load(data, "trades.json")

    matsui = [t for t in trades if t["doc_id"] == "20033695"]
    assert len(matsui) == 4
    assert all(t["ticker"] is None for t in matsui)
    duplicate = dict(matsui[0], doc_id="20099999", id="house_20099999_t0", row_index=0)
    (data / "trades.json").write_text(json.dumps(trades + [duplicate], indent=2) + "\n")
    filings = load(data, "filings.json")
    filings.append({**filings[0], "doc_id": "20099999"})
    (data / "filings.json").write_text(json.dumps(filings, indent=2) + "\n")

    run(2026, data_dir=data, cache_dir=cache, client=client)
    after = load(data, "trades.json")
    assert len([t for t in after if t["possible_amendment"]]) == 5
    assert len(after) == len(trades) + 1


def test_cli_entrypoint(workspace, monkeypatch):
    cache, data, client = workspace
    monkeypatch.setattr(fetch, "open_client", lambda: _NoopCtx(client))
    assert main(["--year", "2026", "--data-dir", str(data), "--cache-dir", str(cache)]) == 0
    assert load(data, "trades.json")


class _NoopCtx:
    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner

    def __exit__(self, *exc):
        return False
