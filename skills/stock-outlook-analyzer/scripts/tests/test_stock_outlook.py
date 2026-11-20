"""Tests for the stock outlook engine (synthetic offline data only)."""

import datetime as dt
import json
import math

import pytest
from outlook_fixtures import make_bars, make_options
from stock_outlook import (
    HORIZONS,
    add_trading_days,
    analyze,
    atm_iv_term_structure,
    backtest,
    compute_signals,
    horizon_sigma,
    interpolate_iv,
    load_log,
    main,
    parse_bars,
    parse_occ,
    price_range,
    render_markdown,
    rsi,
    score_log,
    sma,
)

# ── Parsing ───────────────────────────────────────────────────────────


class TestParsing:
    def test_parse_bars_sorts_and_dedupes(self, up_bars):
        shuffled = list(reversed(up_bars)) + [up_bars[0]]
        bars = parse_bars(shuffled)
        assert len(bars) == len(up_bars)
        assert all(a["date"] < b["date"] for a, b in zip(bars, bars[1:]))
        assert isinstance(bars[0]["close"], float)

    def test_parse_bars_skips_bad_rows(self):
        bars = parse_bars([{"time": "2026-11-19T05:00:00.000+0000", "close": "x"}, {}])
        assert bars == []

    def test_parse_occ(self):
        root, exp, kind, strike = parse_occ("AAPL261218C00345000")
        assert (root, exp, kind, strike) == ("AAPL", dt.date(2026, 12, 18), "call", 345.0)
        assert parse_occ("BRKB270115P00412500")[3] == 412.5
        assert parse_occ("garbage") is None


# ── Indicators ────────────────────────────────────────────────────────


class TestIndicators:
    def test_sma(self):
        assert sma([1, 2, 3, 4], 2) == pytest.approx(3.5)
        assert sma([1, 2], 5) is None

    def test_rsi_bounds(self):
        assert rsi([float(i) for i in range(1, 40)]) == pytest.approx(100.0)
        assert rsi([float(40 - i) for i in range(39)]) == pytest.approx(0.0)
        assert rsi([1.0] * 5) is None

    def test_trading_days(self):
        # Friday + 1 trading day = Monday
        assert add_trading_days(dt.date(2026, 11, 20), 1) == dt.date(2026, 11, 23)
        assert add_trading_days(dt.date(2026, 11, 20), 5) == dt.date(2026, 11, 27)


# ── Volatility and ranges ─────────────────────────────────────────────


class TestVolatility:
    def test_term_structure_uses_atm_only(self):
        as_of = dt.date(2026, 11, 20)
        opts = make_options("FICT", 100.0, as_of, {30: 0.28, 90: 0.24})
        term = atm_iv_term_structure(opts, 100.0, as_of)
        assert [d for d, _ in term] == [30, 90]
        assert [iv for _, iv in term] == pytest.approx([0.28, 0.24])

    def test_term_structure_ignores_expired_and_bad_iv(self):
        as_of = dt.date(2026, 11, 20)
        opts = make_options("FICT", 100.0, as_of, {-3: 0.5, 30: 0.0})
        assert atm_iv_term_structure(opts, 100.0, as_of) == []

    def test_interpolate_total_variance(self):
        term = [(30, 0.20), (90, 0.30)]
        mid = interpolate_iv(term, 60)
        expected = math.sqrt((0.04 * 30 + (0.09 * 90 - 0.04 * 30) * 0.5) / 60)
        assert mid == pytest.approx(expected)
        assert interpolate_iv(term, 5) == pytest.approx(0.20)  # flat before first
        assert interpolate_iv(term, 400) == pytest.approx(0.30)  # flat after last
        assert interpolate_iv([], 30) is None

    def test_horizon_sigma_prefers_iv(self):
        sigma, source = horizon_sigma(21, [(30, 0.28)], hv=0.5)
        assert sigma == pytest.approx(0.28) and source == "implied"
        sigma, source = horizon_sigma(21, [], hv=0.5)
        assert sigma == pytest.approx(0.5) and source == "historical"

    def test_price_range_properties(self):
        lo68, hi68 = price_range(100, 0.3, 21 / 252, 0.68)
        lo95, hi95 = price_range(100, 0.3, 21 / 252, 0.95)
        assert lo95 < lo68 < 100 < hi68 < hi95
        width = math.log(hi68 / lo68) / 2
        assert width == pytest.approx(0.3 * math.sqrt(21 / 252) * 0.9945, rel=1e-3)


