#!/usr/bin/env python3
"""Walk-forward copy trading backtest for Polymarket whales, on the OrcaLayer API.

Question: if you pick wallets by how well copying them worked over the last two months, does copying them work the month after?

This is the method of the write-up, run on the OrcaLayer public API instead of our internal database:
  1. List the league's resolved game markets in the period (slug prefix, e.g. mlb, nba, lol; the main "who wins" market of each
     game) and read every trade of $20 or more in them.
  2. A copy = a wallet's FIRST buy in a market: taker buys (a taker SELL in a MINT match counts as buying the other outcome at
     1 - price) and maker buys; exchange aggregator contracts are skipped. Only buys at 0.30-0.85 are copied.
  3. We pay the wallet's price + 1 cent, the sports taker fee 0.05 x p x (1 - p), stake $10 and hold to resolution.
  4. For each test month: pick wallets on the two months before (rules fixed in advance) and measure them in the test month.
     Only test months whose two pick months lie inside --start .. --end are reported. The t in the output uses standard errors
     clustered by market (copies of the same game share its result).

Rules
  ALL10  every wallet with at least 10 copies in the pick months (the average whale)
  POS    at least 20 copies, mean above zero and t of 2 or more in the pick months
  TOP10  the 10 best by t among wallets with at least 20 copies and a mean above zero
Also printed: the rank correlation (Spearman) between each wallet's pick-month and test-month result.

Usage
  export ORCALAYER_API_KEY=sk_orca_...        # Premium key, https://orcalayer.com/docs/api
  python3 copy_backtest.py --league mlb --start 2026-05-01 --end 2026-09-30
  python3 copy_backtest.py --league lol --start 2026-05-01 --end 2026-09-30 --csv lol.csv
  python3 copy_backtest.py --load-csv lol.csv --start 2026-05-01 --end 2026-09-30     # rerun the analysis, no API calls

Differences from the write-up: the write-up used every market of the league in our database; this script reads a sample of game
markets through the API (see --max-markets), so expect similar, not identical numbers. Trade data in the API starts on May 1, 2026.
Wallet flags such as "Smart Money" are deliberately not used: they are computed today, so they already know the test months.
Only the Python 3.9+ standard library is used.
"""
import argparse
import calendar
import collections
import csv
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://orcalayer.com"
# Polymarket exchange aggregator contracts appear as the taker of routed orders; they are not traders. Matched by address prefix
# (0xe1111800..., 0xe2222d27...; look them up on Polygonscan).
AGGREGATOR_PREFIXES = ("0xe1111800", "0xe2222d27")
FEE_RATE, SLIP, STAKE, MIN_USD, ZONE = 0.05, 0.01, 10.0, 20, (0.30, 0.85)


def get(path, params, key, tries=6):
    url = API + path + "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "orcalayer-copy-backtest/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After") or 5))
                continue
            if e.code in (401, 403):
                sys.exit("API key rejected (%d). A Premium key is required: https://orcalayer.com/docs/api" % e.code)
            if e.code >= 500 and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except (OSError, http.client.HTTPException, ValueError):   # timeouts, resets, cut or non-JSON responses
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    raise RuntimeError("too many retries: " + url)


def fee(p):
    return FEE_RATE * p * (1 - p)


def mean_t(v):
    n = len(v)
    if n < 2:
        return (v[0] if v else 0.0), 0.0
    mu = sum(v) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in v) / (n - 1))
    return mu, (mu / (sd / math.sqrt(n)) if sd else 0.0)


def mean_t_clustered(xs):
    """xs = [(result, market)] -> mean and t with standard errors clustered by market (copies of one game share its result)."""
    n = len(xs)
    if n < 3:
        return (sum(x for x, _ in xs) / n if n else float("nan")), float("nan")
    mu = sum(x for x, _ in xs) / n
    cl = collections.defaultdict(float)
    for x, k in xs:
        cl[k] += x - mu
    g = len(cl)
    se = math.sqrt(sum(s * s for s in cl.values()) * g / (g - 1)) / n if g > 1 else 0.0
    return mu, (mu / se if se else float("nan"))


