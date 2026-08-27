#!/usr/bin/env python3
"""Validated Indonesian-market report bot.

The factual market section is calculated locally from timestamped series. AI
commentary is optional and is never allowed to replace the verified figures.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Iterable

import requests


IDX_STOCKS = ["^JKSE", "BBCA", "BBRI", "TLKM", "ANTM", "ASII"]
FOREX_PAIRS = ["USD/IDR", "EUR/IDR", "EUR/USD", "GBP/USD"]
CRYPTO = ["BTCUSDT", "ETHUSDT"]
LOOKBACK_BARS = 120
TIMEOUT = 25
WIB = dt.timezone(dt.timedelta(hours=7))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5").strip()


@dataclass(frozen=True)
class Point:
    date: dt.date
    close: float


def get_json(url: str, **kwargs):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=TIMEOUT, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # logged after the final retry
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"GET failed after 3 attempts: {url}: {last_error}")


def yahoo_series(symbol: str) -> list[Point]:
    yahoo_symbol = symbol if symbol.startswith("^") else f"{symbol}.JK"
    data = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
        params={"range": "6mo", "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0 daily-market-report"},
    )
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0].get("close", [])
    points = []
    for timestamp, close in zip(timestamps, closes):
        if close is not None and math.isfinite(float(close)):
            date = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date()
            points.append(Point(date, float(close)))
    return points[-LOOKBACK_BARS:]


def forex_series(pair: str) -> list[Point]:
    base, quote = pair.split("/")
    end = dt.date.today()
    start = end - dt.timedelta(days=220)
    data = get_json(
        f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}",
        params={"base": base, "symbols": quote},
    )
    points = [
        Point(dt.date.fromisoformat(day), float(values[quote]))
        for day, values in sorted(data.get("rates", {}).items())
        if quote in values
    ]
    return points[-LOOKBACK_BARS:]


def crypto_series(symbol: str) -> list[Point]:
    normalized = symbol.upper().replace("USDT", "USD")
    product = f"{normalized[:-3]}-{normalized[-3:]}"
    rows = get_json(
        f"https://api.exchange.coinbase.com/products/{product}/candles",
        params={"granularity": 86400},
        headers={"User-Agent": "daily-market-report"},
    )
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    points = []
    for row in rows:
        candle_date = dt.datetime.fromtimestamp(row[0], dt.timezone.utc).date()
        # Coinbase includes today's unfinished daily candle. Never report it as
        # an official daily close.
        if candle_date < today_utc:
            points.append(Point(candle_date, float(row[4])))
    return sorted(points, key=lambda item: item.date)[-LOOKBACK_BARS:]


def pct_change(current: float, comparison: float) -> float | None:
    return None if comparison == 0 else round((current / comparison - 1) * 100, 2)


def rsi14(values: list[float]) -> float | None:
    if len(values) < 15:
        return None
    changes = [values[i] - values[i - 1] for i in range(len(values) - 14, len(values))]
    gains = sum(max(change, 0) for change in changes) / 14
    losses = sum(max(-change, 0) for change in changes) / 14
    if losses == 0:
        return 100.0
    return round(100 - 100 / (1 + gains / losses), 2)


def directional_fibonacci(values: list[float]) -> dict:
    high = max(values)
    low = min(values)
    difference = high - low
    high_index = max(i for i, value in enumerate(values) if value == high)
    low_index = max(i for i, value in enumerate(values) if value == low)
    trend = "up" if low_index < high_index else "down"

    if trend == "up":
        levels = {
            "retr_38.2": high - difference * 0.382,
            "retr_50.0": high - difference * 0.500,
            "retr_61.8": high - difference * 0.618,
            "extension_127.2": high + difference * 0.272,
        }
    else:
        levels = {
            "retr_38.2": low + difference * 0.382,
            "retr_50.0": low + difference * 0.500,
            "retr_61.8": low + difference * 0.618,
            "extension_127.2": low - difference * 0.272,
        }
    return {"direction": trend, **{key: round(value, 4) for key, value in levels.items()}}


def summarize(points: list[Point], max_age_days: int) -> dict:
    if len(points) < 50:
        raise ValueError(f"only {len(points)} valid bars; at least 50 required")
    values = [point.close for point in points]
    as_of = points[-1].date
    age_days = (dt.datetime.now(WIB).date() - as_of).days
    return {
        "as_of": as_of.isoformat(),
        "status": "CURRENT" if age_days <= max_age_days else "STALE",
        "age_days": age_days,
        "last_close": round(values[-1], 4),
        "change_1d_pct": pct_change(values[-1], values[-2]),
        "change_5d_pct": pct_change(values[-1], values[-6]),
        "ma20": round(sum(values[-20:]) / 20, 4),
        "ma50": round(sum(values[-50:]) / 50, 4),
        "rsi14": rsi14(values),
        "period_high": round(max(values), 4),
        "period_low": round(min(values), 4),
        "fibonacci": directional_fibonacci(values),
    }


def fetch_group(symbols: Iterable[str], fetcher, max_age_days: int) -> tuple[dict, list[str]]:
    output, errors = {}, []
    for symbol in symbols:
        try:
            output[symbol] = summarize(fetcher(symbol), max_age_days)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
        time.sleep(0.4)
    return output, errors


def build_snapshot() -> dict:
    idx, idx_errors = fetch_group(IDX_STOCKS, yahoo_series, 5)
    forex, forex_errors = fetch_group(FOREX_PAIRS, forex_series, 5)
    crypto, crypto_errors = fetch_group(CRYPTO, crypto_series, 2)
    snapshot = {
        "generated_wib": dt.datetime.now(WIB).isoformat(timespec="seconds"),
        "idx": idx,
        "forex": forex,
        "crypto": crypto,
        "errors": idx_errors + forex_errors + crypto_errors,
    }
    if not any((idx, forex, crypto)):
        raise RuntimeError("No validated market data was fetched")
    return snapshot


def validate_snapshot(snapshot: dict) -> None:
    """Fail closed so incomplete or stale figures never reach Discord."""
    expected = {
        "idx": len(IDX_STOCKS),
        "forex": len(FOREX_PAIRS),
        "crypto": len(CRYPTO),
    }
    issues = list(snapshot["errors"])
    for group, expected_count in expected.items():
        instruments = snapshot[group]
        if len(instruments) != expected_count:
            issues.append(
                f"{group}: expected {expected_count} instruments, got {len(instruments)}"
            )
        for symbol, metric in instruments.items():
            if metric["status"] != "CURRENT":
                issues.append(
                    f"{symbol}: stale source data as of {metric['as_of']} "
                    f"({metric['age_days']} days old)"
                )
    if issues:
        raise RuntimeError("DATA_QUALITY_BLOCKED: " + " | ".join(issues))


def fmt_number(value) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.2f}"


def deterministic_report(snapshot: dict) -> str:
    sections = []
    labels = (("IDX", snapshot["idx"]), ("FOREX", snapshot["forex"]), ("CRYPTO", snapshot["crypto"]))
    for label, instruments in labels:
        lines = [f"**{label} — verified figures**"]
        for symbol, metric in instruments.items():
            stale = " ⚠️ STALE" if metric["status"] == "STALE" else ""
            lines.append(
                f"`{symbol}` {fmt_number(metric['last_close'])} | "
                f"1D {fmt_number(metric['change_1d_pct'])}% | "
                f"5D {fmt_number(metric['change_5d_pct'])}% | "
                f"RSI14 {fmt_number(metric['rsi14'])} | "
                f"as of {metric['as_of']}{stale}"
            )
        sections.append("\n".join(lines))
    if snapshot["errors"]:
        sections.append("**Data warnings**\n" + "\n".join(f"• {error}" for error in snapshot["errors"]))
    return "\n\n".join(sections)


def optional_ai_commentary(snapshot: dict) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    prompt = (
        "Write a short qualitative Indonesian-market commentary from this validated JSON. "
        "Do not state, copy, calculate, or invent any price, percentage, target, Elliott-wave "
        "count, or trading recommendation. Discuss only macro context and risks.\n\n"
        + json.dumps(snapshot, separators=(",", ":"))
    )
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    response.raise_for_status()
    blocks = response.json().get("content", [])
    return "".join(block.get("text", "") for block in blocks if block.get("type") == "text")


def chunk_text(text: str, size: int = 1900) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines():
        while len(line) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:size])
            line = line[size:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > size:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def post_to_discord(text: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")
    for index, chunk in enumerate(chunk_text(text), start=1):
        response = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=TIMEOUT)
        if response.status_code == 429:
            retry = float(response.json().get("retry_after", 1))
            time.sleep(retry)
            response = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=TIMEOUT)
        response.raise_for_status()
        print(f"Discord chunk {index} delivered")
        time.sleep(0.5)


def main() -> None:
    snapshot = build_snapshot()
    validate_snapshot(snapshot)
    report = deterministic_report(snapshot)
    try:
        ai_text = optional_ai_commentary(snapshot)
    except Exception as exc:
        print(f"[warning] Optional AI commentary failed: {exc}", file=sys.stderr)
        ai_text = ""

    now = dt.datetime.now(WIB)
    message = (
        f"**📊 Indonesian Markets Report — {now:%Y-%m-%d %H:%M} WIB**\n"
        "*Verified source dates are shown for every instrument. Research only.*\n\n"
        f"{report}"
    )
    if ai_text:
        message += f"\n\n**Optional macro commentary**\n{ai_text}"
    post_to_discord(message)
    print("REPORT_DELIVERED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
