#!/usr/bin/env python3
"""
Generate the two summary charts referenced in README.md, from an existing
run's output directory (raw_*.csv + summary_*.json produced by
kalshi_clip_anomaly.py).

Usage:
    python3 make_charts.py --outdir ./data --charts-dir ./charts
"""

import argparse
import csv
import glob
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

import kalshi_clip_anomaly as k

BAND_WIDTH_DEFAULT = 20.0


def chart_dominant_clip_share(outdir: Path, charts_dir: Path):
    """Bar chart: dominant banded clip's % of total notional, per window."""
    labels, shares, bands = [], [], []

    for summary_path in sorted(glob.glob(str(outdir / "summary_*.json"))):
        d = json.load(open(summary_path))
        label = d["label"]
        rows = d["top_clips"]
        cband = k.cluster_rows(rows, d["total_prints"], d["total_notional_usd"], BAND_WIDTH_DEFAULT)
        if not cband:
            continue
        top = cband[0]
        labels.append(label)
        shares.append(top["%notl"])
        bands.append(top["band_center_$"])

    fig, ax = plt.subplots(figsize=(8, 5))
    colors = ["#d62728" if "ETH" in l else "#1f77b4" for l in labels]
    bars = ax.bar(labels, shares, color=colors)

    for bar, band in zip(bars, bands):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                 f"~${band:,.0f}", ha="center", va="bottom", fontsize=9)

    ax.set_ylabel("% of window's total notional volume")
    ax.set_title("Dominant repeated clip size: share of total notional, per window")
    ax.set_ylim(0, max(shares) * 1.2)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    out_path = charts_dir / "dominant_clip_share.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"wrote {out_path}")


def chart_notional_distribution(outdir: Path, charts_dir: Path, label: str = "ETH win2"):
    """
    Rank plot: every distinct clip size (banded) for one window, ranked by
    its share of total notional volume, log-scale y-axis. This makes the
    anomaly visually obvious: one bar towers over a long tail of
    negligible-sized bars, instead of blending into a normal-looking
    histogram.
    """
    summary_path = outdir / f"summary_{label.replace(' ', '_')}.json"
    d = json.load(open(summary_path))
    rows = d["top_clips"]
    total_prints, total_notional = d["total_prints"], d["total_notional_usd"]

    # Re-derive the FULL clip table (not just top 20) from the raw CSV so the
    # tail of the rank plot is real, not just the pre-truncated top rows.
    csv_path = outdir / f"raw_{label.replace(' ', '_')}.csv"
    trades = k.load_trades_from_csv(csv_path)
    full_rows, total_prints, total_notional = k.analyze_window(trades, loop_gap_sec=5.0)
    cband = k.cluster_rows(full_rows, total_prints, total_notional, BAND_WIDTH_DEFAULT)

    top_n = 40
    shares = [r["%notl"] for r in cband[:top_n]]
    ranks = list(range(1, len(shares) + 1))
    colors = ["#d62728" if i == 0 else "#4c72b0" for i in range(len(shares))]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(ranks, shares, color=colors, width=0.9)
    ax.set_yscale("log")
    ax.set_xlabel(f"Clip size, ranked by share of volume (top {top_n} of {len(cband)} distinct sizes)")
    ax.set_ylabel("% of window's total notional volume (log scale)")
    ax.set_title(f"One size dominates the tape: {label} (KXETHPERP)")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    top = cband[0]
    ax.annotate(f"~${top['band_center_$']:,.0f} clip\n{top['%notl']:.1f}% of ALL volume\n"
                f"({top['prints']:,} prints)",
                xy=(1, top["%notl"]), xytext=(6, top["%notl"] * 0.6),
                arrowprops=dict(arrowstyle="->", color="#d62728"),
                color="#d62728", fontsize=10, fontweight="bold")

    # Place the "long tail" callout in the empty upper-right area of the
    # plot (well above the small bars there), not on top of the bars.
    ax.text(0.62, 0.55, f"next {top_n - 1} sizes combined:\n{sum(shares[1:]):.1f}% of volume",
             transform=ax.transAxes, fontsize=9, color="#333333",
             ha="left", va="center",
             bbox=dict(boxstyle="round,pad=0.35", facecolor="white",
                       edgecolor="#999999", alpha=0.9))

    fig.tight_layout()
    out_path = charts_dir / "notional_distribution.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"wrote {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", type=str, default="./data",
                     help="Directory with raw_*.csv and summary_*.json from a previous run.")
    ap.add_argument("--charts-dir", type=str, default=".",
                     help="Where to write the PNG charts (default: current directory, "
                          "so they sit next to README.md with no subfolder).")
    ap.add_argument("--distribution-window", type=str, default="ETH win2",
                     help="Which window's raw trades to use for the distribution chart.")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    charts_dir = Path(args.charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)

    chart_dominant_clip_share(outdir, charts_dir)
    chart_notional_distribution(outdir, charts_dir, label=args.distribution_window)


if __name__ == "__main__":
    main()
