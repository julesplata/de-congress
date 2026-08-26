"""Conservative asset_name -> ticker resolution.

Two sources only, both exact:

1. An explicit parenthetical ticker in the asset name. The Clerk's filing
   system emits these from its own security database - "Apple Inc. (AAPL)" -
   so the parenthetical is a declared ticker field, not an inference.
2. An exact, whole-string match of the asset name against the committed
   symbol list in symbols.tsv.

There is deliberately no fuzzy, partial, token, or prefix matching. Roughly
an eighth of House PTR assets - municipal bonds, medium-term notes,
structured products, LPs, private funds - have no ticker at all, and a wrong
ticker is worse than a missing one. Unresolved is None.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

SYMBOLS_PATH = Path(__file__).with_name("symbols.tsv")

# 1-6 characters, starts with a letter, may carry a share-class dot (BRK.B).
# Excludes CUSIPs (start with a digit or run 9 long), preferred-share codes
# containing '$' (EFC$D), and anything with whitespace ("GLAS Funds, LP").
TICKER_SHAPE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")
PAREN_RE = re.compile(r"\(([^()]{1,20})\)")


class SymbolTable:
    """The committed symbol list: tickers plus an exact company-name index."""

    def __init__(self, tickers: dict[str, str]) -> None:
        self.tickers = tickers
        self.by_name: dict[str, str] = {}
        for ticker, name in tickers.items():
            key = _exact_key(name)
            # A name that maps to more than one ticker is ambiguous; drop it
            # rather than pick one.
            if key in self.by_name and self.by_name[key] != ticker:
                self.by_name[key] = ""
            else:
                self.by_name.setdefault(key, ticker)

    def __len__(self) -> int:
        return len(self.tickers)

    def known(self, ticker: str) -> bool:
        return ticker in self.tickers


def _exact_key(name: str) -> str:
    """Case and whitespace folding only - no token or punctuation fuzzing."""
    return " ".join(name.upper().split())


def load_symbols(path: Path = SYMBOLS_PATH) -> SymbolTable:
    tickers: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            ticker = parts[0].strip().upper()
            if ticker:
                tickers.setdefault(ticker, parts[1].strip())
    return SymbolTable(tickers)


def ticker_from_parenthetical(asset_name: str) -> str | None:
    """Return the declared parenthetical ticker, if the filing states one.

    The ticker is the last parenthetical on the line in the Clerk's template,
    after any descriptive parentheticals.
    """
    candidates = [c for c in PAREN_RE.findall(asset_name) if TICKER_SHAPE.match(c)]
    return candidates[-1] if candidates else None


def ticker_from_exact_name(asset_name: str, symbols: SymbolTable) -> str | None:
    return symbols.by_name.get(_exact_key(asset_name)) or None


def resolve_ticker(asset_name: str, symbols: SymbolTable) -> str | None:
    """Resolve a ticker, or None. Never guesses."""
    ticker = ticker_from_parenthetical(asset_name)
    if ticker:
        return ticker
    return ticker_from_exact_name(asset_name, symbols)
