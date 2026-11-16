"""MEXC spot - tokenized US stocks only (xStocks "AAPLX", Ondo "AAPLON"), paid in USDT.

Halal guard rails: this adapter only ever calls SPOT endpoints. It cannot open
futures, margin or leveraged positions, and it only trades tokens that map to
a stock ticker which the halal screen has already passed.

MEXC has no stop-loss order on spot, so the bot watches the price itself and
sells when the stop or target is reached - the app must stay running while a
position is open. MEXC has no paper account: orders here are real money.
"""
import hashlib
import hmac
import math
import time
import urllib.parse

from .base import Broker, BrokerError, http_json, yahoo_price
from nytime import ny_day_start_utc_ms

BASE = "https://api.mexc.com"
SUFFIXES = ("X", "ON")       # xStocks, Ondo
MAX_SPREAD_PCT = 0.6         # skip illiquid tokens
MAX_DRIFT_PCT = 6.0          # token must trade near the real share price
SLIP_PCT = 0.3               # a buy may pay at most this much above the ask


class Mexc(Broker):
    key = "mexc"
    native_exits = False
    paper = False

    def __init__(self, creds):
        self.api_key = (creds.get("api_key") or "").strip()
        self.secret = (creds.get("secret_key") or "").strip()
        if not self.api_key or not self.secret:
            raise BrokerError("API key and secret key are both required")
        self._info = None
        self._info_at = 0
        self._pair = {}      # ticker -> chosen pair, e.g. AAPL -> AAPLXUSDT

    # ---- plumbing
    def _public(self, path, params=None):
        try:
            return http_json("GET", BASE + path, params=params)
        except BrokerError as e:
            raise BrokerError("MEXC %s: %s" % (path, e))

    def _signed(self, method, path, params=None):
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 20000
        query = urllib.parse.urlencode(p)
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            return http_json(method, "%s%s?%s&signature=%s" % (BASE, path, query, sig),
                             headers={"X-MEXC-APIKEY": self.api_key})
        except BrokerError as e:
            raise BrokerError("MEXC %s %s: %s" % (method, path, e))

    def _symbols(self):
        if not self._info or time.time() - self._info_at > 6 * 3600:
            data = self._public("/api/v3/exchangeInfo")
            self._info = {s["symbol"]: s for s in data.get("symbols", [])
                          if s.get("quoteAsset") == "USDT" and str(s.get("status")) == "1"
                          and s.get("isSpotTradingAllowed")}
            self._info_at = time.time()
        return self._info

    def _book(self, pair):
        b = self._public("/api/v3/ticker/bookTicker", {"symbol": pair})
        bid, ask = float(b.get("bidPrice") or 0), float(b.get("askPrice") or 0)
        if bid <= 0 or ask <= 0:
            raise BrokerError("MEXC: no order book for %s" % pair)
        return bid, ask

    def _find_pair(self, ticker):
        """Pick the token for a stock ticker: must exist, be liquid, and track the share price."""
        if ticker in self._pair:
            return self._pair[ticker], ""
        base = ticker.replace(".", "")
        cands = [base + s + "USDT" for s in SUFFIXES if base + s + "USDT" in self._symbols()]
        if not cands:
            return None, "MEXC lists no tokenized %s" % ticker
        ref = yahoo_price(ticker)
        best, why = None, ""
        for pair in cands:
            try:
                bid, ask = self._book(pair)
            except BrokerError as e:
                why = str(e)
                continue
            mid = (bid + ask) / 2
            spread = 100 * (ask - bid) / mid
            drift = 100 * abs(mid - ref) / ref
            if drift > MAX_DRIFT_PCT:
                why = "%s trades %.1f%% away from the real share price" % (pair, drift)
            elif spread > MAX_SPREAD_PCT:
                why = "%s spread is %.2f%% (too illiquid)" % (pair, spread)
            elif best is None or spread < best[1]:
                best = (pair, spread)
        if not best:
            return None, why
        self._pair[ticker] = best[0]
        return best[0], ""

    def _pair_or_raise(self, ticker):
        pair, why = self._find_pair(ticker)
        if not pair:
            raise BrokerError(why)
        return pair

    def _balances(self):
        return {b["asset"]: (float(b["free"]), float(b["locked"]))
                for b in self._signed("GET", "/api/v3/account").get("balances", [])}

    def _ticker_of(self, asset):
        for s in SUFFIXES:
            if asset.endswith(s) and len(asset) > len(s) and asset + "USDT" in self._symbols():
                name = (self._symbols()[asset + "USDT"].get("fullName") or "").lower()
                if "xstock" in name or "ondo" in name or asset[:-len(s)] in self._pair:
                    return asset[:-len(s)]
        return None

    @staticmethod
    def _floor(x, places):
        m = 10 ** places
        return math.floor(x * m + 1e-9) / m

    # ---- Broker interface
    def account(self):
        bal = self._balances()
        cash = bal.get("USDT", (0.0, 0.0))[0]
        equity = sum(bal.get("USDT", (0.0, 0.0)))
        for p in self._positions(bal):
            equity += p["qty"] * p["price"]
        return {"id": "MEXC spot", "cash": cash, "equity": equity, "currency": "USDT"}

    def _positions(self, bal):
        out = []
        for asset, (free, locked) in bal.items():
            qty = free + locked
            ticker = self._ticker_of(asset) if qty > 0 else None
            if not ticker:
                continue
            try:
                bid, _ = self._book(asset + "USDT")
            except BrokerError:
                continue
            if qty * bid < 1:      # dust below MEXC's 1 USDT minimum order
                continue
            self._pair.setdefault(ticker, asset + "USDT")
            out.append({"symbol": ticker, "qty": qty, "avg_price": None, "price": bid, "pnl": None})
        return out

    def positions(self):
        return self._positions(self._balances())

    def price(self, symbol):
        return self._book(self._pair_or_raise(symbol))[0]

    def can_trade(self, symbol):
        try:
            pair, why = self._find_pair(symbol)
        except BrokerError as e:
            return False, str(e)
        return (True, "") if pair else (False, why)

    def _wait_filled(self, pair, order_id, seconds=6):
        o = {}
        for _ in range(seconds):
            time.sleep(1)
            o = self._signed("GET", "/api/v3/order", {"symbol": pair, "orderId": order_id})
            if o.get("status") in ("FILLED", "CANCELED", "PARTIALLY_CANCELED"):
                return o
        try:
            self._signed("DELETE", "/api/v3/order", {"symbol": pair, "orderId": order_id})
        except BrokerError:
            pass
        time.sleep(1)
        return self._signed("GET", "/api/v3/order", {"symbol": pair, "orderId": order_id})

    def buy(self, symbol, qty, stop, target):
        pair = self._pair_or_raise(symbol)
        bid, ask = self._book(pair)
        limit = round(ask * (1 + SLIP_PCT / 100), 2)   # marketable limit: bounded slippage
        o = self._signed("POST", "/api/v3/order", {
            "symbol": pair, "side": "BUY", "type": "LIMIT",
            "quantity": "%d" % int(qty), "price": "%.2f" % limit})
        done = self._wait_filled(pair, o["orderId"])
        got, paid = float(done.get("executedQty") or 0), float(done.get("cummulativeQuoteQty") or 0)
        if got <= 0:
            raise BrokerError("MEXC: buy of %s did not fill within the price limit" % pair)
        return {"id": str(o["orderId"]), "price": paid / got, "qty": got}

    def close(self, symbol):
        pair = self._pair_or_raise(symbol)
        asset = pair[:-4]
        try:
            self._signed("DELETE", "/api/v3/openOrders", {"symbol": pair})
        except BrokerError:
            pass                       # nothing open
        places = int(self._symbols().get(pair, {}).get("baseAssetPrecision", 3))
        for _ in range(4):
            free = self._balances().get(asset, (0.0, 0.0))[0]
            qty = self._floor(free, places)
            bid, _ = self._book(pair)
            if qty <= 0 or qty * bid < 1:
                return
            o = self._signed("POST", "/api/v3/order", {
                "symbol": pair, "side": "SELL", "type": "MARKET", "quantity": ("%%.%df" % places) % qty})
            self._wait_filled(pair, o["orderId"], seconds=4)
        raise BrokerError("MEXC: could not fully sell %s - check the position on MEXC" % pair)

    def fills(self, symbol, day):
        pair = self._pair_or_raise(symbol)
        bq = bc = sq = sc = 0.0
        for t in self._signed("GET", "/api/v3/myTrades",
                              {"symbol": pair, "startTime": ny_day_start_utc_ms(day), "limit": 100}):
            q, c = float(t["qty"]), float(t["quoteQty"])
            fee = float(t.get("commission") or 0) if t.get("commissionAsset") == "USDT" else 0.0
            if t.get("isBuyer"):
                bq, bc = bq + q, bc + c + fee
            else:
                sq, sc = sq + q, sc + c - fee
        return bq, bc, sq, sc
