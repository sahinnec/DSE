#!/usr/bin/env python3
"""DSE Scout data builder.

Fetches DSE prices/history (via the `bdshare` library), computes technical
fields (ATR14, supports, stop, target, turnover, SMA50/200, circuit flags),
tries to read fundamentals from each company page, and writes docs/data.json.

SAFE-FAIL RULE: anything that cannot be fetched or parsed is left as null.
The HTML model treats null as "incomplete" and refuses to produce a buy plan.
Human-only checks (audit, news, bank/insurance review) are NEVER auto-set.
"""
import json, math, os, sys, time, datetime as dt, traceback
import numpy as np
import pandas as pd

PRICE_MIN, PRICE_MAX = 2.0, 20.0
HIST_DAYS = 420            # calendar days of history (needs ~200 sessions for SMA200)
TICK = 0.1                 # DSE tick size is 0.1 for all prices (verify with broker)
OUT = os.environ.get("OUT", os.path.join(os.path.dirname(__file__), "..", "docs", "data.json"))
DEBUG_DIR = os.environ.get("DEBUG_DIR", os.path.join(os.path.dirname(__file__), "..", "docs", "debug"))
MAX_COMPANIES = int(os.environ.get("MAX_COMPANIES", "400"))

DHAKA = dt.timezone(dt.timedelta(hours=6))


def now_dhaka():
    return dt.datetime.now(DHAKA)


def num(x):
    try:
        if x is None:
            return None
        if isinstance(x, str):
            x = x.replace(",", "").strip()
            if x in ("", "-", "--", "n/a", "N/A"):
                return None
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def pick_col(df, *names):
    low = {str(c).lower().strip(): c for c in df.columns}
    for n in names:
        if n in low:
            return low[n]
    return None


# ---------------------------------------------------------------- technicals
def atr14(h, l, c):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()


def swing_lows(low, k=3):
    out = []
    arr = low.values
    for i in range(k, len(arr) - k):
        w = arr[i - k:i + k + 1]
        if arr[i] == w.min():
            out.append(float(arr[i]))
    return out


def swing_highs(high, k=3):
    out = []
    arr = high.values
    for i in range(k, len(arr) - k):
        w = arr[i - k:i + k + 1]
        if arr[i] == w.max():
            out.append(float(arr[i]))
    return out


def round_tick(x, mode="nearest"):
    q = x / TICK
    if mode == "up":
        return round(math.ceil(q - 1e-9) * TICK, 2)
    if mode == "down":
        return round(math.floor(q + 1e-9) * TICK, 2)
    return round(round(q) * TICK, 2)


def cluster(levels, tol):
    """Merge levels closer than tol (keep the average), return sorted desc."""
    levels = sorted(levels, reverse=True)
    groups = []
    for v in levels:
        if groups and abs(groups[-1][-1] - v) <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def compute_technical(g, price):
    """g: one instrument's history sorted oldest->newest with open/high/low/close/volume/turnover."""
    res = {}
    n = len(g)
    res["sessions"] = n
    if n < 60:
        return res
    h, l, c = g["high"], g["low"], g["close"]
    a = atr14(h, l, c).iloc[-1]
    if not (a and a > 0):
        return res
    res["atr14"] = round(float(a), 3)
    res["sma50"] = round(float(c.tail(50).mean()), 3)
    res["sma200"] = round(float(c.tail(200).mean()), 3) if n >= 200 else None
    t = g["turnover"]
    res["avgTurnover20"] = round(float(t.tail(20).mean()), 0)
    res["lastTurnover"] = round(float(t.iloc[-1]), 0)

    # corporate-action heuristic: DSE circuit is ~10%, so a bigger one-day move
    # in the window means the series is probably NOT adjusted.
    rets = c.pct_change().dropna().tail(120)
    res["historyAdjusted"] = bool((rets.abs() <= 0.115).all())
    # recent lower-circuit style day (close at/near low with >=9% fall) in last 5 sessions
    r5 = c.pct_change().tail(5)
    res["circuitRecent"] = bool(((r5 <= -0.09)).any())

    # supports from swing lows in last 120 sessions
    lows = swing_lows(l.tail(120), 3)
    lv = cluster(lows, a * 0.8)
    near = [x for x in lv if x <= price + a * 0.5]
    near = sorted(near, reverse=True)[:3]
    if len(near) == 3:
        s1, s2, s3 = [round_tick(x, "down") for x in near]
        if s1 > s2 > s3:
            stop = round_tick(s3 - 0.5 * a, "down")
            highs = [x for x in swing_highs(h.tail(120), 3) if x > price + a]
            target = round_tick(min(highs), "down") if highs else round_tick(float(h.tail(120).max()), "down")
            if stop > 0:
                res.update(support1=s1, support2=s2, support3=s3, stop=stop, target=target, levelsVerified=True)

    # bounce confirmation: last two closes >= support1, last close up, a low touched zone 1 in 5 sessions
    if "support1" in res:
        z_high = res["support1"] + 0.5 * a
        touched = bool((l.tail(5) <= z_high).any())
        held = bool((c.tail(2) >= res["support1"]).all()) and c.iloc[-1] > c.iloc[-2]
        res["bounceConfirmed"] = bool(touched and held)
    else:
        res["bounceConfirmed"] = False
    res["tick"] = TICK
    res["lot"] = 1          # market lot differs by security; broker must verify
    return res


