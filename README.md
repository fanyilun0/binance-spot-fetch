# binance-spot-fetch

Daily snapshot of Binance Spot / USD-M Futures market data for a configured token list, stored as JSON in this repo.

## Tokens

Configured in `config.json` (`base_asset` is the Binance asset code, `name` is the ASCII directory/file name):

| name | Binance base asset | Spot pairs | USD-M perpetual |
|---|---|---|---|
| MARSCOIN | `MARSCOIN` | MARSCOINUSDT, MARSCOINUSDC | MARSCOINUSDT |
| NIULAI | `牛来` | 牛来USDT, 牛来USDC | 牛来USDT |
| HYPE | `HYPE` | HYPEUSDT, HYPEUSDC | HYPEUSDT |
| POINS | `POINS` | not listed | not listed |

Pairs are discovered from `exchangeInfo` at every run, so a token that gets listed later is picked up automatically.

## Schedule

`.github/workflows/daily-fetch.yml` runs at **23:55 UTC** daily (GitHub may start scheduled jobs late), on manual dispatch, and as a dry run (no commit, result uploaded as an artifact) on pull requests.

Daily candles are requested by exchange UTC day boundaries, so the result does not depend on when the job actually starts:

- `today_partial_1d` – current UTC day candle so far (`is_final: false`)
- `previous_day_final_1d` – the finalized previous UTC day candle (`is_final: true`)
- `ticker_24h` – rolling 24h ticker at run time (price, bid/ask, volume, quote volume, trade count)

## Output layout

```
data/<UTC date>/<UTC HHMM>/<TOKEN>.json   # spot + futures data for one token
data/<UTC date>/<UTC HHMM>/_run.json      # run summary, validation issues, errors
```

## Data sources and region restrictions

`api.binance.com` and `fapi.binance.com` return HTTP 451 from US IPs, which includes GitHub-hosted runners.

- **Spot** uses `https://data-api.binance.vision` (Binance's public market-data mirror), falling back to `api.binance.com`.
- **Futures**: if `fapi.binance.com` is reachable, live ticker / mark price / funding rate / open interest / daily candles are collected. Otherwise the finalized previous-day candle is taken from the official archive `https://data.binance.vision` (`mode: "archive"`).

Base URLs can be overridden with `BINANCE_SPOT_BASES`, `BINANCE_FUTURES_BASES` (comma-separated) and `BINANCE_ARCHIVE_BASE`, e.g. to route futures through a non-restricted proxy or self-hosted runner.

## Validation

Every run checks, and records in `_run.json` (`issues` / `errors`):

- OHLC consistency (`low <= open, close <= high`, positive prices) and `quote_volume / volume` within `[low, high]`
- current day candle close vs 24h ticker last price (within 2%)
- previous-day spot candle from the API **exactly matches** the official `data.binance.vision` archive (`validation.archive_cross_check`)

The workflow commits the data first and then fails the job if any issue or error was found, so problems are visible in the Actions tab.

## Run locally

```
python3 fetch.py            # writes to ./data
python3 fetch.py --out /tmp/data
python3 -m unittest discover -s tests
```

Only the Python standard library is used (Python 3.10+).
