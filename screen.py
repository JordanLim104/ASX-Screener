#!/usr/bin/env python3
"""
ASX turnaround screener.

Screens a universe of ASX tickers against four criteria defined in criteria.yaml:

  1. Market cap inside a band            (default A$2bn - A$20bn)
  2. Price down at least N% from its 52-week high   (default 50%)
  3. Positive year-on-year revenue growth over the last 3 years
  4. EBITDA "break-even to profitable": latest EBITDA positive, having been
     at/below break-even within the prior couple of years (i.e. a turnaround).

Outputs (written to the directory named in criteria.yaml -> output.dir):
  results.json   structured results, for storage / week-on-week dedup downstream
  email.html     formatted HTML the routine can send via an email connector
  stdout         a concise human-readable summary (shows up in the routine session log)

Data provider is pluggable. Default is yfinance (free, no key, UNofficial - fine for a
personal weekly screen but can break or rate-limit). For reliable ASX coverage set
data.provider: eodhd in criteria.yaml and provide an EODHD_API_KEY environment variable.

Universe tickers in universe.txt are BASE symbols with no suffix (e.g. CSL, BHP).
The correct exchange suffix is appended per provider (.AX for yfinance, .AU for EODHD).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


# --------------------------------------------------------------------------- #
# Config + universe loading
# --------------------------------------------------------------------------- #

def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_universe(path: str) -> list[str]:
    tickers = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tickers.append(line.upper())
    return tickers


# --------------------------------------------------------------------------- #
# Data providers -> normalised metrics dict
#
# Each provider returns:
#   {
#     "base": "CSL", "display": "CSL.AX", "name": "...",
#     "market_cap": float|None, "price": float|None, "high_52w": float|None,
#     "revenue_history": [(year, value), ...]  # ascending by year
#     "ebitda_history":  [(year, value), ...]  # ascending by year
#   }
# Values are in the company's reporting currency (AUD for ASX names).
# --------------------------------------------------------------------------- #

def _sorted_history(pairs: list[tuple[int, float]]) -> list[tuple[int, float]]:
    pairs = [(y, v) for (y, v) in pairs if v is not None]
    pairs.sort(key=lambda p: p[0])
    return pairs


def get_metrics_yfinance(base: str) -> dict:
    import yfinance as yf  # imported lazily so EODHD users don't need it

    display = f"{base}.AX"
    t = yf.Ticker(display)

    def fast(*keys):
        fi = getattr(t, "fast_info", None)
        if fi is None:
            return None
        for k in keys:
            val = None
            try:
                val = fi[k]
            except Exception:
                try:
                    val = getattr(fi, k)
                except Exception:
                    val = None
            if val:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return None

    price = fast("lastPrice", "last_price", "regularMarketPrice")
    high = fast("yearHigh", "year_high", "fiftyTwoWeekHigh")
    mcap = fast("marketCap", "market_cap")
    name = base
    sector = None

    # The .info endpoint is the only source of company name + sector (and a
    # fallback for any price/cap fields fast_info couldn't supply), so always
    # fetch it. It's slower, but a weekly personal screen can afford it.
    info = {}
    try:
        info = t.info or {}
    except Exception:
        info = {}
    price = price or info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    high = high or info.get("fiftyTwoWeekHigh")
    mcap = mcap or info.get("marketCap")
    name = info.get("shortName") or info.get("longName") or base
    sector = info.get("sector") or None

    # Annual income statement for revenue + EBITDA history.
    try:
        stmt = t.income_stmt
    except Exception:
        stmt = None

    def row_history(*labels):
        if stmt is None or getattr(stmt, "empty", True):
            return []
        for label in labels:
            if label in stmt.index:
                series = stmt.loc[label].dropna()
                out = []
                for col, val in series.items():
                    try:
                        year = col.year
                    except AttributeError:
                        year = int(str(col)[:4])
                    out.append((year, float(val)))
                return _sorted_history(out)
        return []

    return {
        "base": base,
        "display": display,
        "name": name,
        "sector": sector,
        "market_cap": _f(mcap),
        "price": _f(price),
        "high_52w": _f(high),
        "revenue_history": row_history("Total Revenue", "TotalRevenue", "Operating Revenue"),
        "ebitda_history": row_history("EBITDA", "Normalized EBITDA"),
    }


def get_metrics_eodhd(base: str, api_key: str) -> dict:
    import requests  # imported lazily

    display = f"{base}.AU"
    fund = requests.get(
        f"https://eodhd.com/api/fundamentals/{display}",
        params={"api_token": api_key, "fmt": "json"},
        timeout=30,
    ).json()

    highlights = fund.get("Highlights", {}) or {}
    technicals = fund.get("Technicals", {}) or {}
    general = fund.get("General", {}) or {}

    mcap = highlights.get("MarketCapitalization")
    high = technicals.get("52WeekHigh")
    name = general.get("Name", base)
    sector = general.get("Sector") or None

    rt = requests.get(
        f"https://eodhd.com/api/real-time/{display}",
        params={"api_token": api_key, "fmt": "json"},
        timeout=30,
    ).json()
    price = rt.get("close")

    yearly = (fund.get("Financials", {}) or {}).get("Income_Statement", {}).get("yearly", {}) or {}
    rev, ebitda = [], []
    for date, rowdata in yearly.items():
        try:
            year = int(str(date)[:4])
        except (TypeError, ValueError):
            continue
        tr = _f(rowdata.get("totalRevenue"))
        eb = _f(rowdata.get("ebitda"))
        if tr is not None:
            rev.append((year, tr))
        if eb is not None:
            ebitda.append((year, eb))

    return {
        "base": base,
        "display": display,
        "name": name,
        "sector": sector,
        "market_cap": _f(mcap),
        "price": _f(price),
        "high_52w": _f(high),
        "revenue_history": _sorted_history(rev),
        "ebitda_history": _sorted_history(ebitda),
    }


def _f(value):
    """Coerce to float, tolerating strings and EODHD's 'None' sentinels."""
    if value is None or value == "None" or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Screening logic
