"""Synthetic, offline data builders for Stock Outlook Analyzer tests."""

import datetime as dt
import math


def make_bars(n=400, start=100.0, daily_drift=0.0, wiggle=0.01, end=dt.date(2026, 11, 19)):
    """Deterministic weekday bars ending at ``end`` (oldest first), Webull-shaped strings."""
    dates = []
    d = end
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d -= dt.timedelta(days=1)
    dates.reverse()
    bars, price = [], start
    for i, day in enumerate(dates):
        price *= math.exp(daily_drift + wiggle * math.sin(i * 1.7) * 1.3)
        bars.append(
            {
                "time": f"{day.isoformat()}T05:00:00.000+0000",
                "open": f"{price * 0.998:.4f}",
                "high": f"{price * 1.01:.4f}",
                "low": f"{price * 0.99:.4f}",
                "close": f"{price:.4f}",
                "volume": "1000000",
            }
        )
    return bars


def occ(symbol, expiry, kind, strike):
    return f"{symbol}{expiry:%y%m%d}{kind}{int(round(strike * 1000)):08d}"


def make_options(symbol, spot, as_of, ivs_by_days):
    """ATM call/put snapshots (plus an off-ATM decoy) for each expiry offset in days."""
    out = []
    atm = round(spot)
    for days, iv in ivs_by_days.items():
        exp = as_of + dt.timedelta(days=days)
        for kind in ("C", "P"):
            out.append({"symbol": occ(symbol, exp, kind, atm), "imp_vol": str(iv)})
            out.append({"symbol": occ(symbol, exp, kind, atm * 1.3), "imp_vol": "0.90"})
    return out