def spearman(a, b):
    n = len(a)
    if n < 5:
        return float("nan")

    def rank(x):
        order = sorted(range(n), key=lambda i: x[i])
        r = [0.0] * n
        for j, i in enumerate(order):
            r[i] = j
        return r
    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else float("nan")


def month_add(ym, k):
    y, m = int(ym[:4]), int(ym[5:7]) + k
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return "%04d-%02d" % (y, m)


def first_buys(trades, other):
    """trades of one market -> {wallet: (ts, id, outcome, price)} for the wallet's first buy (same rule as the write-up)."""
    first = {}
    for t in trades:
        px, et = t.get("price"), t.get("entry_type")
        if not px or (t.get("usd_amount") or 0) < MIN_USD or t.get("side") not in other:
            continue
        for w, d, role in ((t.get("taker"), t.get("taker_direction"), "t"), (t.get("maker"), t.get("maker_direction"), "m")):
            if not w:
                continue
            w = w.lower()
            if role == "t":
                if w.startswith(AGGREGATOR_PREFIXES):
                    continue
                if et == "MINT" and d == "SELL":
                    tok, q = other[t["side"]], 1.0 - px
                elif et not in ("MINT", "MERGE") and d == "BUY":
                    tok, q = t["side"], px
                else:
                    continue
            elif d == "BUY" and et != "MERGE":
                tok, q = t["side"], px
            else:
                continue
            key = (t["timestamp"], t["id"])
            if w not in first or key < first[w][:2]:
                first[w] = (t["timestamp"], t["id"], tok, q)
    return first


