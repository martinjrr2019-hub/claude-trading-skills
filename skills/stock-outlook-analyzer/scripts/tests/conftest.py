"""Shared fixtures for Stock Outlook Analyzer tests (synthetic, offline data)."""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from outlook_fixtures import make_bars, make_options  # noqa: E402


@pytest.fixture
def up_bars():
    return make_bars(daily_drift=0.0015)


@pytest.fixture
def down_bars():
    return make_bars(daily_drift=-0.0015)


@pytest.fixture
def sample_input(up_bars):
    spot = float(up_bars[-1]["close"]) * 1.002
    as_of = dt.date(2026, 11, 20)
    return {
        "symbol": "FICT",
        "as_of": as_of.isoformat(),
        "snapshot": {
            "symbol": "FICT",
            "price": f"{spot:.4f}",
            "fifty_two_wk_high": f"{spot * 1.05:.2f}",
            "fifty_two_wk_low": f"{spot * 0.6:.2f}",
            "pe_ratio": "25.0",
        },
        "bars": up_bars,
        "analyst_rating": {
            "number": "10",
            "strong_buy": "4",
            "buy": "3",
            "hold": "2",
            "under_perform": "1",
            "sell": "0",
        },
        "analyst_target": {"mean": f"{spot * 1.1:.2f}", "low": "1", "high": "999"},
        "earnings": [
            {"expected_publish_date": "2026-11-02", "eps_actual": "2.1", "eps_est": "2.0"},
            {"expected_publish_date": "2027-01-29", "eps_est": "2.2"},
        ],
        "news": [
            {"title": "FICT beats", "sentiment": "POSITIVE", "related_symbols": ["FICT"]},
            {"title": "FICT sued", "sentiment": "NEGATIVE", "related_symbols": ["FICT"]},
            {"title": "FICT launches", "sentiment": "POSITIVE", "related_symbols": ["FICT"]},
            {"title": "Other co", "sentiment": "NEGATIVE", "related_symbols": ["OTHR"]},
        ],
        "options": make_options("FICT", spot, as_of, {7: 0.30, 30: 0.28, 90: 0.26, 180: 0.25}),
    }
