#!/usr/bin/env python3
"""Halal stock trading bot (Alpaca, paper trading by default).

What it does each trading day (New York time):
  1. Screens the watchlist and keeps only stocks that pass the halal screen.
  2. Looks for opening-range breakouts on those stocks (trader-scanner skill).
  3. For a fresh breakout: plans stop + target (trader-exit), sizes the
     position to your max loss (trader-budget), checks it against your limits
     (trader-risk), then OPENS the trade with a bracket order.
  4. CLOSES trades: the stop-loss / take-profit sit at the broker, and anything
     still open is sold at the flat-by time (15:55) - no overnight holds.

Halal trading rules built in: long only (no short selling), cash only (no
margin or leverage), shares only (no options, futures or crypto).

Commands:
  python3 bot.py screen            halal verdict for every watchlist stock
  python3 bot.py screen AAPL JPM   ... or for the tickers you name
  python3 bot.py check             test the broker connection
  python3 bot.py run --dry-run     full bot, but only prints the orders
  python3 bot.py run               full bot, places orders (paper account)
  python3 bot.py status            account, open positions, today's trades
  python3 bot.py close AAPL        close one position now
  python3 bot.py close-all         close every position the bot opened
  python3 bot.py once --now "2026-11-13 10:00"   replay one moment (dry run)
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import halal
from broker import Alpaca, BrokerError

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SKILLS = os.path.join(HERE, ".claude", "skills")
STATE = os.path.join(DATA, "state.json")
NY = ZoneInfo("America/New_York")


# ------------------------------------------------------------------ helpers
def log(msg):
    line = "%s  %s" % (dt.datetime.now(NY).strftime("%Y-%m-%d %H:%M:%S ET"), msg)
    print(line, flush=True)
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "bot.log"), "a", encoding="utf-8") as f:
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
    cfg = load_json(os.path.join(HERE, "config.json"), None)
    if cfg is None:
        sys.exit("config.json is missing or is not valid JSON.")
    return cfg


def hhmm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def skill(folder, script, args, now=None, timeout=300):
    """Run one of the installed skill tools and return (exit code, parsed JSON or None, text)."""
    cmd = [sys.executable, os.path.join(SKILLS, folder, script)] + args
    if now:
        cmd += ["--now", now.strftime("%Y-%m-%d %H:%M")]
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, None, "timed out"
    try:
        return p.returncode, json.loads(p.stdout), p.stdout
    except ValueError:
        return p.returncode, None, (p.stdout + p.stderr).strip()


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
def halal_universe(cfg, quiet=False):
    ok, bad = [], []
    for s in cfg["watchlist"]:
        (ok if halal.check(s, cfg)["status"] == "HALAL" else bad).append(s.upper())
    if not quiet:
        log("Halal screen: %d of %d watchlist stocks pass. Excluded: %s"
            % (len(ok), len(cfg["watchlist"]), ", ".join(bad) or "none"))
    return ok


def breadth(cfg, today):
    """Market-health score 0-100 from the market-breadth-analyzer skill, once a day."""
    out = os.path.join(DATA, "breadth")
    cached = load_json(os.path.join(out, "today.json"), {})
    if cached.get("date") == today:
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


def entry_block(cfg, state, now):
    """Reason why no new trade may be opened right now, or None."""
    r, s, today = cfg["risk"], cfg["safety"], now.date().isoformat()
    t = now.time()
    if now.weekday() >= 5:
        return "weekend"
    if t < hhmm(r["no_entries_before"]) or t >= hhmm(r["no_entries_after"]):
        return "outside the entry window %s-%s" % (r["no_entries_before"], r["no_entries_after"])
    todays = [x for x in state["trades"] if x["date"] == today and not x.get("dry")]
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
    wk = sum(x.get("pnl") or 0 for x in state["trades"]
             if x["status"] == "closed" and x["date"] >= week_start(today))
    if -wk >= s["max_weekly_loss_usd"]:
        return "circuit breaker: weekly loss limit ($%s) reached" % s["max_weekly_loss_usd"]
    b = breadth(cfg, today)
    if b.get("score") is not None and b["score"] < s["min_breadth_score"]:
        return "market breadth is weak (%.0f/100, %s)" % (b["score"], b["zone"])
    return None


# ------------------------------------------------------------------ trading
def find_signals(cfg, universe, state, now, pinned):
    """Fresh opening-range breakouts on halal stocks, most traded first."""
    rc, res, text = skill("trader-scanner", "scan.py",
                          ["orb", "--symbols", ",".join(universe), "--json", "--no-write", "--quiet"],
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
            at = dt.datetime.combine(now.date(), dt.datetime.strptime(h["at"], "%I:%M %p").time(), NY)
        except (KeyError, ValueError):
            continue
        age = (now - at).total_seconds() / 60.0
        if h["symbol"] in traded or not 0 <= age <= cfg["strategy"]["max_signal_age_minutes"]:
            continue
        fresh.append(h)
    return fresh


def plan_order(cfg, hit, budget, now, pinned):
    """Run exit -> budget -> risk skills. Returns (order dict, None) or (None, reason)."""
    write_skill_config(cfg, budget)
    n = now if pinned else None
    st = cfg["strategy"]
    rc, plan, text = skill("trader-exit", "exit.py", [
        "plan", "--symbol", hit["symbol"], "--entry", str(hit["price"]),
        "--or-high", str(hit["or_high"]), "--or-low", str(hit["or_low"]),
        "--stop", st["stop"], "--target", st["target"], "--write", "--json"], n)
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


def open_trade(cfg, broker, state, hit, now, dry, pinned):
    sym = hit["symbol"]
    # Gate 1: halal, re-checked at the moment of the trade.
    verdict = halal.check(sym, cfg)
    if verdict["status"] != "HALAL":
        log("SKIP %s: not halal (%s)" % (sym, "; ".join(verdict["reasons"])))
        return False
    cash = cfg["risk"]["budget"]
    if broker:
        acct = broker.account()
        cash = float(acct["cash"])  # settled cash only: never buying_power (that is margin)
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            log("Account is blocked for trading at Alpaca.")
            return False
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
    trade = dict(order, date=now.date().isoformat(), opened_at=now.strftime("%H:%M"),
                 status="open", pnl=None, order_id=None)
    if dry or not broker:
        log("DRY RUN - would place: " + desc)
        trade.update(dry=True, status="closed", pnl=0.0)
    else:
        a = broker.asset(sym)
        if not a.get("tradable"):
            log("SKIP %s: not tradable at Alpaca" % sym)
            return False
        placed = broker.buy_bracket(sym, order["qty"], order["stop"], order["target"])
        trade["order_id"] = placed.get("id")
        log("OPENED (%s): %s" % ("paper" if broker.paper else "LIVE", desc))
    state["trades"].append(trade)
    save_json(STATE, state)
    return True


def realized_pnl(broker, sym, day):
    """Net cash from today's filled orders in one symbol: sells minus buys."""
    bought = sold = bq = sq = 0.0
    for o in broker.orders("closed", [sym], after=day + "T00:00:00Z"):
        q, p = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
        if q <= 0:
            continue
        if o["side"] == "buy":
            bought, bq = bought + q * p, bq + q
        else:
            sold, sq = sold + q * p, sq + q
    return bq, sq, round(sold - bought, 2)


