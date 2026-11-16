"""Alpaca (US stocks). Stop-loss and take-profit rest at Alpaca as a bracket order."""
import time

from .base import Broker, BrokerError, http_json

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class Alpaca(Broker):
    key = "alpaca"
    native_exits = True

    def __init__(self, creds):
        self.api_key = (creds.get("api_key") or "").strip()
        self.secret = (creds.get("secret_key") or "").strip()
        if not self.api_key or not self.secret:
            raise BrokerError("API key and secret key are both required")
        self.paper = bool(creds.get("paper", True))
        self.base = PAPER_URL if self.paper else LIVE_URL

    def _req(self, method, path, body=None, params=None):
        try:
            return http_json(method, self.base + path, body=body, params=params, headers={
                "APCA-API-KEY-ID": self.api_key, "APCA-API-SECRET-KEY": self.secret})
        except BrokerError as e:
            msg = str(e)
            if "HTTP 401" in msg or ("HTTP 403" in msg and path == "/v2/account"):
                msg += (" - keys rejected. Paper keys only work with 'Paper account' ticked, "
                        "live keys with it unticked.")
            raise BrokerError("Alpaca %s %s: %s" % (method, path, msg))

    def account(self):
        a = self._req("GET", "/v2/account")
        if a.get("trading_blocked") or a.get("account_blocked"):
            raise BrokerError("Alpaca reports this account is blocked for trading")
        # cash, not buying_power: buying power includes margin, which the bot never uses
        return {"id": a.get("account_number"), "cash": float(a["cash"]),
                "equity": float(a["equity"]), "currency": "USD"}

    def is_open(self):
        return bool(self._req("GET", "/v2/clock")["is_open"])

    @staticmethod
    def _pos(p):
        return {"symbol": p["symbol"], "qty": float(p["qty"]), "avg_price": float(p["avg_entry_price"]),
                "price": float(p["current_price"] or 0), "pnl": float(p["unrealized_pl"] or 0)}

    def positions(self):
        return [self._pos(p) for p in self._req("GET", "/v2/positions")]

    def position(self, symbol):
        try:
            return self._pos(self._req("GET", "/v2/positions/" + symbol))
        except BrokerError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def price(self, symbol):
        p = self.position(symbol)
        if not p:
            raise BrokerError("no position in %s" % symbol)
        return p["price"]

    def can_trade(self, symbol):
        try:
            a = self._req("GET", "/v2/assets/" + symbol)
        except BrokerError as e:
            return False, str(e)
        return (True, "") if a.get("tradable") else (False, "not tradable at Alpaca")

    def _orders(self, status, symbol, after=None):
        p = {"status": status, "limit": 200, "nested": "false", "direction": "desc", "symbols": symbol}
        if after:
            p["after"] = after
        return self._req("GET", "/v2/orders", params=p)

    def buy(self, symbol, qty, stop, target):
        o = self._req("POST", "/v2/orders", body={
            "symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "market",
            "time_in_force": "day", "order_class": "bracket",
            "take_profit": {"limit_price": "%.2f" % float(target)},
            "stop_loss": {"stop_price": "%.2f" % float(stop)}})
        return {"id": o.get("id"), "price": None, "qty": None}

    def close(self, symbol):
        cancelled = 0
        for o in self._orders("open", symbol):
            try:
                self._req("DELETE", "/v2/orders/" + o["id"])
                cancelled += 1
            except BrokerError:
                pass
        if cancelled:
            time.sleep(2)
        last = None
        for _ in range(3):
            if not self.position(symbol):
                return
            try:
                self._req("DELETE", "/v2/positions/" + symbol)
                return
            except BrokerError as e:  # shares still held by an order being cancelled
                last = e
                time.sleep(2)
        raise last

    def pending_buy(self, symbol):
        return any(o["side"] == "buy" for o in self._orders("open", symbol))

    def fills(self, symbol, day):
        bq = bc = sq = sc = 0.0
        for o in self._orders("closed", symbol, after=day + "T00:00:00Z"):
            q, p = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
            if q <= 0:
                continue
            if o["side"] == "buy":
                bq, bc = bq + q, bc + q * p
            else:
                sq, sc = sq + q, sc + q * p
        return bq, bc, sq, sc
