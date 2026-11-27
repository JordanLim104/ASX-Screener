#!/usr/bin/env python3
"""
Price history for the dashboard's chart ranges (1D, 1W, 1M, 3M, 6M, 1Y, 5Y).

Uses TradingView's chart websocket (the feed behind its public charts, unofficial,
no login: data is delayed). One connection per run, three series per ticker
(5-minute, hourly, daily), only for the tickers on the dashboard (~35), with a
short pause between tickers. Writes docs/data/bars/{TICKER}.json and adds a
1-year weekly "spark" array to each stock in latest.json.

Bars are deployed with the site rather than committed, so the repo stays small.
"""

from __future__ import annotations

import json
import random
import re
import string
import sys
import time
from pathlib import Path

import websocket  # websocket-client

WS_URL = "wss://data.tradingview.com/socket.io/websocket?type=chart"
SERIES = [("i5", "5", 160), ("h1", "60", 170), ("d1", "1D", 1300)]
PAUSE = 0.6          # seconds between tickers
TIMEOUT = 20         # seconds per series


def _rid(prefix):
    return prefix + "".join(random.choices(string.ascii_lowercase, k=12))


def _frame(obj):
    s = json.dumps(obj, separators=(",", ":"))
    return f"~m~{len(s)}~m~{s}"


def _split(raw):
    """Split a socket.io-style payload of ~m~<len>~m~<body> chunks."""
    out, i = [], 0
    while raw.startswith("~m~", i):
        j = raw.index("~m~", i + 3)
        n = int(raw[i + 3:j])
        out.append(raw[j + 3:j + 3 + n])
        i = j + 3 + n
    return out


class Feed:
    def __init__(self):
        self.ws = websocket.create_connection(
            WS_URL, header=["Origin: https://www.tradingview.com"], timeout=TIMEOUT)
        self.cs = _rid("cs_")
        self.n = 0
        self._send("set_auth_token", ["unauthorized_user_token"])
        self._send("chart_create_session", [self.cs, ""])

    def _send(self, m, p):
        self.ws.send(_frame({"m": m, "p": p}))

    def bars(self, symbol, resolution, count):
        self.n += 1
        sym_id, ser_id = f"sym_{self.n}", f"s_{self.n}"
        self._send("resolve_symbol", [self.cs, sym_id,
                   "=" + json.dumps({"symbol": symbol, "adjustment": "splits", "session": "regular"})])
        self._send("create_series", [self.cs, ser_id, f"s{self.n}", sym_id, resolution, count, ""])
        t0 = time.time()
        while time.time() - t0 < TIMEOUT:
            for body in _split(self.ws.recv()):
                if body.startswith("~h~"):
                    self.ws.send(f"~m~{len(body)}~m~{body}")   # heartbeat
                    continue
                try:
                    d = json.loads(body)
                except ValueError:
                    continue
                m = d.get("m")
                if m == "timescale_update" and ser_id in d["p"][1]:
                    rows = d["p"][1][ser_id].get("s", [])
                    self._send("remove_series", [self.cs, ser_id])
                    return [(int(r["v"][0]), r["v"][4]) for r in rows]
                if m in ("symbol_error", "series_error"):
                    return []
                if m in ("critical_error", "protocol_error"):
                    raise RuntimeError(f"TradingView {m}: {d['p']}")
        return []

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _last_session(rows):
    """5-minute bars of the most recent trading day only."""
    if not rows:
        return []
    last_day = time.strftime("%Y-%m-%d", time.gmtime(rows[-1][0] + 10 * 3600))
    return [r for r in rows if time.strftime("%Y-%m-%d", time.gmtime(r[0] + 10 * 3600)) == last_day]


def _weekly(rows):
    """Downsample daily rows to the last close of each ISO week."""
    out, last_key = [], None
    for t, c in rows:
        key = time.strftime("%G-%V", time.gmtime(t))
        if key == last_key:
            out[-1] = (t, c)
        else:
            out.append((t, c))
            last_key = key
    return out


def main(data_dir="docs/data"):
    data_dir = Path(data_dir)
    latest_p = data_dir / "latest.json"
    doc = json.load(open(latest_p))
    stocks = doc["matches"] + doc["borderline"]
    bars_dir = data_dir / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)

    feed, ok = Feed(), 0
    try:
        for s in stocks:
            sym = s["symbol"]
            try:
                got = {k: feed.bars(sym, res, n) for k, res, n in SERIES}
            except (websocket.WebSocketException, OSError) as e:
                print(f"  {s['ticker']}: connection dropped ({e}); reconnecting", file=sys.stderr)
                feed.close(); time.sleep(5); feed = Feed()
                continue
            daily = got["d1"]
            if not daily:
                print(f"  {s['ticker']}: no bars", file=sys.stderr)
                continue
            cut_1y = daily[-1][0] - 366 * 86400
            intraday = _last_session(got["i5"])
            day = lambda t: time.strftime("%Y-%m-%d", time.gmtime(t + 10 * 3600))
            prev_close = daily[-1][1]
            if intraday and day(daily[-1][0]) == day(intraday[-1][0]):
                prev_close = daily[-2][1] if len(daily) > 1 else None
                # 5-minute bars stop before the closing auction; add the official close.
                if intraday[-1][1] != daily[-1][1]:
                    intraday.append((intraday[-1][0] + 900, daily[-1][1]))
            out = {
                "prev_close": prev_close,
                "intraday": intraday,
                "hourly": got["h1"],
                "daily": [r for r in daily if r[0] >= cut_1y - 5 * 86400],   # 1Y of daily
                "weekly": _weekly(daily),                                     # ~5Y weekly
            }
            (bars_dir / f"{s['ticker']}.json").write_text(json.dumps(out, separators=(",", ":")))
            s["spark"] = [round(c, 4) for _, c in _weekly([r for r in daily if r[0] >= cut_1y])]
            s["has_bars"] = True
            ok += 1
            time.sleep(PAUSE)
    finally:
        feed.close()

    latest_p.write_text(json.dumps(doc, indent=1))
    print(f"Price history saved for {ok}/{len(stocks)} stocks.")


if __name__ == "__main__":
    main(*sys.argv[1:])
