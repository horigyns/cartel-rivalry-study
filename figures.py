#!/usr/bin/env python3
"""Static figures for the report, from results/analysis.json (run analysis.py
first). One figure, three panels, cells on the y axis, the two microstructures
as two fixed-order series with 95% bootstrap intervals.

    python figures.py   -> results/fig1_cells.png, results/fig1_cells.svg
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import sys

HERE = Path(__file__).parent
R = json.loads((HERE / "results" / "analysis.json").read_text())
# `python figures.py --corrected` draws the defection panel from
# results/analysis_corrected.json (bloc-instructed sides only) and writes
# fig1_cells_corrected.*; the originals are left untouched.
CORRECTED = "--corrected" in sys.argv
if CORRECTED:
    RC = json.loads((HERE / "results" / "analysis_corrected.json").read_text())
    for arm in RC:
        if arm == "note":
            continue
        for cell, d in RC[arm]["cells"].items():
            R[arm][cell]["summary"]["defection_rate"] = d["defection_corrected"]
            R[arm][cell]["ci_defect"] = d["ci_defect_corrected"]
STEM = "fig1_cells_corrected" if CORRECTED else "fig1_cells"

SERIES = [("accept-only", "#2a78d6"), ("seller-ask", "#eb6834")]   # fixed order, validated
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
CELLS = [
    ("ch-none_neutral", "no channel"),
    ("ch-public_neutral", "public channel"),
    ("ch-private_neutral", "private channel"),
    ("ch-private_neutral_full", "private, full bloc"),
    ("ch-private_unregulated", "private, unregulated"),
    ("ch-private_neutral_cf", "counterfactual (buyers alone)"),
]
PANELS = [
    ("median round closed", "median_round_closed", "ci_round", (0, 20)),
    ("mean price position", "mean_price_pos", "ci_pos", (0, 1)),
    ("defection rate (share of episodes)" + (", bloc-instructed sides only" if CORRECTED else ""), "defection_rate", "ci_defect", (0, 1)),
]

plt.rcParams.update({"font.family": "sans-serif", "font.size": 9, "axes.edgecolor": GRID,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK,
                     "figure.facecolor": SURF, "axes.facecolor": SURF})
fig, axes = plt.subplots(1, 3, figsize=(12, 4.2), sharey=True, gridspec_kw={"wspace": 0.08})
y = list(range(len(CELLS)))[::-1]
for ax, (title, key, cik, xlim) in zip(axes, PANELS):
    ax.set_title(title, loc="left", color=INK, fontsize=9.5, pad=8)
    ax.grid(axis="x", color=GRID, lw=0.8); ax.set_axisbelow(True)
    for spine in ("top", "right", "left"): ax.spines[spine].set_visible(False)
    ax.set_xlim(*xlim)
    for si, (arm, col) in enumerate(SERIES):
        off = 0.16 if si == 0 else -0.16
        for yi, (cell, _) in zip(y, CELLS):
            d = R[arm].get(cell)
            if not d: continue
            v = d["summary"][key]; lo, hi = d[cik]
            if lo is not None:
                ax.plot([lo, hi], [yi + off, yi + off], color=col, lw=2, solid_capstyle="round", alpha=0.9)
            ax.plot(v, yi + off, "o", ms=6, color=col, mec=SURF, mew=1.5, zorder=3)
        # counterfactual reference for position: a thin dashed line per arm
        if key == "mean_price_pos":
            ref = R[arm]["ch-private_neutral_cf"]["summary"]["mean_price_pos"]
            ax.axvline(ref, color=col, lw=1, ls=(0, (3, 3)), alpha=0.7)
            ax.text(ref, len(CELLS) - 0.45, f"cf {ref:.2f}", color=col, fontsize=7.5, ha="center", va="bottom")
axes[1].set_xlabel("0 = lowest seller floor, 1 = highest buyer ceiling", color=INK2, fontsize=8)
axes[0].set_xlabel("rounds (cap 20)", color=INK2, fontsize=8)
axes[0].set_yticks(y); axes[0].set_yticklabels([lab for _, lab in CELLS])
axes[0].set_ylim(-0.7, len(CELLS) - 0.3)
handles = [plt.Line2D([], [], color=c, marker="o", lw=2, ms=6, mec=SURF, label=a) for a, c in SERIES]
fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False, bbox_to_anchor=(0.5, -0.02),
           labelcolor=INK2, fontsize=9)
fig.text(0.01, 0.995, "DeepSeek-V3 blocs, 72 episodes per microstructure, 95% bootstrap intervals. "
         "Position reference: the counterfactual arm's mean (dashed).",
         color=INK2, fontsize=8, va="top")
axes[0].set_xticks([0, 5, 10, 15, 20]); axes[1].set_xticks([0, .25, .5, .75, 1]); axes[2].set_xticks([0, .25, .5, .75, 1])
fig.subplots_adjust(left=0.19, right=0.99, top=0.86, bottom=0.17, wspace=0.14)
for ext in ("png", "svg"):
    fig.savefig(HERE / "results" / f"{STEM}.{ext}", dpi=200, bbox_inches="tight", facecolor=SURF)
print(f"wrote results/{STEM}.png and .svg")