def journal(trade):
    path = os.path.join(DATA, "trades.csv")
    cols = ["date", "symbol", "qty", "entry", "stop", "target", "max_loss", "opened_at",
            "closed_at", "close_reason", "pnl"]
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(trade)


def sync(broker, state, now):
    """Notice trades that the broker closed (stop or target hit) and record the result."""
    for t in state["trades"]:
        if t["status"] != "open":
            continue
        if broker.position(t["symbol"]):
            continue
        if any(o["side"] == "buy" for o in broker.orders("open", [t["symbol"]])):
            continue  # entry still working
        bq, sq, pnl = realized_pnl(broker, t["symbol"], t["date"])
        t.update(status="closed", closed_at=now.strftime("%H:%M"))
        if bq == 0:
            t.update(pnl=0.0, close_reason=t.get("close_reason") or "entry never filled")
        else:
            t.update(pnl=pnl, close_reason=t.get("close_reason") or
                     ("target hit" if pnl > 0 else "stop hit"))
        log("CLOSED %s: %s, P&L $%.2f" % (t["symbol"], t["close_reason"], t["pnl"]))
        journal(t)
        save_json(STATE, state)


def close_trades(broker, state, now, reason, symbols=None):
    n = 0
    for t in state["trades"]:
        if t["status"] != "open" or (symbols and t["symbol"] not in symbols):
            continue
        try:
            broker.close(t["symbol"])
            t["close_reason"] = reason
            log("CLOSING %s (%s)" % (t["symbol"], reason))
            n += 1
        except BrokerError as e:
            log("!! Could not close %s: %s" % (t["symbol"], e))
    save_json(STATE, state)
    if n:
        time.sleep(3)
        sync(broker, state, now)
    return n


