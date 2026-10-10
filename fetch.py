#!/usr/bin/env python3
"""Collect Binance Spot / USD-M Futures market data for configured tokens and save as JSON.

Output layout: data/<UTC date>/<UTC HHMM>/<TOKEN>.json plus data/<UTC date>/<UTC HHMM>/_run.json.

Daily candles are always fetched by exchange UTC day boundaries, so the finalized
previous-day record is correct even if the scheduled job starts late.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAY_MS = 86_400_000

SPOT_BASES = [
    b for b in os.environ.get(
        "BINANCE_SPOT_BASES",
        "https://data-api.binance.vision,https://api.binance.com,https://api1.binance.com",
    ).split(",") if b
]
FUTURES_BASES = [
    b for b in os.environ.get("BINANCE_FUTURES_BASES", "https://fapi.binance.com").split(",") if b
]
ARCHIVE_BASE = os.environ.get("BINANCE_ARCHIVE_BASE", "https://data.binance.vision")

KLINE_FIELDS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume",
]
NUMERIC_FIELDS = {"open", "high", "low", "close", "volume", "quote_volume",
                  "taker_buy_volume", "taker_buy_quote_volume"}


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, body: str):
        super().__init__(f"{status} {url} {body[:200]}")
        self.status = status


def http_get(url: str, params: dict | None = None, retries: int = 3, timeout: int = 20) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "binance-spot-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = HttpError(url, e.code, body)
            if e.code in (400, 403, 404, 451):
                raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = HttpError(url, None, str(e))
        time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def get_json(bases: list[str], path: str, params: dict | None = None):
    """Try each base URL in order; return (data, base_used)."""
    errors: list[HttpError] = []
    for base in bases:
        try:
            return json.loads(http_get(base + path, params)), base
        except HttpError as e:
            errors.append(e)
    raise HttpError(path, errors[-1].status if errors else None, " | ".join(map(str, errors)))


def utc_day_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_kline(row: list) -> dict:
    k = {}
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


def api_daily_kline(bases: list[str], path: str, symbol: str, d: date):
    data, base = get_json(bases, path, {
        "symbol": symbol, "interval": "1d", "startTime": utc_day_start_ms(d), "limit": 1,
    })
    if not data or int(data[0][0]) != utc_day_start_ms(d):
        return None, base
    return parse_kline(data[0]), base


def archive_daily_kline(market: str, symbol: str, d: date) -> dict | None:
    prefix = {"spot": "data/spot", "futures": "data/futures/um"}[market]
    name = f"{symbol}-1d-{d.isoformat()}"
    url = f"{ARCHIVE_BASE}/{prefix}/daily/klines/{urllib.parse.quote(symbol)}/1d/{urllib.parse.quote(name)}.zip"
    try:
        raw = http_get(url)
    except HttpError as e:
        if e.status == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8")
    rows = [r for r in csv.reader(io.StringIO(text)) if r and r[0].strip().isdigit()]
    if not rows:
        return None
    k = parse_kline(rows[0])
    k["source_url"] = url
    return k


def check_kline(k: dict, label: str) -> list[str]:
    issues = []
    o, h, l, c = (float(k[f]) for f in ("open", "high", "low", "close"))
    vol, qv = float(k["volume"]), float(k["quote_volume"])
    if min(o, h, l, c) <= 0:
        issues.append(f"{label}: non-positive price")
    if not (l <= min(o, c) and max(o, c) <= h):
        issues.append(f"{label}: OHLC inconsistent (o={o} h={h} l={l} c={c})")
    if vol < 0 or qv < 0:
        issues.append(f"{label}: negative volume")
    if vol > 0 and not (l * 0.999 <= qv / vol <= h * 1.001):
        issues.append(f"{label}: quote_volume/volume={qv / vol} outside [low, high]")
    return issues


def same_kline(a: dict, b: dict) -> list[str]:
    diffs = []
    for f in ("open_time", "trades"):
        if a[f] != b[f]:
            diffs.append(f)
    for f in NUMERIC_FIELDS:
        if abs(float(a[f]) - float(b[f])) > 1e-9 * max(1.0, abs(float(a[f]))):
            diffs.append(f)
    return sorted(diffs)


def discover_spot_symbols(tokens: list[dict], quotes: list[str]) -> tuple[dict, str]:
    info, base = get_json(SPOT_BASES, "/api/v3/exchangeInfo", {"permissions": "SPOT"})
    found: dict[str, list[dict]] = {t["name"]: [] for t in tokens}
    by_base = {t["base_asset"]: t["name"] for t in tokens}
    for s in info["symbols"]:
        name = by_base.get(s["baseAsset"])
        if name and s["quoteAsset"] in quotes:
            found[name].append({"symbol": s["symbol"], "quote": s["quoteAsset"], "status": s["status"]})
    return found, base


def discover_futures_symbols(tokens: list[dict], quotes: list[str]) -> tuple[dict, str]:
    found: dict[str, list[dict]] = {t["name"]: [] for t in tokens}
    try:
        info, base = get_json(FUTURES_BASES, "/fapi/v1/exchangeInfo")
    except HttpError as e:
        return {}, f"unavailable (HTTP {e.status})" if e.status else "unavailable"
    by_base = {t["base_asset"]: t["name"] for t in tokens}
    for s in info["symbols"]:
        name = by_base.get(s["baseAsset"])
        if name and s["quoteAsset"] in quotes and s.get("contractType") == "PERPETUAL":
            found[name].append({"symbol": s["symbol"], "quote": s["quoteAsset"], "status": s["status"]})
    return found, base


def collect_spot(symbol: str, today: date, prev: date) -> tuple[dict, list[str]]:
    out: dict = {"symbol": symbol}
    issues: list[str] = []
    ticker, base = get_json(SPOT_BASES, "/api/v3/ticker/24hr", {"symbol": symbol})
    out["source"] = base
    out["ticker_24h"] = {k: ticker[k] for k in (
        "lastPrice", "bidPrice", "askPrice", "openPrice", "highPrice", "lowPrice",
        "weightedAvgPrice", "priceChange", "priceChangePercent", "volume", "quoteVolume", "count",
    )}
    out["ticker_24h"]["openTime_utc"] = ms_to_iso(ticker["openTime"])
    out["ticker_24h"]["closeTime_utc"] = ms_to_iso(ticker["closeTime"])

    today_k, _ = api_daily_kline(SPOT_BASES, "/api/v3/klines", symbol, today)
    if today_k:
        today_k["is_final"] = False
        issues += check_kline(today_k, f"spot {symbol} {today} partial")
        if today_k["close"] and abs(float(today_k["close"]) / float(ticker["lastPrice"]) - 1) > 0.02:
            issues.append(f"spot {symbol}: today close {today_k['close']} vs ticker last {ticker['lastPrice']} differ >2%")
    out["today_partial_1d"] = today_k

    prev_k, _ = api_daily_kline(SPOT_BASES, "/api/v3/klines", symbol, prev)
    validation: dict = {}
    if prev_k:
        prev_k["is_final"] = True
        issues += check_kline(prev_k, f"spot {symbol} {prev} final")
        archived = archive_daily_kline("spot", symbol, prev)
        if archived is None:
            validation["archive_cross_check"] = "archive not published yet"
        else:
            diffs = same_kline(prev_k, archived)
            validation["archive_cross_check"] = "match" if not diffs else f"mismatch: {diffs}"
            validation["archive_url"] = archived["source_url"]
            if diffs:
                issues.append(f"spot {symbol} {prev}: API vs archive mismatch {diffs}")
    out["previous_day_final_1d"] = prev_k
    out["validation"] = validation
    return out, issues


def collect_futures_live(symbol: str, today: date, prev: date) -> tuple[dict, list[str]]:
    out: dict = {"symbol": symbol, "mode": "live"}
    issues: list[str] = []
    ticker, base = get_json(FUTURES_BASES, "/fapi/v1/ticker/24hr", {"symbol": symbol})
    premium, _ = get_json(FUTURES_BASES, "/fapi/v1/premiumIndex", {"symbol": symbol})
    oi, _ = get_json(FUTURES_BASES, "/fapi/v1/openInterest", {"symbol": symbol})
    out["source"] = base
    out["ticker_24h"] = {k: ticker[k] for k in (
        "lastPrice", "openPrice", "highPrice", "lowPrice", "weightedAvgPrice",
        "priceChange", "priceChangePercent", "volume", "quoteVolume", "count",
    )}
    out["mark_price"] = premium.get("markPrice")
    out["index_price"] = premium.get("indexPrice")
    out["last_funding_rate"] = premium.get("lastFundingRate")
    out["open_interest"] = oi.get("openInterest")
    today_k, _ = api_daily_kline(FUTURES_BASES, "/fapi/v1/klines", symbol, today)
    prev_k, _ = api_daily_kline(FUTURES_BASES, "/fapi/v1/klines", symbol, prev)
    for k, final, d in ((today_k, False, today), (prev_k, True, prev)):
        if k:
            k["is_final"] = final
            issues += check_kline(k, f"futures {symbol} {d}")
    out["today_partial_1d"] = today_k
    out["previous_day_final_1d"] = prev_k
    return out, issues


def collect_futures_archive(symbol: str, prev: date) -> tuple[dict | None, list[str]]:
    """Fallback when fapi.binance.com is geo-blocked: finalized daily candle from data.binance.vision."""
    k = archive_daily_kline("futures", symbol, prev)
    if k is None:
        return None, []
    k["is_final"] = True
    return {
        "symbol": symbol,
        "mode": "archive",
        "source": ARCHIVE_BASE,
        "note": "fapi.binance.com unreachable from runner; using finalized daily kline from data.binance.vision",
        "previous_day_final_1d": k,
    }, check_kline(k, f"futures {symbol} {prev} archive")


def run(now: datetime, out_root: Path, config: dict) -> tuple[dict, int]:
    tokens, quotes = config["tokens"], config["quote_assets"]
    today = now.date()
    prev = today - timedelta(days=1)
    run_dir = out_root / today.isoformat() / now.strftime("%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)

    spot_syms, spot_base = discover_spot_symbols(tokens, quotes)
    fut_syms, fut_base = discover_futures_symbols(tokens, quotes)
    futures_live = bool(fut_syms)

    summary: dict = {
        "run_at_utc": now.isoformat().replace("+00:00", "Z"),
        "today_utc": today.isoformat(),
        "previous_day_utc": prev.isoformat(),
        "spot_source": spot_base,
        "futures_source": fut_base if futures_live else f"archive ({fut_base})",
        "tokens": {},
        "issues": [],
        "errors": [],
    }

    for t in tokens:
        name = t["name"]
        record: dict = {
            "token": name, "base_asset": t["base_asset"],
            "run_at_utc": summary["run_at_utc"], "spot": [], "futures": [],
        }
        for s in spot_syms.get(name, []):
            if s["status"] != "TRADING":
                continue
            try:
                data, issues = collect_spot(s["symbol"], today, prev)
                record["spot"].append(data)
                summary["issues"] += issues
            except HttpError as e:
                summary["errors"].append(f"spot {s['symbol']}: {e}")

        if futures_live:
            candidates = [s["symbol"] for s in fut_syms.get(name, []) if s["status"] == "TRADING"]
        else:
            candidates = [t["base_asset"] + q for q in quotes]
        for sym in candidates:
            try:
                if futures_live:
                    data, issues = collect_futures_live(sym, today, prev)
                else:
                    data, issues = collect_futures_archive(sym, prev)
                if data:
                    record["futures"].append(data)
                    summary["issues"] += issues
            except HttpError as e:
                summary["errors"].append(f"futures {sym}: {e}")

        listed = bool(record["spot"] or record["futures"])
        record["status"] = "ok" if listed else "not_listed_on_binance"
        (run_dir / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["tokens"][name] = {
            "status": record["status"],
            "spot_symbols": [s["symbol"] for s in record["spot"]],
            "futures_symbols": [f["symbol"] for f in record["futures"]],
        }

    (run_dir / "_run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok_tokens = [n for n, v in summary["tokens"].items() if v["status"] == "ok"]
    exit_code = 0 if ok_tokens and not summary["errors"] and not summary["issues"] else 1
    return summary, exit_code


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(ROOT / "data"), help="output root directory")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    summary, code = run(datetime.now(timezone.utc).replace(microsecond=0), Path(args.out), config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
