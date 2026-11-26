"""
Gold two-window bot for the Alpaca PAPER account (GLD).

Gold moves around the clock: Asia (from 18:00 ET), London (from ~03:00 ET), New York.
Testing (17 Nov 2026) showed only two windows with an edge, so the bot trades only those:

  Window 1  LONDON-LATE  07:00 -> 09:15 ET  (GLD pre-market, limit orders only)
            Votes: Asia move (PAXG 18:00->03:00), early London (GLD 04:00->07:00), gap vs
            yesterday's close. 2 of 3 must agree.
  Window 2  NY-MORNING   09:30 -> 11:00 ET  (regular hours)
            Votes: Asia, London (04:00->09:15), gap, price vs pre-market VWAP, last 30 min.
            3 of 5 must agree.

Each trade has a 1% protective stop. Everything is closed by the window end; never overnight.
Paper only: the trading URL is hard-coded to paper-api.

Usage:
  python gold_bot.py --check          connect, show account and next session
  python gold_bot.py --backtest 63    replay both windows on the last 63 sessions (~90 days)
  python gold_bot.py --dry-run        run today's routine without placing orders
  python gold_bot.py                  run today's routine on the paper account
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAPER_URL = "https://paper-api.alpaca.markets"   # paper only, never change
DATA_URL = "https://data.alpaca.markets"

# ---- Settings ---------------------------------------------------------------
SYMBOL = "GLD"
LDN_LEVERAGE = 2.0         # pre-market size, x equity (thinner market, keep smaller)
AM_LEVERAGE = 4.0          # regular-hours size, x equity (Alpaca intraday max is 4x)
AM_TRADE = False           # NY window: False = watch only (logs what it would have made, no orders).
                           # Backtest 17 Nov: -3.5% (90d) and -3.4% (180d) unleveraged, so off until it proves itself.
STOP_PCT = 1.0             # protective stop, % from entry
LDN_COST_PCT = 0.05        # assumed round-trip cost in backtests
AM_COST_PCT = 0.02
MAX_DRAWDOWN_PCT = 25.0    # halt if equity falls this far below the starting equity
GOAL_MULTIPLE = 2.0
# -----------------------------------------------------------------------------

JOURNAL = HERE / "journal.csv"
STATE = HERE / "state.json"
LOG_DIR = HERE / "logs"
KEY = SECRET = ""
_log_file = None
JOURNAL_FIELDS = ["date", "window", "score", "signals", "side", "qty", "entry", "exit",
                  "outcome", "pnl", "equity_after", "progress_x"]


# ------------------------------------------------------------------ utilities
def log(msg):
    # Always Trinidad time (AST, UTC-4, no DST), whether on the laptop or a cloud runner set to UTC
    line = f"{datetime.now(timezone(timedelta(hours=-4))).strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    if _log_file:
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_env():
    global KEY, SECRET
    # Cloud runner (GitHub Actions): keys come from repository secrets as environment variables
    KEY = os.environ.get("ALPACA_API_KEY", "").strip()
    SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if KEY and SECRET:
        return
    # Laptop: keys live outside Google Drive so they never sync: %USERPROFILE%\.alpaca\gold_bot.env
    env = Path.home() / ".alpaca" / "gold_bot.env"
    if not env.exists():
        env = HERE / ".env"
    if not env.exists():
        sys.exit("Missing keys: put ALPACA_API_KEY / ALPACA_SECRET_KEY (paper) in %USERPROFILE%\\.alpaca\\gold_bot.env")
    for raw in env.read_text(encoding="utf-8-sig").splitlines():
        if "=" in raw and not raw.strip().startswith("#"):
            k, v = raw.split("=", 1)
            if k.strip() == "ALPACA_API_KEY":
                KEY = v.strip()
            elif k.strip() == "ALPACA_SECRET_KEY":
                SECRET = v.strip()
    if not KEY or not SECRET or "paste" in KEY.lower():
        sys.exit("trading/.env is missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")


class ApiError(RuntimeError):
    def __init__(self, code, text):
        super().__init__(f"HTTP {code}: {text[:300]}")
        self.code = code


def api(method, base, path, body=None, params=None):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET,
        "Content-Type": "application/json", "Accept": "application/json"})
    for attempt in range(8):   # ~4 minutes of retries, e.g. while Wi-Fi reconnects after sleep
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 7:
                time.sleep(15)
                continue
            raise ApiError(e.code, e.read().decode(errors="replace")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 7:
                raise
            log(f"Network hiccup ({e}); retrying")
            time.sleep(30)


def trade_api(method, path, body=None, params=None):
    return api(method, PAPER_URL, path, body, params)


def now_utc():
    return datetime.now(timezone.utc)


def et_tz(d):
    """US Eastern offset for a date (DST: 2nd Sunday March to 1st Sunday November)."""
    def nth_sunday(y, m, n):
        first = date(y, m, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    dst = nth_sunday(d.year, 3, 2) <= d < nth_sunday(d.year, 11, 1)
    return timezone(timedelta(hours=-4 if dst else -5))


def et_today():
    n = now_utc()
    return n.astimezone(et_tz(n.date())).date()


def ts(bar):
    return datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bars(symbol, start, end, timeframe="5Min", feed="sip"):
    out, token = [], None
    if feed == "sip":   # free plan: SIP data must be at least 15 minutes old
        end = min(end, now_utc() - timedelta(minutes=16))
        if end <= start:
            return []
    while True:
        params = {"timeframe": timeframe, "start": iso(start), "end": iso(end),
                  "feed": feed, "limit": 10000, "adjustment": "raw"}
        if token:
            params["page_token"] = token
        res = api("GET", DATA_URL, f"/v2/stocks/{symbol}/bars", params=params)
        out += res.get("bars") or []
        token = res.get("next_page_token")
        if not token:
            return out


def paxg_bars(start, end):
    out, token = [], None
    while True:
        p = {"symbols": "PAXG/USD", "timeframe": "1Hour", "start": iso(start), "end": iso(end), "limit": 10000}
        if token:
            p["page_token"] = token
        r = api("GET", DATA_URL, "/v1beta3/crypto/us/bars", params=p)
        out += (r.get("bars") or {}).get("PAXG/USD", [])
        token = r.get("next_page_token")
        if not token:
            return out


def px_at(bs, dt):
    """Last close of a bar starting at or before dt (within 3 hours)."""
    c = [b["c"] for b in bs if dt - timedelta(hours=3) <= ts(b) <= dt]
    return float(c[-1]) if c else None


def vwap(bs):
    v = sum(b["v"] for b in bs)
    return sum(b["c"] * b["v"] for b in bs) / v if v else None


def pct(a, b):
    return (b - a) / a * 100


def sgn(x, thr=0.0):
    return 1 if x > thr else -1 if x < -thr else 0


def session_open(d):
    cal = trade_api("GET", "/v2/calendar", params={"start": d.isoformat(), "end": d.isoformat()})
    if not cal or cal[0]["date"] != d.isoformat():
        return None
    return datetime.combine(d, dtime.fromisoformat(cal[0]["open"]), et_tz(d))


def prev_close(before_open):
    daily = bars(SYMBOL, before_open - timedelta(days=10),
                 before_open - timedelta(hours=9, minutes=30), "1Day")
    return float(daily[-1]["c"]) if daily else None


# ------------------------------------------------------------ the signals
def ldn_score(asia_pct, p4, p7, pc):
    votes = {"asia": sgn(asia_pct, 0.05), "early_london": sgn(pct(p4, p7), 0.05), "gap": sgn(pct(pc, p7), 0.1)}
    s = sum(votes.values())
    return (s if abs(s) >= 2 else 0), votes


def am_score(asia_pct, p4, p915, pc, pm_vwap, p845):
    votes = {"asia": sgn(asia_pct, 0.05), "london": sgn(pct(p4, p915), 0.05), "gap": sgn(pct(pc, p915), 0.1),
             "vs_vwap": sgn(p915 - pm_vwap) if pm_vwap else 0, "last30": sgn(pct(p845, p915), 0.02)}
    s = sum(votes.values())
    return (s if abs(s) >= 3 else 0), votes


def asia_move(pax, d):
    tz = et_tz(d)
    a0 = px_at(pax, datetime.combine(d - timedelta(days=1), dtime(18), tz))
    a1 = px_at(pax, datetime.combine(d, dtime(3), tz))
    return pct(a0, a1) if a0 and a1 else 0.0


# ------------------------------------------------------------- backtest
def walk(bs, start, end, side, entry):
    """Exit price/outcome between start and end with the protective stop (5-min bars)."""
    stop = entry * (1 - side * STOP_PCT / 100)
    last = entry
    for b in bs:
        t = ts(b)
        if t < start:
            continue
        if t >= end:
            break
        if (side == 1 and b["l"] <= stop) or (side == -1 and b["h"] >= stop):
            return stop, "stop"
        last = b["c"]
    return last, "time"


def backtest(n):
    today = et_today()
    cal = trade_api("GET", "/v2/calendar", params={
        "start": (today - timedelta(days=int(n * 1.6) + 15)).isoformat(),
        "end": (today - timedelta(days=1)).isoformat()})[-n:]
    first = date.fromisoformat(cal[0]["date"])
    t0 = datetime.combine(first - timedelta(days=5), dtime(), timezone.utc)
    t1 = datetime.combine(today, dtime(), timezone.utc)
    pax = paxg_bars(t0, t1)
    closes = {b["t"][:10]: float(b["c"]) for b in bars(SYMBOL, t0, t1, "1Day")}

    res = {"LDN": [], "AM": []}
    order = []
    for s in cal:
        d = date.fromisoformat(s["date"])
        tz = et_tz(d)
        o = datetime.combine(d, dtime.fromisoformat(s["open"]), tz)
        at = lambda h, m=0: datetime.combine(d, dtime(h, m), tz)
        prior = [k for k in closes if k < s["date"]]
        pc = closes[max(prior)] if prior else None
        bs = bars(SYMBOL, at(4), o + timedelta(minutes=95))
        pm = [b for b in bs if ts(b) < o]
        if not (pc and pm):
            continue
        asia = asia_move(pax, d)
        p4 = float(pm[0]["o"])
        p7 = px_at(pm, at(7) - timedelta(minutes=5))
        p915 = px_at(pm, at(9, 15) - timedelta(minutes=5))
        if p7 and p915:
            sc, _ = ldn_score(asia, p4, p7, pc)
            if sc:
                side = sgn(sc)
                ex, why = walk(bs, at(7), at(9, 15), side, p7)
                r = side * pct(p7, ex) - LDN_COST_PCT
                res["LDN"].append(r)
                order.append((s["date"], "LDN", side, r, why))
        p845 = px_at(pm, at(8, 45) - timedelta(minutes=5))
        rth = [b for b in bs if ts(b) >= o]
        if p915 and p845 and rth:
            sc, _ = am_score(asia, p4, p915, pc, vwap([b for b in pm if ts(b) < at(9, 15)]), p845)
            if sc:
                side = sgn(sc)
                entry = float(rth[0]["o"])
                ex, why = walk(bs, o, o + timedelta(minutes=90), side, entry)
                r = side * pct(entry, ex) - AM_COST_PCT
                res["AM"].append(r)
                order.append((s["date"], "AM", side, r, why))

    print(f"{len(cal)} sessions: {cal[0]['date']} to {cal[-1]['date']}\n")
    for k, lev in (("LDN", LDN_LEVERAGE), ("AM", AM_LEVERAGE)):
        r = res[k]
        if r:
            print(f"{k:<4} trades {len(r):>3} | wins {sum(x > 0 for x in r) / len(r) * 100:4.0f}% | "
                  f"avg {sum(r) / len(r):+.3f}%/trade | total {sum(r):+.2f}% unleveraged")
    eq, peak, dd = 1.0, 1.0, 0.0
    for _, k, _, r, _ in order:
        if k == "AM" and not AM_TRADE:
            continue
        eq *= 1 + (LDN_LEVERAGE if k == "LDN" else AM_LEVERAGE) * r / 100
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    label = f"{LDN_LEVERAGE}x London" + (f" + {AM_LEVERAGE}x NY" if AM_TRADE else " (NY watch only)")
    print(f"\nAs configured, {label}: {(eq - 1) * 100:+.1f}% over {len(cal)} sessions | "
          f"worst drop {dd * 100:.1f}% | goal +100%")


# ------------------------------------------------------------ live helpers
def wait_until(dt, label):
    secs = (dt - now_utc()).total_seconds()
    if secs > 0:
        log(f"Waiting {secs / 60:.0f} min until {label} ({dt.astimezone(et_tz(dt.date())).strftime('%H:%M')} ET)")
        while (dt - now_utc()).total_seconds() > 0:
            time.sleep(min(30, max(1, (dt - now_utc()).total_seconds())))


def current_price():
    """Real-time IEX trade if recent; otherwise the latest 15-min-delayed SIP bar."""
    try:
        t = api("GET", DATA_URL, f"/v2/stocks/{SYMBOL}/trades/latest", params={"feed": "iex"})["trade"]
        if now_utc() - datetime.fromisoformat(t["t"][:19] + "+00:00") < timedelta(minutes=10):
            return float(t["p"]), "iex live"
    except (ApiError, KeyError, ValueError):
        pass
    bs = bars(SYMBOL, now_utc() - timedelta(hours=2), now_utc() - timedelta(minutes=16), "1Min")
    return (float(bs[-1]["c"]), "sip delayed") if bs else (None, "none")


def get_position():
    try:
        return trade_api("GET", f"/v2/positions/{SYMBOL}")
    except ApiError as e:
        if e.code == 404:
            return None
        raise


def wait_fill(order_id, timeout):
    end = time.time() + timeout
    while time.time() < end:
        o = trade_api("GET", f"/v2/orders/{order_id}", params={"nested": "true"})
        if o["status"] == "filled":
            return o
        if o["status"] in ("canceled", "rejected", "expired"):
            return None
        time.sleep(3)
    try:
        trade_api("DELETE", f"/v2/orders/{order_id}")
    except ApiError:
        pass
    time.sleep(2)
    o = trade_api("GET", f"/v2/orders/{order_id}")
    return o if o["status"] == "filled" else None


def limit_order(side, qty, ref, slip_pct, timeout=120):
    lim = round(ref * (1 + slip_pct / 100) if side == "buy" else ref * (1 - slip_pct / 100), 2)
    o = trade_api("POST", "/v2/orders", body={"symbol": SYMBOL, "qty": str(qty), "side": side, "type": "limit",
                                              "limit_price": f"{lim:.2f}", "time_in_force": "day",
                                              "extended_hours": True})
    return wait_fill(o["id"], timeout)


def cancel_open_orders():
    for o in trade_api("GET", "/v2/orders", params={"status": "open", "symbols": SYMBOL}):
        try:
            trade_api("DELETE", f"/v2/orders/{o['id']}")
        except ApiError:
            pass


def journal(row):
    new = not JOURNAL.exists()
    with open(JOURNAL, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})


def size(lev, price):
    acct = trade_api("GET", "/v2/account")
    eq, bp = float(acct["equity"]), float(acct["buying_power"])
    return int(min(eq * lev, bp * 0.9) // price)


def finish(row, start_eq, side, qty, entry, exit_px, outcome):
    pnl = round(side * (exit_px - entry) * qty, 2) if exit_px else ""
    eq = float(trade_api("GET", "/v2/account")["equity"])
    row.update(exit=exit_px or "", outcome=outcome, pnl=pnl, equity_after=round(eq, 2),
               progress_x=round(eq / start_eq, 4))
    journal(row)
    log(f"{row['window']} closed: {outcome} at {exit_px} | P&L ${pnl} | equity ${eq:,.2f} "
        f"({eq / start_eq:.3f}x of {GOAL_MULTIPLE}x)")


# ------------------------------------------------------------ live windows
def run_london(d, o, pc, pax, start_eq, dry):
    tz = et_tz(d)
    at = lambda h, m=0: datetime.combine(d, dtime(h, m), tz)
    row = {"date": d.isoformat(), "window": "LONDON 07:00-09:15"}
    if now_utc() > at(7, 20):
        log("London window: too late to enter today.")
        return
    wait_until(at(7), "London-late window")
    pm = bars(SYMBOL, at(4), at(4, 30))
    p7, src = current_price()
    if not pm or not p7:
        row["outcome"] = "no trade: missing pre-market data"
        journal(row)
        log(row["outcome"])
        return
    sc, votes = ldn_score(asia_move(pax, d), float(pm[0]["o"]), p7, pc)
    row.update(score=sc, signals=json.dumps(votes))
    log(f"London signals {votes} (price {p7:.2f}, {src}) -> score {sc}")
    if not sc:
        row["outcome"] = "no trade: signals split"
        journal(row)
        return
    side = sgn(sc)
    s_txt = "buy" if side == 1 else "sell"
    qty = size(LDN_LEVERAGE, p7)
    row.update(side=s_txt, qty=qty)
    log(f"London entry: {s_txt} {qty} {SYMBOL} limit near {p7:.2f}")
    if dry or qty < 1:
        row["outcome"] = "dry run" if dry else "no trade: size 0"
        journal(row)
        return
    # Pre-market price can be 15 min stale, so re-price a marketable limit up to 3 times.
    # A limit only caps the worst price; the fill happens at the real bid/ask.
    f = None
    try:
        for slip in (0.25, 0.50, 0.80):
            ref, _ = current_price()
            f = limit_order(s_txt, qty, ref or p7, slip, timeout=60)
            if f or get_position():
                break
            log(f"Entry not filled at {slip}% limit; re-pricing")
    except ApiError as e:
        row["outcome"] = f"order rejected: {e}"
        journal(row)
        log(row["outcome"])
        return
    pos = get_position()
    if not pos:
        row["outcome"] = "no trade: entry not filled"
        journal(row)
        log(row["outcome"])
        return
    entry, qty = float(pos["avg_entry_price"]), abs(int(float(pos["qty"])))   # handles partial fills
    row.update(entry=entry, qty=qty)
    log(f"London filled {s_txt} {qty} at {entry:.2f}")

    stop = entry * (1 - side * STOP_PCT / 100)
    outcome = "time exit"
    while now_utc() < at(9, 15):
        px, _ = current_price()
        if px and ((side == 1 and px <= stop) or (side == -1 and px >= stop)):
            outcome = "stop"
            break
        time.sleep(30)
    exit_side = "sell" if side == 1 else "buy"
    exit_px = None
    for slip in (0.15, 0.40, 1.00):
        pos = get_position()
        if not pos:
            break
        ref, _ = current_price()
        f = limit_order(exit_side, abs(int(float(pos["qty"]))), ref or entry, slip, timeout=90)
        if f and not get_position():
            exit_px = float(f["filled_avg_price"])
            break
    if exit_px is None and get_position():
        log("London exit limit orders did not fill; closing at the 09:30 open.")
        wait_until(o + timedelta(seconds=5), "the open")
        cancel_open_orders()
        c = trade_api("DELETE", f"/v2/positions/{SYMBOL}")
        f = wait_fill(c["id"], 60)
        exit_px = float(f["filled_avg_price"]) if f else None
        outcome += " (closed at open)"
    finish(row, start_eq, side, qty, entry, exit_px, outcome)


def run_morning(d, o, pc, pax, start_eq, dry):
    tz = et_tz(d)
    at = lambda h, m=0: datetime.combine(d, dtime(h, m), tz)
    row = {"date": d.isoformat(), "window": "NY 09:30-11:00"}
    if now_utc() > o + timedelta(minutes=5):
        log("NY window: too late to enter today.")
        return
    wait_until(at(9, 16), "NY-morning signal check")
    pm = bars(SYMBOL, at(4), at(9, 15))          # SIP is 15 min delayed: runs to ~09:00
    p915, src = current_price()
    p845 = px_at(pm, at(8, 45) - timedelta(minutes=5))
    if not pm or not p915 or not p845:
        row["outcome"] = "no trade: missing pre-market data"
        journal(row)
        log(row["outcome"])
        return
    sc, votes = am_score(asia_move(pax, d), float(pm[0]["o"]), p915, pc, vwap(pm), p845)
    row.update(score=sc, signals=json.dumps(votes))
    log(f"NY signals {votes} (price {p915:.2f}, {src}) -> score {sc}")
    if not sc:
        row["outcome"] = "no trade: signals split"
        journal(row)
        return
    if get_position():
        row["outcome"] = "no trade: GLD position still open"
        journal(row)
        log(row["outcome"])
        return
    side = sgn(sc)
    s_txt = "buy" if side == 1 else "sell"
    wait_until(o + timedelta(seconds=5), "the open")
    px, _ = current_price()
    qty = size(AM_LEVERAGE, px or p915)
    row.update(side=s_txt, qty=qty)
    log(f"NY entry: {s_txt} {qty} {SYMBOL} at market (~{px})")
    if not AM_TRADE and px:
        row.update(side=s_txt, qty="", entry=px)
        wait_until(o + timedelta(minutes=90), "NY window end (watch only)")
        px_end, _ = current_price()
        move = side * pct(px, px_end) if px_end else None
        row.update(exit=px_end or "", outcome=f"watch only: would have made {move:+.2f}% before leverage"
                   if move is not None else "watch only: no end price")
        journal(row)
        log(row["outcome"])
        return
    if dry or qty < 1:
        row["outcome"] = "dry run" if dry else "no trade: size 0"
        journal(row)
        return
    stop = (px or p915) * (1 - side * STOP_PCT / 100)
    try:
        order = trade_api("POST", "/v2/orders", body={
            "symbol": SYMBOL, "qty": str(qty), "side": s_txt, "type": "market", "time_in_force": "day",
            "order_class": "oto", "stop_loss": {"stop_price": f"{stop:.2f}"}})
    except ApiError as e:
        row["outcome"] = f"order rejected: {e}"
        journal(row)
        log(row["outcome"])
        return
    f = wait_fill(order["id"], 90)
    if not f:
        row["outcome"] = "no trade: entry not filled"
        journal(row)
        log(row["outcome"])
        return
    entry = float(f["filled_avg_price"])
    row["entry"] = entry
    log(f"NY filled {s_txt} {qty} at {entry:.2f}, stop {stop:.2f}")

    end = o + timedelta(minutes=90)
    exit_px, outcome = None, None
    while now_utc() < end:
        if not get_position():
            leg = next((l for l in (trade_api("GET", f"/v2/orders/{order['id']}",
                                              params={"nested": "true"}).get("legs") or [])
                        if l["status"] == "filled"), None)
            exit_px = float(leg["filled_avg_price"]) if leg else None
            outcome = "stop" if leg else "closed elsewhere"
            break
        time.sleep(20)
    if outcome is None:
        cancel_open_orders()
        time.sleep(2)
        c = trade_api("DELETE", f"/v2/positions/{SYMBOL}")
        f = wait_fill(c["id"], 60)
        exit_px = float(f["filled_avg_price"]) if f else None
        outcome = "time exit"
    finish(row, start_eq, side, qty, entry, exit_px, outcome)


def keep_awake():
    """Stop Windows from going back to sleep while the bot is running (released when it exits)."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass


