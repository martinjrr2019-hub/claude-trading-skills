# Input Schema

`stock_outlook.py --input FILE` reads one JSON object. Numbers may be numbers or numeric strings
(Webull returns strings). Only `symbol` and at least 60 daily `bars` are required.

| Field | Required | Source | Format |
|---|---|---|---|
| `symbol` | yes | user | `"AAPL"` |
| `as_of` | no | today | `"YYYY-MM-DD"`; defaults to the snapshot trade time, then the last bar |
| `snapshot` | recommended | `get_stock_snapshot` | object; uses `price`, `fifty_two_wk_high`, `fifty_two_wk_low`, `pe_ratio`, `pb_ratio`, `ps_ratio`, `eps_ttm`, `yield`, `market_value` |
| `bars` | yes | `get_stock_bars` D, count 80 | rows `[date, open, high, low, close]` (or Webull bar objects); at least 60 |
| `weekly_bars` | recommended | `get_stock_bars` W, count 260 | rows `[date, close]`; drives long-term trend/momentum and the backtest |
| `benchmark_bars` | optional | SPY daily, count 66 | rows `[date, close]`; enables relative strength |
| `options` | recommended | `get_option_snapshot` | list of `{"symbol": OCC symbol, "imp_vol": decimal}` for ATM calls and puts |
| `analyst_rating` | optional | `get_analyst_rating` | object with `number`, `strong_buy`, `buy`, `hold`, `under_perform`, `sell` |
| `analyst_target` | optional | `get_analyst_target_price` | object with `mean`, `low`, `high` |
| `earnings` | optional | `get_stock_earnings_calendar` | list of `{expected_publish_date, eps_actual, eps_est}` |
| `next_earnings_date` | optional | calendar or web search | `"YYYY-MM-DD"`; overrides future dates found in `earnings` |
| `news` | optional | `get_stock_news` | list of `{title, sentiment, related_symbols, publish_time, source}` |

Bars may be in any order; duplicates by date are collapsed. Rows with fewer than 5 values are
read as `[date, close]`. Daily history longer than one year is used directly for the 50/200-day
averages and momentum; otherwise the weekly series is used (10/40-week averages).

## Minimal example

```json
{
  "symbol": "FICT",
  "as_of": "2026-11-20",
  "snapshot": {"price": "101.25", "fifty_two_wk_high": "110", "fifty_two_wk_low": "72"},
  "bars": [["2026-11-19", 100.1, 101.9, 99.4, 101.0], ["2026-11-18", 99.0, 100.5, 98.7, 100.2]],
  "weekly_bars": [["2026-11-13", 99.8], ["2026-11-06", 97.5]],
  "benchmark_bars": [["2026-11-19", 715.87]],
  "options": [
    {"symbol": "FICT261218C00100000", "imp_vol": 0.31},
    {"symbol": "FICT261218P00100000", "imp_vol": 0.32}
  ],
  "analyst_rating": {"number": "10", "strong_buy": "3", "buy": "3", "hold": "3", "under_perform": "1", "sell": "0"},
  "analyst_target": {"mean": "112", "low": "90", "high": "130"},
  "earnings": [{"expected_publish_date": "2026-11-02", "eps_actual": "1.10", "eps_est": "1.02"}],
  "next_earnings_date": "2027-02-01",
  "news": [{"title": "FICT raises guidance", "sentiment": "POSITIVE", "related_symbols": ["FICT"]}]
}
```

(The example is truncated: real input needs at least 60 daily bars.)
