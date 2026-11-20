# Methodology

## Why ranges instead of price targets

Short-horizon stock returns are close to unpredictable: daily direction is near a coin flip, and
the small edges that exist are weak and unstable. What can be estimated well is the *size* of
likely moves, because volatility is persistent and the options market prices it directly. The
outlook is therefore built on ranges, with direction as a small, capped adjustment.

## Price ranges

- Volatility per horizon comes from the at-the-money implied volatility term structure: for each
  expiry, the call/put IV at the strike nearest the price, averaged. Between expiries, IV is
  interpolated in total variance (`iv² × t`); outside the quoted expiries it is held flat.
- Without options data, 20-day realized volatility is used for 1 day and 1 week, 60-day for
  longer horizons.
- Prices follow a driftless lognormal: `ln S_T ~ N(ln S − σ²T/2, σ²T)`, with `T` = trading days /
  252. The 68% range is the 16th-84th percentile; the 95% range the 2.5th-97.5th.
- Expected move = `S × σ × √T` (one standard deviation).

## Evidence components (each scored −1 to +1)

| Component | Calculation | Read |
|---|---|---|
| trend | price vs 50- and 200-day averages, and 50 vs 200 (10/40-week when daily history is short) | each condition ±1/3 |
| momentum_1m | 21-day return in volatility units, ÷2, clipped | trend-following |
| momentum_6m | 6-month return in volatility units, ÷2, clipped | trend-following |
| momentum_12_1 | 12-month return excluding the last month | classic cross-sectional momentum |
| rsi | RSI(14): above 70 scores negative, below 30 positive | short-term mean reversion |
| macd | MACD(12,26,9) histogram ÷ ATR(14) | short-term trend |
| relative_strength | 3-month return minus SPY's, ÷10% | leadership vs the market |
| analyst | average of rating balance and mean-target upside ÷25% | consensus view |
| earnings_surprise | last EPS surprise ÷10% | post-earnings drift |
| news_sentiment | (positive − negative) ÷ headlines tagged to the symbol | very noisy |

## Tilt

For each horizon the available components are combined with fixed weights (short horizons
favour momentum, RSI and MACD; long horizons favour trend, 12-1 momentum, analysts and earnings).
The composite score maps to a probability of an up move:

`P(up) = 0.5 + cap × score`

| Horizon | Cap |
|---|---|
| 1 day | 0 (always 50%, "no reliable edge") |
| 1 week | 3 points |
| 1 month | 5 points |
| 3 months | 7 points |
| 6 months | 8 points |

Labels: |score| < 0.15 is "neutral", below 0.5 "lean bullish/bearish", otherwise
"bullish/bearish". Confidence is "moderate" only for horizons of a month or more when
|score| ≥ 0.35 and at least 70% of the weighted evidence agrees; otherwise "low". There is no
"high" confidence level by design.

**Backtest caution:** when the stock's own backtest shows the tilt did not beat the naive rule for
a horizon (6 months uses the 3-month result), that horizon's confidence is forced to "low" and its
label gets "(unproven)". The probability is left unchanged so the raw evidence stays visible.

## Backtest

Walk-forward over the weekly history (or daily, when weekly bars are not supplied), using only
data available at each point:

- **Range coverage**: how often the price at the horizon landed inside the historical-volatility
  68% / 95% ranges. About 68% / 95% means calibrated; lower means real moves were bigger
  ("too narrow").
- **Tilt hit rate**: trend + 6-month + 12-1 momentum score (the price-only part of the tilt),
  counted when |score| ≥ 0.15.
- **Naive benchmark**: the better of "always up" and "always down". Stocks that rose most of the
  time make "always up" hard to beat; a tilt that does not beat it adds nothing.

The backtest uses historical volatility, not implied volatility (historical IV is not available),
and covers only the price-based part of the tilt.

## Forecast log

Each run appends `{id, symbol, as_of, spot, horizons[{label, target_date, low68, high68, low95,
high95, p_up}]}` to a JSON-lines file (one entry per symbol per day). On later runs, entries whose
target date has passed are scored against the first close on or after the target date: range hit
rates and direction accuracy for horizons where a direction was called.

## Known limitations

- Lognormal tails are too thin: crashes, gaps and earnings moves exceed the 95% range more often
  than 5% of the time.
- Implied volatility around an earnings date is elevated; ranges spanning earnings already include
  the expected earnings move, ranges just after it do not.
- Trading-day arithmetic ignores exchange holidays, so target dates can be off by a day.
- 1-day ranges use the nearest expiry's IV, which may be a weekly option several days out.
- News sentiment labels come from the data provider and are coarse.
- Nothing here accounts for position sizing, taxes or costs. Educational analysis only.
