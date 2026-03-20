"""
option_chain_manager.py
-----------------------
Handles all MongoDB option chain queries.

Actual document schema:
{
  "timestamp":  "2025-10-01T09:16:00",
  "underlying": "NIFTY",
  "expiry":     "2025-10-07",
  "strike":     23900,
  "type":       "CE",          ← "CE" or "PE"
  "close":      771.25,        ← current option price
  "oi":         11700,
  "iv":         0.15370882,
  "delta":      0.94088984,
  "spot_price": 24638.1
}

One document = one option (one CE or PE at one strike).
"""

import logging
from typing import Optional

from pymongo import MongoClient

from .market_calendar import MarketCalendar

logger = logging.getLogger(__name__)


class OptionChainManager:

    def __init__(self, mongo_uri: str = "mongodb://localhost:27017/"):
        self._client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        self._collection = self._client["stock_data"]["option_chain"]
        self._calendar = MarketCalendar(mongo_uri)

    # ------------------------------------------------------------------
    # Backtest timestamp helpers
    # ------------------------------------------------------------------

    def get_backtest_timestamps(
        self, start_date: str, end_date: str, daily_cutoff: str
    ) -> list[str]:
        """
        Return all distinct timestamps between start_date and end_date (inclusive),
        skipping any timestamps that fall on market holidays or weekends.

        Args:
            start_date:   "YYYY-MM-DD"
            end_date:     "YYYY-MM-DD"
            daily_cutoff: "HH:MM"  — position_end_time, no ticks after this

        Example:
          start_date   = "2025-10-01"
          end_date     = "2025-10-10"
          daily_cutoff = "15:29"
          holidays     = {"2025-10-02", "2025-10-04", "2025-10-05"}

          Valid trading days = [Oct 1, Oct 3, Oct 6, Oct 7, Oct 8, Oct 9, Oct 10]
          → timestamps on those days within HH:MM <= 15:29 only
        """
        # Resolve start_date if it falls on a holiday
        resolved_start = self._calendar.resolve_start_date(start_date)

        trading_days: set[str] = set(
            self._calendar.get_trading_days(resolved_start, end_date)
        )

        all_ts: list[str] = sorted(self._collection.distinct("timestamp"))

        result = []
        for ts in all_ts:
            ts_date = self._extract_date(ts)
            ts_time = self._extract_time(ts)

            if ts_date < resolved_start or ts_date > end_date:
                continue
            if ts_date not in trading_days:
                continue  # holiday or weekend — skip entire day
            if ts_time > daily_cutoff:
                continue  # after position_end_time — skip tick
            result.append(ts)

        logger.info(
            f"Backtest timestamps: {len(result)} ticks across "
            f"{len(trading_days)} trading days | {resolved_start} → {end_date} | cutoff={daily_cutoff}"
        )
        return result

    @staticmethod
    def _extract_time(ts: str) -> str:
        """Extract HH:MM from a timestamp string."""
        sep = "T" if "T" in ts else " "
        return ts.split(sep)[-1][:5]  # "HH:MM"

    @staticmethod
    def _extract_date(ts: str) -> str:
        """Extract YYYY-MM-DD from a timestamp string."""
        sep = "T" if "T" in ts else " "
        return ts.split(sep)[0]  # "YYYY-MM-DD"

    # ------------------------------------------------------------------
    # Chain fetching
    # ------------------------------------------------------------------

    def fetch_chain(self, timestamp: str) -> list[dict]:
        """Return all documents for a given timestamp (all expiries)."""
        docs = list(self._collection.find({"timestamp": timestamp}, {"_id": 0}))
        if not docs:
            logger.warning(f"No data for timestamp={timestamp}")
        return docs

    def fetch_chain_for_expiry(self, timestamp: str, expiry: str) -> list[dict]:
        """Return chain documents filtered by a specific expiry date."""
        docs = list(
            self._collection.find(
                {"timestamp": timestamp, "expiry": expiry}, {"_id": 0}
            )
        )
        if not docs:
            logger.warning(f"No data for timestamp={timestamp} expiry={expiry}")
        return docs

    def get_available_expiries(self, timestamp: str) -> list[str]:
        """Return all distinct expiry dates available at this timestamp, sorted."""
        docs = self._collection.find({"timestamp": timestamp}, {"_id": 0, "expiry": 1})
        return sorted({str(doc["expiry"]) for doc in docs if "expiry" in doc})

    # ------------------------------------------------------------------
    # Field extraction
    # ------------------------------------------------------------------

    def get_spot_price(self, chain: list[dict]) -> float:
        for doc in chain:
            if doc.get("spot_price"):
                return float(doc["spot_price"])
        raise ValueError("spot_price not found in chain")

    def get_expiry(self, chain: list[dict]) -> str:
        for doc in chain:
            if doc.get("expiry"):
                return str(doc["expiry"])
        return ""

    def get_sorted_strikes(self, chain: list[dict]) -> list[float]:
        return sorted({float(doc["strike"]) for doc in chain if "strike" in doc})

    def get_atm_strike(self, chain: list[dict], spot: float) -> float:
        strikes = self.get_sorted_strikes(chain)
        return min(strikes, key=lambda s: abs(s - spot))

    def get_strike_step(self, strikes: list[float]) -> float:
        if len(strikes) < 2:
            return 50.0
        return min(strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1))

    # ------------------------------------------------------------------
    # Price lookup  (one doc per CE/PE per strike)
    # ------------------------------------------------------------------

    def get_ce_premium(self, chain: list[dict], strike: float) -> float:
        for doc in chain:
            if float(doc.get("strike", 0)) == strike and doc.get("type") == "CE":
                return float(doc.get("close", 0.0))
        logger.debug(f"CE premium not found for strike={strike}")
        return 0.0

    def get_pe_premium(self, chain: list[dict], strike: float) -> float:
        for doc in chain:
            if float(doc.get("strike", 0)) == strike and doc.get("type") == "PE":
                return float(doc.get("close", 0.0))
        logger.debug(f"PE premium not found for strike={strike}")
        return 0.0

    # ------------------------------------------------------------------
    # Closest-premium strike selection  (hedge_type = 2)
    # ------------------------------------------------------------------

    def get_closest_premium_ce(
        self, chain: list[dict], target_premium: float
    ) -> tuple[float, float]:
        """
        Return (strike, close_price) for the CE option whose close price
        is closest to target_premium.
        """
        ce_docs = [doc for doc in chain if doc.get("type") == "CE"]
        if not ce_docs:
            return (0.0, 0.0)
        best = min(ce_docs, key=lambda d: abs(float(d.get("close", 0)) - target_premium))
        return (float(best["strike"]), float(best.get("close", 0.0)))

    def get_closest_premium_pe(
        self, chain: list[dict], target_premium: float
    ) -> tuple[float, float]:
        """
        Return (strike, close_price) for the PE option whose close price
        is closest to target_premium.
        """
        pe_docs = [doc for doc in chain if doc.get("type") == "PE"]
        if not pe_docs:
            return (0.0, 0.0)
        best = min(pe_docs, key=lambda d: abs(float(d.get("close", 0)) - target_premium))
        return (float(best["strike"]), float(best.get("close", 0.0)))

    # ------------------------------------------------------------------
    # OTM strike traversal
    # ------------------------------------------------------------------

    def get_nth_otm_ce(self, strikes: list[float], atm: float, n: int) -> float:
        above = [s for s in strikes if s > atm]
        return above[n - 1] if len(above) >= n else (above[-1] if above else atm)

    def get_nth_otm_pe(self, strikes: list[float], atm: float, n: int) -> float:
        below = sorted((s for s in strikes if s < atm), reverse=True)
        return below[n - 1] if len(below) >= n else (below[-1] if below else atm)

    def get_5th_otm_premium(
        self, chain: list[dict], atm: float, strikes: list[float]
    ) -> float:
        ce_strike = self.get_nth_otm_ce(strikes, atm, 5)
        pe_strike = self.get_nth_otm_pe(strikes, atm, 5)
        ce_prem = self.get_ce_premium(chain, ce_strike)
        pe_prem = self.get_pe_premium(chain, pe_strike)
        avg = (ce_prem + pe_prem) / 2
        logger.debug(
            f"5th OTM → CE {ce_strike}@{ce_prem:.2f}  PE {pe_strike}@{pe_prem:.2f}  avg={avg:.2f}"
        )
        return avg

    def close(self) -> None:
        self._calendar.close()
        self._client.close()
