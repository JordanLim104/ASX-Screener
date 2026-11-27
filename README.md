# ASX wounded mid-cap screener

Finds ASX mid-caps that have sold off hard but are still growing revenue and
EBITDA-positive. Two ways to run it:

- **`tv_screen.py` + dashboard (recommended).** Pulls the whole ASX from
  TradingView's screener in one request, applies the rules, and writes
  `docs/data/latest.json`. A GitHub Action runs it every weekday evening and
  GitHub Pages serves the mobile dashboard in `docs/`.
- **`screen.py`** (original). Ticker-by-ticker via yfinance/EODHD over
  `universe.txt`, emails an HTML table.

## Criteria (`criteria.yaml`)
1. Market cap A$500m–A$50bn
2. Price ≥ 50% below its 52-week high
3. Revenue grew YoY in each of the last 3 fiscal years
4. Latest-FY EBITDA > 0

If nothing passes, the run loosens to 48% drawdown and EBITDA > −A$50m and says so.
Names that miss exactly one rule by a small margin are listed as borderline. A pass
whose 52-week high was set in the first two weeks of the window is flagged fragile.

## Dashboard
`docs/index.html` is a static page with four styles (Ledger, Column, Index, Broker),
switched from the bar at the bottom or by URL hash (`#ledger`). Set
`DEFAULT_SKIN` and `SHOW_SWITCHER` at the top of its script once you've picked one.

Local preview:
```bash
python tv_screen.py && python -m http.server -d docs 8000
```

### Deploy (one-time)
1. Merge this branch into `main`.
2. Repo **Settings → Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`.
3. Repo **Settings → Actions → General → Workflow permissions**: "Read and write".
4. **Actions → Refresh screener data → Run workflow** to do a first run.

The site is then at `https://jordanlim104.github.io/ASX-Screener/`.

### Refresh cadence and rate limits
TradingView has no official free API; `scanner.tradingview.com` is the endpoint
its public screener page calls. The script makes **one POST per run** for ~400
rows, retries with backoff on 429/5xx, and refuses to overwrite data on a short
response. One run per weekday (07:30 UTC, after the ASX close) is a tiny load.
Fundamentals only move at reporting season (Feb/Aug), so the daily run is really
refreshing prices and drawdowns. Weekly is also reasonable; edit the cron.

---

# Original yfinance screener (`screen.py`)
Same rules as above, fetched per ticker.

## Run locally
```bash
pip install -r requirements.txt
python screen.py                 # uses criteria.yaml + universe.txt
```
Writes `output/results.json` and `output/email.html`, and prints a summary.
The email lists each match with its company name, sector, and a 12-month price
sparkline (built from pure HTML/CSS bars so it renders in Gmail — no images or SVG).

## Data provider
Default is **yfinance** (free, no key, unofficial — fine for a personal weekly
screen). For reliable ASX coverage, set `data.provider: eodhd` in `criteria.yaml`
and add an `EODHD_API_KEY` environment variable (EODHD is an official ASX provider
with fundamentals + EOD prices). Universe tickers are base symbols; the suffix
(`.AX` for yfinance, `.AU` for EODHD) is added automatically.

## Two interpretation choices worth confirming
- **"net YoY revenue growth in last 3 years"** — defaulted to `mode: each_year`
  (revenue grew in *every* one of the last 3 years). Switch to `mode: net` if you
  meant net-higher over the period, tolerating one down year.
- **"break-even to profitable (EBITDA)"** — defaulted to a *turnaround*: latest
  EBITDA positive AND at/below break-even within the prior 2 years. Set
  `require_turnaround: false` to simply require current EBITDA positivity.

## Wiring into the weekly routine
The routine prompt should run the script and send the result, e.g.:

> Run `pip install -r requirements.txt` then `python screen.py`. Read
> `output/email.html` and send it as an email titled "Weekly ASX Screen — {date}"
> to jordanlim104@gmail.com via Gmail. If `output/results.json` shows a fetch error for
> most tickers, email a short failure notice instead.

The script handles the empty-result case itself: if nothing matches the base
criteria it re-runs once with loosened thresholds (drawdown −2% and EBITDA
break-even threshold −A$50M, both set in `criteria.yaml` under `fallback`) and the
email states the actual values used plus a banner noting it loosened.

Set the routine to a **Weekly** schedule trigger (entered in Brisbane time).
EBITDA/revenue data updates only at reporting season, so a weekly cadence mainly
refreshes prices and drawdowns.