def run(dry):
    global _log_file
    keep_awake()
    LOG_DIR.mkdir(exist_ok=True)
    d = et_today()
    _log_file = LOG_DIR / f"{d.isoformat()}.log"
    log(f"Gold bot starting ({'DRY RUN' if dry else 'PAPER'}) for {d}")

    eq = float(trade_api("GET", "/v2/account")["equity"])
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "start_equity" not in state:
        state = {"start_equity": eq, "start_date": d.isoformat()}
        STATE.write_text(json.dumps(state, indent=2))
    start_eq = state["start_equity"]
    log(f"Equity ${eq:,.2f} | start ${start_eq:,.2f} on {state['start_date']} | "
        f"{eq / start_eq:.3f}x of {GOAL_MULTIPLE}x goal")
    if eq < start_eq * (1 - MAX_DRAWDOWN_PCT / 100):
        log(f"Equity is more than {MAX_DRAWDOWN_PCT}% below start. Halted until reviewed.")
        return

    o = session_open(d)
    if not o:
        log("Market closed today.")
        return
    if get_position():
        log("A GLD position is already open that the bot didn't make today. Not trading.")
        return
    pc = prev_close(o)
    pax = paxg_bars(o - timedelta(days=2), o)
    run_london(d, o, pc, pax, start_eq, dry)
    run_morning(d, o, pc, pax, start_eq, dry)
    log("Done for today.")


def check():
    acct = trade_api("GET", "/v2/account")
    clock = trade_api("GET", "/v2/clock")
    print(f"Connected to PAPER account {acct['account_number']} ({acct['status']})")
    print(f"Equity ${float(acct['equity']):,.2f} | buying power ${float(acct['buying_power']):,.2f} | "
          f"shorting {'on' if acct.get('shorting_enabled') else 'off'}")
    print(f"Market open now: {clock['is_open']} | next open {clock['next_open']}")
    px, src = current_price()
    print(f"GLD price: {px} ({src})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Gold two-window bot (Alpaca paper)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backtest", type=int, metavar="SESSIONS")
    a = ap.parse_args()
    load_env()
    try:
        if a.check:
            check()
        elif a.backtest:
            backtest(a.backtest)
        else:
            run(a.dry_run)
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
