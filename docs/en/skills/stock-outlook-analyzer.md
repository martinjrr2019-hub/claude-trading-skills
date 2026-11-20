---
layout: default
title: "Stock Outlook Analyzer"
grand_parent: English
parent: Skill Guides
nav_order: 60
lang_peer: /ja/skills/stock-outlook-analyzer/
permalink: /en/skills/stock-outlook-analyzer/
generated: true
---

# Stock Outlook Analyzer
{: .no_toc }

Deep, multi-horizon outlook for a single US stock or ETF using live Webull data - probability ranges for the next day, week, month, 3 and 6 months from options implied volatility, an evidence-weighted directional tilt with capped confidence, bull/base/bear scenarios with trigger levels, a backtest of the method on the stock's own history, and a self-scoring forecast log. Use when the user asks where a stock is headed, wants a deep analysis or outlook, asks for price predictions, expected moves or ranges over days, weeks or months, or wants to know how reliable such forecasts are.
{: .fs-6 .fw-300 }

<span class="badge badge-free">No API</span>

[Download Skill Package (.skill)](https://github.com/tradermonty/claude-trading-skills/raw/main/skill-packages/stock-outlook-analyzer.skill){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[View Source on GitHub](https://github.com/tradermonty/claude-trading-skills/tree/main/skills/stock-outlook-analyzer){: .btn .fs-5 .mb-4 .mb-md-0 }

<details open markdown="block">
  <summary>Table of Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## 1. Overview

Produce an honest, quantitative outlook for one ticker across five horizons (1 day, 1 week,
1 month, 3 months, 6 months). Stock prices cannot be predicted reliably, so this skill does not
issue price calls. It answers the questions that can be answered:

- **How far is the stock likely to move?** 68% and 95% price ranges from the options market's
  implied volatility (historical volatility as fallback).
- **Which way does the evidence lean?** A small, capped tilt built from trend, momentum, RSI,
  MACD, relative strength, analyst views, earnings surprise and news sentiment. The 1-day tilt
  is always neutral: there is no reliable edge at that horizon.
- **Has this approach worked on this stock before?** A walk-forward backtest on five years of the
  stock's own history, compared against naive "always up" / "always down" rules.
- **Has it worked since we started using it?** Every run is logged; later runs score matured
  forecasts automatically.

Data comes from the Webull MCP tools (`mcp__Webull__*`). The computation is done by
`scripts/stock_outlook.py` (Python standard library only).

---

## 2. When to Use

- "Do a deep analysis of NVDA", "What's the outlook for AAPL?"
- "Where will TSLA be next week / in 3 months?", "Predict MSFT's next move"
- "What's the expected move for AMD over the next month?"
- "How accurate have your outlooks been?"

For options trades built on the outlook, hand off to `options-strategy-advisor` (Step 6).

---

## 3. Prerequisites

- Webull MCP server connected (tools named `mcp__Webull__*`). Without it, ask the user for the
  data or use another source, keeping the same input format.
- Python 3.9+. No third-party packages.

---

## 4. Quick Start

```bash
python3 <skill-dir>/scripts/stock_outlook.py \
  --input reports/stock_outlook_input_<SYM>_<YYYY-MM-DD>.json \
  --output-dir reports/ \
  --log-path state/stock_outlook/forecast_log.jsonl
```

---

## 5. Workflow

### Step 1: Confirm the ticker

Use `US_STOCK` for shares and `US_ETF` for ETFs. If the user gives a company name, resolve the
ticker first.

### Step 2: Gather data with Webull

Make independent calls in parallel. Keep the data compact: it is copied into the input file.

1. **Snapshot**: `get_stock_snapshot(symbols=SYM, category, extend_hour_required=false,
   overnight_required=false)`. Keep `price`, `fifty_two_wk_high`, `fifty_two_wk_low`,
   `pe_ratio`, `pb_ratio`, `ps_ratio`, `eps_ttm`, `yield`, `market_value`.
2. **Daily bars**: `get_stock_bars(symbols=[SYM], category, timespan="D", count=80)`.
   Write as compact rows `[date, open, high, low, close]`, prices rounded to 2 decimals.
3. **Weekly bars**: `get_stock_bars(..., timespan="W", count=260)` (five years).
   Write as `[date, close]`. These drive the 50/200-day trend proxies (10/40-week), 6- and
   12-month momentum and the backtest.
4. **Benchmark**: `get_stock_bars(symbols=["SPY"], category="US_ETF", timespan="D", count=66)`
   as `[date, close]`.
5. **Options implied volatility**:
   - Pick the listed strike nearest the price (strikes are usually every $1, $2.50 or $5).
   - `get_option_contracts(category="US_OPTION", underlying_symbols=SYM, direction="CALL",
     strike_price_gte=K, strike_price_lte=K, page_size=100)` lists the expirations.
   - Choose up to 6 expirations closest to 3, 7, 30, 60, 150 and 240 calendar days.
   - Build call and put OCC symbols (e.g. `AAPL261218C00345000` and `...P00345000`) and call
     `get_option_snapshot(category="US_OPTION", symbols=...)` (max 20 per call).
   - Keep only `{"symbol", "imp_vol"}` per option.
   - If the stock has no listed options, skip this; the script falls back to historical volatility.
6. **Analysts**: `get_analyst_rating` and `get_analyst_target_price` (category as above).
7. **Earnings**: `get_stock_earnings_calendar`. It usually lists past quarters only. Find the next
   report date with web search if available and put it in `next_earnings_date`; otherwise leave it
   out and say the date is unknown.
8. **News**: `get_stock_news(symbol=SYM)`, first page. Keep `title`, `sentiment`,
   `related_symbols`, `publish_time`, `source`. Read the summaries yourself for Step 5; use
   `get_news_detail` for any item that looks material.

### Step 3: Write the input file

Write one JSON object as described in `references/input_schema.md` to
`reports/stock_outlook_input_<SYM>_<YYYY-MM-DD>.json` (create `reports/` if needed).

### Step 4: Run the engine

```bash
python3 <skill-dir>/scripts/stock_outlook.py \
  --input reports/stock_outlook_input_<SYM>_<YYYY-MM-DD>.json \
  --output-dir reports/ \
  --log-path state/stock_outlook/forecast_log.jsonl
```

`<skill-dir>` is the directory containing this SKILL.md. The script writes
`reports/stock_outlook_<SYM>_<date>.md` and `.json`, scores any matured forecasts in the log, and
records today's forecast (re-running the same day replaces that day's entry). Use `--no-log` when
the user only wants a one-off look.

