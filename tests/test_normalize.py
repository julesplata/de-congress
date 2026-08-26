import pytest

from ingest.models import FilingIndexEntry, RawRow, Transaction
from ingest.normalize import (
    NormalizeError,
    deduplicate,
    flag_amendments,
    normalize_filing,
    normalize_owner,
    normalize_row,
    normalize_transaction_type,
    parse_amount,
    parse_us_date,
)

ENTRY = FilingIndexEntry(
    doc_id="20034401",
    year=2026,
    filing_type="P",
    filing_date="2026-04-24",
    prefix="Hon.",
    first="Robert E.",
    last="Latta",
)


def raw(**kw):
    base = dict(
        row_index=0,
        owner="SP",
        asset_name="Farmers & Merchants Bancorp, Inc. (FMAO)",
        asset_type="ST",
        transaction_type="P",
        transaction_date="04/20/2026",
        notification_date="04/20/2026",
        amount="$1,001 - $15,000",
        comment=None,
    )
    base.update(kw)
    return RawRow(**base)


@pytest.mark.parametrize(
    "label,expected",
    [
        ("$1,001 - $15,000", (1001, 15000)),
        ("$15,001 - $50,000", (15001, 50000)),
        ("$5,000,001 - $25,000,000", (5000001, 25000000)),
        ("$1,000 or less", (0, 1000)),
        ("$2,722.50", (2722, 2722)),
        ("$15.00", (15, 15)),
    ],
)
def test_parse_amount_brackets(label, expected):
    assert parse_amount(label) == expected


@pytest.mark.parametrize(
    "label,low",
    [("Spouse/DC Over $1,000,000", 1000001), ("$50,000,000 +", 50000000)],
)
def test_open_ended_brackets_have_no_ceiling(label, low):
    """Unbounded brackets get high == low; the verbatim label is authoritative."""
    got_low, got_high = parse_amount(label)
    assert got_low == low
    assert got_high == got_low


def test_parse_amount_rejects_junk():
    with pytest.raises(NormalizeError):
        parse_amount("")
    with pytest.raises(NormalizeError):
        parse_amount("a lot of money")


@pytest.mark.parametrize(
    "value,expected",
    [(None, "self"), ("", "self"), ("SP", "SP"), ("JT", "JT"), ("DC", "DC"), ("??", "unknown")],
)
def test_normalize_owner(value, expected):
    assert normalize_owner(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("P", "Purchase"),
        ("S", "Sale (Full)"),
        ("S (partial)", "Sale (Partial)"),
        ("S (Partial)", "Sale (Partial)"),
        ("E", "Exchange"),
    ],
)
def test_normalize_transaction_type(value, expected):
    assert normalize_transaction_type(value) == expected


@pytest.mark.parametrize("value", [None, "", "X", "Purchase!"])
def test_bad_transaction_type_rejected(value):
    with pytest.raises(NormalizeError):
        normalize_transaction_type(value)


def test_parse_us_date():
    assert parse_us_date("04/20/2026") == "2026-04-20"
    assert parse_us_date("1/2/2026") == "2026-01-02"
    with pytest.raises(NormalizeError):
        parse_us_date("2026-04-20")
    with pytest.raises(NormalizeError):
        parse_us_date("02/30/2026")


def test_normalize_row_matches_data_contract():
    txn = normalize_row(raw(), ENTRY)
    assert txn.id == "house_20034401_t0"
    assert txn.source_id == "house_clerk"
    assert txn.doc_url.endswith("/ptr-pdfs/2026/20034401.pdf")
    assert txn.filer_name == "Hon. Robert E. Latta"
    assert txn.transaction_date == "2026-04-20"
    assert txn.filing_date == "2026-04-24"
    assert txn.days_to_file == 4
    assert txn.is_late is False
    assert txn.ticker is None  # resolution happens after normalization


def test_is_late_boundary():
    assert normalize_row(raw(transaction_date="03/10/2026"), ENTRY).days_to_file == 45
    assert normalize_row(raw(transaction_date="03/10/2026"), ENTRY).is_late is False
    assert normalize_row(raw(transaction_date="03/09/2026"), ENTRY).is_late is True


def test_bad_rows_are_dropped_not_coerced():
    rows = [raw(row_index=0), raw(row_index=1, amount="???"), raw(row_index=2, transaction_type="Z")]
    kept, dropped = normalize_filing(rows, ENTRY)
    assert [t.row_index for t in kept] == [0]
    assert [d.row_index for d in dropped] == [1, 2]
    assert all(d.reason for d in dropped)


def test_deduplicate_on_doc_id_and_row_index():
    a = normalize_row(raw(), ENTRY)
    assert len(deduplicate([a, a, a])) == 1


def _txn(doc_id, ticker="AAPL", row_index=0):
    return Transaction(
        id=f"house_{doc_id}_t{row_index}",
        doc_id=doc_id,
        doc_url=f"https://example.invalid/{doc_id}.pdf",
        filer_name="Hon. A B",
        transaction_date="2026-01-05",
        filing_date="2026-01-20",
        owner="self",
        ticker=ticker,
        asset_name="Apple Inc. (AAPL)",
        asset_type="ST",
        transaction_type="Purchase",
        amount_range_low=1001,
        amount_range_high=15000,
        amount_range_label="$1,001 - $15,000",
        days_to_file=15,
        is_late=False,
        comment=None,
        row_index=row_index,
    )


def test_amendments_are_flagged_and_both_records_kept():
    out = flag_amendments([_txn("111"), _txn("222")])
    assert len(out) == 2
    assert all(t.possible_amendment for t in out)


def test_same_filing_duplicates_are_not_flagged_as_amendments():
    out = flag_amendments([_txn("111", row_index=0), _txn("111", row_index=1)])
    assert not any(t.possible_amendment for t in out)


def test_different_trades_are_not_flagged():
    out = flag_amendments([_txn("111", ticker="AAPL"), _txn("222", ticker="MSFT")])
    assert not any(t.possible_amendment for t in out)
