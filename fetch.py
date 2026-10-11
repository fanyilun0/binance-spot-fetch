#!/usr/bin/env python3
"""Collect official Binance daily (UTC) klines for configured tokens and save as JSON.

For each token, the finalized 1d kline of the trade date (default: previous UTC day) is collected for
every Spot pair and USD-M perpetual. Sources are official Binance endpoints only:

- Spot: market-data REST API (data-api.binance.vision), cross-checked against the official archive
- Futures: official archive data.binance.vision (fapi.binance.com is geo-blocked for US IPs)

Every archive file is verified against its published SHA256 .CHECKSUM.

Output layout: data/<fetch UTC date>/<fetch UTC HHMM>/<TOKEN>.json plus _run.json.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SPOT_BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
ARCHIVE_BASE = "https://data.binance.vision"
ARCHIVE_PREFIX = {"spot": "data/spot", "futures": "data/futures/um"}
FUTURES_SPOT_MAX_DIFF = 0.05
SPOT_KLINES_DOCS = "https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#klinecandlestick-data"
SPOT_EXCHANGE_INFO_DOCS = "https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#exchange-information"
ARCHIVE_DOCS = "https://github.com/binance/binance-public-data"

KLINE_FIELDS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume",
]
NUMERIC_FIELDS = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"]


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, body: str):
        super().__init__(f"{status} {url} {body[:200]}")
        self.status = status


class ValidationError(Exception):
    pass


def http_get(url: str, params: dict | None = None, retries: int = 3, timeout: int = 30) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: HttpError | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "binance-spot-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = HttpError(url, e.code, e.read().decode("utf-8", "replace"))
            if e.code in (400, 403, 404, 451):
                raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = HttpError(url, None, str(e))
        time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def get_json(bases: list[str], path: str, params: dict | None = None):
    """Try each base URL in order; return (data, base_used, full_request_url)."""
    errors: list[HttpError] = []
    for base in bases:
        url = base + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
        try:
            return json.loads(http_get(url)), base, url
        except HttpError as e:
            errors.append(e)
    raise HttpError(path, errors[-1].status if errors else None, " | ".join(map(str, errors)))


def utc_day_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_kline(row: list) -> dict:
    k: dict = {}
    for i, name in enumerate(KLINE_FIELDS):
        v = row[i]
        if name in ("open_time", "close_time"):
            v = int(v)
            if v > 10**14:  # spot archive uses microseconds since 2025
                v //= 1000
        elif name == "trades":
            v = int(v)
        else:
            v = str(v)
        k[name] = v
    k["open_time_utc"] = ms_to_iso(k["open_time"])
    k["close_time_utc"] = ms_to_iso(k["close_time"])
    return k


def check_kline(k: dict, d: date) -> list[str]:
    issues = []
    if k["open_time"] != utc_day_start_ms(d) or k["close_time"] != utc_day_start_ms(d + timedelta(days=1)) - 1:
        issues.append(f"kline does not cover UTC day {d}")
    o, h, l, c = (float(k[f]) for f in ("open", "high", "low", "close"))
    vol, qv = float(k["volume"]), float(k["quote_volume"])
    if min(o, h, l, c) <= 0:
        issues.append("non-positive price")
    if not (l <= min(o, c) and max(o, c) <= h):
        issues.append(f"OHLC inconsistent (o={o} h={h} l={l} c={c})")
    if vol < 0 or qv < 0:
        issues.append("negative volume")
    if vol > 0 and not (l * 0.999 <= qv / vol <= h * 1.001):
        issues.append(f"quote_volume/volume={qv / vol} outside [low, high]")
    return issues


def kline_diffs(a: dict, b: dict) -> list[str]:
    diffs = [f for f in ("open_time", "close_time", "trades") if a[f] != b[f]]
    diffs += [f for f in NUMERIC_FIELDS if abs(float(a[f]) - float(b[f])) > 1e-9 * max(1.0, abs(float(a[f])))]
    return sorted(diffs)


def archive_url(market: str, symbol: str, d: date) -> str:
    name = urllib.parse.quote(f"{symbol}-1d-{d.isoformat()}.zip")
    return f"{ARCHIVE_BASE}/{ARCHIVE_PREFIX[market]}/daily/klines/{urllib.parse.quote(symbol)}/1d/{name}"


def api_provenance(endpoint: str, params: dict, request_url: str, retrieved_at: str, docs: str) -> dict:
    return {
        "type": "rest_api",
        "provider": "Binance",
        "method": "GET",
        "endpoint": endpoint,
        "params": params,
        "request_url": request_url,
        "retrieved_at_utc": retrieved_at,
        "docs": docs,
        "response_path": "$[0]",
        "field_map": {f: f"$[0][{i}]" for i, f in enumerate(KLINE_FIELDS)},
        "verify": f"curl -s '{request_url}'",
    }


def archive_daily_kline(market: str, symbol: str, d: date) -> dict | None:
    """Official archived 1d kline, SHA256-verified. None if the file does not exist."""
    url = archive_url(market, symbol, d)
    retrieved_at = now_iso()
    try:
        raw = http_get(url)
    except HttpError as e:
        if e.status == 404:
            return None
        raise
    checksum_url = url + ".CHECKSUM"
    expected = http_get(checksum_url).decode().split()[0].lower()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValidationError(f"SHA256 mismatch for {url}: {actual} != {expected}")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        member = z.namelist()[0]
        text = z.read(member).decode("utf-8")
    lines = list(csv.reader(io.StringIO(text)))
    data_rows = [(n, r) for n, r in enumerate(lines, start=1) if r and r[0].strip().isdigit()]
    if len(data_rows) != 1:
        raise ValidationError(f"expected 1 row in {url}, got {len(data_rows)}")
    line_no, row = data_rows[0]
    has_header = bool(lines and lines[0] and not lines[0][0].strip().isdigit())
    k = parse_kline(row)
    provenance = {
        "type": "official_archive_file",
        "provider": "Binance (data.binance.vision)",
        "url": url,
        "checksum_url": checksum_url,
        "sha256": actual,
        "sha256_verified": True,
        "file_in_zip": member,
        "csv_header": has_header,
        "csv_line": line_no,
        "field_map": {f: f"column {i + 1}" for i, f in enumerate(KLINE_FIELDS)},
        "timestamp_unit": "microseconds" if int(row[0]) > 10**14 else "milliseconds",
        "retrieved_at_utc": retrieved_at,
        "docs": ARCHIVE_DOCS,
        "verify": (
            f"curl -s '{url}' -o kline.zip && curl -s '{checksum_url}'; echo; sha256sum kline.zip "
            f"&& unzip -p kline.zip | sed -n '{line_no}p'"
        ),
    }
    return {"kline": k, "url": url, "sha256": actual, "provenance": provenance}


def discover_spot_symbols(tokens: list[dict], quotes: list[str]) -> tuple[dict, dict]:
    params = {"permissions": "SPOT"}
    retrieved_at = now_iso()
    info, base, url = get_json(SPOT_BASES, "/api/v3/exchangeInfo", params)
    found: dict[str, list[str]] = {t["name"]: [] for t in tokens}
    by_base = {t["base_asset"]: t["name"] for t in tokens}
    for s in info["symbols"]:
        name = by_base.get(s["baseAsset"])
        if name and s["quoteAsset"] in quotes and s["status"] == "TRADING":
            found[name].append(s["symbol"])
    discovery = {
        "type": "rest_api",
        "provider": "Binance",
        "method": "GET",
        "endpoint": f"{base}/api/v3/exchangeInfo",
        "params": params,
        "request_url": url,
        "retrieved_at_utc": retrieved_at,
        "docs": SPOT_EXCHANGE_INFO_DOCS,
        "selection": f"$.symbols[?(@.baseAsset in config.tokens[].base_asset && @.quoteAsset in {quotes} && @.status == 'TRADING')]",
        "verify": f"curl -s '{url}'",
    }
    return found, discovery


def collect_spot(symbol: str, d: date) -> tuple[dict, list[str]]:
    params = {"symbol": symbol, "interval": "1d", "startTime": utc_day_start_ms(d), "limit": 1}
    retrieved_at = now_iso()
    data, base, request_url = get_json(SPOT_BASES, "/api/v3/klines", params)
    if not data or int(data[0][0]) != utc_day_start_ms(d):
        raise ValidationError(f"spot {symbol}: API returned no 1d kline for {d}")
    k = parse_kline(data[0])
    issues = [f"spot {symbol} {d}: {i}" for i in check_kline(k, d)]
    archived = archive_daily_kline("spot", symbol, d)
    validation: dict = {}
    if archived is None:
        validation["archive_cross_check"] = "archive not published"
        issues.append(f"spot {symbol} {d}: official archive not published yet, cross-check skipped")
    else:
        diffs = kline_diffs(k, archived["kline"])
        validation.update({
            "archive_cross_check": "match" if not diffs else f"mismatch: {diffs}",
            "archive_provenance": archived["provenance"],
        })
        if diffs:
            issues.append(f"spot {symbol} {d}: API vs official archive mismatch {diffs}")
    return {
        "symbol": symbol, "market": "spot", "source": f"{base}/api/v3/klines",
        "provenance": api_provenance(f"{base}/api/v3/klines", params, request_url, retrieved_at, SPOT_KLINES_DOCS),
        "daily_kline": k, "validation": validation,
    }, issues


def collect_futures(base_asset: str, quotes: list[str], d: date) -> tuple[list[dict], list[str]]:
    out, issues = [], []
    for symbol in (base_asset + q for q in quotes):
        archived = archive_daily_kline("futures", symbol, d)
        if archived is None:
            # Listed symbol whose file for `d` is missing but the day before exists -> not published yet.
            if archive_daily_kline("futures", symbol, d - timedelta(days=1)) is not None:
                issues.append(f"futures {symbol} {d}: official archive not published yet")
            continue
        k = archived["kline"]
        issues += [f"futures {symbol} {d}: {i}" for i in check_kline(k, d)]
        out.append({
            "symbol": symbol, "market": "usdm_perpetual", "source": archived["url"],
            "provenance": archived["provenance"],
            "daily_kline": k, "validation": {"archive_sha256_verified": archived["sha256"]},
        })
    return out, issues


def cross_market_check(record: dict) -> list[str]:
    spot = {s["symbol"]: float(s["daily_kline"]["close"]) for s in record["spot"]}
    issues = []
    for f in record["futures"]:
        s_close = spot.get(f["symbol"])
        if s_close:
            diff = abs(float(f["daily_kline"]["close"]) / s_close - 1)
            f["validation"]["spot_close_diff_pct"] = round(diff * 100, 4)
            if diff > FUTURES_SPOT_MAX_DIFF:
                issues.append(f"{f['symbol']}: futures close differs from spot close by {diff:.2%}")
    return issues


def run(now: datetime, trade_date: date, out_root: Path, config: dict) -> tuple[dict, int]:
    tokens, quotes = config["tokens"], config["quote_assets"]
    run_dir = out_root / now.date().isoformat() / now.strftime("%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = now.isoformat().replace("+00:00", "Z")

    spot_syms, discovery = discover_spot_symbols(tokens, quotes)
    summary: dict = {
        "fetched_at_utc": fetched_at,
        "trade_date_utc": trade_date.isoformat(),
        "spot_source": discovery["endpoint"].removesuffix("/api/v3/exchangeInfo"),
        "futures_source": ARCHIVE_BASE,
        "spot_symbol_discovery": discovery,
        "futures_symbol_discovery": {
            "type": "official_archive_file",
            "rule": f"symbol = base_asset + quote for quote in {quotes}; listed if "
                    f"{ARCHIVE_BASE}/{ARCHIVE_PREFIX['futures']}/daily/klines/<SYMBOL>/1d/<SYMBOL>-1d-<trade_date>.zip exists",
            "docs": ARCHIVE_DOCS,
        },
        "tokens": {},
        "issues": [],
        "errors": [],
    }

    for t in tokens:
        name = t["name"]
        record: dict = {
            "token": name, "base_asset": t["base_asset"], "trade_date_utc": trade_date.isoformat(),
            "fetched_at_utc": fetched_at, "spot": [], "futures": [],
        }
        for symbol in spot_syms.get(name, []):
            try:
                data, issues = collect_spot(symbol, trade_date)
                record["spot"].append(data)
                summary["issues"] += issues
            except (HttpError, ValidationError) as e:
                summary["errors"].append(f"spot {symbol}: {e}")
        try:
            futures, issues = collect_futures(t["base_asset"], quotes, trade_date)
            record["futures"] = futures
            summary["issues"] += issues
        except (HttpError, ValidationError) as e:
            summary["errors"].append(f"futures {t['base_asset']}: {e}")
        summary["issues"] += cross_market_check(record)

        record["status"] = "ok" if record["spot"] or record["futures"] else "not_listed_on_binance"
        (run_dir / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["tokens"][name] = {
            "status": record["status"],
            "spot_symbols": [s["symbol"] for s in record["spot"]],
            "futures_symbols": [f["symbol"] for f in record["futures"]],
        }

    (run_dir / "_run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = any(v["status"] == "ok" for v in summary["tokens"].values())
    return summary, 0 if ok and not summary["errors"] and not summary["issues"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(ROOT / "data"), help="output root directory")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    p.add_argument("--date", type=date.fromisoformat, help="UTC trade date YYYY-MM-DD (default: previous UTC day)")
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    trade_date = args.date or (now.date() - timedelta(days=1))
    if trade_date >= now.date():
        p.error("--date must be a completed UTC day (before today)")
    summary, code = run(now, trade_date, Path(args.out), config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
