"""Interactive Brokers through the Client Portal Gateway (runs on your own computer).

IBKR has no API key: you download the Client Portal Gateway from IBKR, start
it, and log in at https://localhost:5000 in your browser. This adapter talks
to that local gateway. Stop-loss and take-profit rest at IBKR as child orders.

BETA: written from IBKR's Web API documentation and not yet run against a real
gateway. Use an IBKR paper account (account id starts with DU) first.
"""
import time

from .base import Broker, BrokerError, http_json


class Ibkr(Broker):
    key = "ibkr"
    native_exits = True

    def __init__(self, creds):
        self.base = (creds.get("gateway_url") or "https://localhost:5000").rstrip("/") + "/v1/api"
        self.acct = (creds.get("account_id") or "").strip()
        self._conid = {}
        self._ready = False

    def _req(self, method, path, body=None, params=None):
        try:
            return http_json(method, self.base + path, body=body, params=params, insecure=True)
        except BrokerError as e:
            msg = str(e)
            if "cannot connect" in msg:
                msg = "the Client Portal Gateway is not running at %s" % self.base[:-7]
            elif "HTTP 401" in msg:
                msg = "not logged in - open the gateway address in your browser and log in"
            raise BrokerError("IBKR %s: %s" % (path, msg))

    def _init(self):
        if self._ready:
            return
        st = self._req("POST", "/iserver/auth/status") or {}
        if not st.get("authenticated"):
            raise BrokerError("IBKR: not logged in - open the gateway address in your browser and log in")
        accts = [a.get("accountId") or a.get("id") for a in (self._req("GET", "/portfolio/accounts") or [])]
        if not accts:
            raise BrokerError("IBKR: no accounts returned by the gateway")
        if not self.acct:
            self.acct = accts[0]
        elif self.acct not in accts:
            raise BrokerError("IBKR: account %s is not one of %s" % (self.acct, ", ".join(accts)))
        self.paper = self.acct.upper().startswith("DU")
        self._req("GET", "/iserver/accounts")      # required once before order calls
        self._ready = True

    def _con(self, symbol):
        if symbol not in self._conid:
            res = self._req("GET", "/iserver/secdef/search", params={"symbol": symbol, "secType": "STK"}) or []
            us = [r for r in res if r.get("symbol") == symbol and
                  any(x in (r.get("description") or "") for x in ("NASDAQ", "NYSE", "ARCA", "AMEX"))]
            if not (us or res):
                raise BrokerError("IBKR: %s not found" % symbol)
            self._conid[symbol] = int((us or res)[0]["conid"])
        return self._conid[symbol]

    def account(self):
        self._init()
        led = (self._req("GET", "/portfolio/%s/ledger" % self.acct) or {})
        usd = led.get("USD") or led.get("BASE") or {}
        return {"id": self.acct, "cash": float(usd.get("cashbalance") or 0),
                "equity": float((led.get("BASE") or usd).get("netliquidationvalue") or 0),
                "currency": "USD"}

    def positions(self):
        self._init()
        out = []
        for p in self._req("GET", "/portfolio/%s/positions/0" % self.acct) or []:
            if p.get("assetClass") != "STK" or not float(p.get("position") or 0):
                continue
            sym = p.get("ticker") or (p.get("contractDesc") or "").split(" ")[0]
            self._conid.setdefault(sym, int(p["conid"]))
            out.append({"symbol": sym, "qty": float(p["position"]), "avg_price": float(p.get("avgPrice") or 0),
                        "price": float(p.get("mktPrice") or 0), "pnl": float(p.get("unrealizedPnl") or 0)})
        return out

    def price(self, symbol):
        p = self.position(symbol)
        if not p:
            raise BrokerError("no position in %s" % symbol)
        return p["price"]

    def can_trade(self, symbol):
        try:
            self._init()
            self._con(symbol)
            return True, ""
        except BrokerError as e:
            return False, str(e)

    def _confirm(self, reply):
        """IBKR answers some orders with a question; confirm until it returns order ids."""
        for _ in range(5):
            if isinstance(reply, list) and reply and "id" in reply[0] and "message" in reply[0]:
                reply = self._req("POST", "/iserver/reply/%s" % reply[0]["id"], body={"confirmed": True})
            else:
                break
        if isinstance(reply, dict) and reply.get("error"):
            raise BrokerError("IBKR order rejected: %s" % reply["error"])
        return reply

    def buy(self, symbol, qty, stop, target):
        self._init()
        conid, ref = self._con(symbol), "bot-%s-%d" % (symbol, int(time.time()))
        common = {"acctId": self.acct, "conid": conid, "tif": "DAY", "quantity": int(qty)}
        orders = [
            dict(common, cOID=ref, orderType="MKT", side="BUY"),
            dict(common, parentId=ref, orderType="LMT", side="SELL", price=round(float(target), 2)),
            dict(common, parentId=ref, orderType="STP", side="SELL", price=round(float(stop), 2)),
        ]
        reply = self._confirm(self._req("POST", "/iserver/account/%s/orders" % self.acct,
                                        body={"orders": orders}))
        oid = reply[0].get("order_id") if isinstance(reply, list) and reply else None
        return {"id": str(oid or ref), "price": None, "qty": None}

    def _live_orders(self, symbol):
        data = self._req("GET", "/iserver/account/orders") or {}
        return [o for o in data.get("orders", []) if o.get("ticker") == symbol and
                o.get("status") in ("PreSubmitted", "Submitted", "PendingSubmit")]

    def pending_buy(self, symbol):
        self._init()
        return any(o.get("side") == "BUY" for o in self._live_orders(symbol))

    def close(self, symbol):
        self._init()
        for o in self._live_orders(symbol):
            try:
                self._req("DELETE", "/iserver/account/%s/order/%s" % (self.acct, o["orderId"]))
            except BrokerError:
                pass
        time.sleep(2)
        p = self.position(symbol)
        if not p or p["qty"] <= 0:
            return
        self._confirm(self._req("POST", "/iserver/account/%s/orders" % self.acct, body={"orders": [{
            "acctId": self.acct, "conid": self._con(symbol), "orderType": "MKT", "side": "SELL",
            "tif": "DAY", "quantity": int(p["qty"])}]}))

    def fills(self, symbol, day):
        self._init()
        bq = bc = sq = sc = 0.0
        for t in self._req("GET", "/iserver/account/trades") or []:
            if t.get("symbol") != symbol or str(t.get("trade_time", ""))[:8] < day.replace("-", ""):
                continue
            q, p = float(t.get("size") or 0), float(t.get("price") or 0)
            if t.get("side") == "B":
                bq, bc = bq + q, bc + q * p
            else:
                sq, sc = sq + q, sc + q * p
        return bq, bc, sq, sc
