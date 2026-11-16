"""Stocks in Play: is a breakout happening on unusual volume?

From Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy For The U.S. Equity
Market" (2024, SSRN 4729284): an opening-range breakout only had an edge on stocks
whose opening-range volume was well above normal (relative volume >= 100%), with a
daily ATR worth trading (> $0.50), ranked by relative volume.

One free Yahoo request per stock: a month of 5-minute bars gives today's opening-range
volume, the same window on the previous 14 sessions, the daily ATR(14) and today's VWAP.
"""
import datetime as dt

from brokers.base import BrokerError, http_json
from nytime import utc_offset_hours

OPEN = dt.time(9, 30)
DAYS = 14


def _bars(symbol):
    sym = symbol.upper().replace(".", "-")
    last = None
    for host in ("query2", "query1"):
        try:
            d = http_json("GET", "https://%s.finance.yahoo.com/v8/finance/chart/%s"
                          "?range=1mo&interval=5m&includePrePost=false" % (host, sym), timeout=20)
            r = d["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            out = []
            for i, ts in enumerate(r["timestamp"]):
                if q["close"][i] is None:
                    continue
                utc = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=ts)
                et = utc + dt.timedelta(hours=utc_offset_hours(utc))
                out.append({"t": et, "h": q["high"][i], "l": q["low"][i], "c": q["close"][i],
                            "v": q["volume"][i] or 0})
            return out
        except (BrokerError, KeyError, TypeError, IndexError) as e:
            last = e
    raise BrokerError("no 5-minute history for %s (%s)" % (symbol, last))


def measure(symbol, now, or_minutes=15):
    """{'rvol', 'atr', 'vwap', 'last'} for `symbol` at New York time `now` (bars completed by then)."""
    days = {}
    for b in _bars(symbol):
        if b["t"].time() >= OPEN and b["t"] + dt.timedelta(minutes=5) <= max(now, b["t"]):
            days.setdefault(b["t"].date(), []).append(b)
    today = days.pop(now.date(), [])
    today = [b for b in today if b["t"] + dt.timedelta(minutes=5) <= now]
    # whole sessions only: a day cut off at the start of the 1-month window would drag the average down
    prior = [days[d] for d in sorted(d for d in days if d < now.date()) if days[d][0]["t"].time() == OPEN][-DAYS:]
    if not today or len(prior) < 5:
        raise BrokerError("not enough history for %s" % symbol)

    end = dt.datetime.combine(now.date(), OPEN) + dt.timedelta(minutes=or_minutes)

    def window_vol(bars):
        return sum(b["v"] for b in bars if b["t"].time() < end.time())

    avg = sum(window_vol(d) for d in prior) / len(prior)
    rvol = window_vol(today) / avg if avg else 0.0

    trs, prev_close = [], None
    for d in prior:
        h = max(b["h"] for b in d if b["h"] is not None)
        lo = min(b["l"] for b in d if b["l"] is not None)
        trs.append(h - lo if prev_close is None else max(h - lo, abs(h - prev_close), abs(lo - prev_close)))
        prev_close = d[-1]["c"]
    atr = sum(trs) / len(trs)

    pv = sum((b["h"] + b["l"] + b["c"]) / 3.0 * b["v"] for b in today if b["h"] is not None)
    v = sum(b["v"] for b in today if b["h"] is not None)
    vwap = pv / v if v else today[-1]["c"]
    return {"rvol": round(rvol, 2), "atr": round(atr, 2), "vwap": round(vwap, 2),
            "last": round(today[-1]["c"], 2)}


def filter_hits(hits, cfg, now, log):
    """Keep breakouts on Stocks in Play, most unusual volume first. Adds h['sip']."""
    sc = cfg["strategy"].get("stocks_in_play", {})
    if not sc.get("enabled", True):
        return hits
    or_min = cfg["strategy"].get("or_minutes", 15)
    keep = []
    for h in hits:
        try:
            m = measure(h["symbol"], now, or_min)
        except BrokerError as e:
            log("SKIP %s: stocks-in-play check failed (%s)" % (h["symbol"], e))
            continue
        why = []
        if m["rvol"] < sc.get("min_rvol", 1.0):
            why.append("relative volume %.2fx < %.2fx" % (m["rvol"], sc.get("min_rvol", 1.0)))
        if m["atr"] < sc.get("min_atr_usd", 0.5):
            why.append("daily ATR $%.2f < $%.2f" % (m["atr"], sc.get("min_atr_usd", 0.5)))
        if sc.get("require_above_vwap", True) and h["price"] <= m["vwap"]:
            why.append("price %.2f not above VWAP %.2f" % (h["price"], m["vwap"]))
        if why:
            log("SKIP %s: not in play (%s)" % (h["symbol"], "; ".join(why)))
            continue
        h["sip"] = m
        keep.append(h)
    keep.sort(key=lambda x: -x["sip"]["rvol"])
    return keep
