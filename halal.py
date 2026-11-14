"""Halal (Shariah) stock screen.

Two gates, both must pass before the bot may trade a ticker:

1. Business activity  - the company's SEC industry code (SIC) and a block list
   rule out conventional finance, insurance, alcohol, tobacco, gambling,
   pork, adult/entertainment media and (optionally) weapons.
2. Financial ratios   - AAOIFI Shariah Standard 21 style:
     interest-bearing debt            / market cap  < 30 %
     cash + interest-bearing holdings / market cap  < 30 %
     interest (impermissible) income  / revenue     <  5 %

Data: SEC EDGAR (company filings, free) and Yahoo's chart endpoint (price).
A ticker that cannot be verified is UNKNOWN and is NOT traded.

This is an automated approximation, not a fatwa. Check anything that matters
against a certified screener (Zoya, Musaffa, Islamicly) or your own scholar,
and use "always_block" / "always_allow" in config.json to overrule it.
"""
import datetime as dt
import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CACHE = os.path.join(DATA, "halal_cache.json")
TICKERS = os.path.join(DATA, "sec_tickers.json")
BROWSER_UA = "Mozilla/5.0 (compatible; halal-stock-bot/1.0)"

# (low, high, reason) - SEC SIC industry codes that fail the business screen.
SIC_BLOCK = [
    (2011, 2013, "meat packing (pork / non-zabiha)"),
    (2082, 2085, "alcoholic beverages"),
    (2100, 2199, "tobacco"),
    (4830, 4841, "broadcasting / cable entertainment"),
    (5180, 5182, "alcohol wholesale"),
    (5194, 5194, "tobacco wholesale"),
    (5813, 5813, "bars / drinking places"),
    (5921, 5921, "liquor stores"),
    (5993, 5993, "tobacco stores"),
    (6000, 6199, "conventional banking / lending (riba)"),
    (6200, 6289, "brokerage / conventional investment services"),
    (6300, 6411, "conventional insurance"),
    (6710, 6726, "investment / holding offices"),
    (7011, 7011, "hotels and casino hotels"),
    (7800, 7841, "motion pictures / video entertainment"),
    (7990, 7999, "gambling / amusement"),
]
SIC_WEAPONS = [
    (3480, 3489, "ordnance / weapons"),
    (3760, 3769, "guided missiles"),
    (3795, 3795, "tanks / military vehicles"),
    (3812, 3812, "defense electronics"),
]

DEBT_LT = ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
           "DebtAndCapitalLeaseObligations", "DebtInstrumentCarryingAmount", "SeniorNotes",
           "NotesPayable", "ConvertibleNotesPayable", "LongTermNotesPayable"]
DEBT_LT_NONCUR = ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermNotesAndLoans",
                  "ConvertibleNotesPayableNoncurrent", "ConvertibleDebtNoncurrent"]
DEBT_LT_CUR = ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "NotesPayableCurrent", "DebtCurrent",
               "ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"]
DEBT_ST = ["ShortTermBorrowings", "CommercialPaper", "OtherShortTermBorrowings",
           "LineOfCredit", "ShortTermBankLoansAndNotesPayable"]
DEBT_TOTAL = ["DebtLongtermAndShorttermCombinedAmount"]
INTEREST_PAID = ["InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating",
                 "InterestPaidNet", "InterestPaid", "InterestExpenseLongTermDebt",
                 "InterestExpenseBorrowings", "InterestCostsIncurred"]
CASH = ["CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"]
CASH_AND_ST = ["CashCashEquivalentsAndShortTermInvestments"]
INV_ST = ["ShortTermInvestments", "MarketableSecuritiesCurrent",
          "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "DebtSecuritiesAvailableForSaleCurrent",
          "OtherShortTermInvestments"]
INV_LT = ["MarketableSecuritiesNoncurrent", "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
          "DebtSecuritiesAvailableForSaleNoncurrent", "LongTermInvestments",
          "HeldToMaturitySecuritiesNoncurrent"]
REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
           "SalesRevenueGoodsNet"]
INTEREST_INCOME = ["InvestmentIncomeInterest", "InvestmentIncomeInterestAndDividend",
                   "InterestAndOtherIncome", "InterestIncomeOther", "InvestmentIncomeNet",
                   "InterestAndDividendIncomeOperating", "InterestIncomeOperating",
                   "InterestIncomeExpenseNonoperatingNet", "InterestIncomeExpenseNet"]