# ── Signals and tilt ──────────────────────────────────────────────────


class TestSignals:
    def test_uptrend_scores_positive(self, up_bars):
        bars = parse_bars(up_bars)
        sig = compute_signals(bars, spot=bars[-1]["close"])
        assert sig["trend"]["score"] == pytest.approx(1.0)
        assert sig["momentum_6m"]["score"] > 0

    def test_downtrend_scores_negative(self, down_bars):
        bars = parse_bars(down_bars)
        sig = compute_signals(bars, spot=bars[-1]["close"])
        assert sig["trend"]["score"] == pytest.approx(-1.0)
        assert sig["momentum_6m"]["score"] < 0

    def test_scores_bounded(self, sample_input):
        report = analyze(sample_input)
        for comp in report["signals"].values():
            assert -1.0 <= comp["score"] <= 1.0

    def test_news_counts_only_matching_symbol(self, sample_input):
        report = analyze(sample_input)
        news = report["signals"]["news_sentiment"]
        assert news["value"] == {"positive": 2, "negative": 1, "neutral": 0}


class TestAnalyze:
    def test_horizons_and_ranges(self, sample_input):
        report = analyze(sample_input)
        labels = [h["label"] for h in report["horizons"]]
        assert labels == [h[0] for h in HORIZONS]
        for h in report["horizons"]:
            assert h["low95"] < h["low68"] < report["spot"] < h["high68"] < h["high95"]
            assert 0.0 < h["p_up"] < 1.0
            assert h["vol_source"] == "implied"
        widths = [h["high68"] - h["low68"] for h in report["horizons"]]
        assert widths == sorted(widths)

    def test_one_day_has_no_edge(self, sample_input):
        one_day = analyze(sample_input)["horizons"][0]
        assert one_day["p_up"] == pytest.approx(0.5)
        assert one_day["tilt"] == "no reliable edge"

    def test_tilt_edge_is_capped(self, sample_input):
        for h in analyze(sample_input)["horizons"]:
            assert abs(h["p_up"] - 0.5) <= 0.08 + 1e-9
            assert h["confidence"] in ("low", "moderate")

    def test_uptrend_leans_bullish_beyond_a_week(self, sample_input):
        h = {x["label"]: x for x in analyze(sample_input)["horizons"]}
        assert h["3 months"]["p_up"] > 0.5
        assert "bullish" in h["3 months"]["tilt"]

    def test_hv_fallback_without_options(self, sample_input):
        sample_input["options"] = []
        report = analyze(sample_input)
        assert all(h["vol_source"] == "historical" for h in report["horizons"])

    def test_earnings_flag(self, sample_input):
        report = analyze(sample_input)
        assert report["next_earnings"] == "2027-01-29"
        h = {x["label"]: x for x in report["horizons"]}
        assert h["1 month"]["includes_earnings"] is False
        assert h["3 months"]["includes_earnings"] is True

    def test_scenarios_ordered(self, sample_input):
        for sc in analyze(sample_input)["scenarios"]:
            assert sc["bear"] < sc["base"] < sc["bull"]

    def test_requires_bars(self, sample_input):
        sample_input["bars"] = sample_input["bars"][:30]
        with pytest.raises(ValueError, match="at least"):
            analyze(sample_input)

    def test_markdown_has_disclaimer_and_sections(self, sample_input):
        md = render_markdown(analyze(sample_input))
        for needle in (
            "# Stock Outlook: FICT",
            "Probability Ranges",
            "Evidence",
            "Scenarios",
            "Backtest",
            "not a prediction",
        ):
            assert needle in md


# ── Backtest ──────────────────────────────────────────────────────────


class TestBacktest:
    def test_range_coverage_reasonable(self):
        bars = parse_bars(make_bars(n=500, daily_drift=0.0005, wiggle=0.012))
        bt = backtest(bars)
        for row in bt:
            assert 0.0 <= row["coverage68"] <= 1.0
            assert row["samples"] > 0
            assert 0.0 <= row["base_rate_up"] <= 1.0

    def test_backtest_short_history(self):
        assert backtest(parse_bars(make_bars(n=100))) == []


# ── Forecast log ──────────────────────────────────────────────────────