# --------------------------------------------------------------------------- #

def evaluate(m: dict, cfg: dict) -> tuple[bool, list[str], dict]:
    """Return (passed, fail_reasons, derived_metrics)."""
    reasons: list[str] = []
    d: dict = {}

    # --- 1. Market cap band -------------------------------------------------
    mc = cfg["market_cap"]
    cap = m["market_cap"]
    if cap is None:
        reasons.append("missing market cap")
    else:
        d["market_cap"] = cap
        if cap < mc["min"]:
            reasons.append(f"market cap {cap/1e9:.2f}bn below min")
        if cap > mc["max"]:
            reasons.append(f"market cap {cap/1e9:.2f}bn above max")

    # --- 2. Drawdown from 52-week high -------------------------------------
    dd_cfg = cfg["drawdown"]
    price, high = m["price"], m["high_52w"]
    if price is None or high is None or high <= 0:
        reasons.append("missing price / 52w high")
    else:
        drawdown_pct = (high - price) / high * 100.0
        d["drawdown_pct"] = drawdown_pct
        d["price"] = price
        d["high_52w"] = high
        if drawdown_pct < dd_cfg["min_pct_below_high"]:
            reasons.append(f"only {drawdown_pct:.0f}% below high")

    # --- 3. Revenue growth over last N years -------------------------------
    rg = cfg["revenue_growth"]
    years = rg["years"]
    rev = m["revenue_history"]
    if len(rev) < years + 1:
        reasons.append(f"need {years + 1} yrs revenue, have {len(rev)}")
    else:
        window = rev[-(years + 1):]
        yoy = []
        ok = True
        for prev, cur in zip(window, window[1:]):
            base_val = prev[1]
            if base_val == 0:
                ok = False
                yoy.append(None)
                continue
            g = (cur[1] - base_val) / abs(base_val) * 100.0
            yoy.append(g)
        d["revenue_yoy_pct"] = yoy
        first, last = window[0][1], window[-1][1]
        d["revenue_latest"] = last
        if first > 0:
            d["revenue_cagr_pct"] = ((last / first) ** (1 / years) - 1) * 100.0
        if rg["mode"] == "each_year":
            for g in yoy:
                if g is None or g < rg["min_yoy_pct"]:
                    ok = False
            if not ok:
                reasons.append("revenue not growing every year")
        else:  # "net": net growth over the whole period
            net = ((last - first) / abs(first) * 100.0) if first else None
            d["revenue_net_pct"] = net
            if net is None or net < rg["min_yoy_pct"]:
                reasons.append("net revenue growth below threshold")

    # --- 4. EBITDA break-even -> profitable --------------------------------
    eb_cfg = cfg["ebitda"]
    eb = m["ebitda_history"]
    if not eb:
        reasons.append("missing EBITDA history")
    else:
        latest = eb[-1][1]
        d["ebitda_latest"] = latest
        thr = eb_cfg["breakeven_threshold"]
        if eb_cfg["require_latest_positive"] and latest <= thr:
            reasons.append("latest EBITDA not positive")
        if eb_cfg["require_turnaround"]:
            lookback = eb_cfg["turnaround_lookback"]
            prior = [v for (_, v) in eb[-(lookback + 1):-1]]
            d["ebitda_prior"] = prior
            if not any(v <= thr for v in prior):
                reasons.append("no recent break-even/loss (not a turnaround)")

    return (len(reasons) == 0, reasons, d)


