#!/usr/bin/env python3
"""Stock outlook engine: probability ranges, evidence scoring and a self-scored track record.

Reads one JSON file of market data (Webull-shaped; see references/input_schema.md) and
writes a Markdown + JSON report. It does NOT predict prices. It estimates:

- Likely price ranges per horizon (68% / 95%) from options implied volatility, falling back
  to historical volatility, under a lognormal model.
- A small, capped directional tilt per horizon from trend, momentum, analyst, earnings and
  news evidence. One-day tilt is always neutral: there is no reliable edge at that horizon.
- A backtest of the same method on the stock's own history (range coverage, tilt hit rate).
- A forecast log: each run records its ranges; later runs score matured forecasts.

Usage:
    python3 stock_outlook.py --input data.json --output-dir reports/ \
        --log-path state/stock_outlook/forecast_log.jsonl

Standard library only. Educational analysis; not financial advice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
import sys
from pathlib import Path

TRADING_DAYS = 252
MIN_BARS = 60
NORMAL = statistics.NormalDist()

HORIZONS = [
    ("1 day", 1),
    ("1 week", 5),
    ("1 month", 21),
    ("3 months", 63),
    ("6 months", 126),
]

# Weight of each evidence component per horizon, and the cap on how far the tilt can move
# the probability of an up move away from 50%. The caps are deliberately small.
WEIGHTS = {
    "1 day": {},
    "1 week": {
        "trend": 0.25,
        "momentum_1m": 0.30,
        "rsi": 0.20,
        "macd": 0.15,
        "news_sentiment": 0.10,
    },
    "1 month": {
        "trend": 0.25,
        "momentum_1m": 0.15,
        "momentum_6m": 0.20,
        "rsi": 0.10,
        "macd": 0.10,
        "analyst": 0.10,
        "news_sentiment": 0.05,
        "relative_strength": 0.05,
    },
    "3 months": {
        "trend": 0.30,
        "momentum_6m": 0.20,
        "momentum_12_1": 0.20,
        "analyst": 0.15,
        "earnings_surprise": 0.10,
        "relative_strength": 0.05,
    },
    "6 months": {
        "trend": 0.25,
        "momentum_12_1": 0.30,
        "analyst": 0.25,
        "earnings_surprise": 0.10,
        "momentum_6m": 0.10,
    },
}
MAX_EDGE = {"1 day": 0.0, "1 week": 0.03, "1 month": 0.05, "3 months": 0.07, "6 months": 0.08}
NEUTRAL_BAND = 0.15


# ── Small helpers ─────────────────────────────────────────────────────


def num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def parse_date(value) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).date()
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def add_trading_days(start: dt.date, n: int) -> dt.date:
    """Weekday arithmetic (exchange holidays ignored)."""
    d = start
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def parse_bars(raw) -> list[dict]:
    """Webull bar dicts, or compact rows [date, open, high, low, close, volume] / [date, close]."""
    out: dict[dt.date, dict] = {}
    for row in raw or []:
        if isinstance(row, (list, tuple)):
            if len(row) == 2:
                row = {"date": row[0], "close": row[1]}
            elif len(row) >= 5:
                keys = ("date", "open", "high", "low", "close", "volume")
                row = dict(zip(keys, row))
            else:
                continue
        if not isinstance(row, dict):
            continue
        d = parse_date(row.get("time") or row.get("date"))
        close = num(row.get("close"))
        if d is None or close is None or close <= 0:
            continue
        out[d] = {
            "date": d,
            "open": num(row.get("open")) or close,
            "high": num(row.get("high")) or close,
            "low": num(row.get("low")) or close,
            "close": close,
            "volume": num(row.get("volume")) or 0.0,
        }
    return [out[d] for d in sorted(out)]


_OCC = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


def parse_occ(symbol: str):
    """Parse an OCC option symbol into (root, expiry, 'call'|'put', strike)."""
    m = _OCC.match(str(symbol).strip().upper())
    if not m:
        return None
    root, ymd, kind, strike = m.groups()
    expiry = dt.datetime.strptime(ymd, "%y%m%d").date()
    return root, expiry, "call" if kind == "C" else "put", int(strike) / 1000.0


# ── Indicators ────────────────────────────────────────────────────────


def sma(values, n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def ema_series(values, n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(0.0, b - a))
        losses.append(max(0.0, a - b))
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, loss in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + loss) / n
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(bars, n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(
            max(
                cur["high"] - cur["low"],
                abs(cur["high"] - prev["close"]),
                abs(cur["low"] - prev["close"]),
            )
        )
    return sum(trs[-n:]) / n


def realized_vol(closes, n: int, periods_per_year: int = TRADING_DAYS) -> float | None:
    if len(closes) <= n:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1 : -1], closes[-n:])]
    return statistics.stdev(rets) * math.sqrt(periods_per_year) if len(rets) > 1 else None


# ── Volatility and ranges ─────────────────────────────────────────────


def atm_iv_term_structure(options, spot: float, as_of: dt.date) -> list[tuple[int, float]]:
    """[(calendar days to expiry, ATM IV)] using the strike nearest spot for each expiry."""
    by_exp: dict[dt.date, list[tuple[float, float]]] = {}
    for opt in options or []:
        parsed = parse_occ(opt.get("symbol", ""))
        iv = num(opt.get("imp_vol"))
        if not parsed or iv is None or not 0.01 <= iv <= 5.0:
            continue
        _, expiry, _, strike = parsed
        if expiry <= as_of:
            continue
        by_exp.setdefault(expiry, []).append((strike, iv))
    term = []
    for expiry in sorted(by_exp):
        rows = by_exp[expiry]
        best = min(abs(k - spot) for k, _ in rows)
        if best > spot * 0.10:
            continue
        ivs = [iv for k, iv in rows if abs(k - spot) == best]
        term.append(((expiry - as_of).days, sum(ivs) / len(ivs)))
    return term


def interpolate_iv(term, days: float) -> float | None:
    """Interpolate in total variance (iv^2 * t); flat outside the quoted expiries."""
    if not term:
        return None
    if days <= term[0][0]:
        return term[0][1]
    if days >= term[-1][0]:
        return term[-1][1]
    for (d0, v0), (d1, v1) in zip(term, term[1:]):
        if d0 <= days <= d1:
            w0, w1 = v0 * v0 * d0, v1 * v1 * d1
            w = w0 + (w1 - w0) * (days - d0) / (d1 - d0)
            return math.sqrt(w / days)
    return term[-1][1]


def horizon_sigma(trading_days: int, term, hv: float | None):
    iv = interpolate_iv(term, trading_days * 365 / TRADING_DAYS)
    if iv is not None:
        return iv, "implied"
    return hv, "historical"


def price_range(spot: float, sigma: float, T: float, prob: float) -> tuple[float, float]:
    """Central ``prob`` interval of a driftless lognormal price after T years."""
    z = NORMAL.inv_cdf(0.5 + prob / 2)
    mu = math.log(spot) - 0.5 * sigma * sigma * T
    s = sigma * math.sqrt(T)
    return math.exp(mu - z * s), math.exp(mu + z * s)


# ── Evidence ──────────────────────────────────────────────────────────


def _component(score, value, note, available=True):
    return {"score": round(clip(score), 4), "value": value, "note": note, "available": available}


def _missing(note):
    return _component(0.0, None, note, available=False)


def _momentum(closes, spot, start_back, end_back, hv, label, ppy=TRADING_DAYS):
    if len(closes) < start_back + 1 or not hv:
        return _missing(f"{label}: not enough history")
    start = closes[-start_back - 1]
    end = spot if end_back == 0 else closes[-end_back - 1]
    ret = end / start - 1
    periods = start_back - end_back
    z = math.log(end / start) / (hv * math.sqrt(periods / ppy))
    return _component(z / 2, round(ret, 4), f"{label} return {ret:+.1%} ({z:+.1f} sd)")


def long_term_basis(bars, weekly_bars):
    """Pick the series for trend / 6- and 12-month momentum: (closes, periods per year, name)."""
    if len(bars) > TRADING_DAYS:
        return [b["close"] for b in bars], TRADING_DAYS, "daily"
    if weekly_bars and len(weekly_bars) >= 41:
        return [b["close"] for b in weekly_bars], 52, "weekly"
    return [b["close"] for b in bars], TRADING_DAYS, "daily"


def compute_signals(
    bars,
    spot,
    analyst_rating=None,
    analyst_target=None,
    earnings=None,
    news=None,
    symbol="",
    as_of=None,
    benchmark_bars=None,
    long_closes=None,
    long_ppy=TRADING_DAYS,
) -> dict:
    closes = [b["close"] for b in bars]
    hv60 = realized_vol(closes, 60) or realized_vol(closes, 20)
    lc = long_closes if long_closes is not None else closes
    f = long_ppy / TRADING_DAYS
    long_hv = realized_vol(lc, max(8, round(60 * f)), long_ppy) or hv60
    sig = {}

    s50, s200 = sma(lc, max(2, round(50 * f))), sma(lc, max(3, round(200 * f)))
    if s50 and s200:
        parts = [spot > s50, spot > s200, s50 > s200]
        score = sum(1 if p else -1 for p in parts) / 3
        sig["trend"] = _component(
            score,
            {"sma50": round(s50, 2), "sma200": round(s200, 2)},
            f"price {'above' if spot > s50 else 'below'} 50-day, "
            f"{'above' if spot > s200 else 'below'} 200-day; 50-day "
            f"{'above' if s50 > s200 else 'below'} 200-day",
        )
    elif s50:
        sig["trend"] = _component(
            0.5 if spot > s50 else -0.5,
            {"sma50": round(s50, 2)},
            f"price {'above' if spot > s50 else 'below'} 50-day (no 200-day yet)",
        )
    else:
        sig["trend"] = _missing("not enough history for moving averages")

    sig["momentum_1m"] = _momentum(closes, spot, 21, 0, hv60, "1-month")
    sig["momentum_6m"] = _momentum(lc, spot, round(126 * f), 0, long_hv, "6-month", long_ppy)
    sig["momentum_12_1"] = _momentum(
        lc, spot, round(252 * f), max(1, round(21 * f)), long_hv, "12-month ex last month", long_ppy
    )

    r = rsi(closes + [spot])
    if r is None:
        sig["rsi"] = _missing("not enough history for RSI")
    else:
        score = -(r - 70) / 30 if r > 70 else ((30 - r) / 30 if r < 30 else 0.0)
        zone = "overbought" if r > 70 else ("oversold" if r < 30 else "neutral zone")
        sig["rsi"] = _component(
            score, round(r, 1), f"RSI(14) {r:.0f}, {zone} (mean-reversion read)"
        )

    a = atr(bars)
    if len(closes) >= 35 and a:
        series = closes + [spot]
        macd_line = [x - y for x, y in zip(ema_series(series, 12), ema_series(series, 26))]
        hist = macd_line[-1] - ema_series(macd_line, 9)[-1]
        sig["macd"] = _component(
            hist / a, round(hist, 4), f"MACD histogram {'positive' if hist > 0 else 'negative'}"
        )
    else:
        sig["macd"] = _missing("not enough history for MACD")

    sig["analyst"] = _analyst(analyst_rating, analyst_target, spot)
    sig["earnings_surprise"] = _earnings(earnings, as_of)
    sig["news_sentiment"] = _news(news, symbol)

    bench = parse_bars(benchmark_bars) if benchmark_bars else []
    if len(bench) > 63 and len(closes) > 63:
        own = spot / closes[-64] - 1
        other = bench[-1]["close"] / bench[-64]["close"] - 1
        diff = own - other
        sig["relative_strength"] = _component(
            diff / 0.10, round(diff, 4), f"3-month return vs benchmark {diff:+.1%}"
        )
    else:
        sig["relative_strength"] = _missing("no benchmark data")
    return sig


def _analyst(rating, target, spot):
    parts, notes, value = [], [], {}
    if rating:
        n = num(rating.get("number")) or 0
        if n > 0:
            sb, b = num(rating.get("strong_buy")) or 0, num(rating.get("buy")) or 0
            u, s = num(rating.get("under_perform")) or 0, num(rating.get("sell")) or 0
            rs = (2 * sb + b - u - 2 * s) / (2 * n)
            parts.append(rs)
            value["ratings"] = {
                "analysts": int(n),
                "buy_side": int(sb + b),
                "sell_side": int(u + s),
            }
            notes.append(f"{int(sb + b)}/{int(n)} buy-side ratings")
    mean = num(target.get("mean")) if target else None
    if mean:
        upside = mean / spot - 1
        parts.append(clip(upside / 0.25))
        value["mean_target"] = round(mean, 2)
        value["upside"] = round(upside, 4)
        notes.append(f"mean target ${mean:,.2f} ({upside:+.1%})")
    if not parts:
        return _missing("no analyst data")
    return _component(sum(parts) / len(parts), value, "; ".join(notes))


def _earnings(earnings, as_of):
    rows = []
    for e in earnings or []:
        d = parse_date(e.get("expected_publish_date"))
        a, est = num(e.get("eps_actual")), num(e.get("eps_est"))
        if d and a is not None and est and (as_of is None or d <= as_of):
            rows.append((d, a, est))
    if not rows:
        return _missing("no reported earnings with estimates")
    d, a, est = max(rows)
    surprise = (a - est) / abs(est)
    return _component(
        surprise / 0.10,
        round(surprise, 4),
        f"last EPS {a} vs est {est:.2f} ({surprise:+.1%}) on {d}",
    )


def _news(news, symbol):
    counts = {"positive": 0, "negative": 0, "neutral": 0}
    sym = symbol.upper()
    for item in news or []:
        related = [str(s).upper() for s in item.get("related_symbols") or []]
        if sym and sym not in related and sym not in str(item.get("title", "")).upper():
            continue
        s = str(item.get("sentiment") or "NEUTRAL").lower()
        counts[s if s in counts else "neutral"] += 1
    total = sum(counts.values())
    if total == 0:
        return _component(0.0, counts, "no recent news tagged to this symbol", available=False)
    score = (counts["positive"] - counts["negative"]) / total
    return _component(
        score,
        counts,
        f"{counts['positive']} positive / {counts['negative']} "
        f"negative / {counts['neutral']} neutral recent headlines",
    )


def tilt(signals: dict, label: str) -> dict:
    weights = {k: w for k, w in WEIGHTS[label].items() if signals.get(k, {}).get("available")}
    edge = MAX_EDGE[label]
    if edge == 0 or not weights:
        return {"score": 0.0, "p_up": 0.5, "tilt": "no reliable edge", "confidence": "low"}
    total = sum(weights.values())
    score = sum(signals[k]["score"] * w for k, w in weights.items()) / total
    nonzero = {k: w for k, w in weights.items() if signals[k]["score"] != 0}
    agree = (
        sum(w for k, w in nonzero.items() if (signals[k]["score"] > 0) == (score > 0))
        / sum(nonzero.values())
        if nonzero
        else 0.0
    )
    if abs(score) < NEUTRAL_BAND:
        label_txt = "neutral"
    else:
        side = "bullish" if score > 0 else "bearish"
        label_txt = f"{'lean ' if abs(score) < 0.5 else ''}{side}"
    long_enough = MAX_EDGE[label] >= MAX_EDGE["1 month"]
    confidence = "moderate" if long_enough and abs(score) >= 0.35 and agree >= 0.7 else "low"
    return {
        "score": round(score, 4),
        "p_up": round(0.5 + edge * score, 4),
        "tilt": label_txt,
        "confidence": confidence,
        "agreement": round(agree, 3),
    }


# ── Backtest ──────────────────────────────────────────────────────────


def _tech_score(closes, i, ppy=TRADING_DAYS):
    """Trend + momentum score using only closes[: i + 1] (no look-ahead).

    Lookbacks are defined in trading days and scaled to the bar frequency ``ppy``.
    """
    f = ppy / TRADING_DAYS
    s_fast, s_slow = max(2, round(50 * f)), max(3, round(200 * f))
    m6, m12, skip = max(2, round(126 * f)), max(3, round(252 * f)), max(1, round(21 * f))
    hist = closes[: i + 1]
    spot = hist[-1]
    a, b = sma(hist, s_fast), sma(hist, s_slow)
    hv = realized_vol(hist, max(8, round(60 * f)), ppy)
    if not (a and b and hv) or len(hist) < m12 + 1:
        return None
    trend = sum(1 if p else -1 for p in (spot > a, spot > b, a > b)) / 3
    mom6 = clip(math.log(spot / hist[-m6 - 1]) / (hv * math.sqrt(m6 / ppy)) / 2)
    mom12 = clip(
        math.log(hist[-skip - 1] / hist[-m12 - 1]) / (hv * math.sqrt((m12 - skip) / ppy)) / 2
    )
    return (trend + mom6 + mom12) / 3


def backtest(bars, horizons=None, periods_per_year: int = TRADING_DAYS) -> list[dict]:
    """Walk-forward check of historical-vol ranges and the trend/momentum tilt.

    ``horizons`` are (label, trading days); they are converted to bars of the given frequency.
    """
    horizons = horizons or (("1 week", 5), ("1 month", 21), ("3 months", 63))
    ppy = periods_per_year
    f = ppy / TRADING_DAYS
    start = max(3, round(TRADING_DAYS * f))
    vol_n = max(8, round(20 * f))
    closes = [b["close"] for b in bars]
    out = []
    for label, days in horizons:
        h = max(1, round(days * f))
        hits68 = hits95 = n = ups = calls = right = 0
        for i in range(start, len(closes) - h):
            hv = realized_vol(closes[: i + 1], vol_n, ppy)
            if not hv:
                continue
            lo68, hi68 = price_range(closes[i], hv, h / ppy, 0.68)
            lo95, hi95 = price_range(closes[i], hv, h / ppy, 0.95)
            future = closes[i + h]
            n += 1
            hits68 += lo68 <= future <= hi68
            hits95 += lo95 <= future <= hi95
            up = future > closes[i]
            ups += up
            score = _tech_score(closes, i, ppy)
            if score is not None and abs(score) >= NEUTRAL_BAND:
                calls += 1
                right += (score > 0) == up
        if n:
            row = {
                "horizon": label,
                "samples": n,
                "coverage68": round(hits68 / n, 3),
                "coverage95": round(hits95 / n, 3),
                "base_rate_up": round(ups / n, 3),
                "tilt_calls": calls,
                "tilt_accuracy": round(right / calls, 3) if calls else None,
            }
            row.update(backtest_verdict(row))
            out.append(row)
    return out


def backtest_verdict(row: dict) -> dict:
    """Plain-language read of one backtest row.

    The tilt is compared with the best naive rule (always "up" or always "down"), not with 50%.
    """
    cov = row["coverage68"]
    if abs(cov - 0.68) <= 0.07:
        rng = "well calibrated"
    elif cov < 0.68:
        rng = "too narrow"
    else:
        rng = "too wide"
    acc = row.get("tilt_accuracy")
    naive = max(row["base_rate_up"], 1 - row["base_rate_up"])
    if not row.get("tilt_calls") or acc is None:
        return {"range": rng, "tilt_edge": None, "tilt_beats_naive": None}
    edge = acc - naive
    return {"range": rng, "tilt_edge": round(edge, 3), "tilt_beats_naive": edge > 0}


# Backtest row used to vet each horizon's tilt (6 months uses the longest tested horizon).
BACKTEST_PROXY = {
    "1 week": "1 week",
    "1 month": "1 month",
    "3 months": "3 months",
    "6 months": "3 months",
}


def apply_backtest_caution(t: dict, label: str, bt_by_label: dict) -> dict:
    """Downgrade a tilt whose backtest on this stock failed to beat the naive rule."""
    row = bt_by_label.get(BACKTEST_PROXY.get(label, ""))
    caution = bool(row and row.get("tilt_beats_naive") is False and MAX_EDGE[label] > 0)
    out = dict(t, backtest_caution=caution)
    if caution:
        out["confidence"] = "low"
        if out["tilt"] != "neutral":
            out["tilt"] = f"{out['tilt']} (unproven)"
    return out


# ── Forecast log ──────────────────────────────────────────────────────


def load_log(path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    entries = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_log_entry(path, entry: dict) -> None:
    p = Path(path)
    entries = [e for e in load_log(p) if e.get("id") != entry["id"]]
    entries.append(entry)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))


def log_entry(report: dict) -> dict:
    return {
        "id": f"{report['symbol']}-{report['as_of']}",
        "symbol": report["symbol"],
        "as_of": report["as_of"],
        "spot": report["spot"],
        "horizons": [
            {
                k: h[k]
                for k in ("label", "target_date", "low68", "high68", "low95", "high95", "p_up")
            }
            for h in report["horizons"]
        ],
    }


def score_log(entries, symbol: str, bars) -> dict:
    last = bars[-1]["date"] if bars else None
    details, pending = [], 0
    for e in entries:
        if e.get("symbol") != symbol:
            continue
        for h in e.get("horizons", []):
            target = parse_date(h.get("target_date"))
            if target is None or last is None or target > last:
                pending += 1
                continue
            close = next((b["close"] for b in bars if b["date"] >= target), None)
            if close is None:
                pending += 1
                continue
            p_up = h.get("p_up", 0.5)
            details.append(
                {
                    "as_of": e.get("as_of"),
                    "horizon": h.get("label"),
                    "target_date": h.get("target_date"),
                    "actual": round(close, 4),
                    "hit68": h["low68"] <= close <= h["high68"],
                    "hit95": h["low95"] <= close <= h["high95"],
                    "direction_called": p_up != 0.5,
                    "direction_right": (close > e["spot"]) == (p_up > 0.5) if p_up != 0.5 else None,
                }
            )
    calls = [d for d in details if d["direction_called"]]
    n = len(details)
    return {
        "matured": n,
        "pending": pending,
        "hit68_rate": round(sum(d["hit68"] for d in details) / n, 3) if n else None,
        "hit95_rate": round(sum(d["hit95"] for d in details) / n, 3) if n else None,
        "direction_calls": len(calls),
        "direction_accuracy": round(sum(d["direction_right"] for d in calls) / len(calls), 3)
        if calls
        else None,
        "details": details[-20:],
    }


# ── Analysis ──────────────────────────────────────────────────────────


def analyze(data: dict) -> dict:
    symbol = str(data.get("symbol") or (data.get("snapshot") or {}).get("symbol") or "").upper()
    if not symbol:
        raise ValueError("input needs a 'symbol'")
    bars = parse_bars(data.get("bars"))
    if len(bars) < MIN_BARS:
        raise ValueError(f"need at least {MIN_BARS} daily bars, got {len(bars)}")
    snap = data.get("snapshot") or {}
    spot = num(snap.get("price")) or bars[-1]["close"]
    as_of = (
        parse_date(data.get("as_of")) or parse_date(snap.get("last_trade_time")) or bars[-1]["date"]
    )
    closes = [b["close"] for b in bars]
    hv20, hv60 = realized_vol(closes, 20), realized_vol(closes, 60)
    term = atm_iv_term_structure(data.get("options"), spot, as_of)

    weekly = parse_bars(data.get("weekly_bars"))
    long_closes, long_ppy, long_basis = long_term_basis(bars, weekly)
    signals = compute_signals(
        bars,
        spot,
        data.get("analyst_rating"),
        data.get("analyst_target"),
        data.get("earnings"),
        data.get("news"),
        symbol,
        as_of,
        data.get("benchmark_bars"),
        long_closes,
        long_ppy,
    )

    next_earn = parse_date(data.get("next_earnings_date"))
    if next_earn is None:
        future = [parse_date(e.get("expected_publish_date")) for e in data.get("earnings") or []]
        future = sorted(d for d in future if d and d > as_of)
        next_earn = future[0] if future else None

    if weekly:
        bt, bt_basis = backtest(weekly, periods_per_year=52), "weekly"
    else:
        bt, bt_basis = backtest(bars), "daily"
    bt_by_label = {row["horizon"]: row for row in bt}

    horizons = []
    for label, days in HORIZONS:
        sigma, source = horizon_sigma(days, term, hv20 if days <= 5 else (hv60 or hv20))
        if not sigma:
            raise ValueError("no implied or historical volatility available")
        T = days / TRADING_DAYS
        lo68, hi68 = price_range(spot, sigma, T, 0.68)
        lo95, hi95 = price_range(spot, sigma, T, 0.95)
        target = add_trading_days(as_of, days)
        horizons.append(
            {
                "label": label,
                "trading_days": days,
                "target_date": target.isoformat(),
                "sigma": round(sigma, 4),
                "vol_source": source,
                "expected_move": round(spot * sigma * math.sqrt(T), 2),
                "low68": round(lo68, 2),
                "high68": round(hi68, 2),
                "low95": round(lo95, 2),
                "high95": round(hi95, 2),
                "includes_earnings": bool(next_earn and next_earn <= target),
                **apply_backtest_caution(tilt(signals, label), label, bt_by_label),
            }
        )

    by_label = {h["label"]: h for h in horizons}
    hi20 = max(b["high"] for b in bars[-20:])
    lo20 = min(b["low"] for b in bars[-20:])
    lf = long_ppy / TRADING_DAYS
    s50 = sma(long_closes, max(2, round(50 * lf)))
    s200 = sma(long_closes, max(3, round(200 * lf)))
    scenarios = []
    for label in ("1 month", "3 months"):
        h = by_label[label]
        base = spot * math.exp(-0.5 * h["sigma"] ** 2 * h["trading_days"] / TRADING_DAYS)
        scenarios.append(
            {
                "horizon": label,
                "bear": h["low68"],
                "base": round(base, 2),
                "bull": h["high68"],
                "bull_trigger": round(hi20 if hi20 > spot * 1.001 else h["high68"], 2),
                "bear_trigger": round(max(lo20, s50) if s50 and s50 < spot else lo20, 2),
            }
        )

    last_year = bars[-TRADING_DAYS:]
    tgt = data.get("analyst_target") or {}
    key_levels = {
        "sma20": sma(closes, 20),
        "sma50": s50,
        "sma200": s200,
        "high_20d": hi20,
        "low_20d": lo20,
        "high_52w": num(snap.get("fifty_two_wk_high")) or max(b["high"] for b in last_year),
        "low_52w": num(snap.get("fifty_two_wk_low")) or min(b["low"] for b in last_year),
        "analyst_target_low": num(tgt.get("low")),
        "analyst_target_mean": num(tgt.get("mean")),
        "analyst_target_high": num(tgt.get("high")),
    }
    key_levels = {k: round(v, 2) for k, v in key_levels.items() if v is not None}

    iv30 = interpolate_iv(term, 30)
    options_context = {
        "iv_term": [{"days": d, "iv": round(v, 4)} for d, v in term],
        "iv30": round(iv30, 4) if iv30 else None,
        "hv20": round(hv20, 4) if hv20 else None,
        "hv60": round(hv60, 4) if hv60 else None,
        "iv_hv_ratio": round(iv30 / hv20, 2) if iv30 and hv20 else None,
    }

    headlines = []
    for item in data.get("news") or []:
        related = [str(s).upper() for s in item.get("related_symbols") or []]
        if symbol in related or symbol in str(item.get("title", "")).upper():
            headlines.append(
                {k: item.get(k) for k in ("publish_time", "title", "sentiment", "source")}
            )

    fundamentals = {
        k: num(snap.get(k))
        for k in ("pe_ratio", "pb_ratio", "ps_ratio", "eps_ttm", "yield", "market_value")
        if num(snap.get(k)) is not None
    }

    return {
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "spot": round(spot, 4),
        "horizons": horizons,
        "signals": signals,
        "scenarios": scenarios,
        "key_levels": key_levels,
        "options_context": options_context,
        "next_earnings": next_earn.isoformat() if next_earn else None,
        "fundamentals": fundamentals,
        "headlines": headlines[:10],
        "backtest": bt,
        "backtest_basis": bt_basis,
        "long_term_basis": long_basis,
        "method": {
            "ranges": "driftless lognormal; ATM implied vol term structure, historical vol fallback",
            "tilt_caps": MAX_EDGE,
            "moving_averages": "50/200-day, or 10/40-week when daily history is under a year",
            "neutral_band": NEUTRAL_BAND,
        },
    }


# ── Markdown ──────────────────────────────────────────────────────────


def _money(v):
    return "n/a" if v is None else f"${v:,.2f}"


def _pct(v):
    return "n/a" if v is None else f"{v:.0%}"


def render_markdown(report: dict, track: dict | None = None) -> str:
    r = report
    oc = r["options_context"]
    lines = [
        f"# Stock Outlook: {r['symbol']}",
        "",
        f"**As of:** {r['as_of']}  |  **Price:** {_money(r['spot'])}  |  "
        f"**Next earnings:** {r['next_earnings'] or 'not found'}",
        "",
        "> This is a probabilistic outlook, not a prediction or financial advice. Ranges come from "
        "options-implied volatility (or historical volatility) under a lognormal model; the "
        "directional tilt is deliberately capped and is often wrong.",
        "",
        "## Probability Ranges",
        "",
        "| Horizon | Target date | 68% range | 95% range | Expected move | P(up) | Tilt | Confidence | Vol |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for h in r["horizons"]:
        flag = " (earnings inside)" if h["includes_earnings"] else ""
        lines.append(
            f"| {h['label']}{flag} | {h['target_date']} | {_money(h['low68'])} - {_money(h['high68'])} "
            f"| {_money(h['low95'])} - {_money(h['high95'])} | ±{_money(h['expected_move'])} "
            f"| {h['p_up']:.0%} | {h['tilt']} | {h['confidence']} "
            f"| {h['sigma']:.0%} {h['vol_source']} |"
        )
    if any(h.get("backtest_caution") for h in r["horizons"]):
        lines += [
            "",
            "_(unproven)_: on this stock's history the tilt did not beat simply guessing the more "
            "common direction for that horizon, so its confidence is set to low.",
        ]
    lines += [
        "",
        f"Implied vol (30-day): {_pct(oc['iv30'])}; realized vol 20-day {_pct(oc['hv20'])}, "
        f"60-day {_pct(oc['hv60'])}"
        + (f"; IV/HV ratio {oc['iv_hv_ratio']}" if oc["iv_hv_ratio"] else ""),
        "",
        "## Evidence",
        "",
        "| Component | Score (-1 to +1) | Reading |",
        "|---|---|---|",
    ]
    for name, c in r["signals"].items():
        score = f"{c['score']:+.2f}" if c["available"] else "n/a"
        lines.append(f"| {name.replace('_', ' ')} | {score} | {c['note']} |")
    lines += [
        "",
        "## Scenarios",
        "",
        "| Horizon | Bear | Base | Bull | Bull trigger | Bear trigger |",
        "|---|---|---|---|---|---|",
    ]
    for s in r["scenarios"]:
        lines.append(
            f"| {s['horizon']} | {_money(s['bear'])} | {_money(s['base'])} | {_money(s['bull'])} "
            f"| close above {_money(s['bull_trigger'])} | close below {_money(s['bear_trigger'])} |"
        )
    lines += ["", "## Key Levels", ""]
    lines += [f"- {k.replace('_', ' ')}: {_money(v)}" for k, v in r["key_levels"].items()]
    if r["fundamentals"]:
        lines += ["", "## Fundamentals Snapshot", ""]
        lines += [f"- {k}: {v:,.4g}" for k, v in r["fundamentals"].items()]
    if r["headlines"]:
        lines += ["", "## Recent Headlines", ""]
        for h in r["headlines"]:
            lines.append(
                f"- [{h.get('sentiment') or 'n/a'}] {h.get('title')} ({h.get('publish_time')})"
            )
    lines += [
        "",
        f"## Backtest (this stock's own history, {r.get('backtest_basis', 'daily')} bars)",
        "",
    ]
    if r["backtest"]:
        lines += [
            "| Horizon | Samples | 68% range hit | 95% range hit | Up base rate | Tilt calls | Tilt hit rate |",
            "|---|---|---|---|---|---|---|",
        ]
        for b in r["backtest"]:
            lines.append(
                f"| {b['horizon']} | {b['samples']} | {_pct(b['coverage68'])} | {_pct(b['coverage95'])} "
                f"| {_pct(b['base_rate_up'])} | {b['tilt_calls']} | {_pct(b['tilt_accuracy'])} |"
            )
        lines += ["", "### What the backtest says", ""]
        for b in r["backtest"]:
            rng = {
                "well calibrated": "ranges were about right",
                "too narrow": "ranges were too narrow (real moves were bigger than the volatility implied)",
                "too wide": "ranges were wider than needed",
            }[b["range"]]
            if b["tilt_beats_naive"] is None:
                tilt_txt = "the tilt made no calls"
            elif b["tilt_beats_naive"]:
                tilt_txt = (
                    f"the tilt beat the best naive guess by {b['tilt_edge']:+.0%}, "
                    "a small edge that may not persist"
                )
            else:
                tilt_txt = (
                    f"the tilt did NOT beat simply guessing the more common direction "
                    f"({b['tilt_edge']:+.0%}), so treat it as uninformative here"
                )
            lines.append(f"- {b['horizon']}: {rng}; {tilt_txt}.")
    else:
        lines.append("Not enough history (needs more than one year of bars beyond the horizon).")
    if track is not None:
        lines += ["", "## Track Record (logged forecasts)", ""]
        if track["matured"]:
            lines.append(
                f"{track['matured']} matured forecasts: 68% range hit {_pct(track['hit68_rate'])}, "
                f"95% range hit {_pct(track['hit95_rate'])}, direction right "
                f"{_pct(track['direction_accuracy'])} of {track['direction_calls']} calls. "
                f"{track['pending']} still pending."
            )
        else:
            lines.append(f"No matured forecasts yet ({track['pending']} pending).")
    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", required=True, help="JSON market data file")
    parser.add_argument("--output-dir", default="reports/", help="report directory")
    parser.add_argument(
        "--log-path",
        default="state/stock_outlook/forecast_log.jsonl",
        help="forecast log (JSON lines)",
    )
    parser.add_argument("--no-log", action="store_true", help="do not read or write the log")
    args = parser.parse_args(argv)

    try:
        data = json.loads(Path(args.input).read_text())
        if not isinstance(data, dict):
            raise ValueError("input must be a JSON object")
        report = analyze(data)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    track = None
    if not args.no_log:
        track = score_log(load_log(args.log_path), report["symbol"], parse_bars(data.get("bars")))
        report["track_record"] = track

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"stock_outlook_{report['symbol']}_{report['as_of']}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str))
    stem.with_suffix(".md").write_text(render_markdown(report, track))
    if not args.no_log:
        save_log_entry(args.log_path, log_entry(report))
    print(f"Wrote {stem}.md and {stem}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
