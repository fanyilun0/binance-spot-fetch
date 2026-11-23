#!/usr/bin/env python3
"""Timestamped HYPE quotes from exchange-owned APIs; independent of daily klines."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HL_INFO = "https://api.hyperliquid.xyz/info"
SPOT_URLS = ("https://data-api.binance.vision", "https://api.binance.com")
FUTURES_URLS = ("https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com")
TIMEOUT = 4


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def request_json(url: str, body: dict | None = None) -> tuple[object, int, str]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=payload,
        headers={"User-Agent": "hype-official-quote/1.0", "Content-Type": "application/json"},
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        data = json.load(response)
    return data, round((time.monotonic() - start) * 1000), now_utc()


def try_get(bases: tuple[str, ...], path: str) -> tuple[dict, str, int, str]:
    failures = []
    for base in bases:
        url = base + path
        try:
            data, latency_ms, received_at = request_json(url)
            if not isinstance(data, dict):
                raise ValueError("expected JSON object")
            return data, url, latency_ms, received_at
        except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
            failures.append(f"{base}: {type(exc).__name__}: {str(exc)[:90]}")
    raise RuntimeError("; ".join(failures))


def err_record(exchange: str, market: str, symbol: str, exc: Exception) -> dict:
    return {"status": "error", "exchange": exchange, "market": market, "symbol": symbol,
            "price": None, "error": f"{type(exc).__name__}: {exc}"[:450]}


def binance_spot() -> dict:
    symbol = "HYPEUSDT"
    try:
        result, url, elapsed, observed_at = try_get(
            SPOT_URLS, f"/api/v3/ticker/price?symbol={symbol}")
        price = result.get("price")
        if not price or float(price) <= 0:
            raise ValueError(f"invalid Binance spot price: {price!r}")
        row = {"status": "ok", "exchange": "Binance", "market": "spot", "symbol": symbol,
               "base": "HYPE", "quote": "USDT", "price": str(price), "price_type": "last",
               "observed_at_utc": observed_at, "request_latency_ms": elapsed, "source_url": url}
        try:
            stats, stats_url, _, stats_at = try_get(
                SPOT_URLS, f"/api/v3/ticker/24hr?symbol={symbol}")
            row.update(change_pct_24h=str(stats["priceChangePercent"]),
                       stats_observed_at_utc=stats_at, stats_source_url=stats_url)
        except Exception as exc:
            row["stats_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return row
    except Exception as exc:
        return err_record("Binance", "spot", symbol, exc)


def binance_perp() -> dict:
    symbol = "HYPEUSDT"
    try:
        result, url, elapsed, observed_at = try_get(
            FUTURES_URLS, f"/fapi/v1/ticker/price?symbol={symbol}")
        price = result.get("price")
        if not price or float(price) <= 0:
            raise ValueError(f"invalid Binance perpetual price: {price!r}")
        row = {"status": "ok", "exchange": "Binance", "market": "usdm_perpetual",
               "symbol": symbol, "base": "HYPE", "quote": "USDT", "price": str(price),
               "price_type": "last", "observed_at_utc": observed_at,
               "request_latency_ms": elapsed, "source_url": url}
        try:
            stats, stats_url, _, stats_at = try_get(
                FUTURES_URLS, f"/fapi/v1/ticker/24hr?symbol={symbol}")
            row.update(change_pct_24h=str(stats["priceChangePercent"]),
                       stats_observed_at_utc=stats_at, stats_source_url=stats_url)
        except Exception as exc:
            row["stats_error"] = f"{type(exc).__name__}: {exc}"[:200]
        try:
            premium, premium_url, _, premium_at = try_get(
                FUTURES_URLS, f"/fapi/v1/premiumIndex?symbol={symbol}")
            row.update(mark_price=str(premium["markPrice"]),
                       funding_rate=str(premium["lastFundingRate"]),
                       funding_observed_at_utc=premium_at, funding_source_url=premium_url)
        except Exception as exc:
            row["funding_error"] = f"{type(exc).__name__}: {exc}"[:200]
        return row
    except Exception as exc:
        return err_record("Binance", "usdm_perpetual", symbol, exc)


def hyperliquid_spot() -> dict:
    """Resolve by token.index AND assetCtx.coin, never by list offset.

    Metadata token indices are identifiers rather than positions. The spot
    contexts may not have the same positional ordering as the universe.
    """
    try:
        response, elapsed, observed_at = request_json(HL_INFO, {"type": "spotMetaAndAssetCtxs"})
        if not isinstance(response, list) or len(response) != 2:
            raise ValueError("unexpected spotMetaAndAssetCtxs response")
        meta, contexts = response
        tokens_by_index = {int(t["index"]): t for t in meta["tokens"]}
        contexts_by_coin = {c["coin"]: c for c in contexts if c.get("coin")}
        expected_token_id = "0x0d01dc56dcaaca66ad901c959b4011ec"
        for asset in meta["universe"]:
            if len(asset.get("tokens", [])) != 2:
                continue
            base_token = tokens_by_index.get(int(asset["tokens"][0]), {})
            quote_token = tokens_by_index.get(int(asset["tokens"][1]), {})
            if (base_token.get("name") == "HYPE"
                    and base_token.get("tokenId", "").lower() == expected_token_id
                    and quote_token.get("name") == "USDC"):
                coin = asset["name"]
                ctx = contexts_by_coin.get(coin)
                if ctx is None:
                    raise ValueError(f"HYPE/USDC context for {coin} absent or missing coin identification")
                price = ctx.get("midPx") or ctx.get("markPx")
                if not price or float(price) <= 0:
                    raise ValueError("HYPE/USDC spot midpoint unavailable")
                row = {"status": "ok", "exchange": "Hyperliquid", "market": "spot",
                       "symbol": coin, "base": "HYPE", "quote": "USDC",
                       "price": str(price), "price_type": "mid" if ctx.get("midPx") else "mark",
                       "observed_at_utc": observed_at, "request_latency_ms": elapsed,
                       "token_index": int(base_token["index"]),
                       "token_id": base_token["tokenId"], "asset_index": int(asset["index"]),
                       "source_url": HL_INFO, "request_body": {"type": "spotMetaAndAssetCtxs"}}
                if ctx.get("prevDayPx"):
                    row["prev_day_price"] = str(ctx["prevDayPx"])
                return row
        raise ValueError("native HYPE/USDC pair not found in official metadata")
    except Exception as exc:
        return err_record("Hyperliquid", "spot", "HYPE/USDC", exc)


def hyperliquid_perp() -> dict:
    try:
        response, elapsed, observed_at = request_json(HL_INFO, {"type": "metaAndAssetCtxs"})
        if not isinstance(response, list) or len(response) != 2:
            raise ValueError("unexpected metaAndAssetCtxs response")
        meta, contexts = response
        for idx, asset in enumerate(meta["universe"]):
            if asset["name"] == "HYPE":
                ctx = contexts[idx]
                price = ctx.get("midPx") or ctx.get("markPx")
                if not price or float(price) <= 0:
                    raise ValueError("HYPE perpetual midpoint unavailable")
                row = {"status": "ok", "exchange": "Hyperliquid", "market": "perpetual",
                       "symbol": "HYPE", "base": "HYPE", "quote": "USDC",
                       "price": str(price), "price_type": "mid" if ctx.get("midPx") else "mark",
                       "observed_at_utc": observed_at, "request_latency_ms": elapsed,
                       "source_url": HL_INFO, "request_body": {"type": "metaAndAssetCtxs"}}
                for field in ("markPx", "funding", "openInterest", "prevDayPx"):
                    if ctx.get(field) is not None:
                        row[field] = str(ctx[field])
                if ctx.get("funding") is not None:
                    row["funding_rate"] = str(ctx["funding"])
                    row["funding_interval"] = "hourly"
                return row
        raise ValueError("HYPE perpetual not found in official metadata")
    except Exception as exc:
        return err_record("Hyperliquid", "perpetual", "HYPE", exc)


def capture() -> dict:
    start = now_utc()
    markets = {
        "binance_spot_hypeusdt": binance_spot(),
        "binance_perp_hypeusdt": binance_perp(),
        "hyperliquid_spot_hypeusdc": hyperliquid_spot(),
        "hyperliquid_perp_hype": hyperliquid_perp(),
    }
    # Fail closed if the spot asset is wrongly mapped or repriced by an
    # inconsistent denomination. Do not publish an evidently false quote.
    hl_spot = markets["hyperliquid_spot_hypeusdc"]
    refs = [markets["binance_spot_hypeusdt"], markets["hyperliquid_perp_hype"]]
    if hl_spot["status"] == "ok":
        for ref in refs:
            if ref["status"] == "ok" and abs(float(hl_spot["price"]) / float(ref["price"]) - 1) > 0.10:
                bad_price = hl_spot.pop("price")
                hl_spot["status"] = "error"
                hl_spot["error"] = ("HYPE spot price validation failed: "
                                    f"{bad_price} versus {ref['exchange']} {ref['market']} "
                                    f"{ref['price']} (difference over 10%)")
                break
    ok = sum(item["status"] == "ok" for item in markets.values())
    return {"version": 2, "token": "HYPE", "source_policy": "exchange_official_only",
            "capture_started_at_utc": start, "capture_completed_at_utc": now_utc(),
            "status": "complete" if ok == 4 else "partial" if ok else "unavailable",
            "successful_markets": ok, "markets": markets}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/realtime/HYPE.json")
    args = parser.parse_args()
    result = capture()
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dest.parent,
                                     prefix=".hype-quote-", delete=False) as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        tmp = fh.name
    os.replace(tmp, dest)
    print(f"HYPE quote snapshot: {result['status']} ({result['successful_markets']}/4) -> {dest}")
    for key, row in result["markets"].items():
        print(f"  {key}: {row.get('price') or row.get('error')}")


if __name__ == "__main__":
    main()
