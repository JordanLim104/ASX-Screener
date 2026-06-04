# Weekly ASX turnaround screener

Screens an ASX universe for beaten-down mid/large caps that are still growing
revenue and have just crossed into EBITDA profitability, then emails the matches.

## Criteria (set in `criteria.yaml`)
1. Market cap A$2bn–A$20bn
2. Price ≥ 50% below its 52-week high
3. Positive YoY revenue growth over the last 3 years
4. EBITDA "break-even → profitable" (a turnaround into positive EBITDA)

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
