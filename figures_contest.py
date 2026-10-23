#!/usr/bin/env python3
"""Figure 2: award distance from the agent's own reservation, by market side
and nominal price level, both firm models. The figure the methodological
claim rests on (docs/WRITEUP_PACK.md §5.5–5.6).

    python figures_contest.py      # -> results/fig2_own_distance.png, .svg

One dot per cleared episode (own-distance = award minus own reservation, as
a share of the bargaining zone; sellers measured up from cost, buyers down
from value). The bar is the cell mean. Non-cleared markets have no award and
are counted under each cell.
"""
from __future__ import annotations

import glob
import pickle
import statistics as st
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
OUT = HERE / "results"

# categorical slots 1 and 2 of the reference palette (validated, light mode)
COLOR = {"sell": "#2a78d6", "buy": "#eb6834"}
TEXT, TEXT2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
MODEL_LABEL = {"DS": "DeepSeek-V3 firms", "G4": "GPT-4o firms"}


def own_distance(o) -> float | None:
    lo, hi = o.zone
    span = hi - lo
    d = [(a - o.floors[s]) / span for s, a in o.awarded]
    return st.fmean(d) if d else None


def load():
    cells = defaultdict(list)   # (model, side, level) -> [outcome]
    for p in sorted(glob.glob(str(HERE / "runs_contest" / "*firm*.pkl"))):
        o = pickle.loads(open(p, "rb").read())
        base, _, tag = o.cell_name.partition("@")
        model = "G4" if tag else "DS"
        level = "shift" if base.endswith("_shift100") else "base"
        base = base.removesuffix("_shift100")
        side = "buy" if base.endswith("_buy") else "sell"
        cells[(model, side, level)].append(o)
    return cells


def main() -> None:
    cells = load()
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.6), sharey=True)
    fig.patch.set_facecolor(SURFACE)
    xpos = {("sell", "base"): 0.0, ("buy", "base"): 1.0, ("sell", "shift"): 2.6, ("buy", "shift"): 3.6}
    for ax, model in zip(axes, ("DS", "G4")):
        ax.set_facecolor(SURFACE)
        for (side, level), x in xpos.items():
            outs = cells[(model, side, level)]
            vals = [v for o in outs if (v := own_distance(o)) is not None]
            n_fail = sum(not o.awarded for o in outs)
            # jittered dots, one per cleared episode
            import random
            rng = random.Random(7)
            xs = [x + rng.uniform(-0.18, 0.18) for _ in vals]
            ax.scatter(xs, vals, s=26, color=COLOR[side], alpha=0.55, linewidths=0, zorder=3)
            # cell mean as a thin rounded bar from the baseline
            m = st.fmean(vals)
            ax.bar(x, m, width=0.5, color=COLOR[side], alpha=0.22, linewidth=0, zorder=2)
            ax.plot([x - 0.25, x + 0.25], [m, m], color=COLOR[side], linewidth=2, zorder=4,
                    solid_capstyle="round")
            ax.text(x, m + 0.03, f"{m:.2f}", ha="center", va="bottom", fontsize=9, color=TEXT)
            ax.text(x, -0.06, f"{len(vals)} cleared\n{n_fail} not", ha="center", va="top",
                    fontsize=7.5, color=TEXT2)
        ax.set_xticks([0.5, 3.1])
        ax.set_xticklabels(["base level\ncosts 20–35, values 65–80", "shifted level (+100)\ncosts 120–135, values 165–180"],
                           fontsize=9, color=TEXT)
        ax.tick_params(axis="x", length=0, pad=34)
        ax.set_title(MODEL_LABEL[model], fontsize=11, color=TEXT, loc="left")
        ax.set_ylim(-0.02, 1.0)
        ax.set_xlim(-0.6, 4.2)
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(axis="y", colors=TEXT2, labelsize=9, length=0)
    axes[0].set_ylabel("award distance from own reservation\n(share of bargaining zone)", fontsize=9, color=TEXT2)
    # legend: series identity, never colour alone -> labels name the side
    handles = [plt.Line2D([], [], marker="o", linestyle="", color=COLOR[s], markersize=7,
                          label={"sell": "sellers (distance up from cost)", "buy": "buyers (distance down from value)"}[s])
               for s in ("sell", "buy")]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.005, 0.865), ncol=2,
               frameon=False, fontsize=9, labelcolor=TEXT, handletextpad=0.3, columnspacing=1.6)
    fig.suptitle("Same game, two chairs, two price levels: how far agents settle from their own number",
                 x=0.01, ha="left", fontsize=12, color=TEXT, fontweight="semibold")
    fig.text(0.01, 0.93, "Seed-paired cells; one dot per cleared market, bar = cell mean. The buy–sell gap is 2–4× at the base level\n"
             "and shrinks at the shifted level, where both sides sit at the same share of their own number.",
             fontsize=9, color=TEXT2, va="top", linespacing=1.4)
    fig.tight_layout(rect=(0, 0.02, 1, 0.82))
    OUT.mkdir(exist_ok=True)
    for ext in ("png", "svg"):
        fig.savefig(OUT / f"fig2_own_distance.{ext}", dpi=200, facecolor=SURFACE)
    print("wrote", OUT / "fig2_own_distance.png")


if __name__ == "__main__":
    main()
