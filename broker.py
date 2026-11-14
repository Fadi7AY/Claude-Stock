"""Minimal Alpaca trading client (standard library only).

Keys come from the .env file next to this script (or real environment
variables). Paper trading unless ALPACA_PAPER=false AND the bot is started
with --live.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class BrokerError(Exception):
    pass


def load_env(path=os.path.join(HERE, ".env")):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


class Alpaca:
    def __init__(self, allow_live=False):
        load_env()
        self.key = os.environ.get("ALPACA_API_KEY", "")
        self.secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not self.key or not self.secret or "PASTE" in self.key.upper():
            raise BrokerError("No Alpaca keys found. Copy .env.example to .env and put your keys in it.")
        wants_live = os.environ.get("ALPACA_PAPER", "true").strip().lower() in ("false", "0", "no")
        if wants_live and not allow_live:
            raise BrokerError("ALPACA_PAPER=false but the bot was not started with --live. "
                              "Refusing to touch real money.")
        self.paper = not wants_live
        self.base = PAPER_URL if self.paper else LIVE_URL

    def _req(self, method, path, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403) and path == "/v2/account":
                detail = "keys rejected - paper keys only work with ALPACA_PAPER=true, live keys with false. " + detail
            raise BrokerError("Alpaca %s %s -> HTTP %s %s" % (method, path, e.code, detail))
        except urllib.error.URLError as e:
            raise BrokerError("Cannot reach Alpaca: %s" % e.reason)

    # ---- read
    def account(self):
        return self._req("GET", "/v2/account")

    def clock(self):
        return self._req("GET", "/v2/clock")

    def positions(self):
        return self._req("GET", "/v2/positions")

    def position(self, symbol):
        try:
            return self._req("GET", "/v2/positions/" + symbol)
        except BrokerError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def asset(self, symbol):
        return self._req("GET", "/v2/assets/" + symbol)

    def orders(self, status="open", symbols=None, after=None):
        p = {"status": status, "limit": 200, "nested": "false", "direction": "desc"}
        if symbols:
            p["symbols"] = ",".join(symbols)
        if after:
            p["after"] = after
        return self._req("GET", "/v2/orders", params=p)

    # ---- write
    def buy_bracket(self, symbol, qty, stop, target):
        """Open a long position with its stop-loss and take-profit attached."""
        return self._req("POST", "/v2/orders", body={
            "symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "market",
            "time_in_force": "day", "order_class": "bracket",
            "take_profit": {"limit_price": "%.2f" % float(target)},
            "stop_loss": {"stop_price": "%.2f" % float(stop)}})

    def cancel_orders(self, symbol):
        n = 0
        for o in self.orders("open", [symbol]):
            try:
                self._req("DELETE", "/v2/orders/" + o["id"])
                n += 1
            except BrokerError:
                pass
        return n

    def close(self, symbol):
        """Close a position: cancel its resting stop/target first, then sell what is held."""
        if self.cancel_orders(symbol):
            time.sleep(2)
        last = None
        for _ in range(3):
            if not self.position(symbol):
                return None
            try:
                return self._req("DELETE", "/v2/positions/" + symbol)
            except BrokerError as e:  # shares still held by an order being cancelled
                last = e
                time.sleep(2)
        raise last
