"""The trading engine: one cycle = sync, manage exits, flatten at the close, look for one entry.

Used by both the desktop app (app.py) and the command line (bot.py).
"""
import csv
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import threading
import time

import brokers
import halal
import sip
from brokers import BrokerError
from nytime import now_ny, regular_hours

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SKILLS = os.path.join(HERE, ".claude", "skills")
STATE = os.path.join(DATA, "state.json")
LOG = os.path.join(DATA, "bot.log")
CONFIG = os.path.join(HERE, "config.json")
LOCK = threading.RLock()


# ------------------------------------------------------------------ files
def log(msg):
    line = "%s  %s" % (now_ny().strftime("%Y-%m-%d %H:%M:%S ET"), msg)
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass
    os.makedirs(DATA, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG, None)
    if cfg is None:
        raise BrokerError("config.json is missing or is not valid JSON")
    return cfg


def load_state():
    return load_json(STATE, {"trades": []})


def save_state(state):
    with LOCK:
        save_json(STATE, state)


def hhmm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


# ------------------------------------------------------------------ skills
def skill(folder, script, args, now=None, timeout=300):
    """Run one of the installed skill tools: (exit code, parsed JSON or None, text)."""
    cmd = [sys.executable, os.path.join(SKILLS, folder, script)] + args
    if now:
        cmd += ["--now", now.strftime("%Y-%m-%d %H:%M")]
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 1, None, "timed out"
    try:
        return p.returncode, json.loads(p.stdout), p.stdout
    except ValueError:
        return p.returncode, None, ((p.stdout or "") + (p.stderr or "")).strip()


def write_skill_config(cfg, budget):
    """The skills read trader/config.json; keep it in step with config.json."""
    r = dict(cfg["risk"])
    r.update({"budget": round(budget, 2), "require_stop": True, "fractional": False,
              "broker": "alpaca", "paper": True, "max_orders_per_day": 10,
              "blocked": cfg.get("halal", {}).get("always_block", []),
              "allow_options": False, "allow_margin": False, "allow_short": False,
              "pdt_guard": False, "sec_user_agent": cfg.get("sec_user_agent", "")})
    save_json(os.path.join(HERE, "trader", "config.json"), r)


# ------------------------------------------------------------------ gates
def halal_universe(cfg):
    ok, bad = [], []
    for s in cfg["watchlist"]:
        (ok if halal.check(s, cfg)["status"] == "HALAL" else bad).append(s.upper())
    log("Halal screen: %d of %d watchlist stocks pass. Excluded: %s"
        % (len(ok), len(cfg["watchlist"]), ", ".join(bad) or "none"))
    return ok


def breadth(today, fetch=True):
    """Market-health score 0-100 from the market-breadth-analyzer skill, once a day."""
    out = os.path.join(DATA, "breadth")
    cached = load_json(os.path.join(out, "today.json"), {})
    if cached.get("date") == today or not fetch:
        return cached
    res = {"date": today, "score": None, "zone": "unavailable"}
    script = os.path.join(SKILLS, "market-breadth-analyzer", "scripts", "market_breadth_analyzer.py")
    try:
        os.makedirs(out, exist_ok=True)
        subprocess.run([sys.executable, script, "--output-dir", out], cwd=HERE,
                       capture_output=True, text=True, timeout=180)
        newest = sorted(glob.glob(os.path.join(out, "market_breadth_2*.json")))[-1]
        comp = load_json(newest, {}).get("composite", {})
        res.update(score=comp.get("composite_score"), zone=comp.get("zone"))
    except Exception as e:
        res["zone"] = "unavailable (%s)" % e
    save_json(os.path.join(out, "today.json"), res)
    return res


def week_start(day):
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def real_trades(state, account_id=None):
    return [t for t in state["trades"] if not t.get("dry")
            and (account_id is None or t.get("account") == account_id)]