def download(a, key):
    """-> list of copies [wallet, market_slug, first_buy_ts, price, won, result_usd_per_10] read from the API."""
    lg = a.league.strip().lower()
    game = re.compile(r"^%s-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$" % re.escape(lg))   # main "who wins" market of a game
    markets, off = [], 0
    while True:
        d = get("/api/public/v1/markets/resolved", {"slug_prefix": lg + "-", "end_from": a.start, "end_to": a.end,
                                                     "limit": 500, "offset": off}, key)
        page = d.get("markets", [])
        for m in page:
            fp = m.get("final_price_yes")
            if game.match(m.get("slug") or "") and fp is not None and (fp >= 0.99 or fp <= 0.01):
                markets.append(m)
        off += len(page)
        time.sleep(a.pause)
        if not page or not d.get("has_more"):
            break
    markets.sort(key=lambda m: m["slug"])
    if a.max_markets and len(markets) > a.max_markets:
        step = len(markets) / a.max_markets
        markets = [markets[int(i * step)] for i in range(a.max_markets)]
    print("%s resolved game markets, end date %s .. %s: reading %d" % (lg, a.start, a.end, len(markets)), file=sys.stderr)
    if a.markets_out:
        with open(a.markets_out, "w") as f:
            f.write("\n".join(m["condition_id"] for m in markets) + "\n")
    rows = []
    other = {"token1": "token2", "token2": "token1"}
    for i, m in enumerate(markets):
        winner = "token1" if m["final_price_yes"] >= 0.99 else "token2"
        last = max(calendar.timegm(time.strptime(m[k][:10], "%Y-%m-%d")) for k in ("end_date", "resolved_at") if m.get(k))
        params = {"wallets": "all", "min_usd": MIN_USD, "limit": 500, "before": last + 3 * 86400}
        trades = []
        while True:                                                   # every trade of $20+ in the market
            d = get("/api/public/v1/market/%s/whale-trades" % m["condition_id"], params, key)
            trades += d.get("trades", [])
            time.sleep(a.pause)
            if not d.get("has_more"):
                break
            params["before"], params["before_id"] = d["next_before"], d["next_before_id"]
        for w, (ts, _, tok, q) in first_buys(trades, other).items():
            if not ZONE[0] <= q <= ZONE[1]:
                continue
            p = q + SLIP
            res = STAKE / (p + fee(p)) * (1.0 if tok == winner else 0.0) - STAKE
            rows.append([w, m["slug"], ts, round(q, 4), tok == winner, round(res, 4)])
        if (i + 1) % 100 == 0:
            print("  %d / %d markets, copies %d" % (i + 1, len(markets), len(rows)), file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--league", default="mlb", help="market slug prefix: mlb, nba, nfl, lol, val, ... (default mlb)")
    ap.add_argument("--start", default="2026-05-01", help="first market end date, YYYY-MM-DD (API trade data starts on 2026-05-01)")
    ap.add_argument("--end", default="2026-09-30", help="last market end date, YYYY-MM-DD")
    ap.add_argument("--max-markets", type=int, default=800, help="game markets to read, spread evenly over the period (0 = all)")
    ap.add_argument("--pause", type=float, default=0.15, help="seconds between requests (default 0.15)")
    ap.add_argument("--csv", help="write one row per copy (wallet, market, time, price, result) to this file")
    ap.add_argument("--load-csv", help="skip the API and rerun the analysis on copies saved earlier with --csv")
    ap.add_argument("--markets-out", help="write the list of markets read to this file")
    a = ap.parse_args()
    if a.load_csv:
        with open(a.load_csv, newline="") as f:
            rows = [[r["wallet"], r["market_slug"], int(r["first_buy_ts"]), float(r["price"]), r["won"] == "True",
                     float(r["result_usd_per_10"])] for r in csv.DictReader(f)]
        print("copies loaded from %s: %d" % (a.load_csv, len(rows)), file=sys.stderr)
    else:
        key = os.environ.get("ORCALAYER_API_KEY")
        if not key:
            sys.exit("Set ORCALAYER_API_KEY (Premium key from https://orcalayer.com/docs/api). Never commit it.")
        rows = download(a, key)
    copies = collections.defaultdict(lambda: collections.defaultdict(list))   # wallet -> month -> [(result per $10, market)]
    for w, slug, ts, q, won, res in rows:
        copies[w][time.strftime("%Y-%m", time.gmtime(ts))].append((res, slug))
    print("copies (first buy per wallet and market, $20+, price %.2f-%.2f): %d by %d wallets" % (ZONE + (len(rows), len(copies))))

    first_month = a.start[:7]
    months = sorted({mo for d in copies.values() for mo in d})
    print("\nResult per $10 copied (their price + 1 cent, taker fee, held to resolution). Pick = the two months before the test")
    print("month, both inside the period. t: standard errors clustered by market. Rank correlation: Spearman, pick vs test month.")
    print("%-8s %-6s %8s %10s %12s %8s %12s" % ("test", "rule", "wallets", "pick mean", "test mean", "t", "test copies"))
    for test in months:
        pick = (month_add(test, -2), month_add(test, -1))
        if pick[0] < first_month:                                     # both pick months must lie inside --start .. --end
            continue
        stats = {}
        for w, d in copies.items():
            v = [x for x, _ in d.get(pick[0], []) + d.get(pick[1], [])]
            if len(v) >= 10:
                stats[w] = (len(v),) + mean_t(v)
        if not stats:
            continue
        rules = {"ALL10": set(stats),
                 "POS": {w for w, (n, mu, t) in stats.items() if n >= 20 and mu > 0 and t >= 2},
                 "TOP10": set(sorted([w for w, (n, mu, t) in stats.items() if n >= 20 and mu > 0], key=lambda w: -stats[w][2])[:10])}
        for rule, ws in rules.items():
            if not ws:
                continue
            tv = [x for w in ws for x in copies[w].get(test, [])]
            pm = sum(stats[w][1] for w in ws) / len(ws)
            tm, tt = mean_t_clustered(tv) if tv else (float("nan"), float("nan"))
            print("%-8s %-6s %8d %+10.2f %+12.2f %8.1f %12d" % (test, rule, len(ws), pm, tm, tt, len(tv)))
        both = [(stats[w][1], sum(x for x, _ in copies[w][test]) / len(copies[w][test])) for w in stats if len(copies[w].get(test, [])) >= 10]
        if len(both) >= 5:
            print("%-8s rank correlation pick -> test (wallets with >= 10 copies in both): %+.2f, n %d" % (
                test, spearman([x for x, _ in both], [y for _, y in both]), len(both)))
    if a.csv and not a.load_csv:
        with open(a.csv, "w", newline="") as f:
            cw = csv.writer(f)
            cw.writerow(["wallet", "market_slug", "first_buy_ts", "price", "won", "result_usd_per_10"])
            cw.writerows(rows)
        print("\nwrote %s (%d copies)" % (a.csv, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
