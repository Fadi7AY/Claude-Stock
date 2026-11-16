"""Trading platforms the bot can be linked to, and where the saved keys live.

To add a platform: write an adapter (see base.Broker), then add an entry to
PLATFORMS with the fields the user has to fill in.
"""
import json
import os
import threading
import uuid

from .alpaca import Alpaca
from .base import Broker, BrokerError
from .ibkr import Ibkr
from .mexc import Mexc
from .sim import Sim

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
STORE = os.path.join(DATA, "accounts.json")
_LOCK = threading.Lock()

# field: {key, label, secret?, type?: text|checkbox|number, default?, help?}
PLATFORMS = [
    {
        "key": "sim", "name": "Simulator (built in)", "kind": "Practice account",
        "israel": "Works anywhere - nothing to sign up for.",
        "about": "Fake money on your own computer, filled at live prices. Try the bot before linking a real platform.",
        "fields": [{"key": "starting_cash", "label": "Starting cash (USD)", "type": "number", "default": 10000}],
        "steps": ["Nothing to set up. Click Add."],
    },
    {
        "key": "ibkr", "name": "Interactive Brokers", "kind": "US stocks", "beta": True,
        "israel": "Available to Israeli residents.",
        "about": "Real shares, stop-loss and take-profit held at the broker. Connects through IBKR's Client "
                 "Portal Gateway running on this computer - there is no API key. Open a CASH account "
                 "(not margin) to keep trading interest-free.",
        "fields": [
            {"key": "gateway_url", "label": "Gateway address", "default": "https://localhost:5000"},
            {"key": "account_id", "label": "Account ID (blank = first account)",
             "help": "Paper accounts start with DU."},
        ],
        "steps": [
            "Download the Client Portal Gateway from interactivebrokers.com (search: 'Client Portal API gateway').",
            "Unzip it and run bin\\run.bat root\\conf.yaml (needs Java).",
            "Open https://localhost:5000 in your browser and log in with your IBKR paper username.",
            "Come back here, click Add, then Test connection.",
        ],
    },
    {
        "key": "mexc", "name": "MEXC", "kind": "Tokenized US stocks (spot, USDT)", "real_only": True,
        "israel": "Israel was not on MEXC's restricted-country list when this was written (Nov 2026) - "
                  "confirm in MEXC's user agreement before funding.",
        "about": "Trades tokenized stocks (xStocks / Ondo, e.g. AAPLX) on the spot market only - never "
                 "futures or margin. MEXC has no practice account, so orders use real money. Stops are "
                 "watched by this app, so it must stay open while a position is open.",
        "fields": [
            {"key": "api_key", "label": "Access Key", "secret": True},
            {"key": "secret_key", "label": "Secret Key", "secret": True},
        ],
        "steps": [
            "On mexc.com: profile icon > API Management > Create New API Key.",
            "Tick ONLY the Spot permissions: Account (read) and Trade. Leave Withdraw and Futures unticked.",
            "Optional but safer: link the key to your IP address.",
            "Copy the Access Key and Secret Key into the boxes here.",
        ],
    },
    {
        "key": "alpaca", "name": "Alpaca", "kind": "US stocks",
        "israel": "Paper accounts are open to everyone. Live accounts for non-US residents are limited - "
                  "check with Alpaca whether Israel is currently accepted.",
        "about": "Real shares, stop-loss and take-profit held at the broker. Free paper-trading account.",
        "fields": [
            {"key": "api_key", "label": "API Key ID", "secret": True},
            {"key": "secret_key", "label": "Secret Key", "secret": True},
            {"key": "paper", "label": "Paper account (fake money)", "type": "checkbox", "default": True},
        ],
        "steps": [
            "Sign up at alpaca.markets and open the Paper Trading dashboard.",
            "Click Generate New Keys and copy the Key ID and the Secret.",
            "Paste both here and keep 'Paper account' ticked.",
        ],
    },
]
_CLASSES = {"alpaca": Alpaca, "mexc": Mexc, "ibkr": Ibkr, "sim": Sim}


def platform(key):
    for p in PLATFORMS:
        if p["key"] == key:
            return p
    raise BrokerError("unknown platform %r" % key)


# ---------------------------------------------------------------- key store
def _read():
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"active": None, "accounts": []}


def _write(store):
    os.makedirs(DATA, exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1)
    try:
        os.chmod(tmp, 0o600)       # keys: readable by you only
    except OSError:
        pass
    os.replace(tmp, STORE)


def _mask(value):
    v = str(value)
    return ("*" * 8 + v[-4:]) if len(v) > 8 else "*" * 8


def list_accounts():
    """Accounts for display: secrets are masked and never leave this module in clear."""
    store = _read()
    out = []
    for a in store["accounts"]:
        p = platform(a["platform"])
        shown = {}
        for f in p["fields"]:
            v = a["creds"].get(f["key"])
            shown[f["key"]] = _mask(v) if f.get("secret") and v else v
        out.append({"id": a["id"], "name": a["name"], "platform": a["platform"],
                    "platform_name": p["name"], "kind": p["kind"], "beta": bool(p.get("beta")),
                    "paper": is_paper(a), "fields": shown, "active": a["id"] == store["active"]})
    return out


def is_paper(acct):
    if acct["platform"] == "sim":
        return True
    if acct["platform"] == "alpaca":
        return bool(acct["creds"].get("paper", True))
    if acct["platform"] == "ibkr":
        return (acct["creds"].get("account_id") or "").upper().startswith("DU")
    return False


def add_account(platform_key, name, creds):
    p = platform(platform_key)
    clean = {}
    for f in p["fields"]:
        v = creds.get(f["key"], f.get("default"))
        if f.get("type") == "checkbox":
            v = bool(v)
        elif f.get("type") == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise BrokerError("%s must be a number" % f["label"])
        else:
            v = str(v or "").strip()
            if f.get("secret") and not v:
                raise BrokerError("%s is required" % f["label"])
        clean[f["key"]] = v
    with _LOCK:
        store = _read()
        acct = {"id": uuid.uuid4().hex[:8], "name": (name or p["name"]).strip()[:40],
                "platform": platform_key, "creds": clean}
        store["accounts"].append(acct)
        if not store["active"]:
            store["active"] = acct["id"]
        _write(store)
    return acct["id"]


def remove_account(account_id):
    with _LOCK:
        store = _read()
        store["accounts"] = [a for a in store["accounts"] if a["id"] != account_id]
        if store["active"] == account_id:
            store["active"] = store["accounts"][0]["id"] if store["accounts"] else None
        _write(store)


def set_active(account_id):
    with _LOCK:
        store = _read()
        if not any(a["id"] == account_id for a in store["accounts"]):
            raise BrokerError("no such account")
        store["active"] = account_id
        _write(store)


def active_id():
    return _read()["active"]


def get_account(account_id=None):
    store = _read()
    account_id = account_id or store["active"]
    for a in store["accounts"]:
        if a["id"] == account_id:
            return a
    return None


_CACHE = {}


def connect(account_id=None):
    """A live Broker object for a saved account (the active one by default)."""
    acct = get_account(account_id)
    if not acct:
        raise BrokerError("No trading platform linked yet. Add one on the Platforms page.")
    sig = (acct["id"], json.dumps(acct["creds"], sort_keys=True))
    if _CACHE.get("sig") != sig:
        creds = dict(acct["creds"], _id=acct["id"])
        cls = _CLASSES[acct["platform"]]
        _CACHE["broker"] = cls(creds, DATA) if cls is Sim else cls(creds)
        _CACHE["sig"] = sig
    return _CACHE["broker"]
