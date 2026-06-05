#!/usr/bin/env python3
"""
Daily Indonesian-markets trading report bot.

Pipeline:
  1. Fetch OHLC data (IDX stocks, forex, crypto, commodity proxies)
  2. Compute Fibonacci levels + basic technicals locally
  3. Send the data to Claude for Elliott Wave / macro analysis
  4. Post the formatted report to a Discord channel via webhook

Runs on a schedule via GitHub Actions (see .github/workflows/daily-report.yml).

Disclaimer: output is research/educational. It is NOT investment advice.
You are the trader making the decisions.
"""

import os
import sys
import json
import time
import datetime as dt
from typing import Optional

import requests

# ----------------------------------------------------------------------------
# Config -- edit WATCHLISTS here. Secrets come from environment variables.
# ----------------------------------------------------------------------------

IDX_STOCKS = ["^JKSE", "BBCA", "BBRI", "TLKM", "ANTM", "ASII"]  # ^JKSE = IDX Composite (JCI)
FOREX_PAIRS = ["USD/IDR", "EUR/IDR", "EUR/USD", "GBP/USD"]  # base/quote
CRYPTO = ["BTCUSDT", "ETHUSDT"]                            # Binance symbols
# Commodity proxies traded on global exchanges / ETFs that track Indonesian exports.
# True CPO/coal/nickel futures need a paid feed. Free industrial-metal proxies aren't
# reliably available, so the free build omits commodities. Add a paid source later.
COMMODITY_PROXIES = []  # disabled on the free tier (see README)

TIMEFRAME = "D"          # daily bars for swing context
LOOKBACK_BARS = 120      # ~6 months of daily bars for wave context

CLAUDE_MODEL = "claude-opus-4-8"
ANTHROPIC_VERSION = "2023-06-01"

# Secrets (set as GitHub Actions secrets / env vars)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
# Data sources are now all keyless:
#   IDX stocks -> Yahoo Finance | forex -> Frankfurter | crypto -> Coinbase
# Only ANTHROPIC_API_KEY and DISCORD_WEBHOOK_URL are required.

TIMEOUT = 20


# ----------------------------------------------------------------------------
# Data fetchers
# ----------------------------------------------------------------------------