def _get(url, ua, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _days_old(iso):
    try:
        return (dt.date.today() - dt.date.fromisoformat(iso[:10])).days
    except (TypeError, ValueError):
        return 10 ** 6


def _cik_map(ua):
    cached = _load(TICKERS, {})
    if cached.get("map") and _days_old(cached.get("fetched")) < 7:
        return cached["map"]
    raw = json.loads(_get("https://www.sec.gov/files/company_tickers.json", ua))
    m = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
    _save(TICKERS, {"fetched": dt.date.today().isoformat(), "map": m})
    return m


def _price(symbol):
    sym = symbol.upper().replace(".", "-")
    for host in ("query2", "query1"):
        try:
            d = json.loads(_get("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1d"
                                % (host, sym), BROWSER_UA, 20))
            return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
        except Exception:
            continue
    return None


def _entries(facts, tag, unit="USD", ns="us-gaap"):
    return facts.get(ns, {}).get(tag, {}).get("units", {}).get(unit, [])


def _instant(facts, tags, end):
    """Largest value among `tags` reported for the balance-sheet date `end`."""
    best = None
    for t in tags:
        vals = [e["val"] for e in _entries(facts, t) if e.get("end") == end and "start" not in e]
        if vals:
            v = vals[-1]
            best = v if best is None else max(best, v)
    return best


def _annual(facts, tags, end=None):
    """Latest full-year value: (end, value). With `end`, the largest for that year."""
    best = None
    for t in tags:
        for e in _entries(facts, t):
            if "start" not in e:
                continue
            try:
                days = (dt.date.fromisoformat(e["end"]) - dt.date.fromisoformat(e["start"])).days
            except ValueError:
                continue
            if not 350 <= days <= 380:
                continue
            if end and e["end"] != end:
                continue
            key = (e["end"], abs(e["val"]))
            if best is None or key > best[0]:
                best = (key, e["val"])
    return (best[0][0], best[1]) if best else (None, None)


def _shares(facts):
    cands = []
    for ns, tag in (("dei", "EntityCommonStockSharesOutstanding"),
                    ("us-gaap", "CommonStockSharesOutstanding"),
                    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
                    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding")):
        es = [e for e in _entries(facts, tag, "shares", ns) if _days_old(e.get("end")) < 400]
        if es:
            last_end = max(e["end"] for e in es)
            cands.append(max(e["val"] for e in es if e["end"] == last_end))
    return max(cands) if cands else None


def _sic_reason(sic, exclude_weapons):
    table = SIC_BLOCK + (SIC_WEAPONS if exclude_weapons else [])
    for lo, hi, why in table:
        if lo <= sic <= hi:
            return why
    return None


def screen(symbol, cfg):
    """Screen one ticker from live data. Returns a dict with status HALAL / NOT_HALAL / UNKNOWN."""
    h = cfg.get("halal", {})
    ua = cfg.get("sec_user_agent") or "halal-stock-bot/1.0 (personal research script)"
    sym = symbol.upper()
    out = {"symbol": sym, "checked": dt.date.today().isoformat(), "status": "UNKNOWN",
           "reasons": [], "notes": []}

    def done(status, reason=None):
        out["status"] = status
        if reason:
            out["reasons"].append(reason)
        return out

    if sym in [s.upper() for s in h.get("always_block", [])]:
        return done("NOT_HALAL", "on your always_block list")
    if sym in [s.upper() for s in h.get("always_allow", [])]:
        out["notes"].append("allowed by your always_allow list - automatic screen skipped")
        return done("HALAL")

    try:
        cik = _cik_map(ua).get(sym) or _cik_map(ua).get(sym.replace(".", "-"))
        if not cik:
            return done("UNKNOWN", "not found in SEC filings (ETF, foreign listing or new ticker)")
        sub = json.loads(_get("https://data.sec.gov/submissions/CIK%010d.json" % cik, ua))
        time.sleep(0.15)
        out["name"] = sub.get("name")
        sic = int(sub.get("sic") or 0)
        out["sic"] = sic
        out["industry"] = sub.get("sicDescription")
        why = _sic_reason(sic, h.get("exclude_weapons", True))
        if why:
            return done("NOT_HALAL", "business: %s (SIC %d %s)" % (why, sic, out["industry"]))
        if not sic:
            return done("UNKNOWN", "no industry code on file")
        facts = json.loads(_get("https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % cik, ua)
                           ).get("facts", {})
        time.sleep(0.15)
    except Exception as e:  # network or parse problem: never trade on a guess
        return done("UNKNOWN", "could not read SEC data: %s" % e)

    assets = [e for e in _entries(facts, "Assets") if "start" not in e]
    if not assets:
        return done("UNKNOWN", "no US-GAAP balance sheet (foreign filer?)")
    end = max(e["end"] for e in assets)
    if _days_old(end) > 400:
        return done("UNKNOWN", "latest balance sheet is stale (%s)" % end)
    total_assets = _instant(facts, ["Assets"], end)

    lt_whole = _instant(facts, DEBT_LT, end) or 0
    lt_parts = (_instant(facts, DEBT_LT_NONCUR, end) or 0) + (_instant(facts, DEBT_LT_CUR, end) or 0)
    debt = max(_instant(facts, DEBT_TOTAL, end) or 0,
               max(lt_whole, lt_parts) + (_instant(facts, DEBT_ST, end) or 0))
    debt += _instant(facts, ["FinanceLeaseLiability"], end) or 0
    # Cross-check: companies tag debt in many ways. If the interest they pay is
    # too large for the debt we found, we missed some - assume the worst case.
    _, paid = _annual(facts, INTEREST_PAID)
    paid = abs(paid or 0)
    if paid and debt < paid * 8:
        debt = paid * 25
        out["notes"].append("debt not clearly reported - estimated high from interest paid")
    elif debt == 0:
        out["notes"].append("no debt reported at %s" % end)

    cash = _instant(facts, CASH, end) or 0
    liquid = max(_instant(facts, CASH_AND_ST, end) or 0, cash + (_instant(facts, INV_ST, end) or 0))
    liquid += _instant(facts, INV_LT, end) or 0

    rev_end, revenue = _annual(facts, REVENUE)
    interest = None
    if rev_end:
        _, interest = _annual(facts, INTEREST_INCOME, rev_end)

    price, shares = _price(sym), _shares(facts)
    mcap = price * shares if price and shares else None
    basis = h.get("ratio_basis", "market_cap")
    denom = mcap if basis == "market_cap" else total_assets
    if not denom or denom <= 0:
        return done("UNKNOWN", "could not work out the %s" % basis.replace("_", " "))

    out.update({
        "balance_date": end, "basis": basis, "market_cap": mcap, "total_assets": total_assets,
        "debt": debt, "liquid": liquid, "revenue": revenue, "interest_income": interest,
        "debt_pct": round(100.0 * debt / denom, 2),
        "liquid_pct": round(100.0 * liquid / denom, 2),
        "impure_pct": (round(100.0 * abs(interest) / revenue, 2)
                       if interest is not None and revenue and revenue > 0 else None),
    })

    max_debt, max_liq, max_imp = (h.get("max_debt_pct", 30), h.get("max_liquid_pct", 30),
                                  h.get("max_impure_income_pct", 5))
    if out["debt_pct"] >= max_debt:
        out["reasons"].append("debt is %.1f%% of %s (limit %s%%)"
                              % (out["debt_pct"], basis.replace("_", " "), max_debt))
    if out["liquid_pct"] >= max_liq:
        out["reasons"].append("cash + interest-bearing holdings are %.1f%% of %s (limit %s%%)"
                              % (out["liquid_pct"], basis.replace("_", " "), max_liq))
    if out["impure_pct"] is None:
        if h.get("require_income_data", False):
            out["reasons"].append("interest income not reported separately")
            return done("UNKNOWN")
        out["notes"].append("interest income not reported separately - income test skipped")
    elif out["impure_pct"] >= max_imp:
        out["reasons"].append("interest income is %.1f%% of revenue (limit %s%%)"
                              % (out["impure_pct"], max_imp))
    return done("NOT_HALAL" if out["reasons"] else "HALAL")


def check(symbol, cfg, refresh=False):
    """Cached screen. Results are reused for `cache_days` (default 7)."""
    sym = symbol.upper()
    cache = _load(CACHE, {})
    h = cfg.get("halal", {})
    hit = cache.get(sym)
    overruled = sym in [s.upper() for s in h.get("always_block", []) + h.get("always_allow", [])]
    if hit and not refresh and not overruled and _days_old(hit.get("checked")) < h.get("cache_days", 7):
        return hit
    res = screen(sym, cfg)
    if not overruled:
        cache = _load(CACHE, {})
        cache[sym] = res
        _save(CACHE, cache)
    return res


def is_halal(symbol, cfg):
    return check(symbol, cfg)["status"] == "HALAL"


def table(results):
    lines = ["%-6s %-10s %7s %7s %7s  %s" % ("TICKER", "VERDICT", "debt%", "cash%", "impure%", "why / notes")]
    for r in results:
        f = lambda k: "-" if r.get(k) is None else "%.1f" % r[k]
        lines.append("%-6s %-10s %7s %7s %7s  %s" % (
            r["symbol"], r["status"], f("debt_pct"), f("liquid_pct"), f("impure_pct"),
            "; ".join(r.get("reasons") or r.get("notes") or [r.get("industry") or ""])))
    return "\n".join(lines)