class TestForecastLog:
    def test_score_matured_forecast(self):
        bars = parse_bars(make_bars(n=300, end=dt.date(2026, 11, 19)))
        entry = {
            "id": "FICT-2026-10-01",
            "symbol": "FICT",
            "as_of": "2026-10-01",
            "spot": 100.0,
            "horizons": [
                {
                    "label": "1 week",
                    "target_date": "2026-10-08",
                    "low68": 1.0,
                    "high68": 1e9,
                    "low95": 0.5,
                    "high95": 2e9,
                    "p_up": 0.6,
                },
                {
                    "label": "6 months",
                    "target_date": "2027-04-01",
                    "low68": 1.0,
                    "high68": 1e9,
                    "low95": 0.5,
                    "high95": 2e9,
                    "p_up": 0.6,
                },
            ],
        }
        scored = score_log([entry], "FICT", bars)
        assert scored["matured"] == 1
        assert scored["pending"] == 1
        assert scored["hit68_rate"] == pytest.approx(1.0)

    def test_score_ignores_other_symbols(self):
        bars = parse_bars(make_bars(n=50))
        scored = score_log([{"symbol": "OTHR", "horizons": []}], "FICT", bars)
        assert scored["matured"] == 0 and scored["pending"] == 0


# ── CLI ───────────────────────────────────────────────────────────────


class TestCli:
    def test_end_to_end(self, sample_input, tmp_path):
        inp = tmp_path / "in.json"
        inp.write_text(json.dumps(sample_input))
        log = tmp_path / "log.jsonl"
        out = tmp_path / "reports"
        assert main(["--input", str(inp), "--output-dir", str(out), "--log-path", str(log)]) == 0
        md = list(out.glob("stock_outlook_FICT_*.md"))
        js = list(out.glob("stock_outlook_FICT_*.json"))
        assert len(md) == 1 and len(js) == 1
        assert len(load_log(log)) == 1
        # Re-running the same day replaces, not duplicates, the log entry.
        assert main(["--input", str(inp), "--output-dir", str(out), "--log-path", str(log)]) == 0
        assert len(load_log(log)) == 1

    def test_no_log(self, sample_input, tmp_path):
        inp = tmp_path / "in.json"
        inp.write_text(json.dumps(sample_input))
        log = tmp_path / "log.jsonl"
        args = ["--input", str(inp), "--output-dir", str(tmp_path), "--log-path", str(log)]
        assert main(args + ["--no-log"]) == 0
        assert not log.exists()

    def test_bad_input_exits_1(self, tmp_path, capsys):
        inp = tmp_path / "in.json"
        inp.write_text("{}")
        assert main(["--input", str(inp), "--output-dir", str(tmp_path), "--no-log"]) == 1
        assert "Error" in capsys.readouterr().err


# ── Compact rows and weekly backtest ──────────────────────────────────


class TestCompactAndWeekly:
    def test_parse_compact_rows(self):
        bars = parse_bars([["2026-11-18", 10, 11, 9, 10.5, 1000], ["2026-11-19", 10.4]])
        assert [b["close"] for b in bars] == [10.5, 10.4]
        assert bars[0]["high"] == 11 and bars[1]["high"] == 10.4

    def test_weekly_backtest_used_when_given(self, sample_input):
        weekly = make_bars(n=260, daily_drift=0.003, wiggle=0.02)  # dates unimportant here
        sample_input["weekly_bars"] = [[b["time"][:10], b["close"]] for b in weekly]
        report = analyze(sample_input)
        assert report["backtest_basis"] == "weekly"
        assert report["backtest"] and all(r["samples"] > 0 for r in report["backtest"])

    def test_daily_backtest_by_default(self, sample_input):
        assert analyze(sample_input)["backtest_basis"] == "daily"

    def test_weekly_backtest_scaling(self):
        weekly = parse_bars([[f"2020-01-{1 + i % 28:02d}", 100 + i] for i in range(10)])
        assert backtest(weekly, periods_per_year=52) == []


# ── Weekly long-term signals with short daily history ─────────────────


def _weekly_rows(n=260, drift=0.004, end=dt.date(2026, 11, 13)):
    rows, price = [], 100.0
    for i in range(n):
        price *= math.exp(drift + 0.02 * math.sin(i * 1.3))
        rows.append([(end - dt.timedelta(weeks=n - 1 - i)).isoformat(), round(price, 2)])
    return rows