def safe_get(url: str, **kwargs) -> Optional[dict]:
    """GET with basic error handling; returns parsed JSON or None."""
    try:
        r = requests.get(url, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[warn] GET {url} failed: {e}", file=sys.stderr)
        return None


def _yahoo_chart(yahoo_symbol: str) -> Optional[list]:
    """Daily closes from Yahoo Finance public chart endpoint (no key, no geo-block)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    params = {"range": "6mo", "interval": "1d"}
    data = safe_get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
    try:
        res = data["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        return closes[-LOOKBACK_BARS:] if closes else None
    except (KeyError, IndexError, TypeError):
        return None


def fetch_idx_bars(symbol: str) -> Optional[list]:
    """IDX daily closes via Yahoo Finance. IDX tickers use the .JK suffix.

    Returns a list of iTick-style dicts {"c": close} so build_market_snapshot's
    existing parsing keeps working unchanged.
    """
    yahoo_sym = symbol if symbol.startswith("^") else f"{symbol}.JK"
    closes = _yahoo_chart(yahoo_sym)
    if not closes:
        return None
    return [{"c": c} for c in closes]


def fetch_forex(pair: str) -> Optional[dict]:
    """Daily forex time-series via Frankfurter (no key, no geo-block).

    Returns {"c": [closes...]} to match the rest of the pipeline.
    pair like 'USD/IDR' -> from=USD, to=IDR.
    """
    base, quote = pair.split("/")
    end = dt.date.today()
    start = end - dt.timedelta(days=int(LOOKBACK_BARS * 1.6) + 10)  # pad for weekends/holidays
    url = (f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}"
           f"?base={base}&symbols={quote}")
    data = safe_get(url)
    if not data or "rates" not in data:
        return None
    # rates is a dict keyed by date -> {quote: value}; sort by date ascending.
    closes = [v[quote] for _, v in sorted(data["rates"].items()) if quote in v]
    if not closes:
        return None
    return {"c": closes[-LOOKBACK_BARS:]}


def fetch_crypto(symbol: str) -> Optional[list]:
    """Daily crypto candles via Coinbase (no key, not geo-blocked like Binance).

    Accepts Binance-style symbols ('BTCUSDT') and maps to Coinbase ('BTC-USD').
    Returns a Binance-style list so downstream parsing is unchanged:
    [openTime, o, h, l, c, v]
    """
    s = symbol.upper().replace("USDT", "USD")
    # insert dash before the 3-char quote (USD/EUR etc.)
    product = f"{s[:-3]}-{s[-3:]}"
    data = safe_get(
        f"https://api.exchange.coinbase.com/products/{product}/candles",
        params={"granularity": 86400},  # 1 day; returns up to 300 candles, newest first
        headers={"User-Agent": "daily-report-bot"},
    )
    if not data or not isinstance(data, list):
        return None
    # Coinbase row: [time, low, high, open, close, volume], newest first -> reverse.
    rows = list(reversed(data))[-LOOKBACK_BARS:]
    return [[r[0], r[3], r[2], r[1], r[4], r[5]] for r in rows]


def fetch_commodity(symbol: str) -> Optional[dict]:
    """Commodities require a paid feed on the free tier -- disabled.

    COMMODITY_PROXIES is empty, so this is never called in the free build.
    Kept as a stub so you can wire in a paid source later.
    """
    return None


# ----------------------------------------------------------------------------
# Local technical computation (so Claude reasons over real numbers)
# ----------------------------------------------------------------------------

def fib_levels(high: float, low: float) -> dict:
    """Standard retracement + extension levels for a swing high/low."""
    diff = high - low
    return {
        "swing_high": round(high, 4),
        "swing_low": round(low, 4),
        "retr_0.236": round(high - diff * 0.236, 4),
        "retr_0.382": round(high - diff * 0.382, 4),
        "retr_0.500": round(high - diff * 0.500, 4),
        "retr_0.618": round(high - diff * 0.618, 4),
        "retr_0.786": round(high - diff * 0.786, 4),
        "ext_1.272": round(high + diff * 0.272, 4),
        "ext_1.618": round(high + diff * 0.618, 4),
    }


def summarize_closes(closes: list) -> dict:
    """Compute simple stats from a list of closing prices."""
    if not closes:
        return {}
    n = len(closes)
    hi, lo = max(closes), min(closes)
    last = closes[-1]
    ma20 = round(sum(closes[-20:]) / min(20, n), 4)
    ma50 = round(sum(closes[-50:]) / min(50, n), 4)
    return {
        "last_close": round(last, 4),
        "period_high": round(hi, 4),
        "period_low": round(lo, 4),
        "ma20": ma20,
        "ma50": ma50,
        "pct_from_high": round((last - hi) / hi * 100, 2),
        "fib": fib_levels(hi, lo),
    }


def build_market_snapshot() -> dict:
    """Gather everything into one structured object for Claude."""
    snapshot = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat() + "Z",
                "idx_stocks": {}, "forex": {}, "crypto": {}, "commodities": {}}

    for sym in IDX_STOCKS:
        bars = fetch_idx_bars(sym)
        if bars:
            closes = [float(b.get("c", b.get("close", 0))) for b in bars]
            snapshot["idx_stocks"][sym] = summarize_closes(closes)
        time.sleep(0.5)

    for pair in FOREX_PAIRS:
        fx = fetch_forex(pair)
        if fx and "c" in fx:
            snapshot["forex"][pair] = summarize_closes([float(x) for x in fx["c"]])
        elif fx and "spot" in fx:
            snapshot["forex"][pair] = {"spot_rate": fx["spot"]}
        time.sleep(0.5)

    for sym in CRYPTO:
        kl = fetch_crypto(sym)
        if kl:
            closes = [float(k[4]) for k in kl]
            snapshot["crypto"][sym] = summarize_closes(closes)
        time.sleep(0.3)

    for sym in COMMODITY_PROXIES:
        c = fetch_commodity(sym)
        if c and "c" in c:
            snapshot["commodities"][sym] = summarize_closes([float(x) for x in c["c"]])
        time.sleep(0.5)

    return snapshot


# ----------------------------------------------------------------------------
# Claude analysis
# ----------------------------------------------------------------------------

ANALYSIS_PROMPT = """You are a trading analyst producing a daily report for an active \
trader focused on Indonesian markets. Below is today's market snapshot with real OHLC \
stats and pre-computed Fibonacci levels in JSON.

For EACH instrument that has data, produce a concise two-part analysis:

PART 1 — QUANTITATIVE (Elliott Wave):
- Current wave count (impulse 1-5 or corrective A-B-C), labelled
- Which Fibonacci level price is reacting to (use the provided fib levels)
- Wave invalidation point (a specific price)
- Likely next target (specific price)
- Wave count confidence: High / Medium / Low
- Risk-reward ratio for the most reasonable setup

PART 2 — QUALITATIVE (macro):
- Bank Indonesia (BI) policy angle and IHSG macro environment
- Relevant sector rotation / political-regulatory risk (OJK context)
- Global commodity demand affecting Indonesia where relevant

Rules:
- Use IDR pricing for IDX names; native quote for forex/crypto.
- Keep each instrument to a tight block. Be specific with numbers from the snapshot.
- This is research, NOT investment advice. Do not tell the user to buy or sell.
- If an instrument has no data, skip it silently.
- Total length must fit a Discord message (<1900 chars per asset class). Be dense.

MARKET SNAPSHOT:
{snapshot}
"""


def run_claude(snapshot: dict) -> str:
    if not ANTHROPIC_API_KEY:
        return "[error] ANTHROPIC_API_KEY not set."
    prompt = ANALYSIS_PROMPT.format(snapshot=json.dumps(snapshot, indent=2))
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
    except Exception as e:
        return f"[error] Claude request failed: {e}"


# ----------------------------------------------------------------------------
# Discord delivery
# ----------------------------------------------------------------------------

def post_to_discord(report: str):
    """Discord messages cap at 2000 chars; chunk the report safely."""
    if not DISCORD_WEBHOOK_URL:
        print("[error] DISCORD_WEBHOOK_URL not set.", file=sys.stderr)
        print(report)
        return

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    header = f"**📊 Daily Indonesian Markets Report — {today} UTC**\n*Research only, not investment advice.*\n"
    chunks = chunk_text(header + "\n" + report, 1900)

    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            print(f"[error] Discord post chunk {i} failed: {e}", file=sys.stderr)
        time.sleep(0.6)  # respect Discord rate limits


def chunk_text(text: str, size: int) -> list:
    """Split text into <=size chunks, preferring paragraph boundaries."""
    chunks, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > size:
            if current:
                chunks.append(current)
            current = para
        else:
            current = current + "\n" + para if current else para
    if current:
        chunks.append(current)
    return chunks


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    print("Building market snapshot...")
    snapshot = build_market_snapshot()

    have_data = any(snapshot[k] for k in ("idx_stocks", "forex", "crypto", "commodities"))
    if not have_data:
        print("[error] No market data fetched. Check API keys / network.", file=sys.stderr)
        post_to_discord("⚠️ No market data could be fetched today. Check API keys.")
        sys.exit(1)

    print("Running Claude analysis...")
    report = run_claude(snapshot)

    print("Posting to Discord...")
    post_to_discord(report)
    print("Done.")


if __name__ == "__main__":
    main()