def entry_block(cfg, state, now, account_id):
    """Reason why no new trade may be opened right now, or None."""
    r, s, today = cfg["risk"], cfg["safety"], now.date().isoformat()
    t = now.time()
    if now.weekday() >= 5:
        return "weekend"
    if t < hhmm(r["no_entries_before"]) or t >= hhmm(r["no_entries_after"]):
        return "outside the entry window %s-%s New York time" % (r["no_entries_before"], r["no_entries_after"])
    mine = real_trades(state, account_id)
    todays = [x for x in mine if x["date"] == today]
    if len(todays) >= r["max_trades_per_day"]:
        return "max %d trades a day reached" % r["max_trades_per_day"]
    open_now = [x for x in todays if x["status"] == "open"]
    if len(open_now) >= cfg["strategy"]["max_open_positions"]:
        return "max %d open positions" % cfg["strategy"]["max_open_positions"]
    realized = sum(x.get("pnl") or 0 for x in todays if x["status"] == "closed")
    at_risk = sum(x["max_loss"] for x in open_now)
    if -realized + at_risk + 0.01 >= r["max_daily_loss_usd"]:
        return "daily loss limit ($%s) reached or fully at risk" % r["max_daily_loss_usd"]
    streak = 0
    for x in reversed([x for x in todays if x["status"] == "closed"]):
        if (x.get("pnl") or 0) < 0:
            streak += 1
        else:
            break
    if streak >= s["max_consecutive_losses"]:
        return "circuit breaker: %d losing trades in a row today" % streak
    wk = sum(x.get("pnl") or 0 for x in mine if x["status"] == "closed" and x["date"] >= week_start(today))
    if -wk >= s["max_weekly_loss_usd"]:
        return "circuit breaker: weekly loss limit ($%s) reached" % s["max_weekly_loss_usd"]
    b = breadth(today)
    if b.get("score") is not None and b["score"] < s["min_breadth_score"]:
        return "market breadth is weak (%.0f/100, %s)" % (b["score"], b["zone"])
    return None


# ------------------------------------------------------------------ trading
def find_signals(cfg, universe, state, now, pinned):
    """Fresh opening-range breakouts on halal stocks, most traded first."""
    rc, res, text = skill("trader-scanner", "scan.py",
                          ["orb", "--symbols", ",".join(universe), "--or-minutes",
                           str(cfg["strategy"].get("or_minutes", 15)), "--json", "--no-write", "--quiet"],
                          now if pinned else None)
    if res is None:
        if rc != 1:
            log("Scanner problem: %s" % text[:200])
        return []
    today = now.date().isoformat()
    traded = {x["symbol"] for x in state["trades"] if x["date"] == today}
    fresh = []
    for h in res.get("hits", []):
        try:
            at = dt.datetime.combine(now.date(), dt.datetime.strptime(h["at"], "%I:%M %p").time())
        except (KeyError, ValueError):
            continue
        age = (now - at).total_seconds() / 60.0
        if h["symbol"] in traded or not 0 <= age <= cfg["strategy"]["max_signal_age_minutes"]:
            continue
        fresh.append(h)
    return sip.filter_hits(fresh, cfg, now, log)


def plan_order(cfg, hit, budget, now, pinned):
    """Run exit -> budget -> risk skills. Returns (order dict, None) or (None, reason)."""
    write_skill_config(cfg, budget)
    n = now if pinned else None
    st = cfg["strategy"]
    stop = st["stop"]
    if stop.startswith("datr:"):
        # K x the daily ATR(14) under the entry (the paper used 0.1), as a fixed price for trader-exit
        sp = hit.get("sip")
        if not sp:
            return None, "a datr stop needs the stocks-in-play check turned on"
        stop = "price:%.2f" % (hit["price"] - float(stop[5:]) * sp["atr"])
    rc, plan, text = skill("trader-exit", "exit.py", [
        "plan", "--symbol", hit["symbol"], "--entry", str(hit["price"]),
        "--or-high", str(hit["or_high"]), "--or-low", str(hit["or_low"]),
        "--stop", stop, "--target", st["target"], "--write", "--json"], n)
    if rc != 0 or not plan:
        return None, "exit plan refused: %s" % text[:160]
    rc, sized, text = skill("trader-budget", "size.py", ["--write", "--json"], n)
    if not sized or not sized.get("orders"):
        why = (sized or {}).get("sizing", {}).get("refusal") or text[:160]
        return None, "sizing refused: %s" % why
    rc, risk, text = skill("trader-risk", "check.py", ["--json", "--no-write"], n)
    if not risk or not risk.get("results"):
        return None, "risk check failed to run: %s" % text[:160]
    v = risk["results"][0]
    if v.get("verdict") != "PASS":
        return None, "risk check BLOCK: %s" % "; ".join(v.get("reasons", []))
    o = sized["orders"][0]
    return {"symbol": o["symbol"], "qty": int(o["qty"]), "entry": o["plan"]["entry"],
            "stop": float(o["stop_loss_stop_price"]), "target": float(o["take_profit_limit_price"]),
            "max_loss": o["plan"]["max_loss"], "why": o.get("why", "")}, None


