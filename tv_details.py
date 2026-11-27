#!/usr/bin/env python3
"""
Company descriptions and recent headlines for the stocks on the dashboard.

- Description: TradingView's `business_description`, from one quote-session
  request covering every dashboard stock, trimmed to its first sentence.
- Headlines: TradingView's news feed, one GET per stock with a short pause.

Adds `about` and `news` to each stock in docs/data/latest.json.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests
import websocket  # websocket-client

from tv_bars import WS_URL, _frame, _rid, _split

NEWS_URL = "https://news-headlines.tradingview.com/v2/headlines"
HEADERS = {
    "User-Agent": "asx-screener-dashboard/1.0 (daily personal screen)",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
MAX_HEADLINES = 6
PAUSE = 0.6
MAX_ABOUT = 240


def short_about(text: str | None) -> str | None:
    """First sentence of the business description, capped at MAX_ABOUT chars."""
    if not text:
        return None
    text = " ".join(text.split())
    # Split on a full stop followed by a capitalised word, ignoring "Ltd." / "Inc." etc.
    m = re.search(r"(?<!\bLtd)(?<!\bInc)(?<!\bCo)(?<!\bCorp)(?<!\bPty)(?<!\bNo)\.\s+(?=[A-Z])", text)
    first = text[:m.start() + 1] if m else text
    if len(first) > MAX_ABOUT:
        first = first[:MAX_ABOUT].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first


def fetch_descriptions(symbols: list[str], timeout: float = 25) -> dict[str, str]:
    ws = websocket.create_connection(WS_URL, header=["Origin: https://www.tradingview.com"], timeout=timeout)
    qs = _rid("qs_")
    for m, p in [("set_auth_token", ["unauthorized_user_token"]),
                 ("quote_create_session", [qs]),
                 ("quote_set_fields", [qs, "business_description"]),
                 ("quote_add_symbols", [qs, *symbols])]:
        ws.send(_frame({"m": m, "p": p}))
    out, pending, t0 = {}, set(symbols), time.time()
    try:
        while pending and time.time() - t0 < timeout:
            for body in _split(ws.recv()):
                if body.startswith("~h~"):
                    ws.send(f"~m~{len(body)}~m~{body}")
                    continue
                try:
                    d = json.loads(body)
                except ValueError:
                    continue
                if d.get("m") == "qsd":
                    info = d["p"][1]
                    desc = (info.get("v") or {}).get("business_description")
                    if desc:
                        out[info["n"]] = desc
                        pending.discard(info["n"])
                    elif info.get("s") == "error":
                        pending.discard(info["n"])
    finally:
        ws.close()
    return out


def fetch_headlines(symbol: str) -> list[dict]:
    r = requests.get(NEWS_URL, params={"client": "web", "lang": "en", "symbol": symbol, "streaming": "false"},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    items = sorted(r.json().get("items", []), key=lambda i: i.get("published", 0), reverse=True)
    out = []
    for i in items[:MAX_HEADLINES]:
        path = i.get("storyPath")
        out.append({
            "title": i.get("title"),
            "source": i.get("source") or i.get("provider"),
            "published": i.get("published"),
            # Original article where the provider gives one (e.g. Market Index,
            # Smallcaps); otherwise TradingView's copy of the story (Reuters, Dow Jones).
            "url": i.get("link") or (f"https://www.tradingview.com{path}" if path else None),
        })
    return out


def main(data_dir="docs/data"):
    latest_p = Path(data_dir) / "latest.json"
    doc = json.load(open(latest_p))
    stocks = doc["matches"] + doc["borderline"]

    try:
        descs = fetch_descriptions([s["symbol"] for s in stocks])
    except Exception as e:
        print(f"Descriptions failed: {e}", file=sys.stderr)
        descs = {}

    news_ok = 0
    for s in stocks:
        s["about"] = short_about(descs.get(s["symbol"]))
        try:
            s["news"] = fetch_headlines(s["symbol"])
            news_ok += 1
        except Exception as e:
            print(f"  {s['ticker']}: headlines failed ({e})", file=sys.stderr)
            s["news"] = []
        time.sleep(PAUSE)

    latest_p.write_text(json.dumps(doc, indent=1))
    print(f"Descriptions for {sum(1 for s in stocks if s.get('about'))}/{len(stocks)}, "
          f"headlines for {news_ok}/{len(stocks)} stocks.")


if __name__ == "__main__":
    main(*sys.argv[1:])
