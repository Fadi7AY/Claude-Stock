#!/usr/bin/env python3
"""Command-line control for the halal stock bot. The desktop app (app.py) is the easier way.

  python3 bot.py screen [TICKERS]      halal verdict for the watchlist or the tickers you name
  python3 bot.py platforms             linked platforms (add them in the app)
  python3 bot.py check                 test the active platform's connection
  python3 bot.py run --dry-run         full bot, only prints the orders
  python3 bot.py run                   full bot on the active platform
  python3 bot.py status                account, positions, today's trades
  python3 bot.py close AAPL | close-all
  python3 bot.py once --now "2026-11-13 10:00"    replay one moment (dry run)
"""
import argparse
import datetime as dt
import sys
import time

import brokers
import engine
import halal
from brokers import BrokerError
from nytime import now_ny


def cmd_screen(args):
    cfg = engine.load_config()
    syms = [s.upper() for s in args.symbols] or cfg["watchlist"]
    print("Screening %d stocks (first run downloads SEC filings, about 2 s each)..." % len(syms))
    res = [halal.check(s, cfg, refresh=args.refresh) for s in syms]
    print(halal.table(res))
    print("\n%d HALAL, %d NOT_HALAL, %d UNKNOWN (unknown is never traded)" % tuple(
        sum(r["status"] == k for r in res) for k in ("HALAL", "NOT_HALAL", "UNKNOWN")))


def cmd_platforms(args):
    accts = brokers.list_accounts()
    if not accts:
        print("No platform linked. Start the app (python3 app.py) and open the Platforms page.")
    for a in accts:
        print("%s %-22s %-22s %s" % ("*" if a["active"] else " ", a["name"], a["platform_name"],
                                     "paper" if a["paper"] else "REAL MONEY"))


def cmd_check(args):
    b, a = brokers.connect(), brokers.get_account()
    acc = b.account()
    print("Connected to %s - %s" % (a["name"], "paper / practice" if b.paper else "REAL MONEY"))
    print("  cash %.2f %s   equity %.2f" % (acc["cash"], acc["currency"], acc["equity"]))


def cmd_status(args):
    b, a = brokers.connect(), brokers.get_account()
    acc = b.account()
    print("%s: cash %.2f %s, equity %.2f" % (a["name"], acc["cash"], acc["currency"], acc["equity"]))
    pos = b.positions()
    print("Open positions: %s" % (len(pos) or "none"))
    for p in pos:
        print("  %s %s @ %s, now %s" % (p["symbol"], p["qty"], p["avg_price"], p["price"]))
    today = now_ny().date().isoformat()
    todays = [t for t in engine.real_trades(engine.load_state(), a["id"]) if t["date"] == today]
    print("Bot trades today: %s" % (len(todays) or "none"))
    for t in todays:
        print("  %s %s x%s entry %.2f stop %.2f target %.2f -> %s%s" % (
            t["opened_at"], t["symbol"], t["qty"], t["entry"], t["stop"], t["target"], t["status"],
            "" if t["pnl"] is None else " P&L $%.2f (%s)" % (t["pnl"], t.get("close_reason"))))


def cmd_close(args):
    b, a, state = brokers.connect(), brokers.get_account(), engine.load_state()
    syms = {s.upper() for s in getattr(args, "symbols", [])} or None
    n = engine.close_trades(b, a, state, now_ny(), "closed by you", syms)
    print("Closed %d position(s)." % n if n else "Nothing to close.")


def cmd_once(args):
    cfg = engine.load_config()
    if args.now:
        now = dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M")
        print(engine.cycle(cfg, None, None, {"trades": []}, now, True, {}, pinned=True))
        return
    a = brokers.get_account()
    b = None if args.dry_run else brokers.connect()
    print(engine.cycle(cfg, b, a, engine.load_state(), now_ny(), args.dry_run, {}))


def cmd_run(args):
    r = engine.Runner()
    try:
        r.start("dry" if args.dry_run else "trade", confirm_real=args.live)
    except BrokerError as e:
        if str(e) == "REAL_MONEY_CONFIRM":
            sys.exit("The active platform uses REAL MONEY. Add --live to confirm.")
        raise
    try:
        while r.running:
            time.sleep(1)
    except KeyboardInterrupt:
        r.stop()
        still = [t["symbol"] for t in engine.open_trades(engine.load_state(), r.account_id)]
        if still:
            print("STILL OPEN: %s. Run 'python3 bot.py close-all' to exit now." % ", ".join(still))


def main():
    ap = argparse.ArgumentParser(description="Halal stock trading bot.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("screen"); p.add_argument("symbols", nargs="*"); p.add_argument("--refresh", action="store_true")
    for name in ("platforms", "check", "status", "close-all"):
        sub.add_parser(name)
    p = sub.add_parser("close"); p.add_argument("symbols", nargs="+")
    p = sub.add_parser("once"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--now")
    p = sub.add_parser("run"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--live", action="store_true", help="confirm trading with real money")
    args = ap.parse_args()
    try:
        {"screen": cmd_screen, "platforms": cmd_platforms, "check": cmd_check, "status": cmd_status,
         "close": cmd_close, "close-all": cmd_close, "once": cmd_once, "run": cmd_run}[args.cmd](args)
    except BrokerError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