# --------------------------------------------------------------------------- #
# Output formatting
# --------------------------------------------------------------------------- #

def fmt_money(v):
    if v is None:
        return "-"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    return f"{sign}${a:,.2f}"


def fmt_pct(v, signed=False):
    if v is None:
        return "-"
    return (f"{v:+.1f}%" if signed else f"{v:.1f}%")


def rank_key(cfg):
    by = cfg["ranking"]["by"]
    return {
        "drawdown_pct": lambda r: r["derived"].get("drawdown_pct", -1),
        "revenue_cagr": lambda r: r["derived"].get("revenue_cagr_pct", -1),
        "market_cap": lambda r: r["derived"].get("market_cap", -1),
    }.get(by, lambda r: r["derived"].get("drawdown_pct", -1))


def _ebitda_label(cfg: dict) -> str:
    thr = cfg["ebitda"]["breakeven_threshold"]
    if thr == 0:
        return "EBITDA &gt; 0 (latest yr)"
    return f"EBITDA &gt; {fmt_money(thr)} (latest yr)"


def build_html(passers: list[dict], cfg: dict, run_date: str, loosened: bool) -> str:
    rows = []
    for r in passers:
        d = r["derived"]
        yoy = d.get("revenue_yoy_pct") or []
        yoy_str = " / ".join(fmt_pct(g, signed=True) for g in yoy) if yoy else "-"
        rows.append(f"""
        <tr>
          <td style="font-weight:600">{r['display']}</td>
          <td>{r['name']}</td>
          <td>{r.get('sector') or '-'}</td>
          <td style="text-align:right">{fmt_money(d.get('market_cap'))}</td>
          <td style="text-align:right;color:#b00020">{fmt_pct(d.get('drawdown_pct'))}</td>
          <td style="text-align:right">{yoy_str}</td>
          <td style="text-align:right">{fmt_money(d.get('ebitda_latest'))}</td>
        </tr>""")
    body = "".join(rows) if rows else (
        '<tr><td colspan="7" style="padding:16px;color:#666">'
        'No stocks matched, even after loosening.</td></tr>'
    )

    # Banner shown only when the base criteria returned nothing and the fallback ran.
    loosened_banner = ""
    if loosened:
        loosened_banner = (
            '<p style="background:#fff4e5;border:1px solid #ffd8a8;border-radius:6px;'
            'padding:10px 12px;font-size:13px;color:#8a5a00;margin:0 0 12px">'
            '&#9888; No stocks matched the base criteria, so these were loosened for this run. '
            f'Values used are shown below: drawdown &ge;{cfg["drawdown"]["min_pct_below_high"]:.0f}% '
            f'and {_ebitda_label(cfg).replace("&gt;", ">")}.'
            '</p>'
        )

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a;max-width:760px;margin:0 auto">
  <h2 style="margin-bottom:4px">Weekly ASX Screen &mdash; {run_date}</h2>
  {loosened_banner}
  <p style="color:#666;margin-top:0;font-size:13px">
    <strong>Criteria used this run:</strong>
    market cap {fmt_money(cfg['market_cap']['min'])}&ndash;{fmt_money(cfg['market_cap']['max'])} &middot;
    &ge;{cfg['drawdown']['min_pct_below_high']:.0f}% below 52wk high &middot;
    revenue growth ({cfg['revenue_growth']['mode']}, {cfg['revenue_growth']['years']}yr) &middot;
    {_ebitda_label(cfg)} &middot; {len(passers)} match(es)
  </p>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <thead><tr style="background:#f4f4f5;text-align:left">
      <th style="padding:8px">Ticker</th><th style="padding:8px">Name</th>
      <th style="padding:8px">Sector</th>
      <th style="padding:8px;text-align:right">Mkt Cap</th>
      <th style="padding:8px;text-align:right">Below High</th>
      <th style="padding:8px;text-align:right">Rev YoY (oldest&rarr;newest)</th>
      <th style="padding:8px;text-align:right">EBITDA (latest)</th>
    </tr></thead>
    <tbody>{body}</tbody>
  </table>
  <p style="color:#999;font-size:11px;margin-top:16px">
    Mechanical filter on delayed/end-of-day data. A shortlist for review, not investment advice.
  </p>