def cycle(cfg, broker, state, now, dry, pinned=False, memo={}):
    """One pass: sync, flatten at the close, look for one new entry."""
    if broker and not dry:
        sync(broker, state, now)
        if now.time() >= hhmm(cfg["risk"]["flat_by"]):
            close_trades(broker, state, now, "flat by %s" % cfg["risk"]["flat_by"])
            return "past flat-by time"
    why = entry_block(cfg, state, now)
    if why:
        return why
    bar = now.strftime("%Y-%m-%d %H:") + "%02d" % (now.minute // 5 * 5)
    if memo.get("bar") == bar and not pinned:
        return "waiting for the next 5-minute bar"
    memo["bar"] = bar
    today = now.date().isoformat()
    if memo.get("universe_day") != today:
        memo["universe"], memo["universe_day"] = halal_universe(cfg), today
    if not memo["universe"]:
        return "no halal stocks on the watchlist"
    hits = find_signals(cfg, memo["universe"], state, now, pinned)
    if not hits:
        return "no fresh breakout on %d halal stocks" % len(memo["universe"])
    for h in hits:
        if open_trade(cfg, broker, state, h, now, dry, pinned):
            return "trade opened in %s" % h["symbol"]
    return "breakouts found but none passed the checks"


# ------------------------------------------------------------------ commands
def get_broker(args, required=True):
    try:
        return Alpaca(allow_live=getattr(args, "live", False))
    except BrokerError as e:
        if required:
            sys.exit(str(e))
        return None


def cmd_screen(args, cfg):
    syms = [s.upper() for s in args.symbols] or cfg["watchlist"]
    print("Screening %d stocks (first run downloads SEC filings, about 2 s each)..." % len(syms))
    res = [halal.check(s, cfg, refresh=args.refresh) for s in syms]
    print(halal.table(res))
    print("\n%d HALAL, %d NOT_HALAL, %d UNKNOWN (unknown is never traded)" % tuple(
        sum(r["status"] == k for r in res) for k in ("HALAL", "NOT_HALAL", "UNKNOWN")))


def cmd_check(args, cfg):
    b = get_broker(args)
    a, c = b.account(), b.clock()
    print("Connected to Alpaca - %s account %s" % ("PAPER" if b.paper else "LIVE (real money)",
                                                   a.get("account_number")))
    print("  cash $%s   equity $%s   status %s" % (a["cash"], a["equity"], a["status"]))
    print("  market is %s (next open %s)" % ("OPEN" if c["is_open"] else "closed", c["next_open"]))


def cmd_status(args, cfg):
    b = get_broker(args)
    state = load_json(STATE, {"trades": []})
    a = b.account()
    print("%s account: cash $%s, equity $%s" % ("PAPER" if b.paper else "LIVE", a["cash"], a["equity"]))
    pos = b.positions()
    print("Open positions: %s" % (len(pos) or "none"))
    for p in pos:
        print("  %s %s shares @ %s, now %s, unrealized P&L $%s" % (
            p["symbol"], p["qty"], p["avg_entry_price"], p["current_price"], p["unrealized_pl"]))
    today = dt.datetime.now(NY).date().isoformat()
    todays = [t for t in state["trades"] if t["date"] == today and not t.get("dry")]
    print("Bot trades today: %s" % (len(todays) or "none"))
    for t in todays:
        print("  %s %s x%s entry %.2f stop %.2f target %.2f -> %s%s" % (
            t["opened_at"], t["symbol"], t["qty"], t["entry"], t["stop"], t["target"], t["status"],
            "" if t["pnl"] is None else " P&L $%.2f (%s)" % (t["pnl"], t.get("close_reason"))))


def cmd_close(args, cfg):
    b = get_broker(args)
    state = load_json(STATE, {"trades": []})
    now = dt.datetime.now(NY)
    syms = {s.upper() for s in getattr(args, "symbols", [])} or None
    n = close_trades(b, state, now, "closed by you", syms)
    # A position you name that the bot is not tracking (e.g. after a reset) is closed too.
    for s in (syms or set()) - {t["symbol"] for t in state["trades"]}:
        if b.position(s):
            b.close(s)
            print("Closed %s (not opened by the bot today)." % s)
            n += 1
    print("Closed %d position(s)." % n if n else "Nothing to close.")


def cmd_once(args, cfg):
    state = load_json(STATE, {"trades": []})
    if args.now:
        now = dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=NY)
        state = {"trades": []}  # a replay never touches real state
        print(cycle(cfg, None, state, now, dry=True, pinned=True))
        return
    b = get_broker(args, required=not args.dry_run)
    print(cycle(cfg, b, state, dt.datetime.now(NY), dry=args.dry_run))


