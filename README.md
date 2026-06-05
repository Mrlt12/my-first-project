# Daily Indonesian Markets Report Bot

Generates a daily QUANT (Elliott Wave + Fibonacci) + QUAL (BI/IHSG/OJK macro)
trading report across IDX stocks, forex, crypto, and commodity proxies, then posts
it to a Discord channel. Runs free on GitHub Actions — no server needed.

> ⚠️ **Disclaimer:** Output is research/educational only, **not** investment advice.
> Elliott Wave counts are interpretive. You are the trader making every decision.

---

## What you need (all free)

| Key | Where to get it | Used for |
|-----|-----------------|----------|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys | Claude analysis (paid per use, ~cents/day) |
| `DISCORD_WEBHOOK_URL` | Discord channel → Edit → Integrations → Webhooks → New Webhook → Copy URL | Posting the report |
| `ITICK_API_KEY` | itick.org (free tier) | IDX stock bars |
| `FINNHUB_API_KEY` | finnhub.io (free tier) | Forex + commodity proxies |

Crypto uses Binance's public API — no key needed.

> Note: only the Anthropic API costs money (usage-based, separate from your Claude
> subscription). A single daily report is a few cents. The data APIs are free-tier.

---

## Setup (10 minutes)

### 1. Create the Discord webhook
In your Discord server: pick a channel → ⚙️ Edit Channel → Integrations →
Webhooks → New Webhook → name it → **Copy Webhook URL**.

### 2. Create the GitHub repo
1. Make a new repo (private is fine).
2. Upload all files in this folder, keeping the structure:
   ```
   bot.py
   requirements.txt
   .github/workflows/daily-report.yml
   ```

### 3. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add one secret per key from the table above. Names must match exactly.

### 4. Edit your watchlist (optional)
Open `bot.py`, edit the lists near the top:
```python
IDX_STOCKS  = ["BBCA", "BBRI", "TLKM", "ANTM", "ASII"]
FOREX_PAIRS = ["USD/IDR", "EUR/IDR", "EUR/USD", "GBP/USD"]
CRYPTO      = ["BTCUSDT", "ETHUSDT"]
```

### 5. Test it
Repo → **Actions** tab → "Daily Trading Report" → **Run workflow**.
Watch the run; the report should land in your Discord channel within a minute or two.

### 6. Schedule
It's already scheduled for **16:30 WIB (09:30 UTC), Mon–Fri** — after IDX close.
Change the `cron:` line in the workflow to adjust. (GitHub cron is always UTC;
WIB = UTC+7.)

---

## Local testing (optional)
```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your keys
export $(cat .env | xargs)  # load them
python bot.py
```

---

## Notes & limitations
- **Free data = delayed/EOD**, not true real-time ticks. Fine for an after-close daily report.
- iTick free-tier IDX coverage can occasionally lag; if a ticker returns no data it's skipped.
- CPO/coal don't have clean free APIs; copper (`OANDA:XCU_USD`) is used as an industrial-metal
  sentiment proxy. Swap in a paid feed later if you want true CPO/coal/nickel futures.
- Discord caps messages at 2000 chars; the bot auto-chunks longer reports.
- GitHub Actions scheduled jobs can run a few minutes late under load — normal.
