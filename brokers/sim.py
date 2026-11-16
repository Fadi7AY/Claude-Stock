"""Built-in simulator: a fake-money account on your own computer, no sign-up or keys.

Fills at the live Yahoo price. Good for trying the app before linking a real platform.
"""
import json
import os
import threading

from .base import Broker, BrokerError, yahoo_price
from nytime import now_ny

_LOCK = threading.Lock()


class Sim(Broker):
    key = "sim"
    native_exits = False
    paper = True

    def __init__(self, creds, data_dir):
        self.start = float(creds.get("starting_cash") or 10000)
        self.path = os.path.join(data_dir, "sim_%s.json" % creds.get("_id", "default"))

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"cash": self.start, "pos": {}, "fills": []}

    def _save(self, s):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
        os.replace(self.path + ".tmp", self.path)

    def positions(self):
        out = []
        for sym, p in self._load()["pos"].items():
            try:
                px = yahoo_price(sym)
            except BrokerError:
                px = p["avg"]
            out.append({"symbol": sym, "qty": p["qty"], "avg_price": p["avg"], "price": px,
                        "pnl": round((px - p["avg"]) * p["qty"], 2)})
        return out

    def account(self):
        s = self._load()
        equity = s["cash"] + sum(p["qty"] * p["price"] for p in self.positions())
        return {"id": "Simulator", "cash": s["cash"], "equity": equity, "currency": "USD"}

    def price(self, symbol):
        return yahoo_price(symbol)

    def buy(self, symbol, qty, stop, target):
        px = yahoo_price(symbol)
        with _LOCK:
            s = self._load()
            if qty * px > s["cash"]:
                raise BrokerError("simulator: not enough cash")
            s["cash"] -= qty * px
            s["pos"][symbol] = {"qty": qty, "avg": px}
            s["fills"].append({"day": now_ny().date().isoformat(), "symbol": symbol, "side": "buy",
                               "qty": qty, "price": px})
            self._save(s)
        return {"id": "sim-%d" % len(s["fills"]), "price": px, "qty": qty}

    def close(self, symbol):
        with _LOCK:
            s = self._load()
            p = s["pos"].pop(symbol, None)
            if not p:
                return
            px = yahoo_price(symbol)
            s["cash"] += p["qty"] * px
            s["fills"].append({"day": now_ny().date().isoformat(), "symbol": symbol, "side": "sell",
                               "qty": p["qty"], "price": px})
            self._save(s)

    def fills(self, symbol, day):
        bq = bc = sq = sc = 0.0
        for f in self._load()["fills"]:
            if f["symbol"] != symbol or f["day"] < day:
                continue
            if f["side"] == "buy":
                bq, bc = bq + f["qty"], bc + f["qty"] * f["price"]
            else:
                sq, sc = sq + f["qty"], sc + f["qty"] * f["price"]
        return bq, bc, sq, sc
