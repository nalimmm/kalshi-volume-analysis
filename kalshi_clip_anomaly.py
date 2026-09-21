#!/usr/bin/env python3
"""
Kalshi PERP - Clip-size volume anomaly detector
=================================================

Detects whether a handful of identical notional trade sizes ("clips", e.g.
$5,500) account for an outsized share of a Kalshi PERP market's printed
volume, a pattern that can indicate fixed-lot algorithmic market-making,
or, at the more extreme end, artificially inflated volume.

WHAT THIS SCRIPT DOES
----------------------
1. Pulls PUBLIC trade tape from Kalshi's Perps API (no auth needed):
       GET https://external-api.kalshi.com/trade-api/v2/margin/trades
   for a given ticker + time window, with cursor pagination.
2. Computes notional per trade as  count * price  (see NOTE below, this is
   NOT count*price*contract_size, because Kalshi's `price` field is already
   the dollar price of ONE CONTRACT, which already bakes in contract_size).
3. Groups trades by exact rounded notional ("clip $") and computes, per window:
       prints, contracts, notional, %prt, %notl, loop%
4. Flags the dominant clip size(s) and prints/saves a report.

IMPORTANT METHODOLOGY NOTE (verified empirically against the live API,
2026-09-21):
    - `open_interest_notional_value_dollars` == `open_interest` * `price`
      exactly, for every market checked. This confirms `price` is already a
      dollar amount per contract (it is NOT a per-unit-of-underlying price
      that needs multiplying by contract_size again).
    - So: notional_per_trade = count * price   <-- use this, not
      count*price*contract_size (that would double-count contract_size).

LOOP% HEURISTIC
----------------
There is no official definition of "loop%". This script implements a
transparent, documented heuristic: for a given clip-size group, sort its
trade timestamps; a print is counted as "looped" if another print of the
SAME clip size occurred within `--loop-gap-sec` seconds before or after it.
loop% = looped_prints / total_prints_in_group. This captures tight bursts /
repeated identical-size prints in short succession. Treat it as a
heuristic, not a formal statistical test. The --loop-gap-sec flag lets
you test sensitivity.

USAGE
-----
    python3 kalshi_clip_anomaly.py --windows windows.json --outdir ./out

    or edit the DEFAULT_WINDOWS list below and just run:
    python3 kalshi_clip_anomaly.py

DISCLAIMER
----------
This is a descriptive/statistical tool, not a verdict. Concentrated
identical clip sizes can arise from legitimate algorithmic market-making
using fixed lot sizes, or from artificially inflated volume. This script
only computes and reports the descriptive statistics; it does not itself
prove or disprove wash trading. Interpret results accordingly.
"""

import argparse
import json
import math
import sys
import time
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

BASE_URL = "https://external-api.kalshi.com/trade-api/v2/margin"
TRADES_ENDPOINT = f"{BASE_URL}/trades"

# ---------------------------------------------------------------------------
# Default windows: five example ETH-PERP / BTC-PERP windows from Sept 2026.
# All UTC. Edit freely or override via --windows a JSON file with the same
# shape: list of {"ticker","start","end","label"}.
# ---------------------------------------------------------------------------
DEFAULT_WINDOWS = [
    {"ticker": "KXETHPERP", "start": "2026-09-16T18:07:00Z", "end": "2026-09-16T23:38:00Z", "label": "ETH win1"},
    {"ticker": "KXETHPERP", "start": "2026-09-18T02:19:00Z", "end": "2026-09-18T11:13:00Z", "label": "ETH win2"},
    {"ticker": "KXETHPERP", "start": "2026-09-19T23:19:00Z", "end": "2026-09-20T16:08:00Z", "label": "ETH win3"},
    {"ticker": "KXETHPERP", "start": "2026-09-20T06:23:00Z", "end": "2026-09-20T23:18:00Z", "label": "ETH win4"},
    {"ticker": "KXBTCPERP", "start": "2026-09-20T16:23:00Z", "end": "2026-09-20T23:17:00Z", "label": "BTC win1"},
]