def open_trade(cfg, broker, acct, state, hit, now, dry, pinned=False):
    sym = hit["symbol"]
    # Gate 1: halal, re-checked at the moment of the trade.
    verdict = halal.check(sym, cfg)
    if verdict["status"] != "HALAL":
        log("SKIP %s: not halal (%s)" % (sym, "; ".join(verdict["reasons"])))
        return False
    cash = cfg["risk"]["budget"]
    if broker:
        ok, why = broker.can_trade(sym)
        if not ok:
            log("SKIP %s: %s" % (sym, why))
            return False
        cash = broker.account()["cash"]     # cash only - never margin
    budget = min(cfg["risk"]["budget"], cash)
    order, why = plan_order(cfg, hit, budget, now, pinned)
    if not order:
        log("SKIP %s: %s" % (sym, why))
        return False
    cost = order["qty"] * order["entry"]
    if order["qty"] < 1 or cost > cash:
        log("SKIP %s: needs $%.2f but only $%.2f cash (no margin allowed)" % (sym, cost, cash))
        return False
    desc = ("BUY %d %s ~%.2f  stop %.2f  target %.2f  max loss $%.2f"
            % (order["qty"], sym, order["entry"], order["stop"], order["target"], order["max_loss"]))
    if hit.get("sip"):
        desc += "  (rel. volume %.1fx, ATR $%.2f)" % (hit["sip"]["rvol"], hit["sip"]["atr"])
        order["rvol"] = hit["sip"]["rvol"]
    trade = dict(order, date=now.date().isoformat(), opened_at=now.strftime("%H:%M"),
                 status="open", pnl=None, order_id=None,
                 account=acct["id"] if acct else None, platform=acct["platform"] if acct else None)
    if dry or not broker:
        log("DRY RUN - would place: " + desc)
        trade.update(dry=True, status="closed", pnl=0.0, close_reason="dry run - no order sent")
    else:
        placed = broker.buy(sym, order["qty"], order["stop"], order["target"])
        trade["order_id"] = placed.get("id")
        if placed.get("price"):
            trade["entry"] = round(placed["price"], 4)
        if placed.get("qty"):
            trade["qty"] = placed["qty"]
        log("OPENED on %s (%s): %s" % (acct["name"], "paper" if broker.paper else "REAL MONEY", desc))
    with LOCK:
        state["trades"].append(trade)
        if not pinned:          # a replay never touches the saved history
            save_state(state)
    return True


def journal(trade):
    path = os.path.join(DATA, "trades.csv")
    cols = ["date", "platform", "symbol", "qty", "entry", "stop", "target", "max_loss", "opened_at",
            "closed_at", "close_reason", "pnl"]
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(trade)


def open_trades(state, account_id):
    return [t for t in real_trades(state, account_id) if t["status"] == "open"]


def sync(broker, acct, state, now):
    """Record trades that are no longer held (stop / target hit, or closed by the bot)."""
    for t in open_trades(state, acct["id"]):
        if broker.position(t["symbol"]) or broker.pending_buy(t["symbol"]):
            continue
        bq, bc, sq, sc = broker.fills(t["symbol"], t["date"])
        t.update(status="closed", closed_at=now.strftime("%H:%M"))
        if bq == 0:
            t.update(pnl=0.0, close_reason=t.get("close_reason") or "entry never filled")
        else:
            pnl = round(sc - bc, 2)
            t.update(pnl=pnl, entry=round(bc / bq, 4), exit=round(sc / sq, 4) if sq else None,
                     close_reason=t.get("close_reason") or ("target hit" if pnl > 0 else "stop hit"))
        log("CLOSED %s: %s, P&L $%.2f" % (t["symbol"], t["close_reason"], t["pnl"]))
        journal(t)
        save_state(state)


def close_trades(broker, acct, state, now, reason, symbols=None):
    n = 0
    for t in open_trades(state, acct["id"]):
        if symbols and t["symbol"] not in symbols:
            continue
        try:
            t["close_reason"] = reason
            broker.close(t["symbol"])
            log("CLOSING %s (%s)" % (t["symbol"], reason))
            n += 1
        except BrokerError as e:
            t.pop("close_reason", None)
            log("!! Could not close %s: %s" % (t["symbol"], e))
    save_state(state)
    if n:
        time.sleep(2)
        sync(broker, acct, state, now)
    return n