### Step 5: Write the analysis for the user

Read the generated JSON and Markdown, then write the user-facing analysis. Present in this order:

1. **Bottom line** (2-3 sentences): the likely ranges for the horizons the user cares about and
   whether the evidence leans anywhere. Use the script's numbers; do not invent targets.
2. **Can this be trusted?** State the backtest verdict from "What the backtest says" before any
   directional language. If the tilt did not beat the naive rule for a horizon, say the tilt is
   uninformative for that horizon. If ranges were "too narrow", say real moves have been larger.
3. **Ranges by horizon**: the table, flagging horizons that include an earnings report (earnings
   moves often exceed the range).
4. **Evidence**: the strongest supporting and opposing components, in plain words.
5. **Qualitative read**: what the news and business context add (catalysts, risks, upcoming
   events). Mark this as judgement, separate from the computed numbers.
6. **Scenarios**: bear / base / bull with the trigger levels, and what would change the view.
7. **Track record**: matured forecast stats from the log, or that none have matured yet.
8. Close with: this is a probabilistic outlook, not financial advice or a recommendation to trade.

Rules:
- Never describe the tilt as a prediction or raise its confidence above what the script reports.
- The 1-day horizon gets a range only.
- Do not tell the user to buy or sell.

### Step 6 (optional): Options follow-up

If the user wants a trade structure, use `options-strategy-advisor` with the outlook's numbers:

| Outlook | Strategies to simulate |
|---|---|
| Neutral tilt, IV/HV ratio above ~1.2 | Iron condor with short strikes near the 68% range |
| Lean bullish, moderate confidence | Bull call spread or bull put spread toward the bull trigger |
| Lean bearish, moderate confidence | Bear put spread or bear call spread toward the bear trigger |
| Earnings inside the horizon | Compare long straddle vs iron condor with an IV-crush scenario |

Simulate with `scripts/strategy_simulator.py` from that skill, using the outlook's implied
volatility for the horizon.

### Step 7: Keep the forecast log

The log is what turns this into a measurable track record. It is a local file. In a temporary
cloud session it is lost when the session ends, so offer to commit
`state/stock_outlook/forecast_log.jsonl` to the user's repository after each run.

---

## 6. Resources

**References:**

- `skills/stock-outlook-analyzer/references/input_schema.md`
- `skills/stock-outlook-analyzer/references/methodology.md`

**Scripts:**

- `skills/stock-outlook-analyzer/scripts/stock_outlook.py`