# --------------------------------------------------------------- fundamentals
def walk(obj, path=""):
    """Yield (path, key, value) for every dict key, recursing dicts/lists. DataFrames are expanded."""
    if isinstance(obj, pd.DataFrame):
        yield path, "__df__", obj
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from walk(v, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            yield from walk(v, path + f"[{i}]")


def find_scalar(d, names):
    names = [n.lower() for n in names]
    for _, k, v in walk(d):
        if isinstance(k, str) and k.lower() in names and not isinstance(v, (dict, list, pd.DataFrame)):
            if v not in (None, ""):
                return v
    return None


def nth_row_value(df, col_names, n=1, year_cols=("year", "period", "yearend", "year_end", "date")):
    """Value from the n-th most recent row (n=1 -> second newest)."""
    col = pick_col(df, *col_names)
    if col is None:
        return None
    d = df.copy()
    ycol = pick_col(d, *year_cols)
    if ycol is not None:
        d = d.sort_values(ycol, ascending=False)
    vals = [num(v) for v in d[col].tolist()]
    vals = [v for v in vals if v is not None]
    return vals[n] if len(vals) > n else None


def latest_row_value(df, col_names, year_cols=("year", "period", "yearend", "year_end", "date")):
    col = pick_col(df, *col_names)
    if col is None:
        return None, None
    d = df.copy()
    ycol = pick_col(d, *year_cols)
    if ycol is not None:
        d = d.sort_values(ycol, ascending=False)
    for _, r in d.iterrows():
        v = num(r[col])
        if v is not None:
            return v, (str(r[ycol]) if ycol is not None else None)
    return None, None


def parse_fundamentals(details):
    out = {}
    out["category"] = (find_scalar(details, ["marketCategory", "category", "market_category"]) or "")
    if isinstance(out["category"], str):
        out["category"] = out["category"].strip().upper()[:1] or None
    out["sector"] = find_scalar(details, ["sector", "sectorName"])
    out["faceValue"] = num(find_scalar(details, ["faceValue", "face_value", "facevalue"]))
    nav = eps = prior = ocf = None
    fin_date = None
    for _, k, v in walk(details):
        if k == "__df__":
            if nav is None:
                nav, _d = latest_row_value(v, ["nav", "navpershare", "nav_per_share", "netassetvaluepershare", "nav per share"])
            if eps is None:
                eps, fin_date = latest_row_value(v, ["eps", "basiceps", "basic_eps", "epsbasic", "eps_basic", "earningspershare"])
                if eps is not None:
                    prior = nth_row_value(v, ["eps", "basiceps", "basic_eps", "epsbasic", "eps_basic", "earningspershare"], 1)
            if ocf is None:
                ocf, _o = latest_row_value(v, ["nocfps", "ocfps", "operatingcashflowpershare", "net operating cash flow per share"])
    out["nav"], out["ttmEPS"], out["_epsPeriod"] = nav, eps, fin_date
    out["priorEPS"], out["ocfps"] = prior, ocf
    # dividends: look for a df with cash dividend column
    cash = None
    for _, k, v in walk(details):
        if k == "__df__":
            c, _p = latest_row_value(v, ["cashdividend", "cash_dividend", "cashdividendpct", "cash", "cash_dividend_pct"])
            if c is not None:
                cash = c
                break
    out["cashDividendPct"] = cash
    return out


def parse_year_end(s):
    if not s:
        return None
    s = str(s)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%b %d, %Y", "%Y"):
        try:
            d = dt.datetime.strptime(s[:10] if fmt != "%Y" else s[:4], fmt).date()
            if fmt == "%Y":
                d = dt.date(d.year, 6, 30)   # assume FY end if only year given (marked in notes)
            return d.isoformat()
        except Exception:
            continue
    return None


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if not math.isfinite(float(o)) else float(o)
    return o


# ------------------------------------------------------------------- main
def main(bd=None):
    if bd is None:
        import bdshare as bd
    os.makedirs(DEBUG_DIR, exist_ok=True)
    meta = {"generatedAt": now_dhaka().isoformat(timespec="seconds"), "source": "dse.com.bd via bdshare",
            "warnings": [], "counts": {}}
    # 1. live/closing prices
    px = bd.get_current_trade_data()
    px.columns = [str(c).lower() for c in px.columns]
    sym = pick_col(px, "symbol", "code", "trading_code")
    close = pick_col(px, "close", "ltp")
    px["_p"] = pd.to_numeric(px.get("close"), errors="coerce")
    px["_p"] = px["_p"].where(px["_p"] > 0, pd.to_numeric(px.get("ltp"), errors="coerce"))
    cand = px[(px["_p"] >= PRICE_MIN) & (px["_p"] <= PRICE_MAX)][[sym, "_p"]].dropna()
    cand.columns = ["code", "price"]
    meta["counts"]["allInstruments"] = int(len(px))
    meta["counts"]["inPriceRange"] = int(len(cand))
    codes = cand["code"].astype(str).str.upper().tolist()[:MAX_COMPANIES]

    # 2. history for everything in one go (paged by the library)
    end = now_dhaka().date()
    start = end - dt.timedelta(days=HIST_DAYS)
    hist = bd.get_historical_data(str(start), str(end))
    hist = hist.reset_index()
    hist.columns = [str(c).lower() for c in hist.columns]
    hsym = pick_col(hist, "symbol", "code", "trading_code")
    for col in ("open", "high", "low", "close", "ltp", "volume", "value", "ycp"):
        if col in hist:
            hist[col] = pd.to_numeric(hist[col], errors="coerce")
    hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
    hist = hist.dropna(subset=["date"])
    # DSE reports 'value' in million taka; verify against volume*close
    hist["turnover"] = hist["volume"] * hist["close"]
    if "value" in hist:
        ratio = (hist["value"] * 1e6 / (hist["volume"] * hist["close"]).replace(0, np.nan)).median()
        meta["valueUnitCheck"] = None if not np.isfinite(ratio) else round(float(ratio), 3)
        if np.isfinite(ratio) and 0.5 < ratio < 2:
            hist["turnover"] = hist["value"] * 1e6          # value was in million taka
        elif np.isfinite(ratio) and 0.5e-6 < ratio < 2e-6:
            hist["turnover"] = hist["value"]                # value already in taka
        else:
            meta["warnings"].append("value unit unclear; turnover = volume*close")
    else:
        meta["warnings"].append("value column missing; turnover = volume*close")
    hist = hist.sort_values("date")
    last_session = hist["date"].max().date().isoformat()
    meta["asOf"] = last_session

    # 3. P/E table (cross-check only)
    try:
        pe = bd.get_latest_pe()
        meta["peRows"] = int(len(pe))
    except Exception as e:
        meta["warnings"].append("P/E table failed: %s" % e)

    rows, fund_ok = [], 0
    groups = {k: v for k, v in hist.groupby(hsym) if str(k).upper() in set(codes)}
    for i, code in enumerate(codes):
        g = groups.get(code)
        if g is None:
            g = groups.get(code.lower())
        row = {"code": code, "name": "", "type": "Equity", "origin": "auto",
               "sourceUrl": "https://www.dse.com.bd/company/" + code}
        price = float(cand.loc[cand["code"].str.upper() == code, "price"].iloc[0])
        row["price"] = price
        if g is not None and len(g):
            lastrow = g.iloc[-1]
            row["priceDate"] = lastrow["date"].date().isoformat()
            if abs(float(lastrow["close"]) - price) > 1e-6 and row["priceDate"] == last_session:
                price = float(lastrow["close"]); row["price"] = price
            row.update(compute_technical(g.set_index("date")[["open", "high", "low", "close", "volume", "turnover"]], price))
            row["closeVerified"] = row["priceDate"] == last_session
            row["trading"] = bool(float(lastrow.get("volume") or 0) > 0 and row["priceDate"] == last_session)
        else:
            row["priceDate"] = None
        # fundamentals (best effort)
        try:
            det = bd.get_company_details(code)
            if i < 3:
                with open(os.path.join(DEBUG_DIR, f"company_{code}.json"), "w", encoding="utf-8") as f:
                    json.dump({k: (v.to_dict("records") if isinstance(v, pd.DataFrame) else v) for k, v in det.items()},
                              f, ensure_ascii=False, default=str, indent=1)
            fund = parse_fundamentals(det)
            row["name"] = str(find_scalar(det, ["name", "companyName", "company_name"]) or "")
            fd = parse_year_end(fund.pop("_epsPeriod", None))
            for k, v in fund.items():
                if v is not None:
                    row[k] = v
            if fd:
                row["financialDate"] = fd
            if fund.get("nav") is not None and fund.get("ttmEPS") is not None:
                fund_ok += 1
        except Exception as e:
            row["_fundError"] = str(e)[:120]
        row["reviewDate"] = None          # human reviews set this in the browser
        rows.append(row)
        time.sleep(0.25)

    meta["counts"]["rows"] = len(rows)
    meta["counts"]["withLevels"] = sum(1 for r in rows if "support1" in r)
    meta["counts"]["withFundamentals"] = fund_ok
    if fund_ok == 0 and rows:
        meta["warnings"].append("No fundamentals parsed: check docs/debug/company_*.json and adjust parse_fundamentals().")
    out = {"version": 1, "meta": meta, "rows": rows}
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(clean(out), f, ensure_ascii=False, allow_nan=False, default=str)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return out


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