def watch_exits(broker, acct, state, now):
    """For platforms without stop orders: sell when the price reaches the stop or the target."""
    for t in open_trades(state, acct["id"]):
        try:
            px = broker.price(t["symbol"])
        except BrokerError as e:
            log("!! No price for %s: %s" % (t["symbol"], e))
            continue
        t["last"] = px
        if px <= t["stop"]:
            close_trades(broker, acct, state, now, "stop hit", {t["symbol"]})
        elif px >= t["target"]:
            close_trades(broker, acct, state, now, "target hit", {t["symbol"]})


def cycle(cfg, broker, acct, state, now, dry, memo, pinned=False):
    """One pass. Returns a short status line."""
    aid = acct["id"] if acct else None
    if broker and not dry:
        sync(broker, acct, state, now)
        stale = {t["symbol"] for t in open_trades(state, aid) if t["date"] < now.date().isoformat()}
        if stale:       # the app was closed before the flat-by time on an earlier day
            close_trades(broker, acct, state, now, "left open from an earlier day", stale)
        if not broker.native_exits:
            watch_exits(broker, acct, state, now)
        if now.time() >= hhmm(cfg["risk"]["flat_by"]):
            close_trades(broker, acct, state, now, "flat by %s" % cfg["risk"]["flat_by"])
            return "past the flat-by time - done for today"
    why = entry_block(cfg, state, now, aid)
    if why:
        return "no new trades: " + why
    bar = now.strftime("%Y-%m-%d %H:") + "%02d" % (now.minute // 5 * 5)
    if memo.get("bar") == bar and not pinned:
        return memo.get("last_scan", "waiting for the next 5-minute bar")
    memo["bar"] = bar
    today = now.date().isoformat()
    if memo.get("universe_day") != today:
        memo["universe"], memo["universe_day"] = halal_universe(cfg), today
    if not memo["universe"]:
        return "no halal stocks on the watchlist"
    hits = find_signals(cfg, memo["universe"], state, now, pinned)
    msg = "watching %d halal stocks - no fresh breakout" % len(memo["universe"])
    for h in hits:
        if open_trade(cfg, broker, acct, state, h, now, dry, pinned):
            msg = "trade opened in %s" % h["symbol"]
            break
    else:
        if hits:
            msg = "breakouts found but none passed the checks"
    memo["last_scan"] = msg
    return msg


# ------------------------------------------------------------------ runner
class Runner:
    """Runs the cycle in a background thread; the app starts and stops it."""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.mode = "dry"
        self.account_id = None
        self.message = "stopped"
        self.last_cycle = None
        self.started = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, mode, confirm_real=False):
        if self.running:
            raise BrokerError("the bot is already running")
        acct = brokers.get_account()
        if mode == "trade":
            if not acct:
                raise BrokerError("Link a trading platform first (Platforms page).")
            broker = brokers.connect()
            broker.account()                         # fail early on bad keys
            if broker.real_money and not confirm_real:
                raise BrokerError("REAL_MONEY_CONFIRM")
        self.mode, self.account_id = mode, acct["id"] if acct else None
        self.stop_flag.clear()
        self.started = now_ny().strftime("%H:%M")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=20)
        self.message = "stopped"

    def _loop(self):
        memo, last = {}, None
        dry = self.mode == "dry"
        acct = brokers.get_account(self.account_id)
        label = "DRY RUN (no orders)" if dry else "TRADING on %s" % acct["name"]
        log("Bot started: %s" % label)
        while not self.stop_flag.is_set():
            wait = 60
            try:
                cfg, state, now = load_config(), load_state(), now_ny()
                broker = None
                if acct:
                    try:
                        broker = brokers.connect(acct["id"])
                    except BrokerError:
                        if not dry:
                            raise
                is_open = None
                if broker and not dry:
                    is_open = broker.is_open()
                if is_open is None:
                    is_open = regular_hours(now)
                if is_open:
                    with LOCK:
                        msg = cycle(cfg, broker if not dry else None, acct, state, now, dry, memo)
                    wait = cfg.get("poll_seconds", 60)
                    if broker and not dry and not broker.native_exits and open_trades(state, acct["id"]):
                        wait = 15                    # the bot is the stop-loss: check often
                else:
                    msg = "market closed - waiting for 9:30 New York time"
            except BrokerError as e:
                msg = "platform error: %s" % e
            except Exception as e:      # keep running: open positions still need the flat-by close
                msg = "error: %r" % e
            self.message, self.last_cycle = msg, now_ny().strftime("%H:%M:%S")
            if msg != last:
                log(msg)
                last = msg
            self.stop_flag.wait(wait)
        log("Bot stopped.")