def parse_iso(ts: str) -> int:
    """Parse an ISO8601 'Z' timestamp to unix seconds (UTC)."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return int(dt.datetime.fromisoformat(ts).timestamp())


def parse_created_time(ts: str) -> float:
    """Kalshi created_time -> unix seconds (float, sub-second precision)."""
    ts = ts.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(ts).timestamp()


@dataclass
class Trade:
    trade_id: str
    ticker: str
    count: float
    price: float
    ts: float  # unix seconds
    taker_side: str
    notional: float = field(init=False)

    def __post_init__(self):
        self.notional = self.count * self.price


def fetch_trades(ticker: str, min_ts: int, max_ts: int, page_limit: int = 1000,
                  max_retries: int = 6, sleep_between: float = 0.05,
                  verbose: bool = True) -> list[Trade]:
    """
    Paginate GET /margin/trades for [min_ts, max_ts], newest-first cursor
    pagination (confirmed empirically). Stops once a page's oldest trade
    falls before min_ts, or the cursor stops advancing.
    """
    trades: list[Trade] = []
    cursor: Optional[str] = None
    seen_ids = set()
    page = 0

    while True:
        params = {
            "ticker": ticker,
            "limit": page_limit,
            "min_ts": min_ts,
            "max_ts": max_ts,
        }
        if cursor:
            params["cursor"] = cursor

        for attempt in range(max_retries):
            resp = requests.get(TRADES_ENDPOINT, params=params, timeout=20)
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503, 504):
                backoff = min(2 ** attempt, 30)
                if verbose:
                    print(f"  [retry] HTTP {resp.status_code}, backing off {backoff}s "
                          f"(attempt {attempt + 1}/{max_retries})", file=sys.stderr)
                time.sleep(backoff)
                continue
            resp.raise_for_status()
        else:
            raise RuntimeError(f"Failed after {max_retries} retries: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        page_trades = data.get("trades", [])
        new_cursor = data.get("cursor")
        page += 1

        if not page_trades:
            break

        oldest_ts_in_page = None
        new_this_page = 0
        for t in page_trades:
            ts = parse_created_time(t["created_time"])
            # Track the oldest timestamp on the page REGARDLESS of dedup,
            # so the min_ts stop condition still works even if a page is
            # entirely made of already-seen trades (API edge/overlap case).
            if oldest_ts_in_page is None or ts < oldest_ts_in_page:
                oldest_ts_in_page = ts

            tid = t["trade_id"]
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            new_this_page += 1
            if min_ts <= ts <= max_ts:
                trades.append(Trade(
                    trade_id=tid,
                    ticker=t["ticker"],
                    count=float(t["count"]),
                    price=float(t["price"]),
                    ts=ts,
                    taker_side=t.get("taker_side", ""),
                ))

        # Stall guard: if a page returns zero NEW trades (e.g. the API
        # repeats the same page at a boundary), stop instead of looping.
        if new_this_page == 0:
            if verbose:
                print(f"  [{ticker}] page {page}: no new trades (stall), stopping pagination")
            break

        if verbose and page % 20 == 0:
            print(f"  [{ticker}] page {page}: {len(trades)} trades collected so far "
                  f"(oldest in page = "
                  f"{dt.datetime.fromtimestamp(oldest_ts_in_page, tz=dt.timezone.utc) if oldest_ts_in_page else '?'})",
                  flush=True)

        # Stop conditions:
        if oldest_ts_in_page is not None and oldest_ts_in_page < min_ts:
            break
        if new_cursor is None or new_cursor == cursor:
            break
        cursor = new_cursor
        time.sleep(sleep_between)

    trades.sort(key=lambda t: t.ts)
    return trades


def compute_loop_pct(timestamps: list[float], gap_sec: float) -> float:
    """Fraction of timestamps that have another timestamp in the SAME group
    within gap_sec seconds (either direction). Timestamps must be sorted."""
    n = len(timestamps)
    if n <= 1:
        return 0.0
    looped = [False] * n
    for i in range(n):
        if i > 0 and timestamps[i] - timestamps[i - 1] <= gap_sec:
            looped[i] = True
            looped[i - 1] = True
        if i < n - 1 and timestamps[i + 1] - timestamps[i] <= gap_sec:
            looped[i] = True
            looped[i + 1] = True
    return sum(looped) / n * 100.0


def analyze_window(trades: list[Trade], loop_gap_sec: float, round_to: int = 0) -> "list[dict]":
    """Group by rounded notional (clip $) and compute the summary table."""
    groups: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        clip = round(t.notional, round_to) if round_to > 0 else round(t.notional)
        groups[clip].append(t)

    total_prints = len(trades)
    total_notional = sum(t.notional for t in trades)

    rows = []
    for clip, group in groups.items():
        prints = len(group)
        contracts = sum(t.count for t in group)
        notional = sum(t.notional for t in group)
        timestamps = sorted(t.ts for t in group)
        loop_pct = compute_loop_pct(timestamps, loop_gap_sec)
        rows.append({
            "clip_$": clip,
            "prints": prints,
            "contracts": contracts,
            "notional_$": round(notional, 2),
            "%prt": round(prints / total_prints * 100, 3) if total_prints else 0.0,
            "%notl": round(notional / total_notional * 100, 3) if total_notional else 0.0,
            "loop_%": round(loop_pct, 2),
        })

    rows.sort(key=lambda r: r["%notl"], reverse=True)
    return rows, total_prints, total_notional


def cluster_rows(rows: list[dict], total_prints: int, total_notional: float,
                  band_width: float) -> list[dict]:
    """
    Second-pass aggregation that merges exact-dollar clip rows into bands of
    width `band_width`. This exists because a trader/algo targeting a FIXED
    DOLLAR notional (e.g. ~$5,500) will produce a slightly different exact
    integer clip on every trade as the underlying spot price drifts tick to
    tick (same $ target, contract count re-solved each time), e.g. 5497,
    5498, 5499, 5500, 5501 are almost certainly the SAME logical clip, not
    five different ones. Rounding to the nearest dollar (as in
    `analyze_window`) under-counts the anomaly's true concentration; banding
    recovers it. Verify sensitivity by trying a couple of band widths.
    """
    buckets: dict[int, dict] = {}
    for r in rows:
        b = round(r["clip_$"] / band_width) * band_width
        acc = buckets.setdefault(b, {"band_center_$": b, "prints": 0, "contracts": 0.0,
                                      "notional_$": 0.0, "n_exact_clips": 0})
        acc["prints"] += r["prints"]
        acc["contracts"] += r["contracts"]
        acc["notional_$"] += r["notional_$"]
        acc["n_exact_clips"] += 1

    out = []
    for b, acc in buckets.items():
        acc["notional_$"] = round(acc["notional_$"], 2)
        acc["%prt"] = round(acc["prints"] / total_prints * 100, 3) if total_prints else 0.0
        acc["%notl"] = round(acc["notional_$"] / total_notional * 100, 3) if total_notional else 0.0
        out.append(acc)
    out.sort(key=lambda r: r["%notl"], reverse=True)
    return out


def format_cluster_table(rows: list[dict], top_n: int = 10) -> str:
    cols = ["band_center_$", "n_exact_clips", "prints", "contracts", "notional_$", "%prt", "%notl"]
    if not rows:
        return "(no data)"
    widths = {c: max(len(c), *(len(f"{r[c]:,}" if isinstance(r[c], (int, float)) else str(r[c])) for r in rows[:top_n])) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    lines = [header, "-" * len(header)]
    for r in rows[:top_n]:
        line = "  ".join(
            (f"{r[c]:,}" if isinstance(r[c], (int, float)) else str(r[c])).ljust(widths[c])
            for c in cols
        )
        lines.append(line)
    return "\n".join(lines)


def format_table(rows: list[dict], top_n: int = 15) -> str:
    cols = ["clip_$", "prints", "contracts", "notional_$", "%prt", "%notl", "loop_%"]
    widths = {c: max(len(c), *(len(f"{r[c]:,}" if isinstance(r[c], (int, float)) else str(r[c])) for r in rows[:top_n])) for c in cols} if rows else {c: len(c) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    lines = [header, "-" * len(header)]
    for r in rows[:top_n]:
        line = "  ".join(
            (f"{r[c]:,}" if isinstance(r[c], (int, float)) else str(r[c])).ljust(widths[c])
            for c in cols
        )
        lines.append(line)
    return "\n".join(lines)


def load_trades_from_csv(path: Path) -> list[Trade]:
    """Reload a previously-saved raw_*.csv without hitting the API again."""
    import csv as csvmod
    trades = []
    with open(path) as f:
        for row in csvmod.DictReader(f):
            trades.append(Trade(
                trade_id=row["trade_id"], ticker=row["ticker"],
                count=float(row["count"]), price=float(row["price"]),
                ts=dt.datetime.fromisoformat(row["created_time_utc"]).timestamp(),
                taker_side=row["taker_side"],
            ))
    trades.sort(key=lambda t: t.ts)
    return trades


def run(windows: list[dict], outdir: Path, loop_gap_sec: float, page_limit: int, verbose: bool,
        band_width: float = 20.0, reanalyze_only: bool = False):
    outdir.mkdir(parents=True, exist_ok=True)
    report_lines = []
    all_summary = []

    for w in windows:
        ticker = w["ticker"]
        label = w.get("label", ticker)
        min_ts = parse_iso(w["start"])
        max_ts = parse_iso(w["end"])
        duration_h = (max_ts - min_ts) / 3600
        raw_path = outdir / f"raw_{label.replace(' ', '_')}.csv"

        if reanalyze_only and raw_path.exists():
            print(f"\n=== {label} | {ticker} | reanalyzing cached {raw_path.name} (no API call) ===", flush=True)
            trades = load_trades_from_csv(raw_path)
        else:
            print(f"\n=== {label} | {ticker} | {w['start']} -> {w['end']} ({duration_h:.2f}h) ===", flush=True)
            t0 = time.time()
            trades = fetch_trades(ticker, min_ts, max_ts, page_limit=page_limit, verbose=verbose)
            elapsed = time.time() - t0
            print(f"  fetched {len(trades)} trades in {elapsed:.1f}s")
            if trades:
                with open(raw_path, "w") as f:
                    f.write("trade_id,ticker,count,price,notional,taker_side,created_time_utc\n")
                    for t in trades:
                        iso = dt.datetime.fromtimestamp(t.ts, tz=dt.timezone.utc).isoformat()
                        f.write(f"{t.trade_id},{t.ticker},{t.count},{t.price},{t.notional:.4f},{t.taker_side},{iso}\n")

        if not trades:
            print("  no trades in this window, skipping analysis")
            continue

        rows, total_prints, total_notional = analyze_window(trades, loop_gap_sec)
        cband = cluster_rows(rows, total_prints, total_notional, band_width)

        table_str = format_table(rows, top_n=15)
        cluster_str = format_cluster_table(cband, top_n=10)
        header = (
            f"\n--- {label} ({ticker}) | {w['start']} -> {w['end']} | duration {duration_h:.2f}h ---\n"
            f"total prints: {total_prints:,} | total notional: ${total_notional:,.2f} | "
            f"unique clip sizes: {len(rows)}\n"
        )
        print(header)
        print("[exact-dollar clip$, top 15]")
        print(table_str)
        print(f"\n[banded +/- ${band_width/2:.0f}, top 10, merges near-duplicate clips from price drift]")
        print(cluster_str)

        report_lines.append(header)
        report_lines.append("[exact-dollar clip$, top 15]")
        report_lines.append(table_str)
        report_lines.append(f"\n[banded +/- ${band_width/2:.0f}, top 10, merges near-duplicate clips from price drift]")
        report_lines.append(cluster_str)
        report_lines.append("")

        # Save per-window summary
        summary_path = outdir / f"summary_{label.replace(' ', '_')}.json"
        with open(summary_path, "w") as f:
            json.dump({
                "label": label, "ticker": ticker, "start": w["start"], "end": w["end"],
                "duration_hours": duration_h, "total_prints": total_prints,
                "total_notional_usd": total_notional, "top_clips": rows[:20],
            }, f, indent=2)

        dominant = rows[0] if rows else None
        dominant_band = cband[0] if cband else None
        all_summary.append({
            "label": label, "ticker": ticker, "duration_hours": round(duration_h, 2),
            "total_prints": total_prints, "total_notional_usd": round(total_notional, 2),
            "dominant_clip_$": dominant["clip_$"] if dominant else None,
            "dominant_%notl": dominant["%notl"] if dominant else None,
            "dominant_loop_%": dominant["loop_%"] if dominant else None,
            "dominant_band_center_$": dominant_band["band_center_$"] if dominant_band else None,
            "dominant_band_%notl": dominant_band["%notl"] if dominant_band else None,
        })

    report_path = outdir / "report.md"
    with open(report_path, "w") as f:
        f.write("# Kalshi PERP clip-size anomaly report\n\n")
        f.write("## Cross-window summary\n\n")
        if all_summary:
            cols = list(all_summary[0].keys())
            f.write("| " + " | ".join(cols) + " |\n")
            f.write("|" + "---|" * len(cols) + "\n")
            for s in all_summary:
                f.write("| " + " | ".join(str(s[c]) for c in cols) + " |\n")
        f.write("\n## Per-window detail\n\n```\n")
        f.write("\n".join(report_lines))
        f.write("\n```\n")

    print(f"\nSaved: {report_path}")
    return all_summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows", type=str, default=None,
                     help="Path to a JSON file with a list of {ticker,start,end,label} windows. "
                          "Defaults to the windows built into this script.")
    ap.add_argument("--outdir", type=str, default="./kalshi_clip_out",
                     help="Output directory for raw trades, per-window JSON, and report.md")
    ap.add_argument("--loop-gap-sec", type=float, default=5.0,
                     help="Max gap (seconds) between two same-size prints to count as 'looped'. Default 5s.")
    ap.add_argument("--page-limit", type=int, default=1000, help="Trades per API page (max 1000).")
    ap.add_argument("--band-width", type=float, default=20.0,
                     help="Width ($) for the banded/clustered clip table, to merge near-duplicate "
                          "clips caused by spot-price drift around a fixed dollar target. Default $20.")
    ap.add_argument("--quiet", action="store_true", help="Less verbose pagination logging.")
    ap.add_argument("--reanalyze-only", action="store_true",
                     help="Skip the API entirely and recompute from the raw_*.csv files already saved "
                          "in --outdir from a previous run (fast iteration on --loop-gap-sec/--band-width).")
    args = ap.parse_args()

    if args.windows:
        with open(args.windows) as f:
            windows = json.load(f)
    else:
        windows = DEFAULT_WINDOWS

    run(windows, Path(args.outdir), args.loop_gap_sec, args.page_limit, verbose=not args.quiet,
        band_width=args.band_width, reanalyze_only=args.reanalyze_only)


if __name__ == "__main__":
    main()