def cmd_run(args, cfg):
    b = get_broker(args, required=not args.dry_run)
    mode = "DRY RUN (no orders)" if args.dry_run else ("PAPER" if b.paper else "LIVE - REAL MONEY")
    log("Bot started: %s. Ctrl+C to stop." % mode)
    state = load_json(STATE, {"trades": []})
    last = None
    try:
        while True:
            now = dt.datetime.now(NY)
            try:
                is_open = b.clock()["is_open"] if b else (
                    now.weekday() < 5 and dt.time(9, 30) <= now.time() < dt.time(16, 0))
                msg = cycle(cfg, b, state, now, args.dry_run) if is_open else "market closed"
            except BrokerError as e:
                msg = "broker error: %s" % e
            except Exception as e:  # keep running: open positions still need the flat-by close
                msg = "error: %r" % e
            if msg != last:
                log(msg)
                last = msg
            time.sleep(300 if msg == "market closed" else cfg.get("poll_seconds", 60))
    except KeyboardInterrupt:
        still = [t["symbol"] for t in state["trades"] if t["status"] == "open"]
        log("Bot stopped." + (" STILL OPEN: %s - their stop/target stay at the broker only until "
                              "today's close. Run 'python3 bot.py close-all' to exit now."
                              % ", ".join(still) if still else ""))


def main():
    ap = argparse.ArgumentParser(description="Halal stock trading bot (Alpaca).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("screen"); p.add_argument("symbols", nargs="*"); p.add_argument("--refresh", action="store_true")
    sub.add_parser("check")
    sub.add_parser("status")
    p = sub.add_parser("close"); p.add_argument("symbols", nargs="+")
    sub.add_parser("close-all")
    p = sub.add_parser("once"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--now")
    p = sub.add_parser("run"); p.add_argument("--dry-run", action="store_true")
    for name in ("check", "status", "close", "close-all", "once", "run"):
        sub.choices[name].add_argument("--live", action="store_true",
                                       help="allow a real-money account (also needs ALPACA_PAPER=false)")
    args = ap.parse_args()
    cfg = load_config()
    try:
        {"screen": cmd_screen, "check": cmd_check, "status": cmd_status, "close": cmd_close,
         "close-all": cmd_close, "once": cmd_once, "run": cmd_run}[args.cmd](args, cfg)
    except BrokerError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