</body></html>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="ASX turnaround screener")
    ap.add_argument("--config", default="criteria.yaml")
    ap.add_argument("--universe", default=None, help="override universe file from config")
    args = ap.parse_args()

    cfg = load_config(args.config)
    universe_file = args.universe or cfg.get("universe_file", "universe.txt")
    tickers = load_universe(universe_file)

    provider = cfg["data"]["provider"]
    delay = cfg["data"].get("request_delay_sec", 0.5)
    eodhd_key = os.environ.get("EODHD_API_KEY")
    if provider == "eodhd" and not eodhd_key:
        sys.exit("ERROR: data.provider is 'eodhd' but EODHD_API_KEY is not set.")

    print(f"Screening {len(tickers)} ASX tickers via {provider} ...\n")

    # --- Fetch once, cache the metrics so the fallback pass costs no extra calls ---
    fetched, skipped = [], []
    for i, base in enumerate(tickers, 1):
        try:
            if provider == "eodhd":
                m = get_metrics_eodhd(base, eodhd_key)
            else:
                m = get_metrics_yfinance(base)
            fetched.append(m)
        except Exception as e:  # never let one bad ticker kill the run
            skipped.append({"ticker": base, "reasons": [f"fetch error: {e}"]})
        time.sleep(delay)

    def run_pass(active_cfg):
        out = []
        for m in fetched:
            passed, _reasons, derived = evaluate(m, active_cfg)
            if passed:
                out.append({
                    "display": m["display"],
                    "name": m["name"],
                    "sector": m.get("sector"),
                    "derived": derived,
                })
        return out

    # --- Base pass ---
    passers = run_pass(cfg)
    effective = cfg
    loosened = False

    # --- Fallback: if nothing matched, loosen the two named knobs and re-evaluate ---
    fb = cfg.get("fallback", {})
    if not passers and fb.get("enabled"):
        effective = copy.deepcopy(cfg)
        effective["drawdown"]["min_pct_below_high"] += fb.get("drawdown_delta_pct", 0)
        effective["ebitda"]["breakeven_threshold"] += fb.get("breakeven_threshold_delta", 0)
        passers = run_pass(effective)
        loosened = True
        print(f"\nNo base matches; re-ran loosened: "
              f">={effective['drawdown']['min_pct_below_high']:.0f}% below high, "
              f"EBITDA > {fmt_money(effective['ebitda']['breakeven_threshold'])}")

    passers.sort(key=rank_key(effective), reverse=effective["ranking"]["descending"])
    passers = passers[: effective["output"]["top_n"]]

    run_date = datetime.now(timezone.utc).strftime("%d %b %Y")
    out_dir = Path(cfg["output"]["dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "results.json").write_text(json.dumps({
        "run_date": run_date,
        "loosened": loosened,
        "criteria_used": effective,
        "matches": passers,
        "screened": len(tickers),
        "fetched": len(fetched),
        "skipped_count": len(skipped),
    }, indent=2, default=str))

    (out_dir / "email.html").write_text(build_html(passers, effective, run_date, loosened))

    tag = " (LOOSENED)" if loosened else ""
    print(f"\n{'='*52}\n{len(passers)} match(es){tag} of {len(tickers)} screened.")
    for r in passers:
        d = r["derived"]
        print(f"  {r['display']:<10} {fmt_pct(d.get('drawdown_pct'))} below high | "
              f"mcap {fmt_money(d.get('market_cap'))} | EBITDA {fmt_money(d.get('ebitda_latest'))}")
    print(f"\nWrote {out_dir/'results.json'} and {out_dir/'email.html'}")


if __name__ == "__main__":
    main()
