import pytest

from ingest.resolve import (
    SymbolTable,
    load_symbols,
    resolve_ticker,
    ticker_from_exact_name,
    ticker_from_parenthetical,
)


@pytest.fixture(scope="module")
def symbols():
    return load_symbols()


def test_symbol_list_is_committed_and_loads(symbols):
    assert len(symbols) > 5000
    assert symbols.known("AAPL")
    assert not symbols.known("NOTATICKER")


@pytest.mark.parametrize(
    "asset_name,expected",
    [
        ("Apple Inc. (AAPL)", "AAPL"),
        ("AT&T Inc. (T)", "T"),
        ("Berkshire Hathaway Class B (BRK.B)", "BRK.B"),
        ("Goldman Sachs Group, Inc. (GS)", "GS"),
        ("Sensata Technologies Holding plc Ordinary Shares (ST)", "ST"),
    ],
)
def test_declared_parenthetical_ticker(asset_name, expected):
    assert ticker_from_parenthetical(asset_name) == expected


@pytest.mark.parametrize(
    "asset_name",
    [
        "U.S. Treasury Note due 2/28/2029",
        "US Treasury Bills (91282CGH8)",          # CUSIP, not a ticker
        "Entergy Louisiana LLC (EFC$D)",          # preferred-share code
        "New Water Capital Partners II, LP (GLAS Funds LP)",
        "Riverside CA Elec Util",
        "Morgan Stanley - Select UMA Account # 1",
    ],
)
def test_non_tickers_resolve_to_none(asset_name, symbols):
    assert resolve_ticker(asset_name, symbols) is None


def test_exact_whole_name_match(symbols):
    assert ticker_from_exact_name("NVIDIA CORP", symbols) == "NVDA"
    assert ticker_from_exact_name("  nvidia   corp  ", symbols) == "NVDA"


def test_never_matches_a_partial_or_fuzzy_name(symbols):
    """A wrong ticker is worse than a missing one."""
    for name in [
        "Apple",                      # prefix of "Apple Inc."
        "Apple Inc. Common Stock",    # superstring
        "NVIDIA",
        "Micro Devices",
        "Alphabet Inc. Class A",
    ]:
        assert resolve_ticker(name, symbols) is None


def test_ambiguous_name_mapping_to_two_tickers_is_dropped():
    table = SymbolTable({"AAA": "Duplicate Corp", "BBB": "Duplicate Corp"})
    assert ticker_from_exact_name("Duplicate Corp", table) is None


def test_last_parenthetical_wins():
    assert ticker_from_parenthetical("Fund (Series A) (ABC)") == "ABC"
