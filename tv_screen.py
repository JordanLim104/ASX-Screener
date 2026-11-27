#!/usr/bin/env python3
"""
ASX "wounded mid-cap" screener, TradingView edition.

Pulls the whole ASX universe from TradingView's public screener endpoint in ONE
request (price, 52-week high + its date, market cap, and full fiscal-year revenue
and EBITDA history arrays), applies the rules in criteria.yaml, and writes JSON
for the static dashboard in docs/.

Rate limiting: TradingView has no official free API. scanner.tradingview.com is
the unofficial endpoint its own screener page uses. We are deliberately polite:
one POST per run (~400 rows), one run per weekday, retries with exponential
backoff on 429/5xx, and a plain, honest User-Agent. That is far below what a
single person browsing the screener page generates.

Rules (from criteria.yaml; mirrors the asx-wounded-midcap-screener skill):
  1. Market cap A$500m to A$50bn
  2. Price >= 50% below 52-week high
  3. Revenue grew YoY in EACH of the last 3 fiscal years
  4. Latest-FY EBITDA > 0
Fallback (only if nothing passes): drawdown 48%, EBITDA > -A$50m, flagged.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

SCAN_URL = "https://scanner.tradingview.com/australia/scan"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "asx-screener-dashboard/1.0 (daily personal screen; 1 request/day)",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}

COLUMNS = [
    "name", "description", "close", "change", "currency",
    "price_52_week_high", "price_52_week_high_date", "price_52_week_low",
    "High.6M", "High.3M",
    "market_cap_basic",
    "total_revenue_fy_h", "ebitda_fy_h", "fiscal_period_end_fy",
    "sector", "industry", "earnings_release_next_date",
    "Perf.W", "Perf.1M", "Perf.3M", "Perf.6M", "Perf.Y",
    "type", "subtype", "is_primary", "exchange",
]

# How far outside each rule a name can be and still show up as "borderline".
BORDERLINE = {
    "drawdown_pts": 10.0,       # 40-50% below high
    "mcap_frac": 0.15,          # within 15% of either cap bound
    "rev_yoy_pct": -3.0,        # one year between -3% and 0%
    "ebitda_aud": -75e6,        # EBITDA between -A$75m and 0
}
FRAGILE_DAYS = 14   # 52w high set in the first two weeks of the window


# --------------------------------------------------------------------------- #
def fetch_universe(cfg: dict) -> list[dict]:
    mc = cfg["market_cap"]
    lo = mc["min"] * (1 - BORDERLINE["mcap_frac"])
    hi = mc["max"] * (1 + BORDERLINE["mcap_frac"])
    payload = {
        "columns": COLUMNS,
        "filter": [
            {"left": "market_cap_basic", "operation": "in_range", "right": [lo, hi]},
            {"left": "exchange", "operation": "equal", "right": "ASX"},
            {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
        ],
        "options": {"lang": "en"},
        "range": [0, 1500],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
    }
    last_err = None
    for attempt in range(4):
        try:
            r = requests.post(SCAN_URL, json=payload, headers=HEADERS, timeout=45)
            if r.status_code == 200:
                body = r.json()
                return [dict(zip(COLUMNS, row["d"])) | {"symbol": row["s"]} for row in body["data"]]
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(15 * 2 ** attempt)   # 15s, 30s, 60s
    raise RuntimeError(f"TradingView scan failed: {last_err}")


# --------------------------------------------------------------------------- #
def _hist(arr, n):
    """Latest-first TradingView history array -> oldest-first list of n values (or None)."""
    if not arr:
        return None
    vals = arr[:n]
    if len(vals) < n or any(v is None for v in vals):
        return None
    return list(reversed(vals))


def _ladder(row):
    """Approximate price path from TradingView performance fields (1Y, 6M, 3M, 1M, 1W, now)."""
    close = row.get("close")
    if not close:
        return []
    pts = []
    for key, label in [("Perf.Y", "1Y"), ("Perf.6M", "6M"), ("Perf.3M", "3M"),
                       ("Perf.1M", "1M"), ("Perf.W", "1W")]:
        p = row.get(key)
        if p is not None and p > -100:
            pts.append({"t": label, "p": round(close / (1 + p / 100), 4)})
    pts.append({"t": "Now", "p": close})
    return pts


def evaluate(row: dict, cfg: dict, today: datetime) -> dict:
    mc, dd_cfg, rg, eb_cfg = cfg["market_cap"], cfg["drawdown"], cfg["revenue_growth"], cfg["ebitda"]
    fails = []      # (rule, detail, near_miss: bool)

    cap = row.get("market_cap_basic")
    if cap is None:
        fails.append(("mcap", "no market cap", False))
    elif cap < mc["min"]:
        fails.append(("mcap", f"A${cap/1e6:,.0f}m, below A${mc['min']/1e6:,.0f}m floor",
                      cap >= mc["min"] * (1 - BORDERLINE["mcap_frac"])))
    elif cap > mc["max"]:
        fails.append(("mcap", f"A${cap/1e9:,.1f}bn, above A${mc['max']/1e9:,.0f}bn ceiling",
                      cap <= mc["max"] * (1 + BORDERLINE["mcap_frac"])))

    price, high = row.get("close"), row.get("price_52_week_high")
    dd = None
    if not price or not high:
        fails.append(("drawdown", "no price / 52w high", False))
    else:
        dd = (high - price) / high * 100
        need = dd_cfg["min_pct_below_high"]
        if dd < need:
            fails.append(("drawdown", f"{dd:.1f}% below high, needs {need:.0f}% ({need-dd:.1f} pts short)",
                          dd >= need - BORDERLINE["drawdown_pts"]))

    years = rg["years"]
    rev = _hist(row.get("total_revenue_fy_h"), years + 1)
    yoy = None
    if rev is None:
        fails.append(("revenue", f"fewer than {years+1} fiscal years of revenue", False))
    else:
        yoy = [((b - a) / abs(a) * 100) if a else None for a, b in zip(rev, rev[1:])]
        bad = [g for g in yoy if g is None or g <= rg["min_yoy_pct"]]
        if bad:
            worst = min((g for g in yoy if g is not None), default=None)
            near = len(bad) == 1 and worst is not None and worst >= BORDERLINE["rev_yoy_pct"]
            fails.append(("revenue", f"revenue fell or was flat in {len(bad)} of {years} years"
                          + (f" (worst {worst:+.1f}%)" if worst is not None else ""), near))

    ebh = row.get("ebitda_fy_h") or []
    ebitda = ebh[0] if ebh else None
    thr = eb_cfg["breakeven_threshold"]
    if ebitda is None:
        fails.append(("ebitda", "no EBITDA reported", False))
    elif ebitda <= thr:
        fails.append(("ebitda", f"latest-FY EBITDA A${ebitda/1e6:,.0f}m, needs > A${thr/1e6:,.0f}m",
                      ebitda >= thr + BORDERLINE["ebitda_aud"]))

    # Fragile pass: 52w high set right at the start of the window rolls off soon.
    fragile, dd_6m, high_date = False, None, None
    hd = row.get("price_52_week_high_date")
    if hd:
        high_date = datetime.fromtimestamp(hd, tz=timezone.utc)
        window_start = today - timedelta(days=365)
        fragile = high_date <= window_start + timedelta(days=FRAGILE_DAYS)
    if price and row.get("High.6M"):
        dd_6m = (row["High.6M"] - price) / row["High.6M"] * 100

    if not fails:
        status = "Pass (fragile)" if fragile else "Pass"
    elif len(fails) == 1 and fails[0][2]:
        status = "Borderline"
    else:
        status = "Fail"

    fy_end = row.get("fiscal_period_end_fy")
    nxt = row.get("earnings_release_next_date")
    return {
        "ticker": row["name"],
        "symbol": row["symbol"],
        "name": row.get("description") or row["name"],
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "price": price,
        "change_pct": row.get("change"),
        "high_52w": high,
        "high_52w_date": high_date.date().isoformat() if high_date else None,
        "low_52w": row.get("price_52_week_low"),
        "drawdown_pct": round(dd, 2) if dd is not None else None,
        "drawdown_vs_6m_high_pct": round(dd_6m, 2) if dd_6m is not None else None,
        "market_cap": cap,
        "revenue_fy": rev,              # oldest -> newest
        "revenue_yoy_pct": [round(g, 2) if g is not None else None for g in yoy] if yoy else None,
        "ebitda_fy": ebitda,
        "ebitda_fy_hist": list(reversed(ebh[:4])) if ebh else None,
        "fy_end": datetime.fromtimestamp(fy_end, tz=timezone.utc).date().isoformat() if fy_end else None,
        "next_earnings": datetime.fromtimestamp(nxt, tz=timezone.utc).date().isoformat() if nxt else None,
        "perf": {k: row.get(f"Perf.{k}") for k in ("W", "1M", "3M", "6M", "Y")},
        "ladder": _ladder(row),
        "fragile": fragile,
        "status": status,
        "fails": [{"rule": r, "detail": d} for r, d, _ in fails],
        "tv_url": f"https://www.tradingview.com/symbols/ASX-{row['name']}/",
    }


def screen(rows, cfg, today):
    return [evaluate(r, cfg, today) for r in rows]


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="criteria.yaml")
    ap.add_argument("--out", default="docs/data")
    ap.add_argument("--from-file", help="re-screen a saved raw.json instead of fetching")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    out = Path(args.out)
    (out / "history").mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    rows = json.load(open(args.from_file)) if args.from_file else fetch_universe(cfg)
    if len(rows) < 100:
        sys.exit(f"Only {len(rows)} rows returned; refusing to overwrite dashboard data.")

    results = screen(rows, cfg, now)
    passes = [r for r in results if r["status"].startswith("Pass")]
    effective, loosened = cfg, False

    fb = cfg.get("fallback", {})
    if not passes and fb.get("enabled"):
        effective = copy.deepcopy(cfg)
        effective["drawdown"]["min_pct_below_high"] += fb["drawdown_delta_pct"]
        effective["ebitda"]["breakeven_threshold"] += fb["breakeven_threshold_delta"]
        results = screen(rows, effective, now)
        passes = [r for r in results if r["status"].startswith("Pass")]
        loosened = True

    passes.sort(key=lambda r: r["drawdown_pct"], reverse=True)
    borderline = sorted((r for r in results if r["status"] == "Borderline"),
                        key=lambda r: r["drawdown_pct"] or 0, reverse=True)
    in_band = [r for r in results
               if r["market_cap"] and cfg["market_cap"]["min"] <= r["market_cap"] <= cfg["market_cap"]["max"]]

    # Changes vs previous run
    prev_path = out / "latest.json"
    prev_tickers, prev_date = set(), None
    if prev_path.exists():
        prev = json.load(open(prev_path))
        prev_tickers = {m["ticker"] for m in prev.get("matches", [])}
        prev_date = prev.get("run_at")
    cur = {m["ticker"] for m in passes}
    for m in passes:
        m["is_new"] = bool(prev_date) and m["ticker"] not in prev_tickers

    doc = {
        "run_at": now.isoformat(timespec="seconds"),
        "source": "TradingView screener (scanner.tradingview.com), delayed/end-of-day",
        "criteria": {
            "market_cap_min": effective["market_cap"]["min"],
            "market_cap_max": effective["market_cap"]["max"],
            "drawdown_min_pct": effective["drawdown"]["min_pct_below_high"],
            "revenue_mode": effective["revenue_growth"]["mode"],
            "revenue_years": effective["revenue_growth"]["years"],
            "ebitda_threshold": effective["ebitda"]["breakeven_threshold"],
        },
        "loosened": loosened,
        "universe_count": len(in_band),
        "matches": passes,
        "borderline": borderline[:15],
        "changes": {
            "since": prev_date,
            "added": sorted(cur - prev_tickers) if prev_date else [],
            "removed": sorted(prev_tickers - cur) if prev_date else [],
        },
    }
    prev_path.write_text(json.dumps(doc, indent=1))
    slim = {"run_at": doc["run_at"], "loosened": loosened,
            "matches": [{k: m[k] for k in ("ticker", "price", "high_52w", "drawdown_pct", "market_cap", "status")}
                        for m in passes],
            "borderline": [b["ticker"] for b in borderline]}
    (out / "history" / f"{now.date().isoformat()}.json").write_text(json.dumps(slim))

    # Small index of past runs for a "history" strip
    idx_path = out / "runs.json"
    idx = json.load(open(idx_path)) if idx_path.exists() else []
    idx = [r for r in idx if r["date"] != now.date().isoformat()]
    idx.append({"date": now.date().isoformat(), "matches": sorted(cur), "loosened": loosened})
    idx_path.write_text(json.dumps(idx[-400:], indent=1))

    print(f"{len(passes)} match(es){' (LOOSENED)' if loosened else ''} from {len(in_band)} in band; "
          f"{len(borderline)} borderline.")
    for m in passes:
        print(f"  {m['ticker']:<5} {m['drawdown_pct']:5.1f}% below high  mcap A${m['market_cap']/1e9:.2f}bn"
              f"  EBITDA A${m['ebitda_fy']/1e6:,.0f}m  {m['status']}")


if __name__ == "__main__":
    main()
