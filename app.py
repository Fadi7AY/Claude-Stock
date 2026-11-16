#!/usr/bin/env python3
"""Desktop control app for the halal stock bot.

Starts a small server on this computer only (127.0.0.1) and opens the
dashboard in its own window. Double-click "Stock Bot.bat" or run:

    python3 app.py            (add --no-window to only start the server)
"""
import datetime as dt
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brokers
import engine
import halal
from brokers import BrokerError
from nytime import now_ny, regular_hours

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
TOKEN = secrets.token_urlsafe(24)
RUNNER = engine.Runner()
SCREEN = {"running": False, "done": 0, "total": 0}
_CACHE = {}


def cached(key, seconds, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


# ------------------------------------------------------------------ data for the UI
def broker_view():
    acct = brokers.get_account()
    if not acct:
        return {"linked": False}
    out = {"linked": True, "id": acct["id"], "name": acct["name"], "platform": acct["platform"],
           "platform_name": brokers.platform(acct["platform"])["name"], "paper": brokers.is_paper(acct)}

    def fetch():
        try:
            b = brokers.connect(acct["id"])
            return {"account": b.account(), "positions": b.positions(), "native_exits": b.native_exits,
                    "paper": b.paper}
        except BrokerError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": repr(e)}
    out.update(cached("broker:" + acct["id"], 12, fetch))
    return out


def stats(trades):
    closed = [t for t in trades if t["status"] == "closed"]
    filled = [t for t in closed if t.get("close_reason") != "entry never filled"]
    wins = [t for t in filled if (t.get("pnl") or 0) > 0]
    losses = [t for t in filled if (t.get("pnl") or 0) < 0]
    today = now_ny().date().isoformat()
    week = engine.week_start(today)
    s = lambda ts: round(sum(t.get("pnl") or 0 for t in ts), 2)
    daily = {}
    for t in filled:
        daily[t["date"]] = round(daily.get(t["date"], 0) + (t.get("pnl") or 0), 2)
    days = sorted(daily)[-30:]
    cum, curve = 0.0, []
    for d in sorted(daily):
        cum = round(cum + daily[d], 2)
        curve.append({"date": d, "value": cum})
    return {
        "today": s([t for t in filled if t["date"] == today]),
        "week": s([t for t in filled if t["date"] >= week]),
        "total": s(filled), "count": len(filled), "wins": len(wins), "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(filled), 1) if filled else None,
        "avg_win": round(s(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(s(losses) / len(losses), 2) if losses else None,
        "trades_today": len([t for t in trades if t["date"] == today]),
        "daily": [{"date": d, "value": daily[d]} for d in days], "curve": curve[-90:],
    }


def tail_log(n=200):
    try:
        with open(engine.LOG, encoding="utf-8") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def full_state():
    cfg = engine.load_config()
    state = engine.load_state()
    acct = brokers.get_account()
    aid = acct["id"] if acct else None
    now = now_ny()
    mine = engine.real_trades(state, aid) if aid else engine.real_trades(state)
    names = {a["id"]: a["name"] for a in brokers.list_accounts()}
    trades = [dict(t, account_name=names.get(t.get("account"), t.get("platform") or "-"))
              for t in reversed(state["trades"][-400:])]
    return {
        "now": now.strftime("%a %d %b, %H:%M:%S"), "market_open": regular_hours(now),
        "bot": {"running": RUNNER.running, "mode": RUNNER.mode, "message": RUNNER.message,
                "last_cycle": RUNNER.last_cycle, "started": RUNNER.started,
                "account": names.get(RUNNER.account_id)},
        "broker": broker_view(),
        "open_trades": [t for t in mine if t["status"] == "open"],
        "trades": trades, "stats": stats(mine),
        "breadth": engine.breadth(now.date().isoformat(), fetch=False),
        "config": cfg, "accounts": brokers.list_accounts(), "screen": SCREEN,
        "log": tail_log(),
    }


def halal_view():
    cfg = engine.load_config()
    cache = engine.load_json(halal.CACHE, {})
    h = cfg.get("halal", {})
    block = {s.upper() for s in h.get("always_block", [])}
    allow = {s.upper() for s in h.get("always_allow", [])}
    rows = []
    for s in cfg["watchlist"]:
        s = s.upper()
        r = dict(cache.get(s) or {"symbol": s, "status": "NOT_SCREENED", "reasons": [], "notes": []})
        if s in block:
            r.update(status="NOT_HALAL", reasons=["on your always-block list"])
        elif s in allow:
            r.update(status="HALAL", notes=["on your always-allow list - automatic screen skipped"])
        rows.append(r)
    return {"rows": rows, "screen": SCREEN, "limits": h}


def run_screen(symbols, refresh):
    if SCREEN["running"]:
        return
    cfg = engine.load_config()
    SCREEN.update(running=True, done=0, total=len(symbols))

    def work():
        try:
            for s in symbols:
                try:
                    halal.check(s, cfg, refresh=refresh)
                except Exception as e:
                    engine.log("Screen problem for %s: %r" % (s, e))
                SCREEN["done"] += 1
        finally:
            SCREEN["running"] = False
    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ actions
def clean_symbols(raw):
    out = []
    for s in raw if isinstance(raw, list) else str(raw).replace(",", " ").split():
        s = str(s).strip().upper()
        if s and len(s) <= 6 and s.replace(".", "").isalpha() and s not in out:
            out.append(s)
    return out


def save_settings(body):
    cfg = engine.load_config()
    num = lambda v, lo, hi: max(lo, min(hi, float(v)))
    limits = {"risk": {"budget": (10, 1e7), "max_loss_per_trade_usd": (1, 1e5), "max_daily_loss_usd": (1, 1e6),
                       "max_trades_per_day": (1, 20), "cash_buffer_pct": (0, 90), "max_position_pct": (1, 100),
                       "max_order_usd": (5, 1e7), "min_reward_risk": (0.5, 10)},
              "safety": {"min_breadth_score": (0, 100), "max_consecutive_losses": (1, 20),
                         "max_weekly_loss_usd": (1, 1e7)},
              "strategy": {"max_open_positions": (1, 10), "max_signal_age_minutes": (5, 60)},
              "halal": {"max_debt_pct": (1, 33), "max_liquid_pct": (1, 33), "max_impure_income_pct": (0, 5)}}
    ints = {"max_trades_per_day", "max_consecutive_losses", "max_open_positions", "max_signal_age_minutes"}
    for section, keys in limits.items():
        for k, (lo, hi) in keys.items():
            if k in body.get(section, {}):
                v = num(body[section][k], lo, hi)
                cfg[section][k] = int(v) if k in ints else v
    st = body.get("strategy", {})
    if "or_minutes" in st and int(float(st["or_minutes"])) in (5, 15, 30):
        cfg["strategy"]["or_minutes"] = int(float(st["or_minutes"]))
    sp = body.get("sip", {})
    sip_cfg = cfg["strategy"].setdefault("stocks_in_play", {})
    for k, (lo, hi) in {"min_rvol": (0, 20), "min_atr_usd": (0, 100)}.items():
        if k in sp:
            sip_cfg[k] = num(sp[k], lo, hi)
    for k in ("enabled", "require_above_vwap"):
        if k in sp:
            sip_cfg[k] = bool(sp[k])
    if st.get("stop") in ("or-mid", "or-low", "datr:0.1", "datr:0.25"):
        cfg["strategy"]["stop"] = st["stop"]
    if st.get("target") in ("1R", "1.5R", "2R", "3R", "10R"):
        cfg["strategy"]["target"] = st["target"]
    for k in ("no_entries_before", "no_entries_after", "flat_by"):
        v = body.get("risk", {}).get(k)
        if v:
            dt.datetime.strptime(v, "%H:%M")
            cfg["risk"][k] = v
    hb = body.get("halal", {})
    if "exclude_weapons" in hb:
        cfg["halal"]["exclude_weapons"] = bool(hb["exclude_weapons"])
    for k in ("always_block", "always_allow"):
        if k in hb:
            cfg["halal"][k] = clean_symbols(hb[k])
    if "watchlist" in body:
        wl = clean_symbols(body["watchlist"])
        if not wl:
            raise BrokerError("the watchlist cannot be empty")
        cfg["watchlist"] = wl
    engine.save_json(engine.CONFIG, cfg)
    halal_reset = any(k in hb for k in ("max_debt_pct", "max_liquid_pct", "max_impure_income_pct", "exclude_weapons"))
    if halal_reset:     # limits changed: old verdicts no longer apply
        engine.save_json(halal.CACHE, {})
    return {"ok": True, "rescreen": halal_reset}


def do_close(symbols):
    acct = brokers.get_account()
    b = brokers.connect()
    state = engine.load_state()
    with engine.LOCK:
        n = engine.close_trades(b, acct, state, now_ny(), "closed by you", symbols)
        for s in (symbols or set()) - {t["symbol"] for t in state["trades"]}:
            if b.position(s):           # held at the platform but not opened by the bot
                b.close(s)
                n += 1
    _CACHE.clear()
    return {"ok": True, "closed": n}


def handle(path, body):
    if path == "/api/state":
        return full_state()
    if path == "/api/halal":
        return halal_view()
    if path == "/api/platforms":
        return {"platforms": brokers.PLATFORMS}
    if path == "/api/halal/screen":
        cfg = engine.load_config()
        run_screen(clean_symbols(body.get("symbols") or cfg["watchlist"]), bool(body.get("refresh")))
        return {"ok": True}
    if path == "/api/bot/start":
        RUNNER.start(body.get("mode", "dry"), confirm_real=body.get("confirm") == "REAL MONEY")
        return {"ok": True}
    if path == "/api/bot/stop":
        RUNNER.stop()
        return {"ok": True}
    if path == "/api/close":
        return do_close(set(clean_symbols(body.get("symbols", []))) or None)
    if path == "/api/accounts/add":
        aid = brokers.add_account(body.get("platform"), body.get("name"), body.get("fields", {}))
        _CACHE.clear()
        return {"ok": True, "id": aid}
    if path == "/api/accounts/test":
        b = brokers.connect(body.get("id"))
        a = b.account()
        return {"ok": True, "message": "Connected. Cash %.2f %s, total value %.2f. %s" % (
            a["cash"], a["currency"], a["equity"],
            "Practice money." if b.paper else "This is a REAL MONEY account.")}
    if path == "/api/accounts/remove":
        if RUNNER.running and RUNNER.account_id == body.get("id"):
            raise BrokerError("Stop the bot before removing the platform it is trading on.")
        brokers.remove_account(body.get("id"))
        _CACHE.clear()
        return {"ok": True}
    if path == "/api/accounts/select":
        if RUNNER.running:
            raise BrokerError("Stop the bot before switching platform.")
        brokers.set_active(body.get("id"))
        _CACHE.clear()
        return {"ok": True}
    if path == "/api/config":
        return save_settings(body)
    if path == "/api/quit":
        RUNNER.stop()
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"ok": True}
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path.split("?")[0] in ("/", "/index.html"):
            with open(os.path.join(HERE, "ui", "index.html"), encoding="utf-8") as f:
                return self._send(200, f.read().replace("__TOKEN__", TOKEN).encode(), "text/html")
        self._api({})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._send(400, {"error": "bad request"})
        self._api(body)

    def _api(self, body):
        if not secrets.compare_digest(self.headers.get("X-Token") or "", TOKEN):
            return self._send(403, {"error": "forbidden"})
        try:
            res = handle(self.path.split("?")[0], body)
            self._send(404, {"error": "not found"}) if res is None else self._send(200, res)
        except BrokerError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})


