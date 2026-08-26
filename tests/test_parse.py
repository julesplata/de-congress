"""Fixture-driven parser tests.

The fixtures are real, unmodified PTR PDFs pulled from the Clerk, each chosen
for a specific hazard:

  20034401  single page, single row, all three comment-block kinds
  20034585  four pages, 26 rows, rows wrapping across lines and page breaks,
            amounts wrapping into the asset column's visual line
  20033695  the open-ended "Spouse/DC Over $1,000,000" bracket, and asset
            names containing dates ("U.S. Treasury Note due 2/28/2029") that
            must not be mistaken for transaction dates
  9115808   a scanned paper filing with no text layer at all
"""

import json

import pytest

from conftest import EXPECTED, FIXTURES
from ingest.parse import ParseError, parse_ptr

FIXTURE_DOCS = ["20034401", "20034585", "20033695"]


@pytest.mark.parametrize("doc_id", FIXTURE_DOCS)
def test_fixture_rows_match_field_by_field(doc_id):
    expected = json.loads((EXPECTED / f"{doc_id}.json").read_text())
    rows, anchors = parse_ptr(FIXTURES / f"{doc_id}.pdf")

    assert len(rows) == len(expected["rows"])
    assert anchors == expected["anchor_count"]

    for actual, want in zip(rows, expected["rows"]):
        got = actual.model_dump()
        for field in want:
            assert got[field] == want[field], f"{doc_id} row {want['row_index']}.{field}"


@pytest.mark.parametrize("doc_id", FIXTURE_DOCS)
def test_fixture_reconciles(doc_id):
    rows, anchors = parse_ptr(FIXTURES / f"{doc_id}.pdf")
    assert len(rows) == anchors


def test_single_row_filing_exact():
    rows, anchors = parse_ptr(FIXTURES / "20034401.pdf")
    assert anchors == 1
    assert len(rows) == 1
    row = rows[0]
    assert row.row_index == 0
    assert row.owner == "SP"
    assert row.asset_name == "Farmers & Merchants Bancorp, Inc. (FMAO)"
    assert row.asset_type == "ST"
    assert row.transaction_type == "P"
    assert row.transaction_date == "04/20/2026"
    assert row.notification_date == "04/20/2026"
    assert row.amount == "$1,001 - $15,000"
    # Small-caps labels are recovered from NUL-padded glyphs.
    assert row.comment == (
        "Filing Status: New | Description: dividend reinvestment "
        "| Comments: dividend reinvestment"
    )


def test_row_assembled_across_a_page_break():
    """The [ST] asset-type token for this row sits on the following page."""
    rows, _ = parse_ptr(FIXTURES / "20034585.pdf")
    row = next(r for r in rows if r.asset_name.startswith("Infineon"))
    assert row.asset_name == "Infineon Technologies AG (IFNNY)"
    assert row.asset_type == "ST"
    assert row.transaction_type == "P"
    assert row.transaction_date == "04/13/2026"


def test_wrapped_amount_is_kept_in_its_own_column():
    """"$15,001 -" / "$50,000" wraps onto the asset name's visual line."""
    rows, _ = parse_ptr(FIXTURES / "20034585.pdf")
    row = next(r for r in rows if r.asset_name.startswith("Tractor Supply"))
    assert row.asset_name == "Tractor Supply Company - Common Stock (TSCO)"
    assert row.amount == "$15,001 - $50,000"


def test_asset_name_is_untruncated_across_three_lines():
    rows, _ = parse_ptr(FIXTURES / "20034585.pdf")
    row = next(r for r in rows if "Business Machines" in r.asset_name)
    assert row.asset_name == (
        "International Business Machines Corporation Common Stock (IBM)"
    )
    assert row.transaction_type == "S (partial)"


def test_column_header_block_never_leaks_into_rows():
    """The bold "Type Date Gains >" header line repeats on every page."""
    rows, _ = parse_ptr(FIXTURES / "20034585.pdf")
    for row in rows:
        assert "Type" not in (row.transaction_type or "")
        assert "Gains" not in row.asset_name


def test_date_inside_asset_name_is_not_a_transaction_date():
    rows, _ = parse_ptr(FIXTURES / "20033695.pdf")
    assert len(rows) == 4
    assert rows[0].asset_name == "U.S. Treasury Note due 2/28/2029"
    assert rows[0].transaction_date == "12/15/2025"
    assert rows[0].amount == "Spouse/DC Over $1,000,000"


def test_scanned_filing_raises_parse_error():
    with pytest.raises(ParseError, match="no text layer"):
        parse_ptr(FIXTURES / "9115808.pdf")


def test_comment_text_is_not_clipped_at_the_asset_column():
    rows, _ = parse_ptr(FIXTURES / "20034585.pdf")
    row = rows[0]
    assert row.comment.endswith("Morgan Stanley - Select UMA Account # 1")
