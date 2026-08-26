import io
import time
import zipfile

import pytest

from ingest.fetch import (
    fetch_pdf,
    fetch_year_zip,
    parse_index,
    pdf_path,
    ptr_entries,
    to_iso,
    year_zip_path,
)

INDEX_XML = """﻿<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member><Prefix>Hon.</Prefix><Last>Latta</Last><First>Robert E.</First><Suffix />
    <FilingType>P</FilingType><StateDst>OH05</StateDst><Year>2026</Year>
    <FilingDate>4/24/2026</FilingDate><DocID>20034401</DocID></Member>
  <Member><Prefix /><Last>Aaron</Last><First>Richard</First><Suffix />
    <FilingType>C</FilingType><StateDst>MI04</StateDst><Year>2026</Year>
    <FilingDate>4/15/2026</FilingDate><DocID>8068</DocID></Member>
  <Member><Prefix /><Last>NoDate</Last><First>W</First><Suffix />
    <FilingType>W</FilingType><StateDst /><Year>2026</Year>
    <FilingDate /><DocID>9115765</DocID></Member>
</FinancialDisclosure>
"""


def make_zip(path, xml=INDEX_XML):
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2026FD.txt", "ignored")
        zf.writestr("2026FD.xml", xml.encode("utf-8"))
    path.write_bytes(buf.getvalue())
    return path


def test_to_iso():
    assert to_iso("4/24/2026") == "2026-04-24"
    assert to_iso("12/01/2025") == "2025-12-01"


def test_parse_index_reads_bom_utf8_and_skips_dateless_rows(tmp_path):
    entries = parse_index(make_zip(tmp_path / "2026FD.zip"))
    assert len(entries) == 2  # the blank-FilingDate withdrawal row is skipped
    ptrs = ptr_entries(entries)
    assert len(ptrs) == 1
    entry = ptrs[0]
    assert entry.doc_id == "20034401"
    assert entry.filing_date == "2026-04-24"
    assert entry.filer_name == "Hon. Robert E. Latta"
    assert entry.pdf_url == (
        "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20034401.pdf"
    )


def test_index_order_is_stable(tmp_path):
    entries = parse_index(make_zip(tmp_path / "2026FD.zip"))
    assert [e.doc_id for e in entries] == sorted(e.doc_id for e in entries)


class RefusingClient:
    """Any network access is a test failure."""

    def get(self, url):
        raise AssertionError(f"unexpected network request to {url}")


def test_fresh_index_cache_is_not_redownloaded(tmp_path):
    path = make_zip(year_zip_path(2026, tmp_path))
    assert fetch_year_zip(2026, cache_dir=tmp_path, client=RefusingClient()) == path


def test_stale_index_cache_is_refreshed(tmp_path):
    path = make_zip(year_zip_path(2026, tmp_path))
    old = time.time() - 3600 * 48
    import os

    os.utime(path, (old, old))
    with pytest.raises(AssertionError, match="unexpected network request"):
        fetch_year_zip(2026, cache_dir=tmp_path, client=RefusingClient())


def test_cached_pdf_is_never_redownloaded(tmp_path):
    from ingest.models import FilingIndexEntry

    entry = FilingIndexEntry(
        doc_id="20034401", year=2026, filing_type="P", filing_date="2026-04-24"
    )
    dest = pdf_path(2026, "20034401", tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"%PDF-1.4 cached")
    assert fetch_pdf(entry, cache_dir=tmp_path, client=RefusingClient()) == dest
    assert dest.read_bytes() == b"%PDF-1.4 cached"


def test_index_without_xml_is_rejected(tmp_path):
    path = tmp_path / "bad.zip"
    import io as _io

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("only.txt", "nope")
    path.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="no XML index"):
        parse_index(path)