class TestWeeklyLongTerm:
    def test_short_daily_plus_weekly(self, sample_input):
        sample_input["bars"] = sample_input["bars"][-80:]
        rows = _weekly_rows()
        scale = float(sample_input["snapshot"]["price"]) * 0.98 / rows[-1][1]
        sample_input["weekly_bars"] = [[d, round(c * scale, 2)] for d, c in rows]
        report = analyze(sample_input)
        assert report["long_term_basis"] == "weekly"
        sig = report["signals"]
        assert sig["trend"]["available"] and sig["momentum_12_1"]["available"]
        assert sig["trend"]["score"] > 0
        assert "sma200" in report["key_levels"]

    def test_daily_basis_when_long_daily_history(self, sample_input):
        assert analyze(sample_input)["long_term_basis"] == "daily"

    def test_short_daily_without_weekly_marks_missing(self, sample_input):
        sample_input["bars"] = sample_input["bars"][-80:]
        sig = analyze(sample_input)["signals"]
        assert not sig["momentum_12_1"]["available"]


# ── Backtest verdicts ─────────────────────────────────────────────────


class TestBacktestVerdicts:
    def test_verdict_fields(self):
        from stock_outlook import backtest_verdict

        row = {
            "coverage68": 0.55,
            "coverage95": 0.9,
            "base_rate_up": 0.72,
            "tilt_calls": 100,
            "tilt_accuracy": 0.51,
        }
        v = backtest_verdict(row)
        assert v["range"] == "too narrow"
        assert v["tilt_beats_naive"] is False
        assert v["tilt_edge"] == pytest.approx(0.51 - 0.72)

    def test_verdict_good(self):
        from stock_outlook import backtest_verdict

        row = {
            "coverage68": 0.70,
            "coverage95": 0.95,
            "base_rate_up": 0.52,
            "tilt_calls": 50,
            "tilt_accuracy": 0.60,
        }
        v = backtest_verdict(row)
        assert v["range"] == "well calibrated"
        assert v["tilt_beats_naive"] is True

    def test_verdict_no_calls(self):
        from stock_outlook import backtest_verdict

        v = backtest_verdict(
            {
                "coverage68": 0.8,
                "coverage95": 0.99,
                "base_rate_up": 0.5,
                "tilt_calls": 0,
                "tilt_accuracy": None,
            }
        )
        assert v["range"] == "too wide"
        assert v["tilt_beats_naive"] is None

    def test_markdown_states_verdict(self, sample_input):
        weekly = make_bars(n=260, daily_drift=0.003, wiggle=0.02)
        sample_input["weekly_bars"] = [[b["time"][:10], b["close"]] for b in weekly]
        md = render_markdown(analyze(sample_input))
        assert "What the backtest says" in md


# ── Backtest feeds back into confidence ───────────────────────────────


class TestBacktestCaution:
    def test_failed_backtest_downgrades_confidence(self, sample_input, monkeypatch):
        import stock_outlook

        def fake_backtest(bars, horizons=None, periods_per_year=252):
            rows = []
            for label in ("1 week", "1 month", "3 months"):
                row = {
                    "horizon": label,
                    "samples": 100,
                    "coverage68": 0.68,
                    "coverage95": 0.95,
                    "base_rate_up": 0.70,
                    "tilt_calls": 80,
                    "tilt_accuracy": 0.55,
                }
                row.update(stock_outlook.backtest_verdict(row))
                rows.append(row)
            return rows

        monkeypatch.setattr(stock_outlook, "backtest", fake_backtest)
        report = stock_outlook.analyze(sample_input)
        for h in report["horizons"][1:]:
            assert h["confidence"] == "low"
            assert h["backtest_caution"] is True
            if h["tilt"] != "neutral":
                assert "unproven" in h["tilt"]
        assert report["horizons"][0]["backtest_caution"] is False

    def test_passing_backtest_keeps_confidence(self, sample_input, monkeypatch):
        import stock_outlook

        def fake_backtest(bars, horizons=None, periods_per_year=252):
            row = {
                "horizon": "3 months",
                "samples": 100,
                "coverage68": 0.68,
                "coverage95": 0.95,
                "base_rate_up": 0.52,
                "tilt_calls": 80,
                "tilt_accuracy": 0.62,
            }
            row.update(stock_outlook.backtest_verdict(row))
            return [row]

        monkeypatch.setattr(stock_outlook, "backtest", fake_backtest)
        h = {x["label"]: x for x in stock_outlook.analyze(sample_input)["horizons"]}
        assert h["3 months"]["backtest_caution"] is False
        assert "unproven" not in h["3 months"]["tilt"]