# ------------------------------------------------------------------ window
def open_window(url):
    """Open the dashboard in its own app-style window (Edge or Chrome), else the default browser."""
    wsl = "microsoft" in os.uname().release.lower() if hasattr(os, "uname") else False
    roots = ["/mnt/c"] if wsl else [os.environ.get("SystemDrive", "C:") + "\\"]
    cands = []
    for r in roots:
        for rel in (("Program Files (x86)", "Microsoft", "Edge", "Application", "msedge.exe"),
                    ("Program Files", "Microsoft", "Edge", "Application", "msedge.exe"),
                    ("Program Files", "Google", "Chrome", "Application", "chrome.exe"),
                    ("Program Files (x86)", "Google", "Chrome", "Application", "chrome.exe")):
            cands.append(os.path.join(r, *rel))
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        if shutil.which(name):
            cands.append(shutil.which(name))
    for exe in cands:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, "--app=" + url, "--window-size=1320,880"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    webbrowser.open(url)


def main():
    url = "http://127.0.0.1:%d/" % PORT
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        with socket.socket() as s:      # already running: just show the window
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                print("The app is already running - opening its window.")
                open_window(url)
                return
        raise
    print("Stock Bot is running at %s  (keep this window open; close it to quit)" % url)
    if "--no-window" not in sys.argv:
        threading.Timer(0.5, open_window, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        RUNNER.stop()


if __name__ == "__main__":
    main()
