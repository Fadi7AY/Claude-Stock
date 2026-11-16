"""What every trading platform adapter provides to the bot."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

BROWSER_UA = "Mozilla/5.0 (compatible; halal-stock-bot/1.0)"


class BrokerError(Exception):
    pass


def http_json(method, url, headers=None, body=None, params=None, timeout=30, insecure=False):
    """One HTTP call returning parsed JSON. Raises BrokerError with the server's message."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json", "User-Agent": BROWSER_UA}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise BrokerError("HTTP %s: %s" % (e.code, detail or e.reason))
    except urllib.error.URLError as e:
        raise BrokerError("cannot connect: %s" % e.reason)
    except (ValueError, OSError) as e:
        raise BrokerError("bad response: %s" % e)


def yahoo_price(symbol):
    """Last price of a US stock from Yahoo's free chart endpoint (may lag a little)."""
    sym = symbol.upper().replace(".", "-")
    for host in ("query2", "query1"):
        try:
            d = http_json("GET", "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1m"
                          % (host, sym), timeout=15)
            return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
        except (BrokerError, KeyError, TypeError, IndexError):
            continue
    raise BrokerError("no price for %s" % symbol)


class Broker:
    """Base class. Symbols passed in and out are always plain US stock tickers (AAPL)."""
    key = ""                 # registry id
    native_exits = False     # True: stop-loss and take-profit rest at the platform
    paper = False            # True: fake money

    @property
    def real_money(self):
        return not self.paper

    def account(self):
        """{'id', 'cash', 'equity', 'currency'}"""
        raise NotImplementedError

    def is_open(self):
        """True/False if the platform knows the US market clock, None if it does not."""
        return None

    def positions(self):
        """[{'symbol', 'qty', 'avg_price', 'price', 'pnl'}] - avg_price / pnl may be None."""
        raise NotImplementedError

    def position(self, symbol):
        for p in self.positions():
            if p["symbol"] == symbol:
                return p
        return None

    def price(self, symbol):
        """The price a sale would get right now (bid where available)."""
        raise NotImplementedError

    def can_trade(self, symbol):
        """(True, '') or (False, why)."""
        return True, ""

    def buy(self, symbol, qty, stop, target):
        """Open a long position. Returns {'id', 'price', 'qty'} (price/qty None if not known yet)."""
        raise NotImplementedError

    def close(self, symbol):
        """Cancel resting orders for the symbol and sell everything held."""
        raise NotImplementedError

    def pending_buy(self, symbol):
        return False

    def fills(self, symbol, day):
        """Today's fills in one symbol: (bought qty, cash paid, sold qty, cash received)."""
        raise NotImplementedError
