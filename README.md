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

Each run collects the **finalized 1d kline of the previous UTC day** (`trade_date_utc`). Candles are selected by exchange UTC day boundaries, so the result does not depend on when the job actually starts. Fields: open / high / low / close, `volume` (base asset), `quote_volume` (turnover in quote asset), `trades`, taker buy volume / quote volume.

## Output layout

```
data/<fetch UTC date>/<fetch UTC HHMM>/<TOKEN>.json   # spot + futures daily kline for one token
data/<fetch UTC date>/<fetch UTC HHMM>/_run.json      # run summary, validation issues, errors
```

## Data sources (official Binance only)

`api.binance.com` and `fapi.binance.com` return HTTP 451 from US IPs, which includes GitHub-hosted runners, so:

- **Spot**: REST API `https://data-api.binance.vision/api/v3/klines` (Binance's public market-data endpoint), falling back to `api.binance.com`.
- **USD-M perpetual**: official archive `https://data.binance.vision/data/futures/um/daily/klines/...` (published once the UTC day has closed).

## Validation

Every run checks the following and records results in `_run.json` (`issues` / `errors`) and each symbol's `validation`:

- every archive zip matches its published SHA256 `.CHECKSUM` (`archive_sha256_verified`)
- spot kline from the API **exactly matches** the official archive kline (`archive_cross_check: "match"`)
- kline covers exactly the UTC trade date; OHLC consistent, positive prices, `quote_volume / volume` within `[low, high]`
- futures close within 5% of the same-quote spot close (`spot_close_diff_pct`)
- a futures symbol with archive history but no file for the trade date is reported (not silently skipped)

The workflow commits the data first and then fails the job if any issue or error was found, so problems are visible in the Actions tab.

## Provenance (how to verify every number)

Every spot / futures entry has a `provenance` object; `_run.json` has `spot_symbol_discovery` and `futures_symbol_discovery`. All data comes from APIs or official files (no web scraping, so no DOM paths).

- REST API (`type: "rest_api"`): `method`, `endpoint`, `params`, exact `request_url`, `retrieved_at_utc`, official `docs`, `field_map` (JSON path in the response for each field, e.g. `close` = `$[0][4]`) and a `verify` curl command.
- Official archive file (`type: "official_archive_file"`): zip `url`, `checksum_url`, verified `sha256`, `file_in_zip`, `csv_line`, `field_map` (CSV column per field), `timestamp_unit`, and a `verify` command that downloads the zip, prints the published checksum and the local SHA256, and prints the exact CSV line.
- Spot entries also carry `validation.archive_provenance`: the archive file the API value was cross-checked against.

Example check of a spot close price:

```
jq -r '.spot[0].provenance.verify' data/<date>/<HHMM>/HYPE.json | sh   # then compare $[0][4] with .spot[0].daily_kline.close
```

## Run locally

```
python3 fetch.py            # writes to ./data
python3 fetch.py --date 2026-10-01   # backfill a specific UTC trade date
python3 fetch.py --out /tmp/data
python3 -m unittest discover -s tests
```

Only the Python standard library is used (Python 3.10+).
